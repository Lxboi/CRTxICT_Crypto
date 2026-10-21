"""
models/market_data.py

Modèle de données représentant un instantané de marché pour un actif
(crypto ou Forex/Indices).

Ce module fait le pont entre la couche de collecte
(collectors/binance.py, collectors/polygon.py), qui retourne un
dictionnaire ou une liste bruts, et la couche métier (rules/, ai/), qui a
besoin d'un objet typé, validé et stable dans le temps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class MarketData(BaseModel):
    """
    Représente un instantané des données de marché pour un actif donné
    à un instant donné.

    Attributes:
        symbol: Symbole de l'actif (ex: "BTC", "ETH").
        price_usd: Prix actuel en dollars US.
        price_change_percentage_24h: Variation du prix sur 24h, en pourcentage.
        volume_24h_usd: Volume échangé sur les dernières 24h, en dollars US.
        market_cap_usd: Capitalisation boursière totale, en dollars US.
            None si la source de données ne la fournit pas (c'est le
            cas de Binance, qui ne suit pas cette donnée — contrairement
            à CoinGecko).
        timestamp: Date et heure (UTC) de la collecte de la donnée.
    """

    symbol: str = Field(default="BTC", description="Symbole de l'actif suivi")
    price_usd: float = Field(..., gt=0, description="Prix actuel en USD")
    price_change_percentage_24h: float = Field(
        ..., description="Variation du prix sur 24h, en pourcentage"
    )
    volume_24h_usd: float = Field(
        ..., ge=0, description="Volume échangé sur 24h, en USD"
    )
    market_cap_usd: float | None = Field(
        default=None, description="Capitalisation boursière totale, en USD (absente pour Binance)"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Horodatage UTC de la collecte",
    )

    @field_validator("symbol")
    @classmethod
    def _symbol_uppercase(cls, value: str) -> str:
        """Normalise le symbole en majuscules (ex: 'btc' -> 'BTC')."""
        return value.upper()

    @classmethod
    def from_collector_dict(cls, data: dict[str, Any]) -> "MarketData":
        """
        Construit une instance de MarketData à partir d'un dictionnaire
        générique déjà normalisé au format simple :
        {"price": float, "change_24h": float, "volume": float,
        "market_cap": float}.

        Utilitaire générique indépendant de la source (contrairement à
        from_binance_ticker/from_polygon_daily_bars, qui parsent chacune
        le format natif de leur propre API) — pratique pour construire
        une instance directement en test ou depuis une source déjà
        prétraitée.

        Args:
            data: Dictionnaire au format ci-dessus.

        Returns:
            Une instance validée de MarketData.

        Raises:
            KeyError: Si une clé attendue est absente du dictionnaire.
            pydantic.ValidationError: Si une valeur ne respecte pas les
                contraintes du modèle (ex: prix négatif).
        """
        required_keys = ("price", "change_24h", "volume", "market_cap")
        missing_keys = [key for key in required_keys if key not in data]
        if missing_keys:
            raise KeyError(
                f"Clé(s) manquante(s) dans le dictionnaire de collecte : "
                f"{missing_keys}"
            )

        return cls(
            price_usd=data["price"],
            price_change_percentage_24h=data["change_24h"],
            volume_24h_usd=data["volume"],
            market_cap_usd=data["market_cap"],
        )

    @classmethod
    def from_binance_ticker(cls, data: dict[str, Any], symbol: str) -> "MarketData":
        """
        Construit un MarketData à partir de la réponse brute Binance
        GET /api/v3/ticker/24hr.

        Binance ne fournit pas de capitalisation boursière (donnée hors
        de son domaine, contrairement à CoinGecko) : market_cap_usd
        reste None pour cette source. Toutes les valeurs numériques de
        Binance sont des chaînes de caractères, converties ici en float.

        Args:
            data: Dictionnaire brut renvoyé par
                collectors/binance.py::get_ticker_24hr(). Clés utilisées :
                "lastPrice", "priceChangePercent", "quoteVolume".
            symbol: Symbole interne de l'actif (ex: "BTC", "ETH") —
                Binance renvoie le symbole de la paire ("BTCUSDT"), pas
                celui de l'actif seul, donc à fournir explicitement.

        Returns:
            Une instance validée de MarketData, avec market_cap_usd=None.
        """
        return cls(
            symbol=symbol,
            price_usd=float(data["lastPrice"]),
            price_change_percentage_24h=float(data["priceChangePercent"]),
            volume_24h_usd=float(data["quoteVolume"]),
            market_cap_usd=None,
        )

    @classmethod
    def from_polygon_daily_bars(
        cls, raw_daily_bars: list[dict[str, Any]], symbol: str
    ) -> "MarketData":
        """
        Dérive un instantané de marché à partir des bougies daily
        BRUTES Polygon.io (champs "o"/"h"/"l"/"c"/"v"/"t"), en
        réutilisant les 2 dernières bougies déjà collectées plutôt que
        d'ajouter un appel réseau séparé pour un endpoint "temps réel"
        — une variation calculée sur clôtures daily suffit à l'usage
        actuel du projet.

        market_cap_usd reste None : Polygon ne fournit pas cette
        donnée de façon uniforme pour toutes les classes d'actifs
        (ETF/Forex/Futures n'ont d'ailleurs pas de notion de
        capitalisation boursière au sens classique).

        Args:
            raw_daily_bars: Liste brute renvoyée par
                collectors/polygon.py::get_aggregates() (ou le second
                élément du tuple renvoyé par get_multi_timeframe_data()).
                Au moins 2 bougies sont nécessaires pour calculer une
                variation sur 24h.
            symbol: Symbole interne de l'actif (ex: "SPY", "EURUSD").

        Raises:
            ValueError: Si moins de 2 bougies sont fournies.
        """
        if len(raw_daily_bars) < 2:
            raise ValueError(
                "from_polygon_daily_bars nécessite au moins 2 bougies daily "
                "pour calculer une variation 24h"
            )

        previous_close = raw_daily_bars[-2]["c"]
        latest = raw_daily_bars[-1]
        change_pct = (latest["c"] - previous_close) / previous_close * 100

        return cls(
            symbol=symbol,
            price_usd=latest["c"],
            price_change_percentage_24h=change_pct,
            volume_24h_usd=latest.get("v", 0.0),
            market_cap_usd=None,
        )

    @classmethod
    def from_ibkr_daily_bars(cls, daily_bars_simple: list[list], symbol: str) -> "MarketData":
        """
        Dérive un instantané de marché à partir des bougies daily déjà
        converties au format interne simple [timestamp_ms, open, high,
        low, close] (voir collectors/ibkr.py) — même principe que
        from_polygon_daily_bars (réutilise les 2 dernières bougies
        déjà collectées, pas d'appel réseau séparé pour un "instantané
        temps réel").

        volume_24h_usd reste à 0.0 : IBKR ne fournit pas de volume
        fiable pour le Forex (marché OTC, pas de volume centralisé
        réel — voir la valeur -1 renvoyée par l'API pour ces
        instruments) ni les CFD ; seuls les Futures ont un volume réel,
        mais pas exploité ici pour rester cohérent entre tous les
        actifs IBKR suivis. market_cap_usd reste None (aucun sens pour
        Forex/CFD/Futures).

        Args:
            daily_bars_simple: Bougies daily au format interne simple,
                déjà converties par collectors/ibkr.py. Au moins 2
                bougies sont nécessaires pour calculer une variation 24h.
            symbol: Symbole interne de l'actif (ex: "EURUSD", "DXY").

        Raises:
            ValueError: Si moins de 2 bougies sont fournies.
        """
        if len(daily_bars_simple) < 2:
            raise ValueError(
                "from_ibkr_daily_bars nécessite au moins 2 bougies daily "
                "pour calculer une variation 24h"
            )

        previous_close = daily_bars_simple[-2][4]
        latest = daily_bars_simple[-1]
        change_pct = (latest[4] - previous_close) / previous_close * 100

        return cls(
            symbol=symbol,
            price_usd=latest[4],
            price_change_percentage_24h=change_pct,
            volume_24h_usd=0.0,
            market_cap_usd=None,
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Sérialise l'objet en dictionnaire, utile pour la sauvegarde en base
        de données (database/db.py).
        """
        return self.model_dump(mode="json")


class Candle(BaseModel):
    """
    Représente une bougie OHLC (Open, High, Low, Close) sur une période
    donnée.

    Attributes:
        timestamp: Horodatage UTC d'ouverture de la bougie.
        open: Prix d'ouverture.
        high: Prix le plus haut atteint durant la bougie.
        low: Prix le plus bas atteint durant la bougie.
        close: Prix de clôture.
    """

    timestamp: datetime = Field(..., description="Horodatage UTC de la bougie")
    open: float = Field(..., gt=0)
    high: float = Field(..., gt=0)
    low: float = Field(..., gt=0)
    close: float = Field(..., gt=0)

    @classmethod
    def from_simple_ohlc(cls, raw: list[float]) -> "Candle":
        """
        Construit une Candle à partir d'une entrée déjà normalisée au
        format simple [timestamp_ms, open, high, low, close] — format
        commun produit par collectors/binance.py et collectors/polygon.py
        (chacun convertit son format d'API natif vers cette forme avant
        de la transmettre ici).
        """
        timestamp_ms, open_price, high, low, close = raw
        return cls(
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
            open=open_price,
            high=high,
            low=low,
            close=close,
        )


class CandleSeries(BaseModel):
    """
    Série ordonnée de bougies historiques.

    Utilisée par les règles ayant besoin de contexte au-delà de
    l'instantané courant (ex: DOL Rule pour repérer des zones de
    liquidité, CRT Rule et Purge Rule pour analyser la structure récente
    du marché).
    """

    candles: list[Candle] = Field(default_factory=list)

    @classmethod
    def from_collector_list(cls, raw_candles: list[list[float]]) -> "CandleSeries":
        """
        Construit une CandleSeries à partir de la liste déjà normalisée
        au format simple, produite par collectors/binance.py ou
        collectors/polygon.py (voir Candle.from_simple_ohlc).
        """
        return cls(
            candles=[Candle.from_simple_ohlc(item) for item in raw_candles]
        )

    def __len__(self) -> int:
        return len(self.candles)

    def is_empty(self) -> bool:
        """Indique si la série ne contient aucune bougie."""
        return len(self.candles) == 0

    def resample(self, timeframe: Literal["daily", "weekly", "monthly"]) -> "CandleSeries":
        """
        Agrège cette série de bougies vers une timeframe plus large.

        Nécessaire car l'API CoinGecko gratuite ne fournit pas directement
        de bougies Daily/Weekly/Monthly propres (uniquement des
        granularités de 30 min / 4h / 4 jours selon la profondeur
        demandée). On construit donc ces timeframes nous-mêmes à partir
        d'une série source plus fine.

        Args:
            timeframe: Timeframe cible. "daily" agrège par jour civil,
                "weekly" par semaine ISO, "monthly" par mois civil.

        Returns:
            Une nouvelle CandleSeries, une bougie par période, ordonnée
            chronologiquement. Chaque bougie agrégée prend l'open de la
            première bougie de la période, le close de la dernière, le
            plus haut des highs et le plus bas des lows.
        """
        if self.is_empty():
            return CandleSeries(candles=[])

        groups: dict[tuple[int, int], list[Candle]] = defaultdict(list)

        for candle in self.candles:
            if timeframe == "daily":
                key = (candle.timestamp.year, candle.timestamp.timetuple().tm_yday)
            elif timeframe == "weekly":
                iso_year, iso_week, _ = candle.timestamp.isocalendar()
                key = (iso_year, iso_week)
            elif timeframe == "monthly":
                key = (candle.timestamp.year, candle.timestamp.month)
            else:
                raise ValueError(f"Timeframe non supportée : {timeframe}")

            groups[key].append(candle)

        resampled_candles = [
            Candle(
                timestamp=group[0].timestamp,
                open=group[0].open,
                close=group[-1].close,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
            )
            for _, group in sorted(groups.items())
        ]

        return CandleSeries(candles=resampled_candles)


def build_multi_timeframe_candles(
    raw_ohlc: list[list[float]],
) -> dict[str, CandleSeries]:
    """
    Construit les trois séries de bougies (Monthly, Weekly, Daily) à
    partir d'une seule série brute déjà normalisée au format simple
    [timestamp_ms, open, high, low, close] (voir Candle.from_simple_ohlc).

    Sert de point d'entrée pratique pour peupler MarketContext.candles
    à partir d'un historique daily unique, par resampling — utile pour
    des sources qui ne fournissent pas nativement les granularités
    Monthly/Weekly (contrairement à Binance, dont chaque granularité
    est récupérée nativement via collectors/binance.py, sans passer par
    cette fonction).

    Args:
        raw_ohlc: Liste de bougies brutes au format
            [timestamp_ms, open, high, low, close].

    Returns:
        Un dictionnaire {"monthly": CandleSeries, "weekly": CandleSeries,
        "daily": CandleSeries}.
    """
    base_series = CandleSeries.from_collector_list(raw_ohlc)

    return {
        "monthly": base_series.resample("monthly"),
        "weekly": base_series.resample("weekly"),
        "daily": base_series.resample("daily"),
    }


def format_price_adaptive(price: float) -> str:
    """
    Formate un prix avec un nombre de décimales adapté à son ordre de
    grandeur. Un formatage fixe à 2 décimales fait perdre toute
    précision utile sur les actifs à faible valeur unitaire (TRX, XRP,
    SHIB...) : un SL affiché "$0.04" pourrait en réalité être n'importe
    où entre $0.035 et $0.045 une fois arrondi — un écart de plusieurs
    % sur ce type d'actif, largement suffisant pour fausser une entrée.

    Fonction partagée entre messaging/formatter.py (notification finale) et
    rules/decision_engine.py (log de raisonnement interne) — les deux
    doivent afficher les mêmes prix de la même façon, sous peine de
    confusion (ex: TP1/TP2 d'un actif comme TRX arrondis à 2 décimales
    dans un endroit, à 4 dans l'autre, donnant l'impression à tort de
    deux valeurs identiques).

    Seuils (empiriques, calqués sur ce que font la plupart des
    exchanges pour l'affichage) :
        >= 1        -> 2 décimales  (ex: $95,432.10)
        >= 0.01     -> 4 décimales  (ex: $0.2847)
        >= 0.0001   -> 6 décimales  (ex: $0.000123)
        < 0.0001    -> 8 décimales  (ex: $0.00000012)
    """
    abs_price = abs(price)
    if abs_price >= 1:
        decimals = 2
    elif abs_price >= 0.01:
        decimals = 4
    elif abs_price >= 0.0001:
        decimals = 6
    else:
        decimals = 8
    return f"${price:,.{decimals}f}"