"""
backtest/historical_loader.py

Récupère un historique de bougies Binance sur plusieurs années —
contrairement à collectors/binance.py (pensé pour le live, plafonné à
1000 bougies par requête), ce module pagine automatiquement via
startTime/endTime pour reconstituer un historique complet.

N'écrit jamais dans collectors/binance.py : c'est un module autonome,
propre au backtest, qui réutilise juste sa logique de conversion et ses
constantes pour rester cohérent avec le format interne du projet.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from collectors.binance import (
    BINANCE_BASE_URL,
    FINE_INTRADAY_INTERVAL,
    INTERVAL_MAP,
    _convert_kline_to_simple,
)

# Endpoint public Binance FUTURES (USDⓈ-M) -- séparé du Spot
# (BINANCE_BASE_URL, api.binance.com) utilisé par défaut. Même format
# de klines que le Spot (_convert_kline_to_simple reste valable tel
# quel), mais des PRIX parfois légèrement différents (funding, base)
# et un historique qui peut démarrer plus tard pour certaines paires
# (les contrats à terme sur un actif sont souvent listés après le
# marché Spot correspondant).
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"

# Un cycle de sommeil léger entre pages, par précaution — largement
# sous la limite Binance (6000 poids/minute, klines = poids 2), mais
# évite d'envoyer des dizaines de requêtes en rafale pure pour rien.
_PAGINATION_SLEEP_SECONDS = 0.2


def fetch_paginated_klines(
    symbol: str, interval: str, start_time_ms: int, end_time_ms: int, market: str = "spot",
) -> list[list]:
    """
    Récupère TOUTES les bougies natives entre deux horodatages (ms),
    en paginant automatiquement par blocs de 1000 (limite Binance par
    requête) via startTime/endTime.

    Args:
        symbol: Paire Binance (ex: "BTCUSDT").
        interval: Intervalle natif Binance ("1M", "1w", "1d"...).
        start_time_ms: Horodatage de début (millisecondes epoch UTC).
        end_time_ms: Horodatage de fin (millisecondes epoch UTC).
        market: "spot" (défaut, api.binance.com) ou "futures"
            (fapi.binance.com, contrats à terme USDⓈ-M) — même format
            de réponse dans les deux cas, seul l'endpoint change.

    Returns:
        Liste de klines brutes (format Binance natif, pas encore
        converties) couvrant toute la période demandée, triées
        chronologiquement.
    """
    base_url = BINANCE_FUTURES_BASE_URL if market == "futures" else BINANCE_BASE_URL
    endpoint = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"

    all_klines: list[list] = []
    current_start = start_time_ms

    while current_start < end_time_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_time_ms,
            "limit": 1000,
        }
        response = requests.get(f"{base_url}{endpoint}", params=params, timeout=15)
        response.raise_for_status()
        batch = response.json()

        if not batch:
            break

        all_klines.extend(batch)

        if len(batch) < 1000:
            # Dernière page (moins de 1000 bougies renvoyées) -> terminé.
            break

        # Bougie suivante après la dernière reçue, pour ne pas la redemander.
        last_open_time_ms = batch[-1][0]
        current_start = last_open_time_ms + 1

        time.sleep(_PAGINATION_SLEEP_SECONDS)

    return all_klines


def fetch_historical_multi_timeframe(
    symbol: str,
    years_of_history: int,
    margin_years: int = 1,
    market: str = "spot",
    include_m15: bool = False,
) -> dict[str, list[list]]:
    """
    Récupère l'historique Monthly/Weekly/Daily (et, si demandé, m15)
    complet pour un actif, sur `years_of_history` années de test +
    `margin_years` années de marge supplémentaire AVANT le début du test
    (pour que les ancres CRT Monthly/Weekly aient de quoi regarder en
    arrière dès le premier jour simulé — sans cette marge, les tout
    premiers mois du backtest seraient artificiellement pauvres en
    signaux).

    Args:
        symbol: Symbole interne du projet (ex: "BTC") — converti en
            paire Binance ("BTCUSDT") en interne.
        years_of_history: Nombre d'années à effectivement tester.
        margin_years: Années supplémentaires à récupérer avant le début
            du test, pour le contexte des ancres CRT/DOL.
        market: "spot" (défaut) ou "futures" (contrats à terme USDⓈ-M,
            fapi.binance.com) — voir fetch_paginated_klines(). Les prix
            Spot et Futures peuvent légèrement diverger (funding, base),
            et l'historique Futures démarre parfois plus tard que le
            Spot pour un même actif (contrat listé après coup) : si le
            volume de bougies reçu est nettement inférieur à ce qui est
            attendu pour la période demandée, c'est la cause la plus
            probable (voir l'avertissement dans run_backtest.py).
        include_m15: False par défaut (comportement historique inchangé
            -- backtest Daily/CRT actuel, voir backtest/simulator.py).
            True pour le futur simulateur intraday (Phase 2, pas encore
            livré) : récupère EN PLUS l'historique 15 min
            (FINE_INTRADAY_INTERVAL, "15m") sur la MÊME fenêtre que
            Monthly/Weekly/Daily -- réutilise fetch_paginated_klines(),
            déjà générique sur l'intervalle, sans code de pagination
            supplémentaire. Volumineux (~96 bougies/jour) : pour 5 ans +
            1 an de marge, environ 175 000 bougies m15, soit ~220
            requêtes paginées (~0.2s de pause entre chacune, voir
            _PAGINATION_SLEEP_SECONDS) -- de l'ordre de la minute, pas
            instantané. Aucune clé API requise (Binance, données de
            marché publiques).

    Returns:
        Dict {"monthly": [...], "weekly": [...], "daily": [...]}, avec
        en plus "m15" si include_m15=True -- klines déjà converties au
        format simple interne [timestamp_ms, open, high, low, close].
    """
    ticker = f"{symbol}USDT"
    total_years = years_of_history + margin_years

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=365 * total_years)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    result: dict[str, list[list]] = {}
    for timeframe in ("monthly", "weekly", "daily"):
        interval = INTERVAL_MAP[timeframe]
        raw = fetch_paginated_klines(ticker, interval, start_ms, end_ms, market=market)
        result[timeframe] = [_convert_kline_to_simple(k) for k in raw]

    if include_m15:
        raw_m15 = fetch_paginated_klines(
            ticker, FINE_INTRADAY_INTERVAL, start_ms, end_ms, market=market
        )
        result["m15"] = [_convert_kline_to_simple(k) for k in raw_m15]

    return result


# --- Polygon (Forex/Indices) ---
#
# Contrairement à Binance, l'endpoint Polygon renvoie TOUT l'historique
# demandé en UN SEUL appel (jusqu'à `limit` résultats, 50 000 max côté
# API) — pas besoin de pagination par pages successives. Le vrai frein
# n'est pas le nombre de requêtes, mais la PROFONDEUR D'HISTORIQUE
# disponible selon votre plan Polygon : le plan Basic Currencies
# (Forex, gratuit) est limité à ~2 ans d'historique, contre 6+ ans sur
# Binance pour la crypto. Demander plus ne provoque pas forcément une
# erreur claire — Polygon peut simplement renvoyer moins de données que
# demandé, en commençant à la date la plus ancienne réellement
# disponible. Un avertissement est affiché si le volume de bougies
# renvoyées est nettement inférieur à ce qui était attendu pour la
# période demandée (voir run_backtest.py).

POLYGON_TIMESPAN_MAP: dict[str, tuple[int, str]] = {
    "monthly": (1, "month"),
    "weekly": (1, "week"),
    "daily": (1, "day"),
}


def fetch_historical_multi_timeframe_polygon(
    polygon_ticker: str,
    years_of_history: int,
    margin_years: int = 1,
    include_m15: bool = False,
) -> dict[str, list[list]]:
    """
    Équivalent de fetch_historical_multi_timeframe(), mais pour un
    ticker Polygon (Forex/Indices — ex: "C:EURUSD", "I:SPX").

    Args:
        polygon_ticker: Ticker Polygon EXACT, avec son préfixe
            ("C:" pour Forex, "I:" pour indices) — pas de conversion
            automatique depuis un symbole interne, les tickers Polygon
            ne suivent pas un format aussi uniforme que "XXXUSDT" sur
            Binance.
        years_of_history: Nombre d'années à effectivement tester.
        margin_years: Années de marge en plus AVANT le début du test.
        include_m15: False par défaut (comportement historique
            inchangé). True pour le futur simulateur intraday (Phase 2,
            pas encore livré) : récupère EN PLUS l'historique 15 min via
            fetch_paginated_intraday_polygon() -- voir cette fonction
            pour l'avertissement important sur la profondeur d'historique
            RÉELLEMENT disponible sur le plan Polygon gratuit (à valider
            empiriquement, pas garanti fonctionner comme pour le
            Monthly/Weekly/Daily).

    Returns:
        Dict {"monthly": [...], "weekly": [...], "daily": [...]}, avec
        en plus "m15" si include_m15=True — même format simple interne
        que la version Binance.
    """
    from collectors.polygon import get_aggregates, _convert_aggregate_to_simple

    total_years = years_of_history + margin_years
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=365 * total_years)

    result: dict[str, list[list]] = {}
    for timeframe, (multiplier, timespan) in POLYGON_TIMESPAN_MAP.items():
        raw = get_aggregates(
            polygon_ticker,
            multiplier,
            timespan,
            start_date.isoformat(),
            end_date.isoformat(),
            limit=50000,
        )
        result[timeframe] = [_convert_aggregate_to_simple(bar) for bar in raw]

    if include_m15:
        result["m15"] = fetch_paginated_intraday_polygon(
            polygon_ticker, start_date, end_date, multiplier=15
        )

    return result


# Nombre de bougies max visé par requête Polygon, en marge sous la
# limite dure de l'API (50 000, voir get_aggregates()) -- absorbe les
# journées Forex/CFD à session presque continue (pas de "jour de 1440
# minutes" parfaitement plein) sans jamais dépasser la limite dure.
_POLYGON_TARGET_ROWS_PER_CHUNK = 40_000


def fetch_paginated_intraday_polygon(
    polygon_ticker: str, start_date: date, end_date: date, multiplier: int = 15,
) -> list[list]:
    """
    Récupère l'historique intraday (par défaut 15 min) sur une
    profondeur ARBITRAIRE, en découpant la période demandée en blocs de
    quelques dizaines de jours (contrairement à Monthly/Weekly/Daily,
    UNE requête Polygon suffit rarement pour plusieurs années de
    bougies 15 min — la limite dure de 50 000 lignes par appel serait
    dépassée bien avant). Chaque bloc reste un simple appel à
    get_aggregates(), déjà limité à 5 requêtes/minute (plan gratuit,
    voir collectors/polygon.py::_wait_for_rate_limit) -- aucun code de
    limitation de débit supplémentaire nécessaire ici.

    ⚠️ IMPORTANT -- non vérifié en conditions réelles à ce jour (aucune
    clé POLYGON_API_KEY utilisée pendant le développement) : la PROFONDEUR
    D'HISTORIQUE réellement disponible en 15 min sur le plan Polygon
    gratuit est inconnue -- pourrait être bien plus courte que les ~2
    ans documentés pour le Daily (les plans gratuits limitent
    généralement l'intraday plus sévèrement que le end-of-day). Si le
    volume de bougies renvoyées est nettement inférieur à ce qui est
    demandé, envisager une autre source de données m15 (ex. London
    Strategic Edge), plutôt que de considérer ce résultat comme fiable
    tel quel.

    Args:
        polygon_ticker: Ticker Polygon EXACT (ex: "C:EURUSD").
        start_date: Date de début (incluse).
        end_date: Date de fin (incluse).
        multiplier: Multiplicateur en minutes (15 par défaut, aligné
            sur FINE_INTRADAY_INTERVAL côté Binance).

    Returns:
        Bougies m15 triées chronologiquement, au format interne simple
        [timestamp_ms, open, high, low, close].
    """
    from collectors.polygon import get_aggregates, _convert_aggregate_to_simple

    bars_per_day = max(1, 1440 // multiplier)
    chunk_days = max(1, _POLYGON_TARGET_ROWS_PER_CHUNK // bars_per_day)

    result: list[list] = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end_date)
        raw = get_aggregates(
            polygon_ticker, multiplier, "minute",
            chunk_start.isoformat(), chunk_end.isoformat(), limit=50000,
        )
        result.extend(_convert_aggregate_to_simple(bar) for bar in raw)
        chunk_start = chunk_end + timedelta(days=1)

    return result

