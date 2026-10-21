"""tests/test_execution_costs.py"""

from __future__ import annotations

import pytest

from backtest.execution_costs import (
    DEFAULT_FEE_RATES_PCT,
    compute_costs_in_r,
    resolve_default_fee_pct,
)


class TestResolveDefaultFeePct:
    def test_known_exchange_returns_configured_rate(self):
        assert resolve_default_fee_pct("kraken") == DEFAULT_FEE_RATES_PCT["kraken"]
        assert resolve_default_fee_pct("binance") == DEFAULT_FEE_RATES_PCT["binance"]
        assert resolve_default_fee_pct("okx") == DEFAULT_FEE_RATES_PCT["okx"]

    def test_case_insensitive(self):
        assert resolve_default_fee_pct("KRAKEN") == DEFAULT_FEE_RATES_PCT["kraken"]

    def test_ibkr_has_no_default_commission_per_contract_not_pct(self):
        """IBKR facture par contrat, structurellement différent d'un %% du notionnel -- voir docstring module."""
        assert resolve_default_fee_pct("ibkr") is None

    def test_unknown_exchange_returns_none(self):
        assert resolve_default_fee_pct("some_new_exchange") is None

    def test_none_symbol_returns_none(self):
        assert resolve_default_fee_pct(None) is None


class TestComputeCostsInR:
    def test_basic_round_trip_costs(self):
        # entry=100, exit=110, initial_risk=5, fee=0.05%, slippage=5bps (0.05%) -- même taux ici.
        fees_r, slippage_r = compute_costs_in_r(100.0, 110.0, 5.0, 0.05, 5.0)

        expected = (100.0 + 110.0) * 0.0005 / 5.0
        assert fees_r == pytest.approx(expected)
        assert slippage_r == pytest.approx(expected)

    def test_costs_scale_with_fee_and_slippage_rate(self):
        fees_r, slippage_r = compute_costs_in_r(100.0, 100.0, 5.0, 0.10, 10.0)
        # entry==exit==100 -> price_sum=200
        assert fees_r == pytest.approx(200.0 * 0.001 / 5.0)
        assert slippage_r == pytest.approx(200.0 * 0.001 / 5.0)

    def test_zero_initial_risk_returns_zero_costs_instead_of_dividing_by_zero(self):
        assert compute_costs_in_r(100.0, 110.0, 0.0, 0.05, 5.0) == (0.0, 0.0)

    def test_negative_initial_risk_returns_zero_costs(self):
        assert compute_costs_in_r(100.0, 110.0, -1.0, 0.05, 5.0) == (0.0, 0.0)

    def test_costs_are_always_positive(self):
        fees_r, slippage_r = compute_costs_in_r(50.0, 45.0, 2.0, 0.05, 5.0)
        assert fees_r > 0
        assert slippage_r > 0
