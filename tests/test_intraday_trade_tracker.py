"""tests/test_intraday_trade_tracker.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backtest.intraday_simulator import IntradaySimulatedEntry
from backtest.intraday_trade_tracker import classify_intraday_trades
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


class TestClassifyIntradayTradesWin:
    def test_tp1_touched_marks_win(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, bias="bullish", entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        candles = _m15_series(start, [(100, 101, 99, 100.5), (100.5, 106, 100, 105.5)])

        outcomes = classify_intraday_trades([entry], candles)

        assert len(outcomes) == 1
        assert outcomes[0].outcome == "WIN_TP1"
        assert outcomes[0].highest_tp_reached == 1
        assert outcomes[0].r_multiple == 1.0  # (105-100)/(100-95)

    def test_progresses_through_tp1_then_tp2(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(
            start,
            [
                (100, 101, 99, 100.5),
                (100.5, 106, 100, 105.5),  # touche TP1
                (105.5, 111, 105, 110.5),  # touche TP2 (dernier niveau -> WIN)
            ],
        )

        outcomes = classify_intraday_trades([entry], candles)
        assert outcomes[0].outcome == "WIN_TP2"
        assert outcomes[0].highest_tp_reached == 2


class TestClassifyIntradayTradesLoss:
    def test_sl_touched_before_any_tp_marks_loss(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0)
        candles = _m15_series(start, [(100, 100.5, 94, 94.5)])  # touche direct le SL

        outcomes = classify_intraday_trades([entry], candles)
        assert outcomes[0].outcome == "LOSS"
        assert outcomes[0].r_multiple == -1.0


class TestClassifyIntradayTradesBreakEven:
    def test_tp1_then_return_to_entry_marks_break_even(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=110.0)
        candles = _m15_series(
            start,
            [
                (100, 106, 100, 105.5),  # touche TP1 -> SL déplacé à 100 (entry_price)
                (105.5, 106, 99, 99.5),  # revient toucher le nouveau SL (100) avant TP2
            ],
        )

        outcomes = classify_intraday_trades([entry], candles)
        assert outcomes[0].outcome == "BREAK_EVEN"
        assert outcomes[0].r_multiple == 0.0


class TestClassifyIntradayTradesOpen:
    def test_no_level_touched_marks_open(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0)
        candles = _m15_series(start, [(100, 100.2, 99.8, 100.1)])

        outcomes = classify_intraday_trades([entry], candles)
        assert outcomes[0].outcome == "OPEN"
        assert outcomes[0].r_multiple is None

    def test_no_target_available_yields_no_outcome(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, tp_50=None, tp_100=None, tp_htf_dol=None)
        candles = _m15_series(start, [(100, 106, 94, 100)])

        outcomes = classify_intraday_trades([entry], candles)
        assert outcomes == []


class TestClassifyIntradayTradesAmbiguity:
    def test_same_candle_tp_and_sl_resolves_optimistically_to_tp(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        # Une seule bougie touche à la fois le SL (low<=95) et le TP (high>=105).
        candles = _m15_series(start, [(100, 106, 94, 100)])

        outcomes = classify_intraday_trades([entry], candles)
        assert outcomes[0].outcome == "WIN_TP1"


class TestClassifyIntradayTradesOrderingAndBisect:
    def test_candles_before_entry_timestamp_are_ignored(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        # Une bougie AVANT l'entrée qui touche le SL ne doit jamais compter.
        before = make_candle(100, 100.5, 90, 100, start - timedelta(minutes=15))
        after_ok = make_candle(100, 106, 100, 105.5, start)

        outcomes = classify_intraday_trades([entry], [before, after_ok])
        assert outcomes[0].outcome == "WIN_TP1"

    def test_unsorted_input_candles_still_handled_correctly(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = _entry(entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        c1 = make_candle(100, 101, 99, 100.5, start)
        c2 = make_candle(100.5, 106, 100, 105.5, start + timedelta(minutes=15))
        # Fournies dans le désordre -- classify_intraday_trades doit trier en interne.
        outcomes = classify_intraday_trades([entry], [c2, c1])
        assert outcomes[0].outcome == "WIN_TP1"

    def test_multiple_entries_each_tracked_independently(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        win_entry = _entry(setup_name="A", entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        loss_entry = _entry(setup_name="B", entry_timestamp=start, entry_price=100.0, stop_loss=95.0, tp_50=105.0, tp_100=None)
        candles = _m15_series(start, [(100, 106, 100, 105.5)])  # touche TP pour les deux

        outcomes = classify_intraday_trades([win_entry, loss_entry], candles)
        assert {o.setup_name for o in outcomes} == {"A", "B"}
        assert all(o.outcome == "WIN_TP1" for o in outcomes)
