"""tests/test_realistic_backtest.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.intraday_simulator import IntradaySimulatedEntry
from backtest.realistic_backtest import run_realistic_backtest
from tests.conftest import make_candle


def _entry(
    setup_name="Setup",
    entry_timestamp=None,
    bias="bullish",
    entry_price=100.0,
    stop_loss=95.0,
    tp_50=105.0,
    tp_100=110.0,
    tp_htf_dol=None,
) -> IntradaySimulatedEntry:
    return IntradaySimulatedEntry(
        setup_name=setup_name,
        entry_timestamp=entry_timestamp or datetime(2024, 1, 1, tzinfo=timezone.utc),
        bias=bias,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp_50=tp_50,
        tp_100=tp_100,
        tp_htf_dol=tp_htf_dol,
    )


def _m15_series(start: datetime, ohlc_list: list[tuple[float, float, float, float]]):
    return [
        make_candle(o, h, l, c, start + timedelta(minutes=15 * i))
        for i, (o, h, l, c) in enumerate(ohlc_list)
    ]


DEFAULT_KWARGS = dict(
    starting_capital=10_000.0,
    risk_pct=2.0,
    max_leverage=10.0,
    fee_pct=0.05,
    slippage_bps=5.0,
    reserved_balance_pct=0.0,
    min_risk_reward_ratio=None,
)


class TestExecutedWinTrade:
    def test_win_trade_net_r_reduced_by_costs_and_capital_grows(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(
            start,
            [
                (100, 106, 100, 105.5),  # touche TP1
                (105.5, 111, 105, 110.5),  # touche TP2 (dernier niveau -> WIN)
            ],
        )

        [result] = run_realistic_backtest(
            [entry], candles, **{**DEFAULT_KWARGS, "starting_capital": 10_000.0}
        )

        assert result.rejected is False
        assert result.outcome == "WIN_TP2"
        assert result.r_multiple == pytest.approx(2.0)  # (110-100)/(100-95)

        expected_cost_each = (100.0 + 110.0) * 0.0005 / 5.0
        assert result.fees_paid_r == pytest.approx(expected_cost_each)
        assert result.slippage_cost_r == pytest.approx(expected_cost_each)

        expected_net_r = 2.0 - 2 * expected_cost_each
        assert result.net_r_multiple == pytest.approx(expected_net_r)

        risk_amount = 10_000.0 * 2.0 / 100
        expected_capital_after = 10_000.0 + expected_net_r * risk_amount
        assert result.capital_after == pytest.approx(expected_capital_after)
        assert result.capital_before == pytest.approx(10_000.0)
        # Coûts réduisent forcément le R net par rapport au brut.
        assert result.net_r_multiple < result.r_multiple


class TestExecutedLossTrade:
    def test_loss_trade_costs_shrink_capital_further(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        candles = _m15_series(start, [(100, 100.5, 94, 94.5)])  # touche direct le SL

        [result] = run_realistic_backtest([entry], candles, **DEFAULT_KWARGS)

        assert result.rejected is False
        assert result.outcome == "LOSS"
        assert result.r_multiple == pytest.approx(-1.0)
        # Sur une perte, les coûts creusent encore le résultat net (plus négatif que le brut).
        assert result.net_r_multiple < result.r_multiple
        assert result.capital_after < result.capital_before


class TestOpenTradeDoesNotAffectCapital:
    def test_open_trade_leaves_capital_and_costs_untouched(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        candles = _m15_series(start, [(100, 100.2, 99.8, 100.1)])  # ne touche rien

        [result] = run_realistic_backtest([entry], candles, **DEFAULT_KWARGS)

        assert result.rejected is False
        assert result.outcome == "OPEN"
        assert result.r_multiple is None
        assert result.net_r_multiple is None
        assert result.fees_paid_r is None
        assert result.slippage_cost_r is None
        assert result.capital_after == result.capital_before == pytest.approx(10_000.0)


class TestRejectedFinalLevelAlreadyHit:
    def test_entry_price_already_beyond_only_target_is_rejected(self):
        """
        Réplique database/db.py::save_alert_if_new::initial_highest_tp_hit
        -- une entrée au marché prise exactement au niveau (ou au-delà)
        du dernier objectif disponible n'a plus aucune opportunité réelle.
        """
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, bias="bullish", entry_price=110.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(start, [(110, 111, 109, 110)])

        [result] = run_realistic_backtest([entry], candles, **DEFAULT_KWARGS)

        assert result.rejected is True
        assert result.rejected_reason == "final_level_already_hit"
        assert result.outcome is None
        assert result.capital_after == result.capital_before


class TestRejectedRrTooLow:
    def test_rr_below_configured_minimum_is_rejected(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # rr = (110-100)/(100-95) = 2.0
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(start, [(100, 106, 100, 105.5), (105.5, 111, 105, 110.5)])

        [result] = run_realistic_backtest(
            [entry], candles, **{**DEFAULT_KWARGS, "min_risk_reward_ratio": 5.0}
        )

        assert result.rejected is True
        assert result.rejected_reason.startswith("rr_too_low")
        assert result.capital_after == result.capital_before

    def test_rr_above_configured_minimum_is_not_rejected(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(start, [(100, 106, 100, 105.5), (105.5, 111, 105, 110.5)])

        [result] = run_realistic_backtest(
            [entry], candles, **{**DEFAULT_KWARGS, "min_risk_reward_ratio": 1.5}
        )

        assert result.rejected is False


class TestRejectedLeverageExceeded:
    def test_position_requiring_too_much_leverage_is_rejected(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Distance au SL minuscule -> quantité énorme pour risquer 50% du capital -> levier explosif.
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=99.9, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(start, [(100, 106, 100, 105.5), (105.5, 111, 105, 110.5)])

        [result] = run_realistic_backtest(
            [entry], candles,
            **{**DEFAULT_KWARGS, "starting_capital": 100.0, "risk_pct": 50.0, "max_leverage": 10.0},
        )

        assert result.rejected is True
        assert result.rejected_reason.startswith("leverage_or_sizing")
        assert result.capital_after == result.capital_before == pytest.approx(100.0)


class TestRejectedNoTarget:
    def test_entry_without_any_tp_level_is_rejected(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, tp_50=None, tp_100=None, tp_htf_dol=None)
        candles = _m15_series(start, [(100, 106, 94, 100)])

        [result] = run_realistic_backtest([entry], candles, **DEFAULT_KWARGS)

        assert result.rejected is True
        assert result.rejected_reason == "no_target"


class TestCapitalEvolvesSequentially:
    def test_second_trade_is_sized_against_capital_after_first(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        win_entry = _entry(
            setup_name="A", entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0,
        )
        second_start = start + timedelta(days=1)
        loss_entry = _entry(
            setup_name="B", entry_timestamp=second_start, entry_price=200.0, stop_loss=190.0, tp_50=210.0, tp_100=None,
        )

        win_candles = _m15_series(start, [(100, 106, 100, 105.5), (105.5, 111, 105, 110.5)])
        loss_candles = _m15_series(second_start, [(200, 201, 189, 189.5)])  # touche direct le SL

        results = run_realistic_backtest([win_entry, loss_entry], win_candles + loss_candles, **DEFAULT_KWARGS)

        assert len(results) == 2
        win_result, loss_result = results
        assert win_result.capital_after > 10_000.0
        # Le second trade est sizé contre le capital APRÈS le premier, pas le capital de départ.
        assert loss_result.capital_before == pytest.approx(win_result.capital_after)
        assert loss_result.capital_after < loss_result.capital_before


class TestOutputOrderMatchesInputOrder:
    def test_one_result_per_entry_same_order_including_rejections(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        rejected_entry = _entry(setup_name="rejected", entry_timestamp=start, tp_50=None, tp_100=None, tp_htf_dol=None)
        ok_entry = _entry(setup_name="ok", entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        candles = _m15_series(start, [(100, 106, 100, 105.5)])

        results = run_realistic_backtest([rejected_entry, ok_entry], candles, **DEFAULT_KWARGS)

        assert [r.setup_name for r in results] == ["rejected", "ok"]
        assert results[0].rejected is True
        assert results[1].rejected is False
