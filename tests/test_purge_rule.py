"""Tests pour rules/purge_rule.py : KillZone, timing jour de semaine, timing hebdomadaire."""

from __future__ import annotations

from rules.purge_rule import (
    DEFAULT_ASSET_CATEGORY,
    _check_daily_weekday_timing,
    _check_killzone,
    _check_weekly_timing,
    resolve_asset_category,
)
from tests.conftest import make_candle, ny_datetime


class TestResolveAssetCategory:
    """
    Depuis la simplification architecture crypto-only (août 2026, retrait
    d'IBKR/OKX/Binance-exécution et du multi-utilisateur),
    resolve_asset_category() ne dérive plus rien d'execution/
    ibkr_contracts.py::CONTRACT_SPECS (supprimé) -- elle retourne
    systématiquement "crypto", quel que soit le symbole.
    """

    def test_always_returns_crypto_regardless_of_symbol(self):
        assert resolve_asset_category("BTC") == "crypto"
        assert resolve_asset_category("EURUSD") == "crypto"
        assert resolve_asset_category("XAUUSD") == "crypto"
        assert resolve_asset_category("SOL") == "crypto"

    def test_none_symbol_defaults_to_crypto(self):
        assert resolve_asset_category(None) == "crypto"

    def test_matches_default_asset_category_constant(self):
        assert resolve_asset_category("BTC") == DEFAULT_ASSET_CATEGORY


def test_killzone_london_and_outside():
    moment_london = make_candle(100, 101, 99, 100, ny_datetime(2026, 6, 2, 3))
    in_kz, label = _check_killzone(moment_london)
    assert in_kz is True
    assert "Londres" in label

    moment_outside = make_candle(100, 101, 99, 100, ny_datetime(2026, 6, 2, 13))
    in_kz2, _ = _check_killzone(moment_outside)
    assert in_kz2 is False


def test_daily_weekday_timing_monday_to_tuesday():
    anchor_monday = make_candle(100, 101, 99, 100, ny_datetime(2026, 6, 1))
    purge_tuesday = make_candle(100, 101, 99, 100, ny_datetime(2026, 6, 2))

    ok, expected = _check_daily_weekday_timing(anchor_monday, purge_tuesday)
    assert ok is True
    assert 1 in expected  # Mardi = index 1


def test_daily_weekday_timing_unreferenced_anchor_day():
    anchor_tuesday = make_candle(100, 101, 99, 100, ny_datetime(2026, 6, 2))
    purge_wednesday = make_candle(100, 101, 99, 100, ny_datetime(2026, 6, 3))

    ok, expected = _check_daily_weekday_timing(anchor_tuesday, purge_wednesday)
    assert ok is False
    assert expected == []


def test_weekly_timing_week1_to_week2():
    anchor = make_candle(100, 101, 99, 100, ny_datetime(2026, 5, 4))
    purge_week2 = make_candle(100, 101, 99, 100, ny_datetime(2026, 5, 11))

    ok, gap_weeks = _check_weekly_timing(anchor, purge_week2)
    assert ok is True
    assert gap_weeks == 1

    purge_week3 = make_candle(100, 101, 99, 100, ny_datetime(2026, 5, 18))
    ok2, gap_weeks2 = _check_weekly_timing(anchor, purge_week3)
    assert ok2 is False
    assert gap_weeks2 == 2


def test_purge_rule_evaluate_uses_resolve_asset_category_with_symbol(monkeypatch):
    """
    Câblage : PurgeRule.evaluate() doit dériver asset_category via
    rules.purge_rule.resolve_asset_category(context.symbol) -- plus via
    un cfg.get("asset_category", ...) global (voir TestResolveAssetCategory
    et config/alerts.yaml, correctif août 2026).
    """
    from models.market_data import MarketData
    from rules.engine import MarketContext
    from rules.purge_rule import PurgeRule

    calls = []

    def fake_resolve_asset_category(symbol):
        calls.append(symbol)
        return "forex"

    monkeypatch.setattr("rules.purge_rule.resolve_asset_category", fake_resolve_asset_category)

    context = MarketContext(
        market_data=MarketData(
            price_usd=100, price_change_percentage_24h=0, volume_24h_usd=0, market_cap_usd=0
        ),
        symbol="EURUSD",
    )
    PurgeRule().evaluate(context, config={})

    assert calls == ["EURUSD"]
