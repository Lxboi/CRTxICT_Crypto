"""
run_backtest_intraday.py

Point d'entrée du backtest INTRADAY (Phase 2) : récupère l'historique
m15, rejoue le VRAI pipeline de décision (DecisionEngine.decide(), donc
EntryRule/TsqRule/PurgeRule/CRT) bougie m15 par bougie m15, classe chaque
trade détecté, affiche un résumé et exporte le détail en CSV.

Différence avec run_backtest.py (l'ancien simulateur, toujours
disponible pour comparaison avant/après) : celui-ci ne rejoue que le
biais Monthly/Weekly/Daily + cibles CRT avec une entrée au close du jour
D, sans jamais fournir de données intraday/m15 à DecisionEngine -- ce qui
empêche structurellement PurgeRule de valider un CRT Daily ou 4H comme
"high probability" (voir backtest/intraday_simulator.py, docstring
module, pour le détail). Ce nouveau script comble cet écart.

⚠️ Coût de calcul bien plus élevé que run_backtest.py : simule CHAQUE
bougie m15 fermée (~96/jour) au lieu d'un pas par jour -- un backtest de
plusieurs années peut prendre de plusieurs minutes à plusieurs dizaines
de minutes selon la période testée. Voir --step-every-n-m15 pour un
premier passage exploratoire plus rapide (résultat moins précis), et
réduire --years pour un premier essai si besoin.

Depuis la Phase 3, un SECOND backtest tourne automatiquement juste après
le premier (géométrique/Phase 2) : le backtest RÉALISTE (voir
backtest/realistic_backtest.py) applique les mêmes garde-fous
d'exécution que main.py::dispatch_alert (skip "dernier objectif déjà
atteint", filtre R:R minimum, plafond de levier) et modélise les frais
+ le slippage contre un capital simulé qui évolue trade après trade --
résultat exporté séparément dans backtest_realistic_<label>_<years>y.csv
(voir --starting-capital/--fee-pct/--slippage-bps/--skip-realistic).

Usage :
    # Run standard -- BTC, 1 an, tous les seuils lus depuis config/alerts.yaml
    # (risk_pct_per_trade, reserved_balance_pct, exchange/max_leverage,
    # min_risk_reward_ratio) : identique à ce qu'utiliserait le bot live.
    python run_backtest_intraday.py --source binance --symbol BTC --years 1

    # Forex/CFD via Polygon -- --step-every-n-m15 4 (une décision évaluée
    # toutes les heures au lieu de chaque m15) pour un premier passage
    # exploratoire nettement plus rapide, au prix d'une détection un peu
    # moins réactive des nouvelles entrées (voir --step-every-n-m15 ci-dessous).
    python run_backtest_intraday.py --source polygon --ticker C:EURUSD --years 1 --step-every-n-m15 4

    # Contrats Futures Binance (au lieu de Spot) sur 2 ans.
    python run_backtest_intraday.py --source binance --symbol BTC --market futures --years 2

    # N'exécuter QUE le backtest brut/géométrique (Phase 2), sans le
    # backtest réaliste (Phase 3 -- coûts + garde-fous) qui s'enchaîne
    # automatiquement par défaut.
    python run_backtest_intraday.py --source binance --symbol BTC --years 1 --skip-realistic

    # Backtest réaliste avec un capital de départ et des coûts personnalisés
    # (frais/slippage plus élevés que les valeurs par défaut, ex: pour un
    # actif moins liquide, ou pour un exchange sans taux public connu --
    # voir backtest/execution_costs.py::DEFAULT_FEE_RATES_PCT).
    python run_backtest_intraday.py --source binance --symbol BTC --years 1 --starting-capital 5000 --fee-pct 0.06 --slippage-bps 10

    # Explorer l'IMPACT du risque/de la réserve de marge/du plafond de
    # levier SANS toucher à config/alerts.yaml (donc sans aucun effet sur
    # le bot live) -- pratique pour comparer plusieurs scénarios sur le
    # même run : ici, 0% de réserve de marge au lieu des 30% configurés
    # (voir échange avec l'utilisateur du 16/08 sur le calcul du levier).
    python run_backtest_intraday.py --source binance --symbol BTC --years 1 --reserved-balance-pct 0

    # Même principe pour le risque par trade, le plafond de levier, et le
    # R:R minimum -- combinables entre eux, chacun ne surcharge QUE ce
    # paramètre précis (les autres restent lus depuis config/alerts.yaml).
    python run_backtest_intraday.py --source binance --symbol BTC --years 1 --risk-pct 1
    python run_backtest_intraday.py --source binance --symbol BTC --years 1 --max-leverage 20
    python run_backtest_intraday.py --source binance --symbol BTC --years 1 --min-rr 2.0

    # IBKR (DXY/SP500/NQ, Forex/CFD exécutés via IBKR) : --fee-pct
    # OBLIGATOIRE ici (commission par contrat, aucun taux %% par défaut --
    # voir DEFAULT_FEE_RATES_PCT) -- sans lui, seule la Phase 2 (brute)
    # s'exécute, la Phase 3 (réaliste) est ignorée avec un avertissement.
    python run_backtest_intraday.py --source polygon --ticker C:EURUSD --years 1 --fee-pct 0.10

Voir --help pour la liste complète et le détail de chaque option
(--market, --margin-years, --m15-window-days, --output...).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from backtest.execution_costs import DEFAULT_SLIPPAGE_BPS, resolve_default_fee_pct
from backtest.historical_loader import (
    fetch_historical_multi_timeframe,
    fetch_historical_multi_timeframe_polygon,
)
from backtest.intraday_simulator import DEFAULT_M15_WINDOW_DAYS, run_intraday_simulation
from backtest.intraday_trade_tracker import classify_intraday_trades
from backtest.realistic_backtest import run_realistic_backtest
from backtest.report import export_csv, export_realistic_csv, print_realistic_summary, print_summary
from backtest.simulator import to_candles
from backtest.trade_tracker import TradeOutcome


def _load_decision_config() -> dict:
    """Réutilise config/alerts.yaml (mêmes seuils qu'en live), sans la section orchestration."""
    config_path = Path(__file__).parent / "config" / "alerts.yaml"
    # encoding="utf-8" explicite : sans ça, Windows utilise l'encodage de
    # la console (cp1252 en général) au lieu d'UTF-8, ce qui plante sur
    # les accents/emojis du YAML (UnicodeDecodeError) -- Linux/Mac ne
    # sont généralement pas affectés (locale UTF-8 par défaut), d'où le
    # bug resté invisible jusqu'ici.
    with open(config_path, encoding="utf-8") as f:
        full_config = yaml.safe_load(f)
    return {k: v for k, v in full_config.items() if k != "orchestration"}


def _warn_if_insufficient_m15_history(raw_m15: list, years_requested: int, source: str) -> None:
    """
    Signale si le volume de bougies m15 reçu est nettement inférieur à ce
    qui était attendu pour la période demandée -- ~96 bougies/jour, donc
    ~35 000/an. Symptôme le plus probable côté Polygon (Forex/CFD) : le
    plan gratuit ne fournit peut-être pas assez de profondeur intraday
    (voir backtest/historical_loader.py::fetch_paginated_intraday_polygon,
    non vérifié empiriquement faute de clé API disponible côté Claude --
    à valider par vous avant de vous fier à un backtest Polygon long).
    """
    expected_min_per_year = 96 * 200  # marge large : jours ouvrés seulement, avec tolérance
    expected_min = years_requested * expected_min_per_year
    if len(raw_m15) < expected_min:
        print(
            f"\n⚠️  Seulement {len(raw_m15)} bougies m15 reçues pour {years_requested} "
            f"an(s) demandé(s) (attendu : au moins ~{expected_min})."
        )
        if source == "polygon":
            print(
                "    Cause probable : profondeur d'historique m15 insuffisante sur votre plan "
                "Polygon (non garantie -- voir le commentaire de fetch_paginated_intraday_polygon). "
                "Envisagez le repli London Strategic Edge si ce backtest doit couvrir une période "
                "plus longue. Le backtest continue quand même sur les données disponibles."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest INTRADAY (m15, vraies règles d'entrée live) du Trading_alert_bot."
    )
    parser.add_argument(
        "--source", choices=["binance", "polygon"], default="binance",
        help="Source des données : 'binance' (crypto) ou 'polygon' (Forex/Indices). Défaut : binance.",
    )
    parser.add_argument(
        "--symbol", default="BTC",
        help="[Binance uniquement] Symbole interne (ex: BTC, ETH, SOL...).",
    )
    parser.add_argument(
        "--ticker", default=None,
        help='[Polygon uniquement] Ticker Polygon exact (ex: "C:EURUSD", "I:SPX").',
    )
    parser.add_argument(
        "--market", choices=["spot", "futures"], default="spot",
        help="[Binance uniquement] 'spot' (défaut) ou 'futures' (contrats à terme USDⓈ-M).",
    )
    parser.add_argument("--years", type=int, default=1, help="Nombre d'années à tester (défaut : 1).")
    parser.add_argument(
        "--margin-years", type=int, default=1,
        help="Années de contexte en plus AVANT le début du test, pour les ancres CRT/DOL (défaut : 1).",
    )
    parser.add_argument(
        "--m15-window-days", type=int, default=DEFAULT_M15_WINDOW_DAYS,
        help=(
            "Profondeur (en jours) de la fenêtre glissante m15 exposée au moteur à chaque pas "
            f"(défaut : {DEFAULT_M15_WINDOW_DAYS}, largement au-dessus du plus grand horizon réel "
            "utilisé par une règle -- voir backtest/intraday_simulator.py)."
        ),
    )
    parser.add_argument(
        "--step-every-n-m15", type=int, default=1,
        help=(
            "Évalue une décision toutes les N bougies m15 au lieu de chacune (défaut : 1 = chaque "
            "bougie, le plus fidèle au live). Augmenter (ex: 4 = toutes les heures) accélère "
            "fortement un premier passage exploratoire, au prix d'une détection légèrement moins "
            "réactive des nouvelles entrées."
        ),
    )
    parser.add_argument(
        "--output", default=None,
        help="Chemin du CSV de détail (défaut : backtest_intraday_<label>_<years>y.csv à la racine).",
    )
    parser.add_argument(
        "--starting-capital", type=float, default=10_000.0,
        help=(
            "Capital simulé de départ pour le backtest RÉALISTE (Phase 3 -- coûts + garde-fous "
            "d'exécution, voir backtest/realistic_backtest.py). Défaut : 10000."
        ),
    )
    parser.add_argument(
        "--fee-pct", type=float, default=None,
        help=(
            "Taux de frais taker (%% du notionnel, PAR JAMBE) pour le backtest réaliste. "
            "Défaut : résolu depuis config/alerts.yaml::execution.exchange (voir "
            "backtest/execution_costs.py::DEFAULT_FEE_RATES_PCT) -- OBLIGATOIRE si cet exchange "
            "est 'ibkr' (commission par contrat, non modélisée par défaut)."
        ),
    )
    parser.add_argument(
        "--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS,
        help=f"Slippage forfaitaire par jambe, en points de base (défaut : {DEFAULT_SLIPPAGE_BPS}).",
    )
    parser.add_argument(
        "--skip-realistic", action="store_true",
        help="N'exécute que le backtest brut (Phase 2, géométrique) -- ignore le backtest réaliste (Phase 3).",
    )
    parser.add_argument(
        "--risk-pct", type=float, default=None,
        help=(
            "Surcharge execution.risk_pct_per_trade (config/alerts.yaml) pour CE backtest "
            "uniquement -- ne touche jamais le fichier de config réel (donc aucun impact sur le "
            "risque appliqué en live). Défaut : valeur de config/alerts.yaml."
        ),
    )
    parser.add_argument(
        "--reserved-balance-pct", type=float, default=None,
        help=(
            "Surcharge execution.reserved_balance_pct (config/alerts.yaml) pour CE backtest "
            "uniquement -- ⚠️ en LIVE, cette réserve protège contre un mouvement de prix "
            "défavorable avant liquidation ET laisse de la marge pour vos AUTRES positions "
            "simultanées (~22 actifs suivis sur le même solde réel) -- ce backtest, lui, simule "
            "chaque actif avec un capital totalement indépendant (voir CLAUDE.md, Phase 3), donc "
            "il ne modélise PAS cette contention entre actifs : mettre 0 ici est sûr pour "
            "EXPLORER l'effet sur le levier calculé, mais n'implique pas que 0 soit sûr à "
            "appliquer tel quel dans config/alerts.yaml (execution réelle multi-actifs). "
            "Défaut : valeur de config/alerts.yaml."
        ),
    )
    parser.add_argument(
        "--max-leverage", type=float, default=None,
        help=(
            "Surcharge execution.exchanges[<exchange>].max_leverage (config/alerts.yaml) pour CE "
            "backtest uniquement. Défaut : valeur de config/alerts.yaml pour l'exchange configuré."
        ),
    )
    parser.add_argument(
        "--min-rr", type=float, default=None,
        help="Surcharge execution.min_risk_reward_ratio (config/alerts.yaml) pour CE backtest uniquement.",
    )
    args = parser.parse_args()

    if args.source == "polygon" and not args.ticker:
        parser.error("--ticker est obligatoire avec --source polygon (ex: --ticker C:EURUSD)")

    label = args.symbol if args.source == "binance" else args.ticker
    if args.source == "binance" and args.market == "futures":
        label = f"{label}-FUTURES"

    print(f"Récupération de {args.years + args.margin_years} ans d'historique m15 pour {label} ({args.source})...")
    print("(peut prendre plusieurs minutes -- voir backtest/historical_loader.py pour la pagination)")
    fetch_start = time.monotonic()
    if args.source == "binance":
        raw_data = fetch_historical_multi_timeframe(
            args.symbol, args.years, args.margin_years, market=args.market, include_m15=True
        )
    else:
        raw_data = fetch_historical_multi_timeframe_polygon(
            args.ticker, args.years, args.margin_years, include_m15=True
        )
    print(f"  Récupération terminée en {time.monotonic() - fetch_start:.0f}s.")

    print(
        f"  Monthly : {len(raw_data['monthly'])} bougies | "
        f"Weekly : {len(raw_data['weekly'])} bougies | "
        f"Daily : {len(raw_data['daily'])} bougies | "
        f"M15 : {len(raw_data['m15'])} bougies"
    )
    _warn_if_insufficient_m15_history(raw_data["m15"], args.years + args.margin_years, args.source)

    print(f"\nSimulation intraday (m15) sur les {args.years} dernière(s) année(s)...")
    decision_config = _load_decision_config()

    sim_start = time.monotonic()
    entries = run_intraday_simulation(
        raw_data["monthly"], raw_data["weekly"], raw_data["daily"], raw_data["m15"],
        label, args.years, decision_config,
        m15_window_days=args.m15_window_days,
        step_every_n_m15=args.step_every_n_m15,
    )
    print(f"  {len(entries)} entrée(s) détectée(s) en {time.monotonic() - sim_start:.0f}s.")

    m15_candles = to_candles(raw_data["m15"])
    outcomes: list[TradeOutcome] = classify_intraday_trades(entries, m15_candles)

    print_summary(outcomes, f"{label} (intraday)")

    tsq_refined_count = sum(1 for e in entries if e.priority_entry_source is not None)
    if entries:
        print(
            f"\n  Info : {tsq_refined_count}/{len(entries)} entrée(s) avaient une entrée précise "
            "(Distribution/Modèle 1-4/TSQ) disponible au moment de la détection -- non utilisée "
            "pour le prix simulé ci-dessus (voir backtest/intraday_simulator.py, entrée au marché "
            "par défaut, comme main.py hors cas TSQ Weekly post-TP1) -- ce raffinement reste hors "
            "périmètre du backtest réaliste ci-dessous (voir CLAUDE.md, section Phase 3)."
        )

    safe_label = label.replace(":", "_")
    output_path = Path(args.output) if args.output else Path(f"backtest_intraday_{safe_label}_{args.years}y.csv")
    export_csv(outcomes, output_path)

    if args.skip_realistic:
        return

    # Phase 3 -- coûts réels (frais + slippage) + garde-fous de position
    # sizing/levier contre un capital simulé évoluant trade après trade
    # (voir backtest/realistic_backtest.py). Base = les MÊMES seuils que
    # le live (config/alerts.yaml::execution) ; --risk-pct/
    # --reserved-balance-pct/--max-leverage/--min-rr permettent de les
    # SURCHARGER pour CE backtest uniquement, sans jamais toucher au
    # fichier de config réel (donc sans aucun impact sur le live).
    execution_cfg = decision_config.get("execution") or {}
    exchange = execution_cfg.get("exchange")
    exchanges_cfg = execution_cfg.get("exchanges") or {}

    risk_pct = args.risk_pct if args.risk_pct is not None else execution_cfg.get("risk_pct_per_trade")
    max_leverage = (
        args.max_leverage if args.max_leverage is not None
        else (exchanges_cfg.get(exchange) or {}).get("max_leverage")
    )
    reserved_balance_pct = (
        args.reserved_balance_pct if args.reserved_balance_pct is not None
        else execution_cfg.get("reserved_balance_pct", 0.0)
    )
    min_rr = args.min_rr if args.min_rr is not None else execution_cfg.get("min_risk_reward_ratio")

    fee_pct = args.fee_pct if args.fee_pct is not None else resolve_default_fee_pct(exchange)

    print(
        f"\nBacktest réaliste (coûts + garde-fous) sur {label}, capital de départ "
        f"{args.starting_capital:.0f}, risk_pct={risk_pct}%, reserved_balance_pct={reserved_balance_pct}%, "
        f"max_leverage={max_leverage}x..."
    )
    if fee_pct is None:
        print(
            f"⚠️  Aucun taux de frais par défaut pour l'exchange configuré ('{exchange}', voir "
            "config/alerts.yaml::execution.exchange) -- IBKR facture par contrat, non modélisé ici. "
            "Backtest réaliste (Phase 3) ignoré -- relancez avec --fee-pct pour le forcer."
        )
        return
    if risk_pct is None or max_leverage is None:
        print(
            "⚠️  execution.risk_pct_per_trade ou execution.exchanges[...].max_leverage manquant dans "
            "config/alerts.yaml -- backtest réaliste (Phase 3) ignoré."
        )
        return

    realistic_outcomes = run_realistic_backtest(
        entries, m15_candles, args.starting_capital, risk_pct, max_leverage, fee_pct, args.slippage_bps,
        reserved_balance_pct, min_rr,
    )
    print_realistic_summary(realistic_outcomes, f"{label} (intraday, réaliste)", args.starting_capital)

    realistic_output_path = Path(f"backtest_realistic_{safe_label}_{args.years}y.csv")
    export_realistic_csv(realistic_outcomes, realistic_output_path)


if __name__ == "__main__":
    main()
