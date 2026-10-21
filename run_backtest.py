"""
run_backtest.py

Point d'entrée du backtest : récupère l'historique, rejoue le pipeline
jour par jour, classe chaque trade détecté, affiche un résumé et
exporte le détail en CSV.

Usage :
    python run_backtest.py --source binance --symbol BTC --years 5
    python run_backtest.py --source polygon --ticker C:EURUSD --years 1

Important pour Polygon (Forex/Indices) : le plan Basic Currencies
(gratuit) limite l'historique disponible à ~2 ans, contre 6+ ans sur
Binance pour la crypto — demander --years 5 sur Forex avec un compte
gratuit ne renverra probablement pas assez de données (un avertissement
s'affiche si le volume reçu est nettement inférieur à ce qui était
attendu pour la période demandée).
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from backtest.historical_loader import (
    fetch_historical_multi_timeframe,
    fetch_historical_multi_timeframe_polygon,
)
from backtest.report import export_csv, print_summary
from backtest.simulator import compute_btc_bias_series, run_simulation, to_candles
from backtest.trade_tracker import classify_trades, TradeOutcome


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


def _warn_if_insufficient_history(raw_daily: list, years_requested: int, source: str) -> None:
    """
    Signale si le volume de bougies daily reçu est nettement inférieur à
    ce qui était attendu pour la période demandée — symptôme le plus
    probable : plan Polygon (Forex) limité en profondeur d'historique
    (~2 ans sur le plan Basic Currencies gratuit).
    """
    expected_min = years_requested * 200  # marge large : jours ouvrés seulement, avec tolerance
    if len(raw_daily) < expected_min:
        print(
            f"\n⚠️  Seulement {len(raw_daily)} bougies daily reçues pour {years_requested} "
            f"an(s) demandé(s) (attendu : au moins ~{expected_min}). "
        )
        if source == "polygon":
            print(
                "    Cause probable : votre plan Polygon ne fournit pas assez d'historique "
                "(le plan Basic Currencies gratuit est limité à ~2 ans pour le Forex). "
                "Le backtest continue quand même sur les données disponibles, mais les "
                "résultats seront moins représentatifs sur une période aussi courte."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest du Trading_alert_bot sur historique Binance ou Polygon.")
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
        help=(
            "[Binance uniquement] 'spot' (défaut) ou 'futures' (contrats à terme "
            "USDⓈ-M, fapi.binance.com). Les prix Spot/Futures peuvent légèrement "
            "diverger (funding, base), et l'historique Futures démarre parfois "
            "plus tard pour un même actif."
        ),
    )
    parser.add_argument("--years", type=int, default=5, help="Nombre d'années à tester (défaut : 5).")
    parser.add_argument(
        "--margin-years", type=int, default=1,
        help="Années de contexte en plus AVANT le début du test, pour les ancres CRT/DOL (défaut : 1).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Chemin du CSV de détail (défaut : backtest_<label>_<years>y.csv à la racine).",
    )
    parser.add_argument(
        "--filter-btc-alignment", action="store_true",
        help=(
            "N'exécute (ne compte) une entrée QUE si son biais correspond à celui de BTC ce "
            "jour-là -- comportement historique inchangé sans ce flag (BTC purement indicatif). "
            "Sans effet si --symbol BTC (ou --source polygon, sans rapport avec BTC)."
        ),
    )
    args = parser.parse_args()

    if args.source == "polygon" and not args.ticker:
        parser.error("--ticker est obligatoire avec --source polygon (ex: --ticker C:EURUSD)")

    label = args.symbol if args.source == "binance" else args.ticker
    if args.source == "binance" and args.market == "futures":
        label = f"{label}-FUTURES"

    print(f"Récupération de {args.years + args.margin_years} ans d'historique pour {label} ({args.source})...")
    if args.source == "binance":
        raw_data = fetch_historical_multi_timeframe(args.symbol, args.years, args.margin_years, market=args.market)
    else:
        raw_data = fetch_historical_multi_timeframe_polygon(args.ticker, args.years, args.margin_years)

    print(
        f"  Monthly : {len(raw_data['monthly'])} bougies | "
        f"Weekly : {len(raw_data['weekly'])} bougies | "
        f"Daily : {len(raw_data['daily'])} bougies"
    )
    _warn_if_insufficient_history(raw_data["daily"], args.years + args.margin_years, args.source)

    print(f"\nSimulation jour par jour sur les {args.years} dernières années...")
    decision_config = _load_decision_config()

    btc_bias_by_date = None
    if args.filter_btc_alignment:
        if args.source != "binance":
            print("⚠️  --filter-btc-alignment ignoré (sans rapport avec BTC pour une source Polygon).")
        elif args.symbol == "BTC":
            print("⚠️  --filter-btc-alignment ignoré (BTC est toujours aligné avec lui-même).")
        else:
            print(f"Récupération de l'historique BTC (pour le filtre d'alignement)...")
            raw_btc_data = fetch_historical_multi_timeframe("BTC", args.years, args.margin_years, market=args.market)
            daily_candles_for_dates = to_candles(raw_data["daily"])
            last_date = daily_candles_for_dates[-1].timestamp.date()
            test_start_date = date(last_date.year - args.years, last_date.month, last_date.day)
            btc_bias_by_date = compute_btc_bias_series(
                raw_btc_data["monthly"], raw_btc_data["weekly"], raw_btc_data["daily"],
                test_start_date, decision_config,
            )
            print(f"  Biais BTC calculé sur {len(btc_bias_by_date)} jour(s) -- filtre d'alignement actif.")

    entries = run_simulation(
        raw_data["monthly"], raw_data["weekly"], raw_data["daily"],
        label, args.years, decision_config,
        btc_bias_by_date=btc_bias_by_date,
    )
    print(f"  {len(entries)} entrée(s) détectée(s) sur la période testée.")

    daily_candles = to_candles(raw_data["daily"])

    outcomes: list[TradeOutcome] = classify_trades(entries, daily_candles)

    print_summary(outcomes, label)

    safe_label = label.replace(":", "_")
    output_path = Path(args.output) if args.output else Path(f"backtest_{safe_label}_{args.years}y.csv")
    export_csv(outcomes, output_path)


if __name__ == "__main__":
    main()

