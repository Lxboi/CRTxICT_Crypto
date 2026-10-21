"""
monte_carlo.py

Point d'entrée du Monte Carlo (Phase 4) : ré-échantillonnage bootstrap
de la série de net_r_multiple produite par un backtest RÉALISTE (Phase
3, voir run_backtest_intraday.py) -- prend en entrée le CSV
backtest_realistic_<label>_<years>y.csv déjà exporté, jamais besoin de
relancer tout le backtest intraday (qui peut prendre plusieurs minutes
à plusieurs dizaines de minutes selon la période, voir
run_backtest_intraday.py).

⚠️ --starting-capital et --risk-pct DOIVENT correspondre à ceux
utilisés pour PRODUIRE le fichier --input (mêmes valeurs que le run
run_backtest_intraday.py d'origine) -- sinon la composition du capital
simulé ici ne reproduira pas fidèlement ce que le backtest réaliste a
réellement fait. Les COÛTS (frais/slippage), eux, sont déjà inclus dans
chaque net_r_multiple du CSV -- pas besoin de les repasser ici.

Usage :
    # Standard -- même capital/risque que le backtest réaliste d'origine
    # (10000 / config/alerts.yaml::execution.risk_pct_per_trade par défaut).
    python monte_carlo.py --input backtest_realistic_BTC_1y.csv

    # Résultat reproductible (utile pour comparer avant/après un
    # changement de config) + plus de trajectoires pour affiner les
    # percentiles.
    python monte_carlo.py --input backtest_realistic_BTC_1y.csv --n-simulations 5000 --seed 42

    # Projeter sur un horizon plus long que celui observé (ex: 3 ans si
    # --input couvre ~1 an, donc ~3x son nombre de trades) -- toujours en
    # piochant dans le MÊME sac de résultats observés, jamais de nouvelle
    # valeur de R inventée.
    python monte_carlo.py --input backtest_realistic_BTC_1y.csv --n-trades 120

    # Définition plus prudente de la "ruine" -- ici, perdre la moitié du
    # capital de départ compte déjà comme un échec (défaut : 100% = capital
    # tombé à 0 exactement).
    python monte_carlo.py --input backtest_realistic_BTC_1y.csv --ruin-threshold-pct 50

Voir --help pour la liste complète.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from backtest.monte_carlo import run_monte_carlo
from backtest.report import export_monte_carlo_csv, print_monte_carlo_summary


def _load_net_r_values(input_path: Path) -> list[float]:
    """
    Lit le CSV backtest_realistic_<label>_<years>y.csv (voir
    backtest/report.py::export_realistic_csv) et extrait net_r_multiple
    pour les trades RÉELLEMENT exécutés et CLÔTURÉS uniquement -- jamais
    un trade rejeté par un garde-fou (colonne `rejected`), ni encore
    OPEN (`net_r_multiple` vide dans ce cas).
    """
    values: list[float] = []
    # encoding="utf-8" explicite : même raisonnement Windows/cp1252 que
    # run_backtest_intraday.py -- le CSV a été écrit en UTF-8 par
    # backtest/report.py, le relire sans le préciser planterait sur
    # Windows dès qu'un champ contient un caractère non-ASCII.
    with open(input_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("rejected") == "1":
                continue
            raw = row.get("net_r_multiple", "")
            if raw:
                values.append(float(raw))
    return values


def _resolve_default_risk_pct() -> float | None:
    """Même config que le backtest réaliste (config/alerts.yaml::execution.risk_pct_per_trade)."""
    config_path = Path(__file__).parent / "config" / "alerts.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return (config.get("execution") or {}).get("risk_pct_per_trade")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monte Carlo (bootstrap) sur un backtest réaliste déjà exporté (Phase 3)."
    )
    parser.add_argument(
        "--input", required=True,
        help="Chemin du CSV backtest_realistic_<label>_<years>y.csv à ré-échantillonner (voir run_backtest_intraday.py).",
    )
    parser.add_argument(
        "--starting-capital", type=float, default=10_000.0,
        help=(
            "DOIT correspondre au --starting-capital utilisé pour produire --input (défaut : 10000, "
            "même défaut que run_backtest_intraday.py)."
        ),
    )
    parser.add_argument(
        "--risk-pct", type=float, default=None,
        help=(
            "DOIT correspondre au --risk-pct/execution.risk_pct_per_trade utilisé pour produire "
            "--input. Défaut : lu depuis config/alerts.yaml."
        ),
    )
    parser.add_argument(
        "--n-simulations", type=int, default=2000,
        help="Nombre de trajectoires bootstrap indépendantes simulées (défaut : 2000).",
    )
    parser.add_argument(
        "--n-trades", type=int, default=None,
        help=(
            "Nombre de trades par trajectoire simulée -- défaut : autant que dans --input (même "
            "horizon que le backtest observé). Augmenter pour projeter sur un horizon plus long."
        ),
    )
    parser.add_argument(
        "--ruin-threshold-pct", type=float, default=100.0,
        help="%% de perte du capital de départ considéré comme 'ruine' (défaut : 100 = capital tombé à 0).",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Graine aléatoire pour un résultat reproductible (défaut : tirage réel, non reproductible).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Chemin du CSV de détail par trajectoire (défaut : backtest_montecarlo_<nom_input>.csv).",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"Fichier introuvable : {input_path}")

    net_r_values = _load_net_r_values(input_path)
    if not net_r_values:
        parser.error(
            f"Aucun trade net exploitable dans {input_path} (tous rejetés par un garde-fou, ou encore "
            "OPEN) -- rien à ré-échantillonner."
        )

    risk_pct = args.risk_pct if args.risk_pct is not None else _resolve_default_risk_pct()
    if risk_pct is None:
        parser.error(
            "execution.risk_pct_per_trade manquant dans config/alerts.yaml -- fournissez --risk-pct explicitement."
        )

    observed_r_cumulative = sum(net_r_values)
    print(
        f"{len(net_r_values)} trade(s) net(s) exploitable(s) dans {input_path} "
        f"(R cumulé net observé : {observed_r_cumulative:+.2f}R)."
    )
    print(f"Simulation de {args.n_simulations} trajectoire(s) bootstrap, {risk_pct}% de risque/trade...")

    result = run_monte_carlo(
        net_r_values,
        n_trades=args.n_trades,
        n_simulations=args.n_simulations,
        starting_capital=args.starting_capital,
        risk_pct=risk_pct,
        ruin_threshold_pct=args.ruin_threshold_pct,
        seed=args.seed,
    )

    print_monte_carlo_summary(result, input_path.stem, observed_r_cumulative)

    output_path = Path(args.output) if args.output else Path(f"backtest_montecarlo_{input_path.stem}.csv")
    export_monte_carlo_csv(result, output_path)


if __name__ == "__main__":
    main()
