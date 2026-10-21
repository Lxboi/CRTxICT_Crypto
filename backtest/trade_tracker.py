"""
backtest/trade_tracker.py

Pour chaque entrée détectée par le simulateur, "marche" en avant dans
les données historiques (déjà connues, aucune triche) jusqu'à trouver
l'issue du trade :

  - PERTE : le SL initial est touché avant tout TP.
  - BREAK_EVEN : TP1 est touché en premier (SL alors déplacé au prix
    d'entrée, comme en trading réel), PUIS le prix revient toucher ce
    nouveau SL avant TP2/TP3.
  - WIN_TP1 / WIN_TP2 / WIN_TP3 : le dernier objectif atteint avant
    tout retour au point d'entrée après TP1.
  - OPEN : le trade n'a pas encore trouvé d'issue à la fin des données
    disponibles (toujours "en cours" — ni gagné, ni perdu, ni BE).

Règle explicite pour les cas ambigus (TP et SL touchés la même bougie
journalière, ordre réel inconnu à cette granularité) : on considère
que le TP a été touché EN PREMIER (hypothèse optimiste, décision
utilisateur) — voir _check_day().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from models.market_data import Candle

from backtest.simulator import SimulatedEntry


@dataclass
class TradeOutcome:
    """Résultat final d'un trade simulé, une fois son issue déterminée (ou non, si OPEN)."""

    setup_name: str
    entry_date: date
    exit_date: date | None
    bias: str
    entry_price: float
    outcome: str  # "WIN_TP1" / "WIN_TP2" / "WIN_TP3" / "BREAK_EVEN" / "LOSS" / "OPEN"
    highest_tp_reached: int  # 0, 1, 2 ou 3
    r_multiple: float | None  # (gain ou perte) / (risque initial), None si OPEN


def _available_levels(entry: SimulatedEntry) -> list[tuple[int, float]]:
    """Niveaux de TP disponibles pour cette entrée, dans l'ordre (1, 2, 3)."""
    levels: list[tuple[int, float]] = []
    if entry.tp_50 is not None:
        levels.append((1, entry.tp_50))
    if entry.tp_100 is not None:
        levels.append((2, entry.tp_100))
    if entry.tp_htf_dol is not None:
        levels.append((3, entry.tp_htf_dol))
    return levels


def _touches_level(candle: Candle, bias: str, level_price: float, direction: str) -> bool:
    """
    direction: "tp" (le prix doit ATTEINDRE le niveau dans le sens du
    trade) ou "sl" (le prix doit ATTEINDRE le niveau dans le sens
    opposé, donc invalidant le trade).
    """
    if bias == "bullish":
        if direction == "tp":
            return candle.high >= level_price
        return candle.low <= level_price
    else:  # bearish
        if direction == "tp":
            return candle.low <= level_price
        return candle.high >= level_price


def classify_trades(
    entries: list[SimulatedEntry], daily_candles: list[Candle]
) -> list[TradeOutcome]:
    """
    Classe chaque entrée détectée par le simulateur selon son issue
    réelle, en marchant jour par jour à partir de sa date d'entrée
    (incluse — la bougie du jour d'entrée est la première à pouvoir
    toucher un niveau).
    """
    candles_by_date = {c.timestamp.date(): c for c in daily_candles}
    sorted_dates = sorted(candles_by_date)

    outcomes: list[TradeOutcome] = []

    for entry in entries:
        levels = _available_levels(entry)
        if not levels:
            continue  # aucune cible disponible, rien à suivre

        current_stop = entry.stop_loss
        highest_tp_reached = 0
        outcome: str | None = None
        exit_date: date | None = None

        future_dates = [d for d in sorted_dates if d >= entry.entry_date]

        for day in future_dates:
            candle = candles_by_date[day]
            next_level = next((lvl for lvl in levels if lvl[0] > highest_tp_reached), None)

            tp_touched_today = (
                next_level is not None
                and _touches_level(candle, entry.bias, next_level[1], "tp")
            )
            sl_touched_today = _touches_level(candle, entry.bias, current_stop, "sl")

            if tp_touched_today and sl_touched_today:
                # Ambiguïté (même bougie journalière) : on considère le
                # TP touché en premier (hypothèse optimiste, décision
                # utilisateur) -> on traite le TP ci-dessous, on ignore
                # le SL touché ce même jour.
                sl_touched_today = False

            if tp_touched_today:
                highest_tp_reached = next_level[0]
                if highest_tp_reached == 1:
                    # Premier TP atteint -> SL déplacé au prix d'entrée
                    # (break-even), comme en trading réel.
                    current_stop = entry.entry_price

                if highest_tp_reached == levels[-1][0]:
                    # Dernier objectif disponible atteint -> trade gagné, terminé.
                    outcome = f"WIN_TP{highest_tp_reached}"
                    exit_date = day
                    break
                continue  # objectif intermédiaire atteint, on continue de suivre

            if sl_touched_today:
                outcome = "BREAK_EVEN" if highest_tp_reached >= 1 else "LOSS"
                exit_date = day
                break

        if outcome is None:
            outcome = "OPEN"

        r_multiple = _compute_r_multiple(entry, outcome, highest_tp_reached, levels)

        outcomes.append(
            TradeOutcome(
                setup_name=entry.setup_name,
                entry_date=entry.entry_date,
                exit_date=exit_date,
                bias=entry.bias,
                entry_price=entry.entry_price,
                outcome=outcome,
                highest_tp_reached=highest_tp_reached,
                r_multiple=r_multiple,
            )
        )

    return outcomes


def _compute_r_multiple(
    entry: SimulatedEntry, outcome: str, highest_tp_reached: int, levels: list[tuple[int, float]]
) -> float | None:
    """
    Calcule le résultat en multiple du risque initial (R) : +1R signifie
    un gain égal au risque pris (distance entrée -> SL initial). None
    si le trade est encore OPEN (issue inconnue).
    """
    if outcome == "OPEN":
        return None

    initial_risk = abs(entry.entry_price - entry.stop_loss)
    if initial_risk == 0:
        return None

    if outcome == "LOSS":
        return -1.0
    if outcome == "BREAK_EVEN":
        return 0.0

    # WIN_TPx : distance entrée -> niveau du dernier TP atteint, en multiples du risque initial.
    tp_price = next(price for level_num, price in levels if level_num == highest_tp_reached)
    gain = abs(tp_price - entry.entry_price)
    return gain / initial_risk
