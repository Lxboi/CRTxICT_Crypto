"""
backtest/intraday_trade_tracker.py

Équivalent de backtest/trade_tracker.py, mais pour les entrées produites
par backtest/intraday_simulator.py -- marche bougie m15 par bougie m15
(au lieu de jour par jour) pour déterminer l'issue de chaque trade. Cette
granularité plus fine résout NATIVEMENT l'essentiel de l'ambiguïté "TP et
SL touchés le même jour" documentée dans trade_tracker.py : un ordre réel
est retrouvé la plupart du temps à cette résolution (l'hypothèse
optimiste "TP touché en premier", conservée ci-dessous pour les rares cas
où les deux niveaux tombent dans la MÊME bougie m15, ne s'applique donc
plus qu'à une fraction bien plus faible des trades qu'avec le simulateur
Daily).

Réutilise directement la logique de détection de niveau
(_available_levels/_touches_level) et le calcul du R-multiple
(_compute_r_multiple) de trade_tracker.py -- IntradaySimulatedEntry
expose les mêmes attributs (bias/entry_price/stop_loss/tp_50/tp_100/
tp_htf_dol) que SimulatedEntry, donc ces fonctions s'appliquent telles
quelles (duck typing), sans dupliquer leur code.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime

from models.market_data import Candle

from backtest.intraday_simulator import IntradaySimulatedEntry
from backtest.trade_tracker import TradeOutcome, _available_levels, _compute_r_multiple, _touches_level


def classify_intraday_trades(
    entries: list[IntradaySimulatedEntry], m15_candles: list[Candle]
) -> list[TradeOutcome]:
    """
    Classe chaque entrée détectée par le simulateur intraday selon son
    issue réelle, en marchant bougie m15 par bougie m15 à partir de
    l'instant d'entrée (incluse).

    Le résultat reste un backtest.trade_tracker.TradeOutcome standard
    (entry_date/exit_date en `date`, pas en `datetime`) pour rester
    directement compatible avec backtest/report.py (print_summary/
    export_csv) sans aucune modification -- seule la granularité du
    SUIVI change, pas la forme du résultat final.
    """
    sorted_candles = sorted(m15_candles, key=lambda c: c.timestamp)
    sorted_timestamps = [c.timestamp for c in sorted_candles]

    outcomes: list[TradeOutcome] = []

    for entry in entries:
        levels = _available_levels(entry)
        if not levels:
            continue  # aucune cible disponible, rien à suivre

        current_stop = entry.stop_loss
        highest_tp_reached = 0
        outcome: str | None = None
        exit_timestamp: datetime | None = None

        # bisect plutôt qu'un filtre O(n) répété par entrée : sorted_candles
        # peut compter plusieurs centaines de milliers de bougies m15 sur
        # un backtest de plusieurs années, et le nombre d'entrées peut être
        # de l'ordre de la centaine -- un filtre linéaire par entrée
        # deviendrait le poste de coût dominant du tracker.
        start_index = bisect_left(sorted_timestamps, entry.entry_timestamp)

        for candle in sorted_candles[start_index:]:
            next_level = next((lvl for lvl in levels if lvl[0] > highest_tp_reached), None)

            tp_touched = next_level is not None and _touches_level(candle, entry.bias, next_level[1], "tp")
            sl_touched = _touches_level(candle, entry.bias, current_stop, "sl")

            if tp_touched and sl_touched:
                # Ambiguïté (même bougie m15) : même hypothèse optimiste
                # que trade_tracker.py -- TP considéré touché en premier.
                sl_touched = False

            if tp_touched:
                highest_tp_reached = next_level[0]
                if highest_tp_reached == 1:
                    current_stop = entry.entry_price  # break-even après TP1, comme en trading réel
                if highest_tp_reached == levels[-1][0]:
                    outcome = f"WIN_TP{highest_tp_reached}"
                    exit_timestamp = candle.timestamp
                    break
                continue

            if sl_touched:
                outcome = "BREAK_EVEN" if highest_tp_reached >= 1 else "LOSS"
                exit_timestamp = candle.timestamp
                break

        if outcome is None:
            outcome = "OPEN"

        r_multiple = _compute_r_multiple(entry, outcome, highest_tp_reached, levels)

        outcomes.append(
            TradeOutcome(
                setup_name=entry.setup_name,
                entry_date=entry.entry_timestamp.date(),
                exit_date=exit_timestamp.date() if exit_timestamp else None,
                bias=entry.bias,
                entry_price=entry.entry_price,
                outcome=outcome,
                highest_tp_reached=highest_tp_reached,
                r_multiple=r_multiple,
            )
        )

    return outcomes
