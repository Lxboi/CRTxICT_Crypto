"""Tests pour models/market_data.py : MarketData, Candle, CandleSeries."""

from __future__ import annotations

from models.market_data import Candle, CandleSeries, MarketData, build_multi_timeframe_candles
from tests.conftest import make_candle, ny_datetime


def test_market_data_from_collector_dict():
    raw = {"price": 65000.5, "change_24h": -2.3, "volume": 23641014930.3, "market_cap": 1252049824453.0}
    market_data = MarketData.from_collector_dict(raw)

    assert market_data.price_usd == 65000.5
    assert market_data.symbol == "BTC"


def test_market_data_missing_key_raises():
    incomplete = {"price": 65000.5, "change_24h": -2.3}
    try:
        MarketData.from_collector_dict(incomplete)
        assert False, "aurait dû lever une exception"
    except (KeyError, ValueError):
        pass


def test_candle_series_resample_daily_to_weekly():
    # 8 bougies journalières couvrant un peu plus d'une semaine ISO
    candles = [
        make_candle(100 + i, 101 + i, 99 + i, 100.5 + i, ny_datetime(2026, 6, 1 + i))
        for i in range(8)
    ]
    series = CandleSeries(candles=candles)
    weekly = series.resample("weekly")

    # Doit produire au moins 1 bougie hebdomadaire, avec high/low agrégés
    assert len(weekly) >= 1
    assert weekly.candles[0].high >= max(c.high for c in candles[:7])


def test_build_multi_timeframe_candles():
    base = ny_datetime(2026, 1, 1)
    raw_ohlc = [
        [int((base.timestamp() + i * 4 * 3600) * 1000), 100 + i, 101 + i, 99 + i, 100.5 + i]
        for i in range(200)
    ]
    result = build_multi_timeframe_candles(raw_ohlc)

    assert set(result.keys()) == {"monthly", "weekly", "daily"}
    assert len(result["daily"]) > 0
    assert len(result["weekly"]) > 0
    # Monthly doit avoir moins (ou autant) de bougies que weekly, qui en a
    # moins (ou autant) que daily
    assert len(result["monthly"]) <= len(result["weekly"]) <= len(result["daily"])
