"""
collectors/polygon.py

Collecte de données de marché via l'API Polygon.io — complète
collectors/binance.py (qui reste la source pour la crypto) avec les
ETF, Forex, Futures, actions et indices.

Nécessite une clé API (POLYGON_API_KEY dans .env) — inscription
gratuite sur https://polygon.io/dashboard/signup. Offre gratuite :
5 requêtes/minute, données différées de 15 min sur les actions. Avec
4 requêtes nécessaires par paire et par cycle (une par timeframe), le
plan gratuit limite en pratique à 1 paire Polygon par cycle sans
dépasser le quota — à garder en tête en ajoutant plusieurs actifs.

Note (juillet 2026) : Polygon.io a été rebaptisé "Massive" en octobre
2025. L'ancien domaine api.polygon.io reste supporté "pour une durée
prolongée" d'après leur annonce officielle, donc ce collecteur
fonctionne tel quel — mais une bascule vers api.massive.com pourrait
être nécessaire un jour si l'ancien domaine est retiré.

Format des tickers Polygon à fournir dans main.py (voir DEFAULT_ASSETS) :
  - Actions/ETF : symbole brut, ex: "SPY", "QQQ".
  - Forex : préfixe "C:", ex: "C:EURUSD".
  - Indices : préfixe "I:", ex: "I:SPX".
  - Futures : préfixe "F:" (support variable selon l'abonnement).
"""

from __future__ import annotations

import time
from collections import deque
from datetime import date, timedelta
from typing import Any

import requests
from loguru import logger

from config.settings import get_settings

POLYGON_BASE_URL = "https://api.polygon.io"

# Correspondance timeframe interne du projet -> (multiplier, timespan) Polygon.
# "intraday" passé de 30 à 15 min (août 2026, Phase 1 backtest réaliste)
# pour s'aligner sur FINE_INTRADAY_INTERVAL côté Binance ("15m") -- avec
# 7 jours de profondeur (DEFAULT_LOOKBACK_DAYS ci-dessous), ça reste
# 672 bougies, largement sous klines_limit (1000, config/alerts.yaml).
# Bénéfice direct : True Day Open (rules/entry_rule.py) et les
# liquidity pools (Turtle Soup) sont désormais résolus à la même
# précision pour le Forex/CFD (Polygon) que pour le crypto (Binance).
INTERVAL_MAP: dict[str, tuple[int, str]] = {
    "monthly": (1, "month"),
    "weekly": (1, "week"),
    "daily": (1, "day"),
    "intraday": (15, "minute"),
}

# Profondeur d'historique demandée par timeframe (en jours calendaires
# avant aujourd'hui) — Polygon utilise des dates de début/fin plutôt
# qu'un simple "nombre de bougies" comme Binance.
DEFAULT_LOOKBACK_DAYS: dict[str, int] = {
    "monthly": 365 * 5,
    "weekly": 365 * 2,
    "daily": 365,
    "intraday": 7,
}

DEFAULT_LIMIT = 500

# Durée de validité du cache par timeframe, en secondes -- même
# raisonnement que collectors/ibkr.py::_CACHE_TTL_SECONDS (août 2026,
# bascule Forex/CFD/Futures d'IBKR vers Polygon pour la collecte,
# IBKR conservé uniquement pour l'exécution) : Monthly/Weekly/Daily
# changent trop rarement pour justifier un rafraîchissement à chaque
# cycle, et le faire quand même consommait inutilement le quota
# Polygon (5 requêtes/min sur le plan gratuit -- 14 actifs x 4
# timeframes = 56 requêtes/cycle sans cache, contre 14 en régime
# normal avec cache, largement gérable en quelques minutes au lieu de
# ~11). Aucun cache pour "intraday" (0 = toujours rafraîchi) : seul
# timeframe où la fraîcheur compte réellement à chaque cycle.
_CACHE_TTL_SECONDS: dict[str, int] = {
    "monthly": 86400,  # 24h -- un Monthly ne clôture qu'une fois par mois
    "weekly": 43200,  # 12h -- idem, une fois par semaine
    "daily": 3600,  # 1h -- la bougie daily EN COURS bouge dans la journée,
    # mais closed_candles (utilisé pour CRT/biais) l'exclut de toute
    # façon -- 1h de fraîcheur suffit largement pour les bougies déjà closes
    "intraday": 0,
}

# Cache en mémoire {(ticker, timeframe): (timestamp_du_fetch, bougies_brutes)}
# -- vidé à chaque redémarrage du conteneur, ce qui est très bien : un
# fetch frais au démarrage est de toute façon souhaitable.
_candle_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

# --- Limiteur de débit (plan gratuit : 5 requêtes/minute) ---
# Fenêtre glissante : avant chaque appel, si 5 requêtes ont déjà été
# faites dans la dernière minute, on attend le temps nécessaire plutôt
# que de risquer un HTTP 429. Avec plusieurs actifs Polygon suivis (4
# requêtes chacun, une par timeframe), la collecte peut prendre
# plusieurs minutes — acceptable dans un cycle de 15 minutes, mais à
# garder en tête si vous réduisez poll_interval_minutes.
MAX_REQUESTS_PER_MINUTE = 5
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_request_timestamps: deque[float] = deque()


