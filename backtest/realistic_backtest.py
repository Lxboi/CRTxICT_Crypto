"""
backtest/realistic_backtest.py

Phase 3 du plan "Backtest réaliste + Monte Carlo" : applique, sur les
entrées produites par backtest/intraday_simulator.py, les MÊMES
garde-fous d'exécution que main.py::dispatch_alert (voir chaque
vérification ci-dessous) -- skip "dernier objectif déjà atteint dès la
détection", filtre R:R minimum, et position sizing/levier réels
(execution/position_sizing.py) contre un capital simulé qui évolue
trade après trade -- au lieu du R-multiple géométrique indépendant de
la taille de compte produit par backtest/intraday_trade_tracker.py.

Simplifications assumées pour cette v1 :
  - Un seul actif à la fois, capital dédié -- pas de portefeuille
    partagé entre plusieurs actifs suivis en parallèle (amélioration
    future distincte, déjà notée comme telle dans le plan initial).
  - Les trades sont dimensionnés SÉQUENTIELLEMENT par ordre d'entrée,
    sans tenir compte d'éventuelles positions simultanées ENCORE
    OUVERTES sur ce même actif (ex: un CRT Weekly et un CRT Daily
    actifs en même temps) -- chaque trade est sizé contre TOUT le
    capital disponible à son instant d'entrée, comme si les trades
    précédents étaient déjà clôturés. Une vraie gestion de marge
    concurrente nécessiterait de suivre l'état "ouvert" de chaque
    position dans le temps (pas seulement son issue finale au moment où
    elle est classée) -- hors périmètre de cette v1, à traiter comme
    amélioration future si les résultats s'avèrent sensibles à ce point.
  - Ré-entrée TSQ précise (cas Weekly post-TP1) et blocage de
    coordination HTF (`main.py::_htf_coordination_blocks_execution`) :
    NON reproduits ici -- l'entrée reste toujours au marché avec les
    cibles du CRT, comme en Phase 2 (voir intraday_simulator.py).
  - Un trade encore OPEN à la fin des données n'impacte jamais le
    capital simulé (son PnL réel est inconnu tant qu'il n'est pas
    clôturé) -- capital_after == capital_before pour ces entrées, et
    ses coûts (fees/slippage) restent à None plutôt que d'être estimés
    sur une sortie hypothétique.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime

from execution.position_sizing import compute_position_size, compute_required_leverage
from messaging.formatter import compute_risk_reward_ratio
from models.market_data import Candle

from backtest.execution_costs import compute_costs_in_r
from backtest.intraday_simulator import IntradaySimulatedEntry
from backtest.trade_tracker import _available_levels, _compute_r_multiple, _touches_level


@dataclass
class RealisticTradeOutcome:
    """
    Équivalent de backtest.trade_tracker.TradeOutcome, étendu avec les
    coûts réels et l'évolution du capital simulé (voir docstring
    module). `rejected`/`rejected_reason` couvrent les trades JAMAIS
    exécutés (mêmes garde-fous que main.py::dispatch_alert) -- dans ce
    cas, tous les champs de résultat (outcome/r_multiple/fees_paid_r/
    ...) restent None et le capital n'est pas affecté (capital_after ==
    capital_before).
    """

    setup_name: str
    entry_date: date
    exit_date: date | None
    bias: str
    entry_price: float
    outcome: str | None  # None si rejeté avant toute exécution
    highest_tp_reached: int
    r_multiple: float | None  # brut, avant coûts -- None si rejeté ou encore OPEN
    fees_paid_r: float | None
    slippage_cost_r: float | None
    net_r_multiple: float | None  # après coûts -- None si rejeté ou encore OPEN
    quantity: float | None
    leverage_used: float | None
    capital_before: float
    capital_after: float
    rejected: bool
    rejected_reason: str | None


def _initial_highest_tp_hit(entry: IntradaySimulatedEntry, levels: list[tuple[int, float]]) -> int:
    """
    Réplique database/db.py::save_alert_if_new::initial_highest_tp_hit
    -- quels niveaux sont DÉJÀ dépassés par le prix d'entrée lui-même
    (cas dégénéré : une entrée au marché prise exactement au niveau ou
    au-delà d'une cible, plus aucune opportunité réelle vers elle).
    """
    highest = 0
    for level_num, tp_price in levels:
        already_hit = (entry.bias == "bullish" and entry.entry_price >= tp_price) or (
            entry.bias == "bearish" and entry.entry_price <= tp_price
        )
        if already_hit:
            highest = level_num
    return highest


def _exit_price_for_outcome(
    outcome: str, highest_tp_reached: int, entry: IntradaySimulatedEntry, levels: list[tuple[int, float]]
) -> float:
    """Prix de sortie effectif selon l'issue -- utilisé pour le calcul des coûts de la jambe de sortie."""
    if outcome == "LOSS":
        return entry.stop_loss
    if outcome == "BREAK_EVEN":
        return entry.entry_price  # SL déplacé au PE après TP1, comme en trading réel
    # WIN_TPx
    return next(price for level_num, price in levels if level_num == highest_tp_reached)


def _walk_forward(
    entry: IntradaySimulatedEntry, sorted_candles: list[Candle], sorted_timestamps: list[datetime]
) -> tuple[str, int, datetime | None]:
    """
    Même logique de suivi que
    intraday_trade_tracker.classify_intraday_trades(), appliquée à une
    seule entrée -- dupliquée plutôt que réutilisée directement car ce
    module a besoin d'entrelacer chaque entrée avec ses propres
    garde-fous d'exécution (voir run_realistic_backtest) AVANT de
    connaître son issue, ce que la fonction d'origine (qui traite toute
    la liste d'un coup, sans early-exit possible par entrée) ne permet
    pas facilement sans dupliquer une partie de sa boucle de toute
    façon.
    """
    levels = _available_levels(entry)
    current_stop = entry.stop_loss
    highest_tp_reached = 0
    outcome: str | None = None
    exit_timestamp: datetime | None = None

    start_index = bisect_left(sorted_timestamps, entry.entry_timestamp)
    for candle in sorted_candles[start_index:]:
        next_level = next((lvl for lvl in levels if lvl[0] > highest_tp_reached), None)
        tp_touched = next_level is not None and _touches_level(candle, entry.bias, next_level[1], "tp")
        sl_touched = _touches_level(candle, entry.bias, current_stop, "sl")

        if tp_touched and sl_touched:
            sl_touched = False  # hypothèse optimiste, même convention que trade_tracker.py

        if tp_touched:
            highest_tp_reached = next_level[0]
            if highest_tp_reached == 1:
                current_stop = entry.entry_price
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
    return outcome, highest_tp_reached, exit_timestamp


def run_realistic_backtest(
    entries: list[IntradaySimulatedEntry],
    m15_candles: list[Candle],
    starting_capital: float,
    risk_pct: float,
    max_leverage: float,
    fee_pct: float,
    slippage_bps: float,
    reserved_balance_pct: float = 0.0,
    min_risk_reward_ratio: float | None = None,
) -> list[RealisticTradeOutcome]:
    """
    Rejoue chaque entrée détectée par le simulateur intraday à travers
    les mêmes garde-fous d'exécution que main.py::dispatch_alert
    (dernier objectif déjà atteint, R:R minimum, plafond de levier), et
    fait évoluer un capital simulé trade après trade pour les entrées
    effectivement exécutées.

    Args:
        entries: DANS L'ORDRE CHRONOLOGIQUE (déjà garanti par
            construction, voir intraday_simulator.py) -- indispensable
            pour que l'évolution du capital soit cohérente.
        fee_pct: Taux ALLER (pas aller-retour) en % du notionnel, voir
            backtest/execution_costs.py::DEFAULT_FEE_RATES_PCT.
        slippage_bps: Slippage forfaitaire par jambe, en points de base.

    Returns:
        Une RealisticTradeOutcome par IntradaySimulatedEntry fournie,
        dans le même ordre (y compris les trades rejetés, pour ne
        perdre aucune trace de ce qui a été filtré et pourquoi).
    """
    sorted_candles = sorted(m15_candles, key=lambda c: c.timestamp)
    sorted_timestamps = [c.timestamp for c in sorted_candles]

    capital = starting_capital
    results: list[RealisticTradeOutcome] = []

    for entry in entries:
        levels = _available_levels(entry)

        if not levels:
            results.append(
                RealisticTradeOutcome(
                    setup_name=entry.setup_name, entry_date=entry.entry_timestamp.date(), exit_date=None,
                    bias=entry.bias, entry_price=entry.entry_price, outcome=None, highest_tp_reached=0,
                    r_multiple=None, fees_paid_r=None, slippage_cost_r=None, net_r_multiple=None,
                    quantity=None, leverage_used=None, capital_before=capital, capital_after=capital,
                    rejected=True, rejected_reason="no_target",
                )
            )
            continue

        initial_highest_tp_hit = _initial_highest_tp_hit(entry, levels)
        is_final_level_already_hit = initial_highest_tp_hit == levels[-1][0]
        if is_final_level_already_hit:
            results.append(
                RealisticTradeOutcome(
                    setup_name=entry.setup_name, entry_date=entry.entry_timestamp.date(), exit_date=None,
                    bias=entry.bias, entry_price=entry.entry_price, outcome=None, highest_tp_reached=0,
                    r_multiple=None, fees_paid_r=None, slippage_cost_r=None, net_r_multiple=None,
                    quantity=None, leverage_used=None, capital_before=capital, capital_after=capital,
                    rejected=True, rejected_reason="final_level_already_hit",
                )
            )
            continue

        targets = {
            "tp_50": entry.tp_50, "tp_100": entry.tp_100, "tp_htf_dol": entry.tp_htf_dol,
            "stop_loss": entry.stop_loss,
        }
        rr_ratio = compute_risk_reward_ratio(entry.entry_price, targets)
        if min_risk_reward_ratio is not None and rr_ratio is not None and rr_ratio < min_risk_reward_ratio:
            results.append(
                RealisticTradeOutcome(
                    setup_name=entry.setup_name, entry_date=entry.entry_timestamp.date(), exit_date=None,
                    bias=entry.bias, entry_price=entry.entry_price, outcome=None, highest_tp_reached=0,
                    r_multiple=None, fees_paid_r=None, slippage_cost_r=None, net_r_multiple=None,
                    quantity=None, leverage_used=None, capital_before=capital, capital_after=capital,
                    rejected=True, rejected_reason=f"rr_too_low ({rr_ratio:.2f} < {min_risk_reward_ratio})",
                )
            )
            continue

        try:
            quantity = compute_position_size(capital, risk_pct, entry.entry_price, entry.stop_loss)
            if quantity <= 0:
                raise ValueError("Taille de position calculée nulle ou négative")
            leverage = compute_required_leverage(
                quantity, entry.entry_price, capital, max_leverage, reserved_balance_pct
            )
        except ValueError as e:
            results.append(
                RealisticTradeOutcome(
                    setup_name=entry.setup_name, entry_date=entry.entry_timestamp.date(), exit_date=None,
                    bias=entry.bias, entry_price=entry.entry_price, outcome=None, highest_tp_reached=0,
                    r_multiple=None, fees_paid_r=None, slippage_cost_r=None, net_r_multiple=None,
                    quantity=None, leverage_used=None, capital_before=capital, capital_after=capital,
                    rejected=True, rejected_reason=f"leverage_or_sizing: {e}",
                )
            )
            continue

        outcome, highest_tp_reached, exit_timestamp = _walk_forward(entry, sorted_candles, sorted_timestamps)
        r_multiple = _compute_r_multiple(entry, outcome, highest_tp_reached, levels)

        capital_before = capital
        fees_paid_r = slippage_cost_r = net_r_multiple = None
        exit_date = exit_timestamp.date() if exit_timestamp else None

        if outcome != "OPEN":
            exit_price = _exit_price_for_outcome(outcome, highest_tp_reached, entry, levels)
            initial_risk = abs(entry.entry_price - entry.stop_loss)
            fees_paid_r, slippage_cost_r = compute_costs_in_r(
                entry.entry_price, exit_price, initial_risk, fee_pct, slippage_bps
            )
            net_r_multiple = (r_multiple or 0.0) - fees_paid_r - slippage_cost_r

            risk_amount = capital * risk_pct / 100
            capital = capital_before + net_r_multiple * risk_amount

        results.append(
            RealisticTradeOutcome(
                setup_name=entry.setup_name, entry_date=entry.entry_timestamp.date(), exit_date=exit_date,
                bias=entry.bias, entry_price=entry.entry_price, outcome=outcome,
                highest_tp_reached=highest_tp_reached, r_multiple=r_multiple, fees_paid_r=fees_paid_r,
                slippage_cost_r=slippage_cost_r, net_r_multiple=net_r_multiple, quantity=quantity,
                leverage_used=leverage, capital_before=capital_before, capital_after=capital,
                rejected=False, rejected_reason=None,
            )
        )

    return results
