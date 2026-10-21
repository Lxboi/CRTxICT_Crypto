"""
main.py

Point d'entrée du bot. Orchestre le pipeline complet :

    Binance -> Collecte -> Modèles -> MarketContext -> DecisionEngine
        -> Base de données (historique + déduplication, par paire)
        -> IA (enrichissement du message)
        -> Telegram (envoi de la notification)

Multi-paires : le bot boucle sur toutes les paires configurées (PAIRS)
dans un seul cycle, un seul process, une seule base de données
(distinguée par la colonne `symbol`). Chaque paire reçoit aussi les
bougies de sa paire corrélée (CORRELATED_PAIR) pour la détection de
divergence SMT dans NarrativeRule — les données ne sont collectées
qu'une seule fois par paire et par cycle, puis réutilisées côté SMT
pour éviter les appels redondants.

Deux modes d'exécution :
    python main.py           # boucle planifiée (APScheduler), tourne en continu
    python main.py --once     # un seul cycle, puis sort (utile pour tester,
                               # ou pour un déploiement piloté par cron externe)
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from loguru import logger

# Chemin explicite vers le fichier .env, à côté de main.py — pour le
# renommer, changez uniquement cette ligne (ex: "secrets.env").
ENV_PATH = Path(__file__).parent / ".env"

# Charge .env AVANT les imports des modules qui lisent des variables
# d'environnement au chargement (ex: config/settings.py).
load_dotenv(dotenv_path=ENV_PATH)

import database.db as db
from messaging.formatter import compute_risk_reward_ratio, format_telegram_message
from collectors.binance import (
    get_fine_intraday_klines_simple,
    get_multi_timeframe_klines_simple,
    get_ticker_24hr,
)
from collectors.macro import get_macro_events
from collectors.polygon import get_multi_timeframe_data as get_polygon_multi_timeframe_data
from execution import kill_switch
from execution.client_factory import build_client
from execution.engine import (
    check_and_execute_missing_position,
    check_and_repair_position_protection,
    try_close_position_for_all_users,
    try_execute_entry_for_all_users,
    try_move_stop_to_breakeven_for_all_users,
)
from models.macro_data import MacroEvent
from models.market_data import CandleSeries, MarketData, format_price_adaptive
from notifications.telegram_sender import send_telegram_message
from rules.correlation import build_correlated_pair_map, find_correlated_pairs
from rules.decision_engine import DecisionEngine, DecisionReport
from rules.engine import MarketContext

CONFIG_PATH = Path(__file__).parent / "config" / "alerts.yaml"
LOG_DIR = Path(__file__).parent / "logs"

DEFAULT_POLL_INTERVAL_MINUTES = 15
DEFAULT_KLINES_LIMIT = 1000  # maximum autorisé par requête Binance (voir collectors/binance.py::MAX_KLINES_PER_REQUEST)


_owner_client_cache: Any | None = None


def _build_owner_client(config: dict[str, Any]) -> tuple[Any | None, str | None]:
    """
    Construit VOTRE ExchangeClient Kraken à partir de vos identifiants
    dans .env (KRAKEN_API_KEY/KRAKEN_API_SECRET).

    Simplification architecture (août 2026) : un seul exchange possible
    désormais (voir config/alerts.yaml::execution.exchange, toujours
    "kraken") -- remplace l'ancien _build_owner_clients()/
    _owner_client_for_symbol(), qui géraient potentiellement plusieurs
    exchanges simultanés (crypto + IBKR) routés par symbole. Un seul
    client suffit maintenant pour TOUS les symboles suivis (tous
    crypto). KrakenClient est un client REST sans état de connexion
    persistant (contrairement à l'ancien IBKRClient) -- mis en cache
    une seule fois au niveau du module, sans logique de reconnexion.

    Returns:
        (client, "kraken") si KRAKEN_API_KEY/KRAKEN_API_SECRET sont
        présents dans .env, sinon (None, None) -- l'exécution
        automatique est alors silencieusement ignorée pour tous les
        symboles (comme si rien n'était configuré), la notification
        Telegram continuant de fonctionner normalement.
    """
    global _owner_client_cache

    if _owner_client_cache is not None:
        return _owner_client_cache, "kraken"

    from config.settings import get_settings

    settings = get_settings()
    exchange = (config.get("execution") or {}).get("exchange", "kraken")

    if not (settings.kraken_api_key and settings.kraken_api_secret):
        return None, None

    try:
        client = build_client(exchange, settings.kraken_api_key, settings.kraken_api_secret)
    except ValueError:
        logger.error(f"Exchange '{exchange}' non supporté — exécution ignorée pour vous.")
        return None, None

    _owner_client_cache = client
    return client, exchange


# --- Paires suivies, et appariement pour le SMT ---
# Surchargeable via config/alerts.yaml -> orchestration.assets /
# orchestration.correlated_pairs.
# --- Actifs suivis, par source de données ---
# Surchargeable via config/alerts.yaml -> orchestration.assets. Chaque
# entrée précise la source ("binance" ou "polygon") et le ticker exact
# à utiliser sur cette source. Format des tickers Polygon : voir
# collectors/polygon.py (docstring).
DEFAULT_ASSETS: dict[str, dict[str, str]] = {
    "BTC": {"source": "binance", "ticker": "BTCUSDT"},
    "ETH": {"source": "binance", "ticker": "ETHUSDT"},
    # Exemples Polygon, désactivés par défaut (nécessitent
    # POLYGON_API_KEY) — décommentez ou ajoutez dans
    # config/alerts.yaml -> orchestration.assets pour les activer :
    # "SPY": {"source": "polygon", "ticker": "SPY"},
    # "EURUSD": {"source": "polygon", "ticker": "C:EURUSD"},
}

# Appariement fixe pour le SMT, utilisé comme repli si la recherche de
# corrélation dynamique (rules/correlation.py) ne trouve rien de plus
# fort pour un symbole donné.
DEFAULT_CORRELATED_PAIR: dict[str, str] = {"BTC": "ETH", "ETH": "BTC"}

# Recherche de corrélations dynamique (rules/correlation.py) : activée
# par défaut, désactivable via config/alerts.yaml ->
# orchestration.dynamic_correlation.enabled.
DEFAULT_DYNAMIC_CORRELATION_ENABLED = True
DEFAULT_CORRELATION_THRESHOLD = 0.7


def load_config() -> dict[str, Any]:
    """
    Charge config/alerts.yaml. Si le fichier est absent ou invalide, le
    bot continue avec un dict vide — chaque règle retombe alors sur ses
    propres valeurs par défaut (DEFAULT_* dans chaque module), donc rien
    ne casse, mais c'est signalé clairement dans les logs.
    """
    if not CONFIG_PATH.exists():
        logger.warning(
            f"Fichier de config introuvable ({CONFIG_PATH}) — utilisation des "
            "valeurs par défaut de chaque règle"
        )
        return {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        logger.info(f"Configuration chargée depuis {CONFIG_PATH}")
        return config
    except yaml.YAMLError:
        logger.exception(f"Erreur de parsing YAML dans {CONFIG_PATH} — config ignorée")
        return {}


def _safe_db_call(func, *args) -> None:
    """
    Exécute un appel d'écriture en base sans jamais faire planter le
    cycle en cours : une panne de la DB (disque plein, fichier
    verrouillé, etc.) ne doit pas empêcher la collecte ou la décision de
    se dérouler normalement.
    """
    try:
        func(*args)
    except Exception:
        logger.exception(f"Échec de l'écriture en base ({func.__name__})")


def _collect_economic_events() -> list[MacroEvent] | None:
    """
    Calendrier macro (SoSoValue), collecté UNE SEULE FOIS par cycle
    (partagé entre toutes les paires — ce n'est pas une donnée propre à
    un actif). Absent si SOSOVALUE_API_KEY n'est pas configurée ou si
    l'appel échoue, auquel cas NarrativeRule le signale simplement
    comme indisponible plutôt que de faire échouer tout le cycle.
    """
    try:
        raw_events = get_macro_events()
        events = [MacroEvent.from_collector_dict(e) for e in raw_events]
        logger.debug(f"Calendrier macro : {len(events)} jours récupérés")
        return events
    except ValueError:
        logger.info("SOSOVALUE_API_KEY absente -> calendrier macro non disponible ce cycle")
    except requests.HTTPError:
        logger.exception("Échec de la collecte du calendrier macro (SoSoValue)")
    return None


def _collect_binance_symbol_data(
    symbol: str, ticker: str, config: dict[str, Any]
) -> tuple[MarketData, dict[str, CandleSeries]]:
    """
    Collecte l'instantané et les bougies natives Binance pour un
    symbole donné (ex: "BTC" -> "BTCUSDT").

    Contrairement à CoinGecko, Binance fournit des bougies NATIVES à
    chaque granularité (interval="1M"/"1w"/"1d"/"30m") : plus besoin de
    resampling, contrairement à l'ancien pipeline CoinGecko.

    Raises:
        requests.HTTPError: Si un appel Binance échoue (paire invalide,
            rate limit, etc.) — remonté tel quel, à charge de
            l'appelant de décider comment réagir (voir run_once, qui
            isole chaque actif pour qu'un échec sur l'un n'empêche pas
            les autres).
    """
    orchestration_cfg = config.get("orchestration", {})
    klines_limit = orchestration_cfg.get("klines_limit", DEFAULT_KLINES_LIMIT)
    market = orchestration_cfg.get("market", "spot")  # "spot" ou "futures" (contrats à terme USDⓈ-M)

    logger.info(f"[{symbol}] Collecte de l'instantané de marché (Binance {market})...")
    raw_ticker = get_ticker_24hr(ticker, market=market)
    market_data = MarketData.from_binance_ticker(raw_ticker, symbol=symbol)
    _safe_db_call(db.save_market_snapshot, market_data)

    logger.info(f"[{symbol}] Collecte des bougies natives (Binance {market})...")
    raw_klines = get_multi_timeframe_klines_simple(ticker, limit=klines_limit, market=market)
    for timeframe, raw in raw_klines.items():
        _safe_db_call(db.save_raw_ohlc_snapshot, symbol, timeframe, klines_limit, raw)

    # Série fine (15 min), séparée de "intraday" (1h) : dédiée au
    # resampling précis en CRT 4H et à la résolution fine du "moment de
    # purge" dans rules/purge_rule.py — voir
    # collectors/binance.py::FINE_INTRADAY_INTERVAL pour la justification.
    raw_m15 = get_fine_intraday_klines_simple(ticker, limit=klines_limit, market=market)
    _safe_db_call(db.save_raw_ohlc_snapshot, symbol, "m15", klines_limit, raw_m15)
    raw_klines["m15"] = raw_m15

    candles = {
        timeframe: CandleSeries.from_collector_list(raw) for timeframe, raw in raw_klines.items()
    }

    for timeframe, series in candles.items():
        logger.debug(f"  [{symbol}] {timeframe} : {len(series.candles)} bougies")

    return market_data, candles


def _collect_polygon_symbol_data(
    symbol: str, ticker: str, config: dict[str, Any]
) -> tuple[MarketData, dict[str, CandleSeries]]:
    """
    Collecte l'instantané et les bougies pour un actif Polygon.io (ETF,
    Forex, Futures, actions, indices).

    Contrairement à Binance, Polygon nécessite une clé API
    (POLYGON_API_KEY) et son plan gratuit est limité à 5 requêtes/
    minute — 4 requêtes sont nécessaires par actif et par cycle (une
    par timeframe). Avec plusieurs actifs Polygon suivis en parallèle,
    un rate limit (HTTP 429) devient possible ; il est remonté comme
    n'importe quelle autre requests.HTTPError et isolé par run_once
    (un échec sur un actif Polygon n'empêche pas les autres actifs
    d'être traités ce même cycle).

    Raises:
        ValueError: Si POLYGON_API_KEY est absente de l'environnement.
        requests.HTTPError: Si un appel Polygon échoue.
    """
    orchestration_cfg = config.get("orchestration", {})
    limit = orchestration_cfg.get("klines_limit", DEFAULT_KLINES_LIMIT)

    logger.info(f"[{symbol}] Collecte des bougies (Polygon.io)...")
    candles_simple, raw_daily_bars = get_polygon_multi_timeframe_data(ticker, limit=limit)

    for timeframe, raw in candles_simple.items():
        _safe_db_call(db.save_raw_ohlc_snapshot, symbol, timeframe, limit, raw)

    candles = {
        timeframe: CandleSeries.from_collector_list(raw)
        for timeframe, raw in candles_simple.items()
    }

    for timeframe, series in candles.items():
        logger.debug(f"  [{symbol}] {timeframe} : {len(series.candles)} bougies")

    market_data = MarketData.from_polygon_daily_bars(raw_daily_bars, symbol=symbol)
    _safe_db_call(db.save_market_snapshot, market_data)

    return market_data, candles


def _collect_symbol_data(
    symbol: str, asset_config: dict[str, str], config: dict[str, Any]
) -> tuple[MarketData, dict[str, CandleSeries]]:
    """
    Répartit la collecte vers la bonne source selon `asset_config["source"]`
    ("binance" ou "polygon") — point d'extension pour une future
    troisième source, sans toucher à run_once() ni au reste du pipeline.

    "polygon" reste géré ici même si aucun actif n'est configuré avec
    cette source par défaut depuis la simplification crypto-only (août
    2026, voir config/alerts.yaml) -- le collecteur collectors/polygon.py
    est conservé disponible pour un usage Forex/CFD futur.
    """
    source = asset_config["source"]
    ticker = asset_config["ticker"]

    if source == "binance":
        return _collect_binance_symbol_data(symbol, ticker, config)
    elif source == "polygon":
        return _collect_polygon_symbol_data(symbol, ticker, config)
    else:
        raise ValueError(f"Source de données inconnue pour {symbol} : '{source}'")


def _get_available_tp_levels(alert: dict[str, Any]) -> list[tuple[int, str, float]]:
    """Niveaux de TP disponibles pour cette alerte, dans l'ordre (1, 2, 3)."""
    levels: list[tuple[int, str, float]] = []
    if alert["tp_50"] is not None:
        levels.append((1, "TP1 (50%)", alert["tp_50"]))
    if alert["tp_100"] is not None:
        levels.append((2, "TP2 (100% / DOL)", alert["tp_100"]))
    if alert["tp_htf_dol"] is not None:
        levels.append((3, "TP3 (DOL HTF)", alert["tp_htf_dol"]))
    return levels


def _get_available_tp_levels_from_setup(setup: Any) -> list[tuple[int, str, float]]:
    """
    Équivalent de _get_available_tp_levels(), mais directement depuis un
    TimeframeSetup (avant toute sauvegarde en base) — utilisé par
    dispatch_alert() pour notifier les TP déjà dépassés dès la création.
    """
    targets = setup.crt_targets or {}
    levels: list[tuple[int, str, float]] = []
    if targets.get("tp_50") is not None:
        levels.append((1, "TP1 (50%)", targets["tp_50"]))
    if targets.get("tp_100") is not None:
        levels.append((2, "TP2 (100% / DOL)", targets["tp_100"]))
    if targets.get("tp_htf_dol") is not None:
        levels.append((3, "TP3 (DOL HTF)", targets["tp_htf_dol"]))
    return levels


def _check_awaiting_tsq_entries(
    symbol: str, report: DecisionReport, config: dict[str, Any] | None,
    owner_client: Any | None, owner_exchange: str | None,
) -> None:
    """
    Vérifie, à CHAQUE cycle, si une entrée TSQ précise est maintenant
    disponible pour un setup Weekly dont TP1 était déjà atteint dès la
    détection (voir dispatch_alert() -> db.mark_awaiting_tsq_entry) —
    l'exécution avait été différée plutôt que prise au prix courant.

    Utilise report.priority_entry du cycle COURANT (recalculé à chaque
    appel de DecisionEngine.decide(), donc reflète l'état intraday le
    plus récent) — dès qu'une entrée TSQ apparaît, correspondant à la
    bonne direction, l'exécution se déclenche immédiatement, sans
    attendre un nouveau setup.
    """
    if config is None:
        return

    awaiting_alerts = db.get_alerts_awaiting_tsq_entry(symbol)
    if not awaiting_alerts:
        return

    candidate = report.priority_entry
    if candidate is None or not candidate.source.startswith("TSQ"):
        return  # toujours aucune entrée TSQ disponible ce cycle -- rien à faire

    if _htf_coordination_blocks_execution(symbol, report):
        # Ne consomme PAS l'entrée TSQ disponible ce cycle -- le setup
        # reste "en attente", réévalué au prochain cycle. Le message
        # "Entrée TSQ confirmée" serait trompeur si l'exécution réelle
        # est bloquée : mieux vaut attendre que la coordination HTF
        # se rétablisse plutôt que d'annoncer une confirmation qui n'a
        # jamais eu lieu.
        return

    for alert in awaiting_alerts:
        if candidate.direction != alert["bias"]:
            continue  # entrée TSQ dans le mauvais sens pour ce setup précis

        execution_cfg = config.get("execution") or {}
        risk_pct = execution_cfg.get("risk_pct_per_trade")
        exchange_leverage_config = {
            name: exchange_settings.get("max_leverage")
            for name, exchange_settings in (execution_cfg.get("exchanges") or {}).items()
            if exchange_settings.get("max_leverage") is not None
        }
        if risk_pct is None or not exchange_leverage_config:
            continue

        execution_results = try_execute_entry_for_all_users(
            symbol, candidate.direction, candidate.entry_price, candidate.stop_loss,
            risk_pct, exchange_leverage_config, owner_client, owner_exchange,
            execution_cfg.get("user_risk_overrides"), candidate.take_profits or None,
            execution_cfg.get("reserved_balance_pct", 0.0),
        )
        for user_id, result in execution_results.items():
            if result is not None:
                logger.warning(
                    f"[{symbol}] Exécution (entrée TSQ tardive, {candidate.source}) pour "
                    f"'{user_id}' : {'succès' if result.success else 'échec -> ' + str(result.error_message)}"
                )
                if not result.success:
                    from execution.admin_notifications import notify_execution_failed

                    notify_execution_failed(user_id, symbol, result.error_message or "raison inconnue")

        db.clear_awaiting_tsq_entry(alert["dedup_key"])

        message = (
            f"🟢 *{symbol}* — Entrée TSQ confirmée [{alert['setup_name']}]\n"
            f"Source : {candidate.source}\n"
            f"Prix d'entrée : {format_price_adaptive(candidate.entry_price)}\n"
            f"SL : {format_price_adaptive(candidate.stop_loss)}\n\n"
            "_Ce setup attendait une entrée précise depuis que TP1 était déjà atteint à sa "
            "détection — exécutée maintenant, pas au prix courant d'origine._"
        )
        sent = send_telegram_message(message)
        if not sent:
            logger.warning(f"[{symbol}] [{alert['setup_name']}] Message d'entrée TSQ non envoyé sur Telegram")


def _check_position_protection(
    symbol: str, config: dict[str, Any] | None, owner_client: Any | None, owner_exchange: str | None
) -> None:
    """
    Vérifie, à CHAQUE cycle, que les positions RÉELLEMENT ouvertes sur
    l'exchange (votre compte Kraken) ont bien un stop-loss et
    des take-profits actifs — et répare automatiquement sinon (voir
    execution/engine.py::check_and_repair_position_protection).

    Important si vous ne laissez pas le robot actif en permanence :
    sans cette vérification, une position ouverte sans protection (ex:
    robot arrêté juste après l'entrée, avant que le placement des TP
    n'ait pu se terminer) resterait sans TP/SL jusqu'à ce que vous vous
    en aperceviez manuellement — cette fonction comble ce trou dès que
    le robot revient en ligne, sans attendre un événement particulier.
    """
    if config is None:
        return

    open_alerts = db.get_open_alerts(symbol)
    if not open_alerts:
        return

    # Une seule vérification par symbole suffit : le SL/TP concerne LA
    # POSITION réellement ouverte, pas un setup précis -- on utilise
    # l'alerte la plus récente comme référence pour les niveaux.
    alert = open_alerts[-1]

    repairs = check_and_repair_position_protection(symbol, alert, owner_client, owner_exchange)
    if repairs:
        from execution.admin_notifications import notify_position_protection_repaired

        for user_id, repair_info in repairs.items():
            notify_position_protection_repaired(
                user_id, symbol, repair_info["sl_repaired"], repair_info["tp_repaired"]
            )


def _check_missing_execution(
    symbol: str, report: DecisionReport, config: dict[str, Any] | None, current_price: float,
    owner_client: Any | None, owner_exchange: str | None,
) -> None:
    """
    Exécution de RATTRAPAGE, à CHAQUE cycle -- voir execution/engine.py::
    check_and_execute_missing_position pour le contexte complet (cas
    réel BTC, août 2026 : plusieurs setups notifiés jamais exécutés
    faute d'exécution activée au moment de leur détection —
    dispatch_alert() ne retente JAMAIS l'exécution une fois l'alerte
    déjà enregistrée).

    Sécurité : ne tente JAMAIS de rattraper un setup dont le prix
    ACTUEL a déjà dépassé le stop-loss (position immédiatement
    perdante dès l'ouverture) ou déjà atteint le DERNIER TP disponible
    (plus rien à capturer) -- ces deux cas signifient que le setup est
    en pratique déjà terminé, même si son alerte reste techniquement
    "ouverte" en base jusqu'au prochain passage de _check_tp_sl_hits.
    Voir aussi _htf_coordination_blocks_execution() : le rattrapage
    respecte le MÊME filtre d'incertitude HTF qu'une exécution normale
    -- basé sur le contexte de biais ACTUEL (celui de ce cycle), pas
    celui du moment de la détection d'origine du setup.
    """
    if config is None:
        return

    if _htf_coordination_blocks_execution(symbol, report):
        return

    open_alerts = db.get_open_alerts(symbol)
    if not open_alerts:
        return

    alert = open_alerts[-1]
    bias = alert["bias"]
    stop_loss = alert["stop_loss"]
    entry_price = alert["entry_price"]

    if stop_loss is not None:
        stop_already_crossed = (bias == "bullish" and current_price <= stop_loss) or (
            bias == "bearish" and current_price >= stop_loss
        )
        if stop_already_crossed:
            return

    levels = _get_available_tp_levels(alert)
    if levels:
        _, _, final_tp_price = levels[-1]
        final_tp_already_hit = (bias == "bullish" and current_price >= final_tp_price) or (
            bias == "bearish" and current_price <= final_tp_price
        )
        if final_tp_already_hit:
            return

    if entry_price is None or stop_loss is None:
        return

    execution_cfg = config.get("execution") or {}
    risk_pct = execution_cfg.get("risk_pct_per_trade")
    exchange_leverage_config = {
        name: exchange_settings.get("max_leverage")
        for name, exchange_settings in (execution_cfg.get("exchanges") or {}).items()
        if exchange_settings.get("max_leverage") is not None
    }
    if risk_pct is None or not exchange_leverage_config:
        return

    # R:R minimum -- même garde-fou que dispatch_alert() (voir plus
    # haut dans ce fichier), mais qui en était ABSENT ici (bug réel
    # corrigé, août 2026) : dispatch_alert() ne vérifie le R:R qu'UNE
    # SEULE FOIS, à la toute première détection du setup. Cette
    # fonction-ci retente l'exécution à CHAQUE cycle tant qu'aucune
    # position n'est ouverte (rattrapage) -- sans ce contrôle, un setup
    # dont le R:R était (ou est devenu) défavorable pouvait être
    # exécuté pour de vrai via ce chemin, sans jamais repasser par le
    # filtre. Recalculé avec le prix ACTUEL à chaque cycle, comme pour
    # l'affichage (voir messaging/formatter.py::compute_risk_reward_ratio).
    rr_targets = {
        "stop_loss": stop_loss,
        "tp_50": alert["tp_50"],
        "tp_100": alert["tp_100"],
        "tp_htf_dol": alert["tp_htf_dol"],
    }
    rr_ratio = compute_risk_reward_ratio(current_price, rr_targets)
    min_rr = execution_cfg.get("min_risk_reward_ratio")
    if min_rr is not None and rr_ratio is not None and rr_ratio < min_rr:
        logger.warning(
            f"[{symbol}] [Rattrapage] R:R (1:{rr_ratio:.1f}) en dessous du minimum "
            f"configuré (1:{min_rr:.1f}) -> exécution de rattrapage volontairement IGNORÉE."
        )
        return

    remaining_tp_levels = [
        tp_price for level_num, _, tp_price in levels if level_num > alert["highest_tp_hit"]
    ] or None

    results = check_and_execute_missing_position(
        symbol, bias, entry_price, stop_loss, risk_pct, exchange_leverage_config,
        owner_client, owner_exchange, execution_cfg.get("user_risk_overrides"),
        remaining_tp_levels, execution_cfg.get("reserved_balance_pct", 0.0),
    )
    for user_id, result in results.items():
        if result is not None:
            logger.warning(
                f"[{symbol}] Rattrapage d'exécution pour '{user_id}' : "
                f"{'succès' if result.success else 'échec -> ' + str(result.error_message)}"
            )
            if not result.success:
                from execution.admin_notifications import notify_execution_failed

                notify_execution_failed(user_id, symbol, result.error_message or "raison inconnue")


_NEXT_LOWER_TIMEFRAME: dict[str, str] = {"monthly": "weekly", "weekly": "daily"}


def _htf_coordination_blocks_execution(symbol: str, report: DecisionReport) -> bool:
    """
    Filtre de sélection à l'EXÉCUTION (jamais à la notification, voir
    docstring d'appel) — à la demande explicite de l'utilisateur, août
    2026 : quand la timeframe HTF de référence (celle qui a déterminé
    report.effective_bias — voir report.bias_source_timeframe) est
    FRANCHEMENT contredite par la timeframe immédiatement en dessous,
    "trop d'incertitude" est présente sur le marché pour prendre
    position -- même sur un setup Daily/4H par ailleurs valide.

    Exemples (exactement ceux donnés par l'utilisateur) :
      - HTF = Monthly (bullish), Weekly = bearish -> bloqué, quelle que
        soit la timeframe du setup concerné (Weekly, Daily, 4H...).
      - HTF = Weekly (car Monthly incertain/liquidé ce mois-ci),
        Daily = bearish alors que Weekly est bullish -> bloqué pour
        les setups Daily et en dessous.

    Seule une contradiction FRANCHE bloque (biais opposé) -- une
    timeframe sans biais clair (None, rien détecté dessus) n'invalide
    JAMAIS rien, cohérent avec _check_htf_bias_invalidation : l'absence
    de confirmation n'est pas une objection.

    Limite assumée : ne couvre que Monthly->Weekly et Weekly->Daily
    (les deux cas donnés) -- DolRule ne résout pas de biais "close
    above/below" au niveau 4H (voir rules/dol_rule.py::TIMEFRAME_ORDER,
    qui s'arrête à Daily), donc aucun filtre Daily->4H équivalent ici.
    """
    if report.bias_source_timeframe is None or report.effective_bias is None:
        return False

    next_timeframe = _NEXT_LOWER_TIMEFRAME.get(report.bias_source_timeframe)
    if next_timeframe is None:
        return False

    next_bias = {"weekly": report.weekly_bias, "daily": report.daily_bias}.get(next_timeframe)
    if next_bias is None:
        return False

    blocks = next_bias != report.effective_bias
    if blocks:
        logger.warning(
            f"[{symbol}] Exécution bloquée : {next_timeframe} ({next_bias}) contredit "
            f"la HTF de référence {report.bias_source_timeframe} ({report.effective_bias}) "
            "-- trop d'incertitude, notification maintenue mais aucune exécution tentée."
        )
    return blocks


def _close_position_and_notify_on_failure(
    symbol: str, owner_client: Any | None, owner_exchange: str | None, fraction: float = 1.0
) -> None:
    """
    Remplace les appels directs à try_close_position_for_all_users()
    dans ce fichier -- bug réel corrigé (août 2026) : notre base
    marque l'alerte comme fermée AVANT même de tenter la clôture
    réelle sur l'exchange (nécessaire pour ne pas retenter
    indéfiniment un setup déjà traité) -- si cette clôture réelle
    échoue (ex: panne exchange transitoire), RIEN ne la retentait
    automatiquement, et l'échec ne remontait que dans les logs bruts --
    une position pouvait rester ouverte et NON PROTÉGÉE indéfiniment,
    sans que personne ne s'en aperçoive. Notifie désormais IMMÉDIATEMENT
    en cas d'échec (voir execution/admin_notifications.py::notify_close_failed).
    """
    results = try_close_position_for_all_users(symbol, owner_client, owner_exchange, fraction)
    for user_id, result in results.items():
        if result is not None and not result.success:
            from execution.admin_notifications import notify_close_failed

            notify_close_failed(user_id, symbol, result.error_message or "raison inconnue")


def _move_breakeven_and_notify_on_failure(
    symbol: str, entry_price: float, dedup_key: str, owner_client: Any | None, owner_exchange: str | None,
) -> None:
    """
    Remplace les appels directs à try_move_stop_to_breakeven_for_all_users()
    dans ce fichier -- bug réel corrigé (13 août 2026, panne Kraken 503
    observée en réel sur DOT le 12/08) : un échec de déplacement du SL
    au Breakeven n'était jusqu'ici JAMAIS retenté (une seule tentative,
    au cycle exact où TP1 est franchi) ni notifié (seulement loggé) --
    voir execution/admin_notifications.py::notify_breakeven_failed pour
    le détail. Marque désormais l'alerte pour retentative automatique
    à chaque cycle (voir _check_pending_breakeven ci-dessous) en plus
    de la notification immédiate.
    """
    results = try_move_stop_to_breakeven_for_all_users(symbol, entry_price, owner_client, owner_exchange)
    for user_id, result in results.items():
        if result is not None and not result.success:
            from execution.admin_notifications import notify_breakeven_failed

            db.mark_breakeven_pending(dedup_key)
            notify_breakeven_failed(user_id, symbol, result.error_message or "raison inconnue")


def _check_pending_breakeven(
    symbol: str, config: dict[str, Any] | None, owner_client: Any | None, owner_exchange: str | None,
) -> None:
    """
    Retentative de RATTRAPAGE, à CHAQUE cycle, pour tout déplacement de
    SL au Breakeven resté en échec (voir _move_breakeven_and_notify_on_failure
    et database/db.py::mark_breakeven_pending) -- même principe que
    _check_missing_execution() pour l'exécution d'entrée, appliqué ici
    au déplacement du SL. Sans ce rattrapage, un échec transitoire
    (panne exchange) laissait le SL au niveau initial indéfiniment,
    jusqu'à ce que TP2 soit atteint (voir _check_tp_sl_hits) -- ce qui
    peut prendre longtemps ou ne jamais arriver.
    """
    if config is None:
        return

    for alert in db.get_alerts_awaiting_breakeven(symbol):
        entry_price = alert["entry_price"]
        if entry_price is None:
            db.clear_breakeven_pending(alert["dedup_key"])
            continue

        results = try_move_stop_to_breakeven_for_all_users(symbol, entry_price, owner_client, owner_exchange)
        for user_id, result in results.items():
            if result is None:
                continue
            if result.success:
                db.clear_breakeven_pending(alert["dedup_key"])
                from execution.admin_notifications import notify_breakeven_repaired

                notify_breakeven_repaired(user_id, symbol)
                logger.warning(f"[{symbol}] SL au Breakeven confirmé pour '{user_id}' (après une précédente tentative échouée).")
            else:
                from execution.admin_notifications import notify_breakeven_failed

                notify_breakeven_failed(user_id, symbol, result.error_message or "raison inconnue")


def _check_htf_bias_invalidation(
    symbol: str, report: DecisionReport, config: dict[str, Any] | None = None,
    owner_client: Any | None = None, owner_exchange: str | None = None,
) -> None:
    """
    Invalide immédiatement (clôture réelle + notification) toute alerte
    encore ouverte dont le biais est désormais CONTREDIT par le biais
    d'une timeframe SUPÉRIEURE à la sienne, fraîchement recalculé ce
    cycle (report.monthly_bias / weekly_bias / daily_bias) — cas réel
    corrigé (BTC, août 2026, à la demande explicite de l'utilisateur) :
    un setup Weekly bearish resté ouvert depuis plusieurs semaines,
    alors que le biais Monthly a depuis basculé bullish (CRT valide
    plus récent, voir rules/decision_engine.py::_resolve_timeframe_bias)
    continuait d'être suivi comme si de rien n'était.

    Contrairement à _check_tp_sl_hits (qui clôture sur un événement de
    PRIX), cette vérification clôture sur un événement de CONTEXTE : le
    setup n'a jamais été "touché" par le marché, mais sa justification
    d'origine (le biais HTF qui le rendait "aligné") n'existe plus.

    Hiérarchie utilisée (monthly > weekly > daily > 4h) : un setup
    Weekly n'est vérifié que contre monthly_bias ; un setup Daily contre
    monthly_bias ET weekly_bias ; un setup 4h contre les trois. Un setup
    Monthly n'a par définition aucune timeframe supérieure -- jamais
    invalidé par ce mécanisme. Un biais HTF valant None (rien de clair
    à cette timeframe ce cycle) n'invalide jamais rien -- seule une
    contradiction FRANCHE (biais opposé, pas juste absent) compte.
    """
    htf_bias_by_timeframe: dict[str, list[str | None]] = {
        "weekly": [report.monthly_bias],
        "daily": [report.monthly_bias, report.weekly_bias],
        "4h": [report.monthly_bias, report.weekly_bias, report.daily_bias],
    }

    for alert in db.get_open_alerts(symbol=symbol):
        htf_timeframe = alert["htf_timeframe"]
        alert_bias = alert["bias"]
        setup_label = alert["setup_name"] or htf_timeframe

        contradicting_biases = htf_bias_by_timeframe.get(htf_timeframe, [])
        if not any(b is not None and b != alert_bias for b in contradicting_biases):
            continue

        reason = (
            f"Biais HTF désormais contraire à ce setup {htf_timeframe} "
            f"({alert_bias}) -- réévalué ce cycle"
        )
        logger.warning(f"[{symbol}] {reason} — clôture de l'alerte {alert['dedup_key']}")
        message = (
            f"⚠️ *{symbol}* — Setup invalidé [{setup_label}]\n"
            f"{reason}\n\n"
            "_Le biais d'une timeframe supérieure a changé depuis la détection de ce setup "
            "et le contredit désormais -- clôturé par précaution, sans attendre un SL._"
        )
        sent = send_telegram_message(message)
        if not sent:
            logger.warning(f"[{symbol}] Message d'invalidation (biais HTF) non envoyé sur Telegram")
        db.close_alert(alert["id"], "closed_htf_bias_invalidated", reason)
        if config is not None:
            _close_position_and_notify_on_failure(symbol, owner_client, owner_exchange)


def _check_tp_sl_hits(
    symbol: str, current_price: float, config: dict[str, Any] | None = None,
    owner_client: Any | None = None, owner_exchange: str | None = None,
) -> None:
    """
    Vérifie, pour chaque alerte encore ouverte sur cet actif, si le
    prix actuel a touché un take-profit ou le stop-loss depuis le
    dernier cycle.

    Conformément à la méthodologie : un TP intermédiaire touché (ex:
    TP1) ne clôture PAS le setup — il reste actif, le robot continue à
    le suivre vers le(s) prochain(s) objectif(s) (highest_tp_hit est
    juste mis à jour, voir database/db.py::update_alert_tp_progress).

    Mécanique de break-even (comme en trading réel) : dès que TP1 est
    touché, le SL "effectif" surveillé n'est plus le SL initial mais le
    PRIX D'ENTRÉE (entry_price) — le robot notifie explicitement cette
    consigne ("déplacez votre SL au PE"), puis, si le prix revient
    ensuite à ce niveau avant TP2/TP3, notifie "Breakeven" (position
    fermée à ~0, ni gain ni perte) plutôt qu'un SL classique. Le SL
    initial, lui, ne peut plus jamais être retouché après TP1 (le prix
    est allé au-delà, donc plus loin que ce niveau) — ce n'est donc pas
    une perte de protection.

    Seuls le SL effectif (initial, ou déplacé au PE après TP1) ou le
    DERNIER TP disponible pour ce setup clôturent réellement l'alerte
    et "réinitialisent" le suivi.

    Si le prix a franchi plusieurs niveaux en un seul cycle (mouvement
    violent), seul le niveau le plus élevé atteint est rapporté — les
    niveaux intermédiaires sont considérés implicitement touchés
    (un niveau plus éloigné n'est atteignable qu'en ayant traversé les
    niveaux plus proches).
    """
    open_alerts = db.get_open_alerts(symbol=symbol)

    for alert in open_alerts:
        bias = alert["bias"]
        stop_loss = alert["stop_loss"]
        entry_price = alert["entry_price"]
        highest_tp_hit = alert["highest_tp_hit"]
        setup_label = alert["setup_name"] or alert["htf_timeframe"]

        # Après TP1, le SL "effectif" à surveiller devient le prix
        # d'entrée (break-even), pas le SL initial — sans entry_price
        # connu (alertes créées avant l'ajout de ce champ), on retombe
        # sur le SL initial par sécurité plutôt que de ne rien surveiller.
        at_breakeven = highest_tp_hit >= 1 and entry_price is not None
        effective_stop = entry_price if at_breakeven else stop_loss

        stop_hit = effective_stop is not None and (
            (bias == "bullish" and current_price <= effective_stop)
            or (bias == "bearish" and current_price >= effective_stop)
        )
        if stop_hit:
            if at_breakeven:
                reason = (
                    f"Retour au prix d'entrée (Breakeven) à {format_price_adaptive(current_price)} "
                    f"(niveau : {format_price_adaptive(effective_stop)})"
                )
                logger.warning(
                    f"[{symbol}] {reason} — clôture Breakeven de l'alerte {alert['dedup_key']}"
                )
                message = (
                    f"🟡 *{symbol}* — Position clôturée en Breakeven [{setup_label}]\n"
                    f"{reason}\n\n"
                    "_TP1 avait déjà été atteint, le SL avait été déplacé au prix d'entrée — "
                    "position neutre (ni gain ni perte). Le suivi de ce CRT est réinitialisé._"
                )
                sent = send_telegram_message(message)
                if not sent:
                    logger.warning(f"[{symbol}] Message de clôture (Breakeven) non envoyé sur Telegram")
                db.close_alert(alert["id"], "closed_breakeven", reason)
                if config is not None:
                    _close_position_and_notify_on_failure(symbol, owner_client, owner_exchange)
            else:
                reason = (
                    f"SL touché à {format_price_adaptive(current_price)} "
                    f"(niveau : {format_price_adaptive(effective_stop)})"
                )
                logger.warning(f"[{symbol}] {reason} — clôture de l'alerte {alert['dedup_key']}")
                message = (
                    f"🔴 *{symbol}* — Position clôturée [{setup_label}]\n"
                    f"{reason}\n\n"
                    "_Le suivi de ce CRT est réinitialisé — un nouveau setup pourra être "
                    "notifié normalement._"
                )
                sent = send_telegram_message(message)
                if not sent:
                    logger.warning(f"[{symbol}] Message de clôture (SL) non envoyé sur Telegram")
                db.close_alert(alert["id"], "closed_sl", reason)
                if config is not None:
                    _close_position_and_notify_on_failure(symbol, owner_client, owner_exchange)
            continue

        levels = _get_available_tp_levels(alert)
        newly_reached: tuple[int, str, float] | None = None
        for level_num, label, tp_price in levels:
            if level_num <= highest_tp_hit:
                continue
            tp_hit = (bias == "bullish" and current_price >= tp_price) or (
                bias == "bearish" and current_price <= tp_price
            )
            if tp_hit:
                newly_reached = (level_num, label, tp_price)  # garde le plus eleve atteint

        if newly_reached is None:
            continue

        level_num, label, tp_price = newly_reached
        reason = (
            f"{label} touché à {format_price_adaptive(current_price)} "
            f"(niveau : {format_price_adaptive(tp_price)})"
        )
        is_final_level = level_num == levels[-1][0]

        if is_final_level:
            logger.warning(
                f"[{symbol}] {reason} (dernier objectif) — clôture de l'alerte {alert['dedup_key']}"
            )
            message = (
                f"🟢 *{symbol}* — Position clôturée [{setup_label}]\n"
                f"{reason}\n\n"
                "_Dernier objectif atteint — le suivi de ce CRT est réinitialisé, un "
                "nouveau setup pourra être notifié normalement._"
            )
            sent = send_telegram_message(message)
            if not sent:
                logger.warning(f"[{symbol}] Message de clôture (TP final) non envoyé sur Telegram")
            db.close_alert(alert["id"], "closed_tp", reason)
            if config is not None:
                _close_position_and_notify_on_failure(symbol, owner_client, owner_exchange)
        else:
            next_level = next((l for l in levels if l[0] > level_num), None)
            next_label = f" — prochain objectif : {next_level[1]}" if next_level else ""

            # TP1 spécifiquement déclenche la consigne de déplacement du
            # SL au prix d'entrée (break-even) — ajoutée à la MÊME
            # notification plutôt qu'un message séparé, pour éviter le
            # bruit de deux messages simultanés.
            breakeven_instruction = ""
            if level_num == 1 and entry_price is not None:
                breakeven_instruction = (
                    f"\n\n⚠️ *Déplacez votre SL au prix d'entrée (Breakeven)* : "
                    f"{format_price_adaptive(entry_price)}"
                )

            logger.warning(f"[{symbol}] {reason} — setup [{setup_label}] toujours actif{next_label}")
            message = (
                f"🟢 *{symbol}* — {label} touché [{setup_label}]\n"
                f"{reason}\n\n"
                f"_Setup toujours actif — SL et objectifs suivants toujours surveillés"
                f"{next_label}._"
                f"{breakeven_instruction}"
            )
            sent = send_telegram_message(message)
            if not sent:
                logger.warning(f"[{symbol}] Message de progression TP non envoyé sur Telegram")
            db.update_alert_tp_progress(alert["id"], level_num)

            if config is not None:
                # PAS de clôture partielle déclenchée par le robot ici
                # -- l'ordre TP RÉEL déjà placé sur l'exchange à
                # l'entrée (voir ExchangeClient.place_market_order::
                # take_profit_levels) s'en charge lui-même, sans
                # dépendre du cycle du robot. Appeler
                # try_close_position_for_all_users ici en plus
                # risquerait une DOUBLE réduction de la position (une
                # fois par l'exchange, une fois par le robot).
                #
                # Le robot redimensionne quand même le stop au prix
                # d'entrée pour la position RESTANTE -- après TOUTE
                # progression de TP, pas seulement à TP1 : la taille du
                # stop doit toujours correspondre à ce qui reste
                # réellement ouvert une fois l'ordre TP exécuté (voir
                # execution/*_client.py::move_stop_loss, qui relit la
                # position fraîchement).
                if entry_price is not None:
                    _move_breakeven_and_notify_on_failure(
                        symbol, entry_price, alert["dedup_key"], owner_client, owner_exchange
                    )


def _check_entry_signals(symbol: str, report: DecisionReport) -> None:
    """
    Vérifie, pour chaque setup ouvert sur cet actif dont le biais
    correspond au biais effectif de ce cycle, si une entrée précise
    prioritaire (report.priority_entry — voir
    rules/refined_entry.py::select_priority_entry) a été trouvée et pas
    encore notifiée pour ce setup. Si oui : notification dédiée, puis
    marquage définitif (db.mark_entry_notified) pour ne plus jamais la
    renotifier — conforme à "pas besoin de renotifier à chaque fois,
    le but va être que le robot me notifie la première entrée qu'il
    voit dans l'ordre [modèle 1-2-3, TS, TSQ]".

    Appelée séparément de dispatch_alert() : une entrée précise peut
    apparaître plusieurs cycles APRÈS la notification initiale du
    setup (qui, elle, ne se déclenche qu'une fois à la création du
    setup) — sans ce suivi dédié, une entrée tardive ne serait jamais
    notifiée.
    """
    if report.priority_entry is None:
        return

    open_alerts = db.get_open_alerts(symbol=symbol)
    for alert in open_alerts:
        if alert["entry_notified"] or alert["bias"] != report.effective_bias:
            continue

        entry = report.priority_entry
        setup_label = alert["setup_name"] or alert["htf_timeframe"]
        tp_label = (
            " / ".join(format_price_adaptive(tp) for tp in entry.take_profits)
            if entry.take_profits
            else None
        )

        message_lines = [
            f"🎯 *{symbol}* — Entrée précise trouvée [{setup_label}]",
            f"{entry.source} @ {format_price_adaptive(entry.entry_price)}",
            f"SL : {format_price_adaptive(entry.stop_loss)}",
        ]
        if tp_label:
            message_lines.append(f"TP : {tp_label}")
        message = "\n".join(message_lines)

        logger.warning(
            f"[{symbol}] Entrée précise ({entry.source}) trouvée pour [{setup_label}] "
            "— notification dédiée"
        )
        sent = send_telegram_message(message)
        if not sent:
            logger.warning(f"[{symbol}] Message d'entrée précise non envoyé sur Telegram")
        db.mark_entry_notified(alert["id"], entry.source)


def dispatch_alert(
    report: DecisionReport,
    decision_id: int,
    current_price: float,
    symbol: str,
    btc_bias_context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    owner_client: Any | None = None,
    owner_exchange: str | None = None,
) -> None:
    """
    Sauvegarde chaque setup recommandant une entrée (avec déduplication
    par symbole via database/db.py), et ne notifie réellement que les
    setups réellement nouveaux : un setup déjà alerté à un cycle
    précédent et toujours valide n'est pas renotifié.

    Un message Telegram SÉPARÉ est envoyé par setup nouvellement
    déclenché : si Weekly->Daily ET Daily->Intraday se déclenchent au
    même cycle sur le même actif, ce sont deux opportunités distinctes
    (TPs différents), donc deux messages — les regrouper en un seul
    deviendrait vite ambigu, surtout avec plusieurs actifs suivis en
    parallèle.

    Le message est construit uniquement à partir de l'analyse
    déterministe (DOL/CRT/Narrative/Purge/KL) et du contexte factuel
    (calendrier économique, jours fériés) — aucune IA n'intervient.

    btc_bias_context : biais effectif de BTC calculé plus tôt dans le
    même cycle (voir run_once()) — affiché en référence informative
    dans la notification de TOUT autre actif que BTC, pour aider à
    éviter d'aller à contre-courant de la tendance HTF dominante du
    marché crypto. Ignoré si symbol == "BTC" (pas de référence à
    soi-même) ou si None (BTC pas suivi, ou son biais n'a pas encore
    été déterminé ce cycle).

    Si Telegram échoue, le message reste visible dans les logs — le
    cycle ne plante jamais pour autant, et un échec d'envoi sur un
    setup n'empêche pas les autres d'être tentés.
    """
    new_setups: list[tuple[Any, str, int]] = []
    for setup in report.timeframe_setups:
        if not setup.entry_recommended:
            continue
        result = db.save_alert_if_new(
            decision_id, setup, report.confidence, symbol=symbol, current_price=current_price
        )
        if result is not None:
            dedup_key, initial_highest_tp_hit = result
            new_setups.append((setup, dedup_key, initial_highest_tp_hit))
            logger.warning(f"[{symbol}] Nouveau setup [{setup.setup_name}] : à notifier")
        else:
            logger.info(
                f"[{symbol}] Setup [{setup.setup_name}] déjà alerté précédemment "
                "-> pas de renotification"
            )

    if not new_setups:
        return

    for setup, dedup_key, initial_highest_tp_hit in new_setups:
        final_message = format_telegram_message(
            report, current_price, symbol=symbol, setups=[setup],
            btc_bias_context=btc_bias_context if symbol != "BTC" else None,
        )

        logger.warning("=" * 60)
        logger.warning(f"ALERTE DÉCLENCHÉE — {symbol} [{setup.setup_name}] — message final")
        logger.warning("=" * 60)
        logger.warning(final_message)
        logger.warning("-" * 60)

        sent = send_telegram_message(final_message)
        if sent:
            db.mark_alert_sent(dedup_key)
        else:
            logger.warning(
                f"[{symbol}] [{setup.setup_name}] Message non envoyé sur Telegram (identifiants "
                "absents ou erreur) -> resté visible ci-dessus dans les logs"
            )

        # Calculé AVANT toute exécution : un setup dont le dernier
        # objectif est déjà dépassé au moment même de la détection n'a
        # plus aucune opportunité réelle -- l'ouvrir puis le refermer
        # dans la foulée ne coûterait que des frais/du slippage pour
        # rien, en plus du risque (même bref) d'exposer du capital réel.
        levels = _get_available_tp_levels_from_setup(setup)
        is_final_level_already_hit = bool(levels) and initial_highest_tp_hit == levels[-1][0]

        # R:R trop faible (ex: 1:0.2) : le setup reste notifié (déjà
        # fait ci-dessus), mais l'exécution automatique est bloquée --
        # risquer du capital réel pour un ratio défavorable n'a pas de
        # sens, même si le setup est par ailleurs valide.
        rr_ratio = compute_risk_reward_ratio(current_price, setup.crt_targets) if setup.crt_targets else None
        min_rr = (config.get("execution") or {}).get("min_risk_reward_ratio") if config is not None else None
        is_rr_too_low = min_rr is not None and rr_ratio is not None and rr_ratio < min_rr

        # TP1 déjà atteint dès la détection sur un setup WEEKLY (pas
        # final -- une vraie opportunité reste vers le(s) objectif(s)
        # suivant(s)) : plutôt qu'exécuter immédiatement au prix
        # courant, on attend qu'une entrée TSQ précise (CISD/OTE/
        # Continuation/KOD) confirme -- voir DecisionReport.priority_entry.
        # Si aucune n'est encore disponible CE cycle, l'exécution est
        # différée (marquée en base) et retentée aux cycles suivants
        # par _check_awaiting_tsq_entries(), tant que le setup reste
        # ouvert -- jamais une entrée immédiate "à l'aveugle".
        needs_tsq_entry = (
            setup.timeframe == "weekly" and initial_highest_tp_hit == 1 and not is_final_level_already_hit
        )
        tsq_entry = None
        if needs_tsq_entry:
            candidate = report.priority_entry
            if (
                candidate is not None and candidate.source.startswith("TSQ")
                and candidate.direction == setup.htf_crt.get("bias")
            ):
                tsq_entry = candidate

        execution_entry_price = tsq_entry.entry_price if tsq_entry is not None else current_price
        execution_stop_loss = (
            tsq_entry.stop_loss if tsq_entry is not None
            else (setup.crt_targets.get("stop_loss") if setup.crt_targets else None)
        )

        # Exécution automatique — n'agit QUE pour les utilisateurs
        # (vous inclus) dont le kill-switch est activé (voir
        # execution/kill_switch.py) ; sans quoi, ne fait rien de plus
        # que la notification déjà envoyée ci-dessus. Ne s'exécute PAS
        # si le dernier objectif est déjà atteint dès la détection, ni
        # si le R:R est en dessous du minimum configuré, ni si une
        # entrée TSQ est attendue mais pas encore disponible (voir
        # commentaires ci-dessus).
        if (
            config is not None and setup.crt_targets and setup.htf_crt
            and not is_final_level_already_hit and not is_rr_too_low
            and not (needs_tsq_entry and tsq_entry is None)
            and not _htf_coordination_blocks_execution(symbol, report)
        ):
            execution_cfg = config.get("execution") or {}
            risk_pct = execution_cfg.get("risk_pct_per_trade")
            # Un plafond de levier PAR EXCHANGE (voir config/alerts.yaml::
            # execution.exchanges) -- chaque utilisateur peut être sur un
            # exchange différent, avec des limites réelles très différentes.
            exchange_leverage_config = {
                name: exchange_settings.get("max_leverage")
                for name, exchange_settings in (execution_cfg.get("exchanges") or {}).items()
                if exchange_settings.get("max_leverage") is not None
            }
            if risk_pct is not None and exchange_leverage_config and execution_stop_loss is not None:
                # TP à placer réellement sur l'exchange à l'entrée (voir
                # ExchangeClient.place_market_order::take_profit_levels)
                # -- ceux de l'entrée TSQ si c'est elle qui a été
                # utilisée, sinon ceux du setup (tp_50/tp_100/tp_htf_dol,
                # dans l'ordre croissant de proximité).
                if tsq_entry is not None:
                    tp_levels_for_execution = tsq_entry.take_profits or None
                else:
                    tp_levels_for_execution = [
                        v for v in (
                            setup.crt_targets.get("tp_50"),
                            setup.crt_targets.get("tp_100"),
                            setup.crt_targets.get("tp_htf_dol"),
                        ) if v is not None
                    ] or None
                execution_results = try_execute_entry_for_all_users(
                    symbol, setup.htf_crt["bias"], execution_entry_price, execution_stop_loss,
                    risk_pct, exchange_leverage_config, owner_client, owner_exchange,
                    execution_cfg.get("user_risk_overrides"), tp_levels_for_execution,
                    execution_cfg.get("reserved_balance_pct", 0.0),
                )
                for user_id, result in execution_results.items():
                    if result is not None:
                        logger.warning(
                            f"[{symbol}] Exécution pour '{user_id}' : "
                            f"{'succès' if result.success else 'échec -> ' + str(result.error_message)}"
                        )
                        if not result.success:
                            from execution.admin_notifications import notify_execution_failed

                            notify_execution_failed(user_id, symbol, result.error_message or "raison inconnue")
                if tsq_entry is not None:
                    logger.warning(
                        f"[{symbol}] [{setup.setup_name}] Exécuté via entrée TSQ ({tsq_entry.source}) "
                        f"à {execution_entry_price}, pas au prix courant."
                    )
        elif is_final_level_already_hit and config is not None:
            logger.warning(
                f"[{symbol}] [{setup.setup_name}] Dernier objectif déjà atteint dès la détection "
                "-> exécution automatique volontairement IGNORÉE (aucune position ouverte)."
            )
        elif is_rr_too_low and config is not None:
            logger.warning(
                f"[{symbol}] [{setup.setup_name}] R:R (1:{rr_ratio:.1f}) en dessous du minimum "
                f"configuré (1:{min_rr:.1f}) -> exécution automatique volontairement IGNORÉE "
                "(notification envoyée normalement)."
            )
        elif needs_tsq_entry and tsq_entry is None and config is not None:
            db.mark_awaiting_tsq_entry(dedup_key)
            logger.warning(
                f"[{symbol}] [{setup.setup_name}] TP1 déjà atteint dès la détection (Weekly) -- "
                "en attente d'une entrée TSQ précise avant d'exécuter (vérifié à chaque cycle)."
            )



        # Un ou plusieurs TP peuvent déjà être dépassés dès la création
        # du setup (ex: actif peu volatil en valeur absolue où TP1 est
        # franchi rapidement) — le message initial ci-dessus les
        # mentionne déjà en passant ("(déjà atteint)"), mais sans
        # notification dédiée par palier, l'utilisateur ne le
        # remarquerait jamais explicitement. On notifie donc chaque
        # palier déjà atteint, dans l'ordre, comme le ferait
        # _check_tp_sl_hits() pour un franchissement détecté en cours
        # de route.
        if initial_highest_tp_hit > 0:
            for level_num, label, tp_price in levels:
                if level_num > initial_highest_tp_hit:
                    break

                if level_num == initial_highest_tp_hit and is_final_level_already_hit:
                    # Dernier objectif disponible déjà atteint dès la
                    # création : clôture immédiate (comme le ferait
                    # _check_tp_sl_hits au prochain cycle), sinon ce
                    # setup resterait "ouvert" indéfiniment sans jamais
                    # être détecté comme complété (highest_tp_hit
                    # démarre déjà au maximum, donc plus aucun NOUVEAU
                    # franchissement ne serait jamais détecté).
                    reason = (
                        f"{label} déjà atteint dès la détection à "
                        f"{format_price_adaptive(current_price)} "
                        f"(niveau : {format_price_adaptive(tp_price)})"
                    )
                    # Filet de sécurité : l'exécution est censée avoir
                    # été IGNORÉE plus haut pour ce cas précis (voir
                    # is_final_level_already_hit ci-dessus), donc aucune
                    # position réelle ne devrait exister -- mais on
                    # tente quand même la fermeture par prudence, au cas
                    # où un futur changement de code réintroduirait ce
                    # risque sans qu'on s'en rende compte ici.
                    if config is not None:
                        _close_position_and_notify_on_failure(symbol, owner_client, owner_exchange)
                    progress_message = (
                        f"🟢 *{symbol}* — Position clôturée [{setup.setup_name}]\n"
                        f"{reason}\n\n"
                        "_Dernier objectif déjà atteint dès la détection — le suivi de ce CRT "
                        "est réinitialisé, un nouveau setup pourra être notifié normalement._"
                    )
                    progress_sent = send_telegram_message(progress_message)
                    if not progress_sent:
                        logger.warning(
                            f"[{symbol}] [{setup.setup_name}] Message de clôture immédiate "
                            "non envoyé sur Telegram"
                        )
                    db.close_alert_by_dedup_key(dedup_key, "closed_tp", reason)
                    break

                breakeven_instruction = ""
                if level_num == 1:
                    breakeven_instruction = (
                        f"\n\n⚠️ *Déplacez votre SL au prix d'entrée (Breakeven)* : "
                        f"{format_price_adaptive(current_price)}"
                    )

                progress_message = (
                    f"🟢 *{symbol}* — {label} déjà atteint à la détection [{setup.setup_name}]\n"
                    f"Niveau : {label} à {format_price_adaptive(tp_price)} (prix au moment de la "
                    f"détection : {format_price_adaptive(current_price)})\n\n"
                    "_Setup toujours actif — SL et objectifs suivants toujours surveillés._"
                    f"{breakeven_instruction}"
                )
                progress_sent = send_telegram_message(progress_message)
                if not progress_sent:
                    logger.warning(
                        f"[{symbol}] [{setup.setup_name}] Message de {label} déjà atteint "
                        "non envoyé sur Telegram"
                    )


def run_once(config: dict[str, Any]) -> dict[str, DecisionReport | None]:
    """
    Exécute un cycle complet sur TOUS les actifs configurés (toutes
    sources confondues) : collecte -> construction du contexte (avec
    SMT croisé) -> décision -> dispatch de l'alerte si recommandée.

    Chaque actif est isolé : un échec de collecte sur l'un (rate limit,
    ticker invalide, clé API manquante pour Polygon) n'empêche pas les
    autres d'être traités normalement ce même cycle.

    L'appariement SMT (quelle paire corrélée comparer à chaque symbole)
    est déterminé en 2 temps : recherche dynamique via
    rules/correlation.py (si activée, DEFAULT_DYNAMIC_CORRELATION_ENABLED),
    complétée par DEFAULT_CORRELATED_PAIR pour les symboles non couverts
    par la recherche dynamique (pas assez d'historique, aucune
    corrélation au-dessus du seuil, etc.).

    Returns:
        Dict {symbole: DecisionReport | None} — None pour les actifs
        dont la collecte ou la décision a échoué ce cycle.
    """
    orchestration_cfg = config.get("orchestration", {})
    assets: dict[str, dict[str, str]] = orchestration_cfg.get("assets", DEFAULT_ASSETS)
    static_correlated_pair: dict[str, str] = orchestration_cfg.get(
        "correlated_pairs", DEFAULT_CORRELATED_PAIR
    )
    dynamic_correlation_enabled = orchestration_cfg.get(
        "dynamic_correlation_enabled", DEFAULT_DYNAMIC_CORRELATION_ENABLED
    )
    correlation_threshold = orchestration_cfg.get(
        "correlation_threshold", DEFAULT_CORRELATION_THRESHOLD
    )

    # Contexte partagé, collecté une seule fois pour tout le cycle.
    economic_events = _collect_economic_events()

    # Collecte des données de marché pour CHAQUE actif, une seule fois
    # (réutilisées ensuite comme "secondary_candles" pour la paire
    # corrélée, sans refaire d'appel réseau).
    per_symbol_data: dict[str, tuple[MarketData, dict[str, CandleSeries]]] = {}
    for symbol, asset_config in assets.items():
        try:
            per_symbol_data[symbol] = _collect_symbol_data(symbol, asset_config, config)
        except Exception:
            logger.exception(f"[{symbol}] Échec de la collecte ({asset_config.get('source')})")

    # Recherche dynamique de corrélations parmi les actifs collectés
    # avec succès ce cycle -> complète (sans l'écraser) l'appariement
    # fixe DEFAULT_CORRELATED_PAIR.
    correlated_pair_map: dict[str, str] = dict(static_correlated_pair)
    if dynamic_correlation_enabled and len(per_symbol_data) >= 2:
        candles_by_symbol = {symbol: candles for symbol, (_, candles) in per_symbol_data.items()}
        correlation_results = find_correlated_pairs(
            candles_by_symbol, threshold=correlation_threshold
        )
        if correlation_results:
            logger.info(
                f"Corrélations détectées ce cycle : "
                + ", ".join(
                    f"{r.symbol_a}<->{r.symbol_b} (r={r.correlation:.2f})"
                    for r in correlation_results
                )
            )
        dynamic_map = build_correlated_pair_map(correlation_results)
        # Le dynamique complète le statique mais ne l'écrase pas : un
        # appariement délibérément configuré (ex: BTC<->ETH) reste
        # prioritaire sur une corrélation trouvée automatiquement.
        for symbol, other in dynamic_map.items():
            correlated_pair_map.setdefault(symbol, other)

    reports: dict[str, DecisionReport | None] = {}

    # BTC traité en premier (si suivi) : son biais effectif sert de
    # référence informative dans les notifications des AUTRES actifs
    # (voir dispatch_alert -> format_telegram_message), pour aider à
    # éviter un maximum d'aller à contre-courant de la tendance HTF
    # dominante du marché crypto. Réordonnancement explicite plutôt que
    # de compter sur l'ordre d'insertion du YAML (fragile, pas garanti).
    ordered_symbols = list(assets)
    if "BTC" in ordered_symbols:
        ordered_symbols.remove("BTC")
        ordered_symbols.insert(0, "BTC")

    # Construit UNE SEULE FOIS par cycle (pas par actif) — réutilisé
    # pour toutes les tentatives d'exécution automatique de ce cycle,
    # voir dispatch_alert()/_check_tp_sl_hits(). Un seul client Kraken
    # pour VOUS, utilisé pour tous les symboles (tous crypto).
    symbol_owner_client, symbol_owner_exchange = _build_owner_client(config)

    btc_bias_context: dict[str, Any] | None = None

    for symbol in ordered_symbols:
        if symbol not in per_symbol_data:
            reports[symbol] = None
            continue

        market_data, candles = per_symbol_data[symbol]
        secondary_symbol = correlated_pair_map.get(symbol)
        secondary_candles = (
            per_symbol_data[secondary_symbol][1]
            if secondary_symbol and secondary_symbol in per_symbol_data
            else None
        )

        context = MarketContext(
            market_data=market_data,
            candles=candles,
            economic_events=economic_events,
            symbol=symbol,
            secondary_symbol=secondary_symbol,
            secondary_candles=secondary_candles,
        )

        logger.info(f"[{symbol}] Prix actuel : {format_price_adaptive(market_data.price_usd)}")

        _check_position_protection(symbol, config, symbol_owner_client, symbol_owner_exchange)
        _check_tp_sl_hits(symbol, market_data.price_usd, config, symbol_owner_client, symbol_owner_exchange)
        _check_pending_breakeven(symbol, config, symbol_owner_client, symbol_owner_exchange)

        try:
            engine = DecisionEngine(config=config)
            report = engine.decide(context)
        except Exception:
            logger.exception(f"[{symbol}] Échec du DecisionEngine")
            reports[symbol] = None
            continue

        logger.info(f"[{symbol}] Décision produite :\n" + report.to_text())

        _check_htf_bias_invalidation(symbol, report, config, symbol_owner_client, symbol_owner_exchange)

        _check_missing_execution(symbol, report, config, market_data.price_usd, symbol_owner_client, symbol_owner_exchange)

        _check_awaiting_tsq_entries(symbol, report, config, symbol_owner_client, symbol_owner_exchange)

        if symbol == "BTC" and report.effective_bias is not None:
            btc_bias_context = {
                "bias": report.effective_bias,
                "source_timeframe": report.bias_source_timeframe,
            }

        decision_id = None
        try:
            decision_id = db.save_decision(report, market_data.price_usd, symbol=symbol)
        except Exception:
            logger.exception(f"[{symbol}] Échec de la sauvegarde de la décision en base")

        if report.entry_recommended and decision_id is not None:
            dispatch_alert(
                report, decision_id, market_data.price_usd, symbol, btc_bias_context,
                config, symbol_owner_client, symbol_owner_exchange,
            )
            _check_entry_signals(symbol, report)
        elif not report.entry_recommended:
            logger.info(f"[{symbol}] Aucune entrée recommandée ce cycle")

        reports[symbol] = report

    return reports


def main() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logger.add(
        LOG_DIR / "trading_alert_bot_{time}.log",
        rotation="1 day",
        retention="30 days",
        level="INFO",
    )

    parser = argparse.ArgumentParser(description="Trading_alert_bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exécute un seul cycle puis quitte (au lieu de tourner en continu)",
    )
    args = parser.parse_args()

    logger.info("Démarrage de Trading_alert_bot")
    db.init_db()
    config = load_config()

    if args.once:
        run_once(config)
        return

    orchestration_cfg = config.get("orchestration", {})
    poll_interval_minutes = orchestration_cfg.get(
        "poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES
    )

    # Premier cycle immédiat, avant le début de la planification.
    run_once(config)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_once, "interval", minutes=poll_interval_minutes, args=[config])
    logger.info(
        f"Planification toutes les {poll_interval_minutes} minutes (Ctrl+C pour arrêter)"
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Arrêt de Trading_alert_bot")


if __name__ == "__main__":
    main()