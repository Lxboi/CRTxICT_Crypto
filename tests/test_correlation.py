"""Tests pour rules/correlation.py : détection de corrélation entre actifs."""

from __future__ import annotations

import random

from rules.correlation import (
    build_correlated_pair_map,
    find_correlated_pairs,
    pearson_correlation,
)
from tests.conftest import make_candle, ny_datetime


def test_pearson_correlation_perfect_positive_and_negative():
    assert abs(pearson_correlation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) - 1.0) < 1e-9
    assert abs(pearson_correlation([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) - (-1.0)) < 1e-9


def test_pearson_correlation_constant_series_returns_zero():
    assert pearson_correlation([5, 5, 5, 5], [1, 2, 3, 4]) == 0.0


def test_pearson_correlation_raises_on_length_mismatch():
    try:
        pearson_correlation([1, 2, 3], [1, 2])
        assert False, "aurait dû lever ValueError"
    except ValueError:
        pass


def test_find_correlated_pairs_detects_correlated_and_excludes_random():
    random.seed(42)

    from models.market_data import Candle, CandleSeries

    def make_real_series(prices):
        return {
            "daily": CandleSeries(
                candles=[
                    Candle(
                        timestamp=ny_datetime(2026, 1, 1 + (i % 27)),
                        open=p,
                        high=p * 1.01,
                        low=p * 0.99,
                        close=p,
                    )
                    for i, p in enumerate(prices)
                ]
            )
        }

    btc_prices = [90000 + i * 500 + random.uniform(-100, 100) for i in range(40)]
    correlated_prices = [3000 + i * 20 + random.uniform(-5, 5) for i in range(40)]
    random_prices = [100 + random.uniform(-50, 50) for _ in range(40)]

    candles_by_symbol = {
        "BTC": make_real_series(btc_prices),
        "CORRELATED": make_real_series(correlated_prices),
        "RANDOM": make_real_series(random_prices),
    }

    results = find_correlated_pairs(candles_by_symbol, threshold=0.7)
    pairs_found = {(r.symbol_a, r.symbol_b) for r in results}

    assert ("BTC", "CORRELATED") in pairs_found
    assert not any("RANDOM" in (r.symbol_a, r.symbol_b) for r in results)


def test_build_correlated_pair_map_keeps_strongest_match():
    from rules.correlation import CorrelationResult

    results = [
        CorrelationResult(symbol_a="BTC", symbol_b="ETH", correlation=0.75, sample_size=30),
        CorrelationResult(symbol_a="BTC", symbol_b="SPY", correlation=0.95, sample_size=30),
    ]
    pair_map = build_correlated_pair_map(results)

    # BTC apparaît dans 2 paires -> seule la plus forte (SPY, 0.95) est retenue
    assert pair_map["BTC"] == "SPY"
    assert pair_map["ETH"] == "BTC"
    assert pair_map["SPY"] == "BTC"
