"""Tests pour rules/keylevel_rule.py : FVG, Old H/L, Order Block, Breaker, Rejection."""

from __future__ import annotations

from rules.keylevel_rule import (
    _detect_fvgs,
    _detect_old_highs_lows,
    _detect_order_blocks,
    _detect_breaker_blocks,
    _detect_rejection_blocks,
)
from tests.conftest import make_candle, ny_datetime


def test_detect_fvg_bullish():
    candles = [
        make_candle(100, 102, 99, 101, ny_datetime(2026, 6, 1)),
        make_candle(101, 108, 100, 107, ny_datetime(2026, 6, 2)),
        make_candle(107, 112, 106, 110, ny_datetime(2026, 6, 3)),
    ]
    fvgs = _detect_fvgs(candles)

    assert len(fvgs) == 1
    assert fvgs[0].direction == "bullish"
    assert fvgs[0].zone_low == 102
    assert fvgs[0].zone_high == 106


def test_detect_old_high_excluded_once_swept():
    candles = (
        [make_candle(100, 103, 99, 101, ny_datetime(2026, 6, 1 + i)) for i in range(2)]
        + [make_candle(101, 110, 100, 105, ny_datetime(2026, 6, 3))]
        + [make_candle(105, 104, 98, 100, ny_datetime(2026, 6, 4))]
        + [make_candle(100, 102, 97, 99, ny_datetime(2026, 6, 5))]
    )
    zones_before_sweep = _detect_old_highs_lows(candles, lookback=2)
    assert any(z.kind == "old_high" and z.zone_low == 110 for z in zones_before_sweep)

    candles_swept = candles + [make_candle(99, 115, 98, 112, ny_datetime(2026, 6, 6))]
    zones_after_sweep = _detect_old_highs_lows(candles_swept, lookback=2)
    assert not any(z.kind == "old_high" and z.zone_low == 110 for z in zones_after_sweep)


def test_detect_order_block_and_breaker():
    candles = [
        make_candle(100, 101, 99, 100.5, ny_datetime(2026, 6, 1)),
        make_candle(100.5, 102, 100, 101.5, ny_datetime(2026, 6, 2)),
        make_candle(101.5, 106, 101, 105, ny_datetime(2026, 6, 3)),  # swing high = 106
        make_candle(105, 104, 100, 101, ny_datetime(2026, 6, 4)),
        make_candle(101, 103, 99, 100, ny_datetime(2026, 6, 5)),
        make_candle(100, 101, 98, 99, ny_datetime(2026, 6, 6)),  # future OB (baissière)
        make_candle(99, 115, 98.5, 114, ny_datetime(2026, 6, 7)),  # displacement haussier
        make_candle(114, 116, 113, 115, ny_datetime(2026, 6, 8)),
        make_candle(115, 117, 114, 116, ny_datetime(2026, 6, 9)),
    ]
    order_blocks = _detect_order_blocks(candles, lookback=2, displacement_multiplier=1.5)

    assert any(z.direction == "bullish" and z.zone_low == 98 for z in order_blocks)

    # Cassure de l'OB vers le bas -> devient un breaker baissier
    candles_broken = candles + [make_candle(116, 116.5, 90, 95, ny_datetime(2026, 6, 10))]
    order_blocks_2 = _detect_order_blocks(candles_broken, lookback=2, displacement_multiplier=1.5)
    breakers = _detect_breaker_blocks(order_blocks_2, candles_broken)

    assert any(z.kind == "breaker_block" and z.direction == "bearish" for z in breakers)


def test_detect_rejection_block():
    candles = [
        make_candle(100, 101, 99, 100.5, ny_datetime(2026, 6, 1)),
        make_candle(100.5, 102, 100, 101, ny_datetime(2026, 6, 2)),
        make_candle(101, 112, 100.5, 102, ny_datetime(2026, 6, 3)),  # mèche haute dominante
        make_candle(102, 103, 98, 99, ny_datetime(2026, 6, 4)),
        make_candle(99, 100, 97, 98.5, ny_datetime(2026, 6, 5)),
    ]
    rejections = _detect_rejection_blocks(candles, lookback=2, wick_ratio_threshold=0.5)

    assert any(z.kind == "rejection_block" and z.direction == "bearish" for z in rejections)
