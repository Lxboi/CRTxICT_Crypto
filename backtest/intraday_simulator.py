"""
backtest/intraday_simulator.py

Simulateur intraday (Phase 2 du plan "Backtest réaliste + Monte Carlo") :
rejoue la VRAIE logique de décision live (rules/decision_engine.py::
DecisionEngine.decide(), donc EntryRule/TsqRule/PurgeRule/CRT sur
Monthly/Weekly/Daily/4H/intraday/m15), au lieu du biais Daily seul + cibles
CRT du simulateur historique (backtest/simulator.py) — qui ne teste PAS
les règles d'entrée réelles du bot.

Différence clé avec backtest/simulator.py::run_simulation() : celui-ci
NE FOURNIT jamais de série "intraday"/"m15" à DecisionEngine -> PurgeRule
ne peut jamais évaluer un CRT Daily ou 4H comme "high probability" (elle a
besoin de l'intraday pour ça, voir rules/purge_rule.py) -> en pratique,
SEULS les setups Weekly (qui n'ont pas besoin d'intraday pour leur
critère de timing) et Monthly (pas de filtre de purge défini) peuvent
jamais produire entry_recommended=True dans l'ancien simulateur. Ce
nouveau simulateur, en fournissant de vraies données m15, débloque
Daily et 4H — d'où un nombre de trades détectés généralement bien
supérieur (attendu, pas un bug).

Prix d'entrée / SL / TP simulés : réplique le chemin par défaut RÉEL de
main.py::run_once() (voir son dispatch_alert()) — dès qu'un
TimeframeSetup devient entry_recommended=True pour la première fois
(déduplication par setup_name, identique à l'ancien simulateur ET à
database/db.py::save_alert_if_new en live), une entrée est enregistrée
AU PRIX DU MARCHÉ courant, avec le SL et les TP du CRT
(setup.crt_targets) — c'est le chemin qu'emprunte main.py dans
l'écrasante majorité des cas réels. Le chemin secondaire live (attendre
une entrée TSQ précise quand un setup Weekly a déjà atteint TP1 à sa
détection) est un raffinement d'exécution, volontairement laissé à la
Phase 3 ("coûts réels + garde-fous") pour ne pas mélanger logique de
décision (ce module) et logique d'exécution/position sizing (Phase 3).
priority_entry_source est quand même capturé à titre informatif (voir
IntradaySimulatedEntry), pour permettre une analyse a posteriori de
combien d'entrées auraient bénéficié d'un raffinement TSQ.

Anti-lookahead : à chaque bougie m15 fermée, seules les bougies dont la
période ENTIÈRE est terminée avant l'instant simulé sont exposées au
moteur — jamais de bougie en cours de formation, quelle que soit la
timeframe. Concrètement, chaque série (monthly/weekly/daily/intraday/m15)
se termine par une bougie PLACEHOLDER (voir _placeholder_candle) dont le
seul rôle est d'être ignorée par le `series.candles[:-1]` que TOUTES les
règles appliquent déjà systématiquement (convention déjà en place dans
tout rules/*.py, voir MarketContext.candles) — exactement la même forme
que ce que le pipeline live fournit (dernière bougie = période en cours).

"intraday" (1h) est dérivé par resampling depuis "m15" (pas de source 1h
séparée en backtest, contrairement au live où Binance fournit du 1h
natif) via rules/purge_rule.py::_resample_to_4h, réutilisée ici avec une
grille horaire complète (0..23h NY) au lieu d'une grille 4H — une
bougie par heure NY, alignement identique à ce que produirait une vraie
agrégation 1h.

Fenêtre glissante bornée (voir DEFAULT_M15_WINDOW_DAYS) et cache des
séries Monthly/Weekly/Daily par date simulée (voir _HtfCache) : sans ces
deux optimisations, reconstruire tout l'historique à CHAQUE bougie m15
simulée (potentiellement plusieurs centaines de milliers de pas sur un
backtest de plusieurs années) rendrait le calcul infaisable en pratique
(coût quadratique). Aucune des deux n'affecte le résultat : la fenêtre
m15 dépasse largement le plus grand horizon réellement utilisé par une
règle (CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME["4h"] = 60 bougies 4H = 10 jours,
voir rules/crt_rule.py) et le cache HTF ne fait que réutiliser un
résultat identique tant que la date simulée ne change pas (Monthly/
Weekly/Daily ne peuvent de toute façon pas changer plus souvent qu'une
fois par jour).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from models.market_data import Candle, CandleSeries
from rules.decision_engine import DecisionEngine
from rules.engine import MarketContext
from rules.purge_rule import _resample_to_4h

from backtest.simulator import (
    _build_market_data,
    _truncate_candles_strict,
    to_candles,
)

# Grille horaire NY complète (0..23) -- réutilise _resample_to_4h (conçue
# pour une grille 4H) avec un pas d'1h : bucket_start() y sélectionne
# systématiquement l'heure de la bougie elle-même (puisque chaque heure
# est présente dans la grille), ce qui produit exactement une bougie par
# heure NY -- seule façon de dériver "intraday" (1h) à partir de "m15" en
# backtest (voir docstring module).
_HOURLY_GRID_HOURS: tuple[int, ...] = tuple(range(24))

# Profondeur de la fenêtre glissante de bougies m15 exposée au moteur à
# chaque pas simulé -- voir docstring module pour la justification
# (50% de marge au-dessus du plus grand horizon réel, 10 jours pour un
# CRT 4H). Ajustable : augmenter si une méthodologie future a besoin de
# regarder plus loin en arrière sur l'intraday.
DEFAULT_M15_WINDOW_DAYS = 15

# Nombre minimum de bougies Daily closes requis pour tenter une décision
# à un pas donné -- même seuil que l'ancien simulateur
# (backtest/simulator.py::run_simulation), conservé pour rester au moins
# aussi prudent avant de scanner quoi que ce soit.
_MIN_DAILY_CANDLES_REQUIRED = 3


@dataclass
class IntradaySimulatedEntry:
    """
    Une entrée détectée à un instant m15 donné de la simulation intraday,
    prête à être suivie par backtest/intraday_trade_tracker.py.

    Mêmes champs que backtest.simulator.SimulatedEntry (tp_50/tp_100/
    tp_htf_dol/stop_loss/bias/entry_price), avec entry_timestamp (datetime)
    au lieu de entry_date (date) -- la granularité m15 permet de savoir
    PRÉCISÉMENT à quel instant l'entrée a été détectée, nécessaire pour
    marcher bougie par bougie dans intraday_trade_tracker.py plutôt que
    jour par jour.
    """

    setup_name: str
    entry_timestamp: datetime
    bias: str  # "bullish" / "bearish"
    entry_price: float
    stop_loss: float
    tp_50: float | None
    tp_100: float | None
    tp_htf_dol: float | None
    # Informatif uniquement (voir docstring module) -- PAS utilisé pour le
    # prix d'entrée/SL/TP simulés ci-dessus, qui suivent toujours le
    # chemin par défaut (marché + cibles du CRT). Permet une analyse a
    # posteriori en Phase 3 de la fréquence des entrées TSQ précises.
    priority_entry_source: str | None = None


def _placeholder_candle(timestamp: datetime, price: float) -> Candle:
    """
    Bougie "en cours de formation" -- toujours ignorée par le
    `series.candles[:-1]` que chaque règle applique déjà (voir docstring
    module). Ses valeurs OHLC n'ont aucune importance (jamais lues), mais
    doivent rester > 0 (contrainte Candle, voir models/market_data.py).
    """
    return Candle(timestamp=timestamp, open=price, high=price, low=price, close=price)


class _HtfCache:
    """
    Cache les séries Monthly/Weekly/Daily CLOSES (sans placeholder) pour
    une date simulée donnée -- reconstruites uniquement quand la date
    change, pas à chaque bougie m15 (voir docstring module).
    """

    def __init__(
        self,
        monthly_candles: list[Candle],
        weekly_candles: list[Candle],
        daily_candles: list[Candle],
    ) -> None:
        self._monthly_candles = monthly_candles
        self._weekly_candles = weekly_candles
        self._daily_candles = daily_candles
        self._cached_date: date | None = None
        self._cached: dict[str, list[Candle]] | None = None

    def get_closed(self, simulated_date: date) -> dict[str, list[Candle]]:
        if self._cached_date == simulated_date and self._cached is not None:
            return self._cached

        result = {
            "monthly": _truncate_candles_strict(self._monthly_candles, simulated_date, "monthly"),
            "weekly": _truncate_candles_strict(self._weekly_candles, simulated_date, "weekly"),
            "daily": _truncate_candles_strict(self._daily_candles, simulated_date, "daily"),
        }
        self._cached_date = simulated_date
        self._cached = result
        return result


def run_intraday_simulation(
    raw_monthly: list[list[float]],
    raw_weekly: list[list[float]],
    raw_daily: list[list[float]],
    raw_m15: list[list[float]],
    symbol: str,
    years_to_test: int,
    decision_config: dict[str, Any],
    m15_window_days: int = DEFAULT_M15_WINDOW_DAYS,
    step_every_n_m15: int = 1,
) -> list[IntradaySimulatedEntry]:
    """
    Rejoue le pipeline de décision COMPLET (DecisionEngine.decide(), donc
    EntryRule/TsqRule/PurgeRule/CRT sur toutes les timeframes) bougie m15
    par bougie m15, sur les `years_to_test` dernières années disponibles
    dans raw_m15 (le reste de raw_m15, et tout raw_monthly/weekly/daily,
    sert de marge de contexte pour les ancres CRT/DOL -- voir
    backtest/historical_loader.py pour margin_years).

    Args:
        step_every_n_m15: 1 par défaut (évalue CHAQUE bougie m15 fermée,
            comportement le plus fidèle au live). Augmenter (ex: 4 pour
            évaluer toutes les heures) réduit le temps de calcul d'autant
            SANS perdre la précision m15 des données elles-mêmes (la
            fenêtre glissante reste construite à partir de tout le m15
            disponible) -- seule la fréquence de détection d'une NOUVELLE
            entrée est réduite. Recommandé pour un premier passage
            exploratoire sur plusieurs années ; remettre à 1 pour un
            résultat définitif.

    Returns:
        Liste des entrées détectées, dans l'ordre chronologique -- à
        transmettre à backtest/intraday_trade_tracker.py::
        classify_intraday_trades() pour connaître leur issue.
    """
    monthly_candles = to_candles(raw_monthly)
    weekly_candles = to_candles(raw_weekly)
    daily_candles = to_candles(raw_daily)
    m15_candles = to_candles(raw_m15)

    if not m15_candles or not daily_candles:
        return []

    last_date = m15_candles[-1].timestamp.date()
    # Arithmétique par timedelta plutôt que date(year - N, month, day) :
    # évite tout ValueError sur un 29 février tombant sur une année non
    # bissextile N années plus tôt.
    test_start_date = last_date - timedelta(days=365 * years_to_test)

    already_recorded: set[str] = set()
    entries: list[IntradaySimulatedEntry] = []

    engine = DecisionEngine(config=decision_config)
    htf_cache = _HtfCache(monthly_candles, weekly_candles, daily_candles)

    window_start_idx = 0
    window_span = timedelta(days=m15_window_days)

    for i, m15_candle in enumerate(m15_candles):
        # Instant simulé : juste après la clôture de cette bougie m15
        # (son ouverture + 15 min) -- la bougie i elle-même est donc déjà
        # une donnée CLOSE et connue à cet instant.
        now = m15_candle.timestamp + timedelta(minutes=15)
        simulated_date = now.date()

        if simulated_date < test_start_date:
            continue
        if step_every_n_m15 > 1 and i % step_every_n_m15 != 0:
            continue

        # Avance la fenêtre glissante m15 (jamais en arrière -- les deux
        # bornes ne font qu'augmenter au fil de la boucle).
        window_cutoff = now - window_span
        while window_start_idx < i and m15_candles[window_start_idx].timestamp < window_cutoff:
            window_start_idx += 1

        m15_window_closed = m15_candles[window_start_idx : i + 1]
        if len(m15_window_closed) < 3:
            continue  # historique m15 insuffisant pour analyser quoi que ce soit

        current_price = m15_window_closed[-1].close

        htf_closed = htf_cache.get_closed(simulated_date)
        if len(htf_closed["daily"]) < _MIN_DAILY_CANDLES_REQUIRED:
            continue  # historique Daily insuffisant (comme l'ancien simulateur)

        placeholder = _placeholder_candle(now, current_price)
        intraday_hourly = _resample_to_4h(m15_window_closed, _HOURLY_GRID_HOURS)

        candles = {
            "monthly": CandleSeries(candles=htf_closed["monthly"] + [placeholder]),
            "weekly": CandleSeries(candles=htf_closed["weekly"] + [placeholder]),
            "daily": CandleSeries(candles=htf_closed["daily"] + [placeholder]),
            "intraday": CandleSeries(candles=(intraday_hourly or [placeholder])),
            "m15": CandleSeries(candles=m15_window_closed + [placeholder]),
        }

        context = MarketContext(
            market_data=_build_market_data(symbol, current_price),
            candles=candles,
            symbol=symbol,
            simulated_now=now,
        )

        report = engine.decide(context)

        priority_entry_source = report.priority_entry.source if report.priority_entry else None

        for setup in report.timeframe_setups:
            if not setup.entry_recommended or not setup.htf_crt or not setup.crt_targets:
                continue
            if setup.setup_name in already_recorded:
                continue

            already_recorded.add(setup.setup_name)
            entries.append(
                IntradaySimulatedEntry(
                    setup_name=setup.setup_name,
                    entry_timestamp=now,
                    bias=setup.htf_crt["bias"],
                    entry_price=current_price,
                    stop_loss=setup.crt_targets["stop_loss"],
                    tp_50=setup.crt_targets.get("tp_50"),
                    tp_100=setup.crt_targets.get("tp_100"),
                    tp_htf_dol=setup.crt_targets.get("tp_htf_dol"),
                    priority_entry_source=priority_entry_source,
                )
            )

    return entries
