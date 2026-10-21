"""
backtest/report.py

Agrège les TradeOutcome produits par trade_tracker.classify_trades()
en statistiques lisibles (winrate, R moyen, répartition des issues),
et permet l'export en CSV pour analyse plus poussée (Excel, etc.).

print_realistic_summary()/export_realistic_csv()/compute_equity_curve()
(Phase 3) font de même pour les RealisticTradeOutcome produits par
backtest/realistic_backtest.py -- coûts réels, capital simulé, courbe
d'equity et drawdown max -- entrée directe pour la Phase 4 (Monte
Carlo, ré-échantillonnage bootstrap de la série de net_r_multiple).

print_monte_carlo_summary()/export_monte_carlo_csv() (Phase 4) résument
et exportent le MonteCarloResult produit par
backtest/monte_carlo.py::run_monte_carlo().
"""

from __future__ import annotations

import csv
import statistics
from collections import Counter
from pathlib import Path

from backtest.monte_carlo import MonteCarloResult, percentile

from backtest.realistic_backtest import RealisticTradeOutcome
from backtest.trade_tracker import TradeOutcome


def print_summary(outcomes: list[TradeOutcome], symbol: str) -> None:
    """Affiche un résumé lisible des résultats du backtest dans la console."""
    closed = [o for o in outcomes if o.outcome != "OPEN"]
    still_open = [o for o in outcomes if o.outcome == "OPEN"]

    total = len(closed)
    print("=" * 60)
    print(f"RÉSULTATS DU BACKTEST — {symbol}")
    print("=" * 60)
    print(f"Total de trades détectés : {len(outcomes)}")
    print(f"  Clôturés (issue connue) : {total}")
    print(f"  Encore ouverts (fin des données) : {len(still_open)}")

    if total == 0:
        print("\nAucun trade clôturé -> pas de statistiques calculables.")
        return

    wins = [o for o in closed if o.outcome.startswith("WIN_")]
    break_evens = [o for o in closed if o.outcome == "BREAK_EVEN"]
    losses = [o for o in closed if o.outcome == "LOSS"]

    winrate_brut = len(wins) / total * 100
    winrate_sans_perte = (len(wins) + len(break_evens)) / total * 100

    print(f"\n  Gagnants (TP atteint) : {len(wins)} ({winrate_brut:.1f}%)")
    for tp_level in (1, 2, 3):
        count = sum(1 for o in wins if o.highest_tp_reached == tp_level)
        if count:
            print(f"    dont TP{tp_level} (dernier objectif atteint) : {count}")
    print(f"  Break-even (TP1 puis retour au PE) : {len(break_evens)} ({len(break_evens)/total*100:.1f}%)")
    print(f"  Perdants (SL initial touché) : {len(losses)} ({len(losses)/total*100:.1f}%)")

    print(f"\n  Winrate brut (gagnants / total) : {winrate_brut:.1f}%")
    print(f"  Winrate élargi (gagnants + break-even) : {winrate_sans_perte:.1f}%")

    r_values = [o.r_multiple for o in closed if o.r_multiple is not None]
    if r_values:
        avg_r = sum(r_values) / len(r_values)
        total_r = sum(r_values)
        print(f"\n  R moyen par trade : {avg_r:+.2f}R")
        print(f"  R cumulé (somme de tous les trades) : {total_r:+.2f}R")

    print("=" * 60)


def compute_equity_curve(outcomes: list[RealisticTradeOutcome], starting_capital: float) -> list[float]:
    """
    Courbe d'equity (capital simulé après chaque trade CLÔTURÉ et
    effectivement EXÉCUTÉ, dans l'ordre chronologique -- les trades
    rejetés par un garde-fou ou encore OPEN n'ajoutent aucun point,
    puisqu'ils n'affectent jamais le capital, voir
    backtest/realistic_backtest.py). Premier point = starting_capital
    (avant tout trade) -- entrée directe pour la Phase 4 (Monte Carlo).
    """
    curve = [starting_capital]
    for o in outcomes:
        if not o.rejected and o.outcome != "OPEN":
            curve.append(o.capital_after)
    return curve


def compute_max_drawdown_pct(equity_curve: list[float]) -> float:
    """Drawdown maximum (en %) observé sur la courbe d'equity fournie (0.0 si vide ou monotone croissante)."""
    peak = equity_curve[0] if equity_curve else 0.0
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    return max_drawdown


