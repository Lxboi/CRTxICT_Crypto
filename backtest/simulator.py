"""
backtest/simulator.py

Rejoue le pipeline de décision jour par jour sur des données
historiques, en ne donnant JAMAIS au moteur de décision une donnée
future par rapport au jour simulé — c'est le point le plus critique
d'un backtest valide (éviter le biais de "lookahead").

Pour chaque jour simulé D :
  - Monthly : seuls les mois ENTIÈREMENT terminés avant D (mois
    courant exclu, même partiellement écoulé).
  - Weekly : seules les semaines ISO entièrement terminées avant D.
  - Daily : tous les jours STRICTEMENT avant D (le jour D lui-même est
    traité comme "en cours de formation", exactement comme le fait le
    bot en live avec `candles[:-1]`).
  - Le prix "actuel" utilisé par DecisionEngine est la clôture de la
    dernière bougie daily disponible (donc la clôture de D-1) — pas le
    prix du jour D, qui n'est pas encore connu au moment simulé.

La bougie du jour D elle-même sert ensuite au trade_tracker pour
vérifier si un SL/TP a été touché CE jour-là (voir trade_tracker.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from models.market_data import Candle, CandleSeries
from rules.decision_engine import DecisionEngine, DecisionReport
from rules.engine import MarketContext


@dataclass
class SimulatedEntry:
    """Une entrée détectée à un jour donné de la simulation, prête à être suivie par trade_tracker."""

    setup_name: str
    entry_date: date
    bias: str  # "bullish" / "bearish"
    entry_price: float
    stop_loss: float
    tp_50: float | None
    tp_100: float | None
    tp_htf_dol: float | None


def to_candles(raw: list[list[float]]) -> list[Candle]:
    """Convertit une liste de klines déjà simplifiées en objets Candle triés chronologiquement."""
    candles = [
        Candle(
            timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
        )
        for row in raw
    ]
    return sorted(candles, key=lambda c: c.timestamp)


def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def _iso_week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _truncate_candles_strict(all_candles: list[Candle], simulated_date: date, timeframe: str) -> list[Candle]:
    """
    Ne garde que les bougies dont la PÉRIODE ENTIÈRE est terminée avant
    `simulated_date` — jamais de bougie en cours de formation à cette
    date, quelle que soit la timeframe.
    """
    if timeframe == "monthly":
        return [c for c in all_candles if _month_key(c.timestamp.date()) < _month_key(simulated_date)]
    if timeframe == "weekly":
        return [c for c in all_candles if _iso_week_key(c.timestamp.date()) < _iso_week_key(simulated_date)]
    # daily
    return [c for c in all_candles if c.timestamp.date() < simulated_date]


def compute_btc_bias_series(
    raw_btc_monthly: list[list[float]],
    raw_btc_weekly: list[list[float]],
    raw_btc_daily: list[list[float]],
    test_start_date: date,
    decision_config: dict[str, Any],
) -> dict[date, str | None]:
    """
    Calcule le biais effectif de BTC (`DecisionReport.effective_bias`)
    pour chaque jour simulé à partir de `test_start_date` — même
    principe de troncature stricte anti-lookahead que run_simulation()
    (jamais de donnée future par rapport au jour simulé).

    Utilisé pour FILTRER l'exécution des autres actifs sur leur
    alignement avec BTC (voir run_simulation(btc_bias_by_date=...)) —
    par opposition au comportement historique où le biais BTC n'était
    qu'indicatif dans la notification, jamais un critère d'entrée.

    Returns:
        Dict {date: "bullish"/"bearish"/None} — None les jours où
        aucun biais clair n'est disponible pour BTC (historique
        insuffisant, ou aucun momentum de biais détecté ce jour-là).
    """
    monthly_candles = to_candles(raw_btc_monthly)
    weekly_candles = to_candles(raw_btc_weekly)
    daily_candles = to_candles(raw_btc_daily)

    if not daily_candles:
        return {}

    engine = DecisionEngine(config=decision_config)
    bias_by_date: dict[date, str | None] = {}

    simulated_dates = [c.timestamp.date() for c in daily_candles if c.timestamp.date() >= test_start_date]

    for simulated_date in simulated_dates:
        context_daily = _truncate_candles_strict(daily_candles, simulated_date, "daily")
        if len(context_daily) < 3:
            continue

        context_monthly = _truncate_candles_strict(monthly_candles, simulated_date, "monthly")
        context_weekly = _truncate_candles_strict(weekly_candles, simulated_date, "weekly")
        current_price = context_daily[-1].close

        context = MarketContext(
            market_data=_build_market_data("BTC", current_price),
            candles={
                "monthly": CandleSeries(candles=context_monthly),
                "weekly": CandleSeries(candles=context_weekly),
                "daily": CandleSeries(candles=context_daily),
            },
            symbol="BTC",
        )
        report: DecisionReport = engine.decide(context)
        bias_by_date[simulated_date] = report.effective_bias

    return bias_by_date


def run_simulation(
    raw_monthly: list[list[float]],
    raw_weekly: list[list[float]],
    raw_daily: list[list[float]],
    symbol: str,
    years_to_test: int,
    decision_config: dict[str, Any],
    btc_bias_by_date: dict[date, str | None] | None = None,
) -> list[SimulatedEntry]:
    """
    Rejoue le pipeline jour par jour sur les `years_to_test` dernières
    années disponibles dans raw_daily (le reste sert de marge de
    contexte pour les ancres CRT/DOL, voir historical_loader.py).

    Args:
        btc_bias_by_date: Si fourni (voir compute_btc_bias_series()),
            une entrée n'est retenue QUE SI son biais correspond à
            celui de BTC ce jour-là — sinon, comportement historique
            inchangé (BTC purement indicatif, jamais un filtre).
            Un jour sans biais BTC connu (clé absente ou valeur None)
            rejette prudemment l'entrée plutôt que de l'accepter par
            défaut (ambiguïté = pas de trade, jamais l'inverse).

    Returns:
        Liste des entrées détectées, dans l'ordre chronologique — à
        transmettre à trade_tracker.classify_trades() pour connaître
        leur issue (GAIN/BREAK-EVEN/PERTE).
    """
    monthly_candles = to_candles(raw_monthly)
    weekly_candles = to_candles(raw_weekly)
    daily_candles = to_candles(raw_daily)

    if not daily_candles:
        return []

    last_date = daily_candles[-1].timestamp.date()
    test_start_date = date(last_date.year - years_to_test, last_date.month, last_date.day)

    # Setups déjà détectés (par nom stable) -> ne pas re-enregistrer le
    # même trade à chaque jour où il reste valide, exactement comme la
    # déduplication par dedup_key en live (voir database/db.py).
    already_recorded: set[str] = set()
    entries: list[SimulatedEntry] = []

    engine = DecisionEngine(config=decision_config)

    simulated_dates = [c.timestamp.date() for c in daily_candles if c.timestamp.date() >= test_start_date]

    for simulated_date in simulated_dates:
        context_monthly = _truncate_candles_strict(monthly_candles, simulated_date, "monthly")
        context_weekly = _truncate_candles_strict(weekly_candles, simulated_date, "weekly")
        context_daily = _truncate_candles_strict(daily_candles, simulated_date, "daily")

        if len(context_daily) < 3:
            continue  # historique insuffisant pour analyser quoi que ce soit

        current_price = context_daily[-1].close  # clôture du dernier jour CONNU (D-1)

        context = MarketContext(
            market_data=_build_market_data(symbol, current_price),
            candles={
                "monthly": CandleSeries(candles=context_monthly),
                "weekly": CandleSeries(candles=context_weekly),
                "daily": CandleSeries(candles=context_daily),
            },
            symbol=symbol,
        )

        report = engine.decide(context)

        for setup in report.timeframe_setups:
            if not setup.entry_recommended or not setup.htf_crt or not setup.crt_targets:
                continue
            if setup.setup_name in already_recorded:
                continue

            if btc_bias_by_date is not None:
                btc_bias_today = btc_bias_by_date.get(simulated_date)
                if btc_bias_today is None or btc_bias_today != setup.htf_crt["bias"]:
                    continue  # pas aligné avec BTC ce jour-là (ou biais BTC inconnu) -> rejeté

            already_recorded.add(setup.setup_name)
            entries.append(
                SimulatedEntry(
                    setup_name=setup.setup_name,
                    entry_date=simulated_date,
                    bias=setup.htf_crt["bias"],
                    entry_price=current_price,
                    stop_loss=setup.crt_targets["stop_loss"],
                    tp_50=setup.crt_targets.get("tp_50"),
                    tp_100=setup.crt_targets.get("tp_100"),
                    tp_htf_dol=setup.crt_targets.get("tp_htf_dol"),
                )
            )

    return entries


def _build_market_data(symbol: str, price: float):
    """Construit un MarketData minimal (seul price_usd compte pour la logique de décision)."""
    from models.market_data import MarketData

    return MarketData(
        symbol=symbol,
        price_usd=price,
        price_change_percentage_24h=0.0,
        volume_24h_usd=0.0,
        market_cap_usd=0.0,
    )