def _wait_for_rate_limit() -> None:
    """
    Respecte la limite de MAX_REQUESTS_PER_MINUTE requêtes/minute :
    bloque (time.sleep) le temps nécessaire avant de laisser passer un
    nouvel appel, plutôt que de risquer un HTTP 429. Partagé entre tous
    les appels get_aggregates(), donc entre tous les actifs Polygon
    suivis (pas de limiteur par actif, mais un seul global, cohérent
    avec le fait que le quota est par clé API, pas par ticker).
    """
    now = time.monotonic()
    while _request_timestamps and now - _request_timestamps[0] > _RATE_LIMIT_WINDOW_SECONDS:
        _request_timestamps.popleft()

    if len(_request_timestamps) >= MAX_REQUESTS_PER_MINUTE:
        sleep_time = _RATE_LIMIT_WINDOW_SECONDS - (now - _request_timestamps[0]) + 0.1
        if sleep_time > 0:
            logger.debug(f"Rate limit Polygon : pause de {sleep_time:.1f}s avant le prochain appel")
            time.sleep(sleep_time)
        now = time.monotonic()
        while _request_timestamps and now - _request_timestamps[0] > _RATE_LIMIT_WINDOW_SECONDS:
            _request_timestamps.popleft()

    _request_timestamps.append(time.monotonic())


def _get_api_key() -> str:
    """
    Raises:
        ValueError: Si POLYGON_API_KEY est absente de l'environnement.
    """
    api_key = get_settings().polygon_api_key
    if not api_key:
        raise ValueError(
            "POLYGON_API_KEY absente de l'environnement -> impossible "
            "d'appeler l'API Polygon.io"
        )
    return api_key


def get_aggregates(
    ticker: str,
    multiplier: int,
    timespan: str,
    from_date: str,
    to_date: str,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """
    Récupère les bougies agrégées (bars) pour un ticker Polygon.

    Args:
        ticker: Ticker Polygon (ex: "SPY", "C:EURUSD", "I:SPX").
        multiplier: Multiplicateur de l'intervalle (ex: 1 pour "1 day").
        timespan: Unité de temps ("minute", "hour", "day", "week", "month").
        from_date: Date de début, format "YYYY-MM-DD".
        to_date: Date de fin, format "YYYY-MM-DD".
        limit: Nombre maximum de bougies (max ~50000 côté API, mais le
            plan gratuit est de toute façon limité en requêtes/minute).

    Returns:
        Liste de dicts bruts Polygon, en ordre chronologique croissant :
        {"o", "h", "l", "c", "v", "t" (ms), "vw", "n"}.

    Raises:
        ValueError: Si POLYGON_API_KEY est absente.
        requests.HTTPError: Si l'appel échoue (ticker invalide, quota
            dépassé — HTTP 429 sur le plan gratuit au-delà de 5
            requêtes/minute).
    """
    url = (
        f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker}/range/"
        f"{multiplier}/{timespan}/{from_date}/{to_date}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": limit, "apiKey": _get_api_key()}

    _wait_for_rate_limit()
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()
    if data.get("status") not in ("OK", "DELAYED"):
        raise requests.HTTPError(f"Réponse Polygon inattendue pour {ticker} : {data}")

    return data.get("results", [])


def _convert_aggregate_to_simple(bar: dict[str, Any]) -> list:
    """
    Convertit une bougie brute Polygon vers le format interne simple
    [timestamp_ms, open, high, low, close] — même format pivot que
    collectors/binance.py, pour que le reste du pipeline (modèles,
    règles) ne voie aucune différence entre les sources.
    """
    return [bar["t"], bar["o"], bar["h"], bar["l"], bar["c"]]


def get_multi_timeframe_data(
    ticker: str, limit: int = DEFAULT_LIMIT
) -> tuple[dict[str, list[list]], list[dict]]:
    """
    Récupère les bougies (converties au format simple) pour les 4
    timeframes internes du projet, ET les bougies daily BRUTES à part
    (nécessaires pour construire l'instantané de marché avec le volume
    — voir models.market_data.MarketData.from_polygon_daily_bars —
    sans avoir à refaire un appel réseau séparé).

    Mis en CACHE en mémoire pour Monthly/Weekly/Daily (voir
    _CACHE_TTL_SECONDS) — mêmes bénéfices que collectors/ibkr.py :
    réduit fortement la pression sur le quota Polygon (5 req/min sur
    le plan gratuit). Intraday reste toujours rafraîchi (aucun cache).

    Returns:
        (candles_simple, raw_daily_bars) — candles_simple est un dict
        {"monthly": [...], "weekly": [...], "daily": [...],
        "intraday": [...]}.
    """
    today = date.today()
    candles_simple: dict[str, list[list]] = {}
    raw_daily_bars: list[dict] = []

    for timeframe, (multiplier, timespan) in INTERVAL_MAP.items():
        cache_key = (ticker, timeframe)
        ttl = _CACHE_TTL_SECONDS.get(timeframe, 0)
        if ttl > 0 and cache_key in _candle_cache:
            fetched_at, cached_bars = _candle_cache[cache_key]
            if time.time() - fetched_at < ttl:
                if timeframe == "daily":
                    raw_daily_bars = cached_bars
                candles_simple[timeframe] = [_convert_aggregate_to_simple(bar) for bar in cached_bars]
                continue

        lookback_days = DEFAULT_LOOKBACK_DAYS[timeframe]
        from_date = (today - timedelta(days=lookback_days)).isoformat()
        to_date = today.isoformat()

        raw_bars = get_aggregates(ticker, multiplier, timespan, from_date, to_date, limit)
        if timeframe == "daily":
            raw_daily_bars = raw_bars

        candles_simple[timeframe] = [_convert_aggregate_to_simple(bar) for bar in raw_bars]
        if ttl > 0:
            _candle_cache[cache_key] = (time.time(), raw_bars)

    return candles_simple, raw_daily_bars


if __name__ == "__main__":
    candles, daily_raw = get_multi_timeframe_data("SPY", limit=10)
    print(f"{len(daily_raw)} bougies daily brutes récupérées pour SPY")
    if daily_raw:
        print(daily_raw[-1])