def print_realistic_summary(outcomes: list[RealisticTradeOutcome], symbol: str, starting_capital: float) -> None:
    """
    Affiche un résumé lisible des résultats du backtest RÉALISTE (Phase
    3 -- coûts réels + garde-fous de position sizing/levier), en
    complément de print_summary() (qui reste le résumé "brut",
    géométrique, sans coûts ni garde-fous).
    """
    rejected = [o for o in outcomes if o.rejected]
    executed = [o for o in outcomes if not o.rejected]
    closed = [o for o in executed if o.outcome != "OPEN"]
    still_open = [o for o in executed if o.outcome == "OPEN"]

    print("=" * 60)
    print(f"RÉSULTATS DU BACKTEST RÉALISTE (coûts + garde-fous) — {symbol}")
    print("=" * 60)
    print(f"Total d'entrées détectées : {len(outcomes)}")
    print(f"  Rejetées par un garde-fou (jamais exécutées) : {len(rejected)}")
    if rejected:
        reasons = Counter(o.rejected_reason for o in rejected)
        for reason, count in reasons.most_common():
            print(f"    - {reason} : {count}")
    print(f"  Exécutées : {len(executed)}")
    print(f"    Clôturées (issue connue) : {len(closed)}")
    print(f"    Encore ouvertes (fin des données, capital non affecté) : {len(still_open)}")

    if not closed:
        print("\nAucun trade exécuté et clôturé -> pas de statistiques de coûts/capital calculables.")
        print("=" * 60)
        return

    gross_r = [o.r_multiple for o in closed if o.r_multiple is not None]
    net_r = [o.net_r_multiple for o in closed if o.net_r_multiple is not None]
    fees_r = [o.fees_paid_r for o in closed if o.fees_paid_r is not None]
    slippage_r = [o.slippage_cost_r for o in closed if o.slippage_cost_r is not None]

    print(f"\n  R cumulé BRUT (avant coûts, {len(gross_r)} trade(s)) : {sum(gross_r):+.2f}R")
    print(f"  Frais cumulés : {sum(fees_r):.2f}R")
    print(f"  Slippage cumulé : {sum(slippage_r):.2f}R")
    print(f"  R cumulé NET (après coûts) : {sum(net_r):+.2f}R")

    equity_curve = compute_equity_curve(outcomes, starting_capital)
    final_capital = equity_curve[-1]
    total_return_pct = (final_capital / starting_capital - 1) * 100 if starting_capital else 0.0
    max_drawdown_pct = compute_max_drawdown_pct(equity_curve)

    print(f"\n  Capital de départ : {starting_capital:.2f}")
    print(f"  Capital final (trades clôturés uniquement) : {final_capital:.2f}")
    print(f"  Rendement total : {total_return_pct:+.1f}%")
    print(f"  Drawdown maximum (sur le capital simulé) : {max_drawdown_pct:.1f}%")
    print("=" * 60)


def export_realistic_csv(outcomes: list[RealisticTradeOutcome], output_path: Path) -> None:
    """Exporte le détail de chaque entrée (rejetée ou exécutée) du backtest réaliste en CSV."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "setup_name", "entry_date", "exit_date", "bias", "entry_price", "outcome",
                "r_multiple", "fees_paid_r", "slippage_cost_r", "net_r_multiple", "quantity",
                "leverage_used", "capital_before", "capital_after", "rejected", "rejected_reason",
            ]
        )
        for o in outcomes:
            writer.writerow(
                [
                    o.setup_name,
                    o.entry_date.isoformat(),
                    o.exit_date.isoformat() if o.exit_date else "",
                    o.bias,
                    f"{o.entry_price:.8f}",
                    o.outcome or "",
                    f"{o.r_multiple:.3f}" if o.r_multiple is not None else "",
                    f"{o.fees_paid_r:.4f}" if o.fees_paid_r is not None else "",
                    f"{o.slippage_cost_r:.4f}" if o.slippage_cost_r is not None else "",
                    f"{o.net_r_multiple:.3f}" if o.net_r_multiple is not None else "",
                    f"{o.quantity:.8f}" if o.quantity is not None else "",
                    f"{o.leverage_used:.2f}" if o.leverage_used is not None else "",
                    f"{o.capital_before:.2f}",
                    f"{o.capital_after:.2f}",
                    "1" if o.rejected else "0",
                    o.rejected_reason or "",
                ]
            )
    print(f"\nDétail réaliste exporté vers : {output_path}")


def export_csv(outcomes: list[TradeOutcome], output_path: Path) -> None:
    """Exporte le détail de chaque trade en CSV, pour analyse dans Excel ou ailleurs."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["setup_name", "entry_date", "exit_date", "bias", "entry_price", "outcome", "r_multiple"]
        )
        for o in outcomes:
            writer.writerow(
                [
                    o.setup_name,
                    o.entry_date.isoformat(),
                    o.exit_date.isoformat() if o.exit_date else "",
                    o.bias,
                    f"{o.entry_price:.8f}",
                    o.outcome,
                    f"{o.r_multiple:.3f}" if o.r_multiple is not None else "",
                ]
            )
    print(f"\nDétail exporté vers : {output_path}")


def print_monte_carlo_summary(
    result: MonteCarloResult, symbol: str, observed_r_cumulative: float | None = None
) -> None:
    """
    Affiche un résumé lisible du Monte Carlo (Phase 4) : bandes de
    percentile du rendement/capital final, distribution du drawdown
    maximum, probabilité de ruine.

    Args:
        observed_r_cumulative: R cumulé NET réellement observé dans le
            backtest réaliste d'origine (Phase 3, avant tout
            ré-échantillonnage) -- purement informatif, affiché en
            comparaison (voir note sur la convergence ci-dessous), pas
            utilisé dans le calcul.
    """
    print("=" * 60)
    print(f"MONTE CARLO (bootstrap) — {symbol}")
    print("=" * 60)
    print(f"Trajectoires simulées : {result.n_simulations} × {result.n_trades_per_simulation} trade(s) chacune")
    print(
        f"Capital de départ : {result.starting_capital:.2f} | Risque/trade : {result.risk_pct}% | "
        f"Seuil de ruine : -{result.ruin_threshold_pct:.0f}% du capital de départ"
    )

    returns_pct = [
        (c / result.starting_capital - 1) * 100 if result.starting_capital else 0.0
        for c in result.final_capitals
    ]

    print("\n  Rendement total simulé, par percentile (capital final entre parenthèses) :")
    for p in (5, 25, 50, 75, 95):
        print(f"    p{p:<2} : {percentile(returns_pct, p):+7.1f}%   ({percentile(result.final_capitals, p):.2f})")

    print("\n  Drawdown maximum simulé, par percentile :")
    for p in (50, 75, 95):
        print(f"    p{p:<2} : {percentile(result.max_drawdowns_pct, p):.1f}%")

    print(
        f"\n  Probabilité de ruine (perte >= {result.ruin_threshold_pct:.0f}% du capital de départ, à "
        f"n'importe quel moment de la trajectoire) : {result.probability_of_ruin:.1%}"
    )

    mean_final_capital = statistics.fmean(result.final_capitals)
    mean_return_pct = (
        (mean_final_capital / result.starting_capital - 1) * 100 if result.starting_capital else 0.0
    )
    print(f"\n  Rendement moyen simulé (toutes trajectoires confondues) : {mean_return_pct:+.1f}%")
    if observed_r_cumulative is not None:
        print(
            "  R cumulé NET observé dans le backtest réaliste d'origine (une seule séquence) : "
            f"{observed_r_cumulative:+.2f}R"
        )
        print(
            "  (comparaison informative -- une seule séquence historique n'est qu'UN tirage possible "
            "parmi tous ceux simulés ci-dessus ; pas d'égalité exacte attendue, voir CLAUDE.md.)"
        )
    print("=" * 60)


def export_monte_carlo_csv(result: MonteCarloResult, output_path: Path) -> None:
    """Exporte le détail de chaque trajectoire simulée (capital final, rendement, drawdown max) en CSV."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["simulation", "final_capital", "return_pct", "max_drawdown_pct", "ruined"])
        ruin_level = result.starting_capital * (1 - result.ruin_threshold_pct / 100)
        for i, (capital, drawdown) in enumerate(zip(result.final_capitals, result.max_drawdowns_pct), start=1):
            return_pct = (capital / result.starting_capital - 1) * 100 if result.starting_capital else 0.0
            writer.writerow(
                [i, f"{capital:.2f}", f"{return_pct:.2f}", f"{drawdown:.2f}", "1" if capital <= ruin_level else "0"]
            )
    print(f"\nDétail Monte Carlo exporté vers : {output_path}")
