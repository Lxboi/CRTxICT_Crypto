"""
collectors/binance.py

Collecte des données de marché via l'API Binance — SPOT par défaut,
FUTURES (USDⓈ-M, contrats à terme) en option via le paramètre `market`
de chaque fonction. Caractéristiques :
  - Bougies NATIVES à toutes les granularités utiles (Monthly, Weekly,
    Daily, intraday) : pas de resampling nécessaire.
  - Aucune clé API nécessaire pour les données de marché publiques,
    sur les deux marchés.
  - Jusqu'à 1000 bougies par requête, sur n'importe quelle granularité.
  - Support natif du multi-paires (BTCUSDT, ETHUSDT, etc.).

Endpoints utilisés :
  - Spot (https://developers.binance.com/docs/binance-spot-api-docs) :
    GET /api/v3/klines, GET /api/v3/ticker/24hr.
  - Futures (https://developers.binance.com/docs/derivatives/usds-margined-futures) :
    GET /fapi/v1/klines, GET /fapi/v1/ticker/24hr — même format de
    réponse que le Spot, endpoint et domaine différents seulement.
"""

from __future__ import annotations

import requests

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"

# Correspondance timeframe interne du projet -> intervalle natif Binance.
INTERVAL_MAP: dict[str, str] = {
    "monthly": "1M",
    "weekly": "1w",
    "daily": "1d",
    "intraday": "1h",  # 1000 bougies = ~41,7 jours (vs ~20,8 jours en 30m) ; toujours
                        # compatible avec la détection exacte du True Day Open (minuit NY
                        # tombe toujours sur une frontière d'heure UTC ronde, été comme hiver)
}

MAX_KLINES_PER_REQUEST = 1000


def _klines_url(market: str) -> str:
    base = BINANCE_FUTURES_BASE_URL if market == "futures" else BINANCE_BASE_URL
    endpoint = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"
    return f"{base}{endpoint}"


def _ticker_url(market: str) -> str:
    base = BINANCE_FUTURES_BASE_URL if market == "futures" else BINANCE_BASE_URL
    endpoint = "/fapi/v1/ticker/24hr" if market == "futures" else "/api/v3/ticker/24hr"
    return f"{base}{endpoint}"


def get_klines(symbol: str, interval: str, limit: int = 500, market: str = "spot") -> list[list]:
    """
    Récupère les bougies natives Binance pour une paire et un
    intervalle donnés.

    Args:
        symbol: Paire Binance (ex: "BTCUSDT", "ETHUSDT").
        interval: Intervalle natif Binance ("1M", "1w", "1d", "30m",
            etc. — voir INTERVAL_MAP pour les équivalences avec les
            timeframes internes du projet).
        limit: Nombre de bougies à récupérer (max 1000 côté API).
        market: "spot" (défaut) ou "futures" (contrats à terme USDⓈ-M).

    Returns:
        Liste brute de klines Binance : chaque élément est
        [open_time_ms, open, high, low, close, volume, close_time_ms,
        quote_volume, num_trades, taker_buy_base, taker_buy_quote,
        ignore]. open/high/low/close/volume sont des CHAÎNES (pas des
        floats) côté API — voir _convert_kline_to_simple() pour la
        conversion.

    Raises:
        requests.HTTPError: Si l'appel échoue (symbole invalide, etc.)
    """
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, MAX_KLINES_PER_REQUEST)}

    response = requests.get(_klines_url(market), params=params, timeout=10)
    response.raise_for_status()

    return response.json()


def _convert_kline_to_simple(kline: list) -> list:
    """
    Convertit une kline brute Binance vers le format interne simple
    déjà utilisé par models.market_data.CandleSeries.from_collector_list()
    : [timestamp_ms, open, high, low, close] (valeurs numériques).

    Ce choix évite de dupliquer la logique de construction de
    CandleSeries pour une deuxième source de données : Binance est
    simplement convertie vers le même format pivot que CoinGecko
    utilisait, donc le reste du pipeline (modèles, règles) ne voit
    aucune différence.
    """
    open_time_ms = kline[0]
    return [open_time_ms, float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])]


def get_klines_simple(symbol: str, interval: str, limit: int = 500, market: str = "spot") -> list[list]:
    """
    Comme get_klines(), mais renvoie déjà converti au format interne
    simple [timestamp_ms, open, high, low, close] — prêt à être passé
    tel quel à CandleSeries.from_collector_list().
    """
    raw_klines = get_klines(symbol, interval, limit, market=market)
    return [_convert_kline_to_simple(k) for k in raw_klines]


def get_multi_timeframe_klines_simple(
    symbol: str, limit: int = 500, market: str = "spot"
) -> dict[str, list[list]]:
    """
    Récupère les bougies natives (déjà converties au format interne
    simple) pour les 4 timeframes internes du projet
    (monthly/weekly/daily/intraday) en une série d'appels groupés.

    Returns:
        Dict {"monthly": [...], "weekly": [...], "daily": [...],
        "intraday": [...]}.
    """
    return {
        timeframe: get_klines_simple(symbol, interval, limit, market=market)
        for timeframe, interval in INTERVAL_MAP.items()
    }


# Granularité fine, séparée de "intraday" (1h) : dédiée au resampling
# précis en bougies 4H et à la résolution fine du "moment de purge"
# pour les filtres KillZone/Algo Times de rules/purge_rule.py. Avec
# 1000 bougies de 15 min = ~10,4 jours de profondeur — largement
# suffisant pour quelques CRT 4H récents, sans consommer le budget de
# requêtes déjà utilisé par "intraday".
FINE_INTRADAY_INTERVAL = "15m"


def get_fine_intraday_klines_simple(symbol: str, limit: int = 500, market: str = "spot") -> list[list]:
    """
    Récupère les bougies 15 min (déjà converties au format interne
    simple), séparément des 4 timeframes principaux — voir
    FINE_INTRADAY_INTERVAL pour la justification.
    """
    return get_klines_simple(symbol, FINE_INTRADAY_INTERVAL, limit, market=market)


def get_ticker_24hr(symbol: str, market: str = "spot") -> dict:
    """
    Récupère l'instantané 24h d'une paire (prix, variation, volume).

    Args:
        symbol: Paire Binance (ex: "BTCUSDT").
        market: "spot" (défaut) ou "futures".

    Returns:
        Dict brut Binance avec notamment lastPrice, priceChangePercent,
        volume, quoteVolume (tous en chaînes de caractères).

    Raises:
        requests.HTTPError: Si l'appel échoue.
    """
    params = {"symbol": symbol}

    response = requests.get(_ticker_url(market), params=params, timeout=10)
    response.raise_for_status()

    return response.json()


if __name__ == "__main__":
    ticker = get_ticker_24hr("BTCUSDT")
    print(f"BTC/USDT : ${float(ticker['lastPrice']):,.2f} ({ticker['priceChangePercent']}%)")

    klines = get_klines_simple("BTCUSDT", "1d", limit=5)
    print(f"{len(klines)} bougies daily récupérées")
    print(klines[-1])
