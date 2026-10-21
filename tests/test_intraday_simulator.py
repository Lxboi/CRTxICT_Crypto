"""
tests/test_intraday_simulator.py

Tests de backtest/intraday_simulator.py -- se concentrent sur les
propriétés STRUCTURELLES du simulateur (anti-lookahead, déduplication
par setup_name, respect de step_every_n_m15, fenêtre glissante) plutôt
que sur la détection d'un vrai pattern CRT (déjà couvert par les tests
de rules/decision_engine.py) : DecisionEngine.decide() est monkeypatché
pour renvoyer des DecisionReport contrôlés, ce qui permet de vérifier
précisément CE QUE LE SIMULATEUR FAIT DE CE QUE LE MOTEUR LUI RENVOIE,
indépendamment de la logique interne du moteur lui-même.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import backtest.intraday_simulator as intraday_simulator
from backtest.intraday_simulator import (
    IntradaySimulatedEntry,
    _placeholder_candle,
    run_intraday_simulation,
)
from rules.decision_engine import DecisionReport, TimeframeSetup
from rules.refined_entry import RefinedEntry


def _ts_ms(year, month, day, hour=0, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def _daily_raw(start_day: int, count: int, month: int = 1, year: int = 2024) -> list[list]:
    return [
        [_ts_ms(year, month, start_day + i), 100.0, 101.0, 99.0, 100.5]
        for i in range(count)
    ]


def _m15_raw(days: int, bars_per_day: int, month: int = 1, year: int = 2024) -> list[list]:
    raw = []
    for day in range(days):
        for bar in range(bars_per_day):
            minute_of_day = bar * 15
            hour = minute_of_day // 60
            minute = minute_of_day % 60
            ts = _ts_ms(year, month, 1 + day, hour, minute)
            raw.append([ts, 100.0, 100.5, 99.5, 100.2])
    return raw


def _build_dataset():
    # 6 jours de Monthly/Weekly/Daily (suffisant pour passer le seuil de
    # 3 bougies Daily closes après quelques pas), et du m15 dense (24
    # bougies/jour -- pas besoin des 96 réelles pour ces tests structurels).
    raw_monthly = _daily_raw(1, 6)
    raw_weekly = _daily_raw(1, 6)
    raw_daily = _daily_raw(1, 6)
    raw_m15 = _m15_raw(days=6, bars_per_day=24)
    return raw_monthly, raw_weekly, raw_daily, raw_m15


def _empty_report() -> DecisionReport:
    return DecisionReport(timeframe_setups=[], priority_entry=None)


def _report_with_setup(setup_name: str, priority_source: str | None = None) -> DecisionReport:
    setup = TimeframeSetup(
        timeframe="daily",
        setup_name=setup_name,
        htf_crt={"bias": "bullish"},
        crt_targets={"stop_loss": 95.0, "tp_50": 102.0, "tp_100": 105.0, "tp_htf_dol": None},
        purge_checked=True,
        purge_high_probability=True,
        entry_recommended=True,
    )
    priority_entry = (
        RefinedEntry(source=priority_source, direction="bullish", entry_price=100.0, stop_loss=95.0)
        if priority_source
        else None
    )
    return DecisionReport(timeframe_setups=[setup], priority_entry=priority_entry)


class TestPlaceholderCandle:
    def test_ohlc_all_equal_to_price(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candle = _placeholder_candle(now, 123.45)
        assert candle.timestamp == now
        assert candle.open == candle.high == candle.low == candle.close == 123.45


class TestRunIntradaySimulationDedup:
    def test_same_setup_name_recorded_only_once(self, monkeypatch):
        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()

        def fake_decide(self, context):
            return _report_with_setup("Daily CRT (01/01)")

        monkeypatch.setattr(intraday_simulator.DecisionEngine, "decide", fake_decide)

        entries = run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
        )

        assert len(entries) == 1
        assert entries[0].setup_name == "Daily CRT (01/01)"
        assert entries[0].bias == "bullish"
        assert entries[0].stop_loss == 95.0
        assert entries[0].tp_50 == 102.0

    def test_new_setup_name_adds_a_second_entry(self, monkeypatch):
        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()
        call_count = {"n": 0}

        def fake_decide(self, context):
            call_count["n"] += 1
            # Bascule vers un second setup après quelques appels.
            name = "Setup A" if call_count["n"] < 20 else "Setup B"
            return _report_with_setup(name)

        monkeypatch.setattr(intraday_simulator.DecisionEngine, "decide", fake_decide)

        entries = run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
        )

        names = {e.setup_name for e in entries}
        assert names == {"Setup A", "Setup B"}

    def test_no_entry_recommended_yields_no_entries(self, monkeypatch):
        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()

        monkeypatch.setattr(
            intraday_simulator.DecisionEngine, "decide", lambda self, context: _empty_report()
        )

        entries = run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
        )
        assert entries == []

    def test_priority_entry_source_captured_informatively(self, monkeypatch):
        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()

        monkeypatch.setattr(
            intraday_simulator.DecisionEngine,
            "decide",
            lambda self, context: _report_with_setup("Setup A", priority_source="TSQ - CISD"),
        )

        entries = run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
        )
        assert entries[0].priority_entry_source == "TSQ - CISD"


class TestRunIntradaySimulationAntiLookahead:
    def test_no_future_candle_ever_visible_across_any_timeframe(self, monkeypatch):
        """
        À chaque appel de decide(), TOUTES les bougies visibles dans
        context.candles (une fois le placeholder final retiré via
        [:-1], comme le fait chaque règle en production) doivent avoir
        un timestamp STRICTEMENT antérieur à l'instant simulé
        (context.simulated_now) -- jamais de donnée future.
        """
        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()
        violations = []

        def fake_decide(self, context):
            now = context.simulated_now
            for timeframe, series in context.candles.items():
                closed = series.candles[:-1]
                for c in closed:
                    if c.timestamp >= now:
                        violations.append((timeframe, c.timestamp, now))
                # Le dernier élément (placeholder) doit être à "now" pile,
                # jamais dans le futur non plus.
                if series.candles and series.candles[-1].timestamp > now:
                    violations.append((f"{timeframe}-placeholder", series.candles[-1].timestamp, now))
            return _empty_report()

        monkeypatch.setattr(intraday_simulator.DecisionEngine, "decide", fake_decide)

        run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
        )

        assert violations == []

    def test_simulated_now_advances_monotonically(self, monkeypatch):
        seen_now: list[datetime] = []

        def fake_decide(self, context):
            seen_now.append(context.simulated_now)
            return _empty_report()

        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()
        monkeypatch.setattr(intraday_simulator.DecisionEngine, "decide", fake_decide)

        run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
        )

        assert len(seen_now) > 1
        assert seen_now == sorted(seen_now)
        assert len(set(seen_now)) == len(seen_now)  # jamais deux pas au même instant


class TestStepEveryNM15:
    def test_step_every_n_m15_reduces_call_count(self, monkeypatch):
        raw_monthly, raw_weekly, raw_daily, raw_m15 = _build_dataset()
        counts = {"every_1": 0, "every_4": 0}

        def make_counter(key):
            def fake_decide(self, context):
                counts[key] += 1
                return _empty_report()
            return fake_decide

        monkeypatch.setattr(intraday_simulator.DecisionEngine, "decide", make_counter("every_1"))
        run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
            step_every_n_m15=1,
        )

        monkeypatch.setattr(intraday_simulator.DecisionEngine, "decide", make_counter("every_4"))
        run_intraday_simulation(
            raw_monthly, raw_weekly, raw_daily, raw_m15, "BTC", years_to_test=1, decision_config={},
            step_every_n_m15=4,
        )

        assert counts["every_1"] > counts["every_4"]
        # Environ un facteur 4 (à la marge d'arrondi du seuil de warmup près).
        assert counts["every_4"] <= counts["every_1"] / 2


class TestRunIntradaySimulationEmptyInput:
    def test_empty_m15_returns_empty_list(self):
        entries = run_intraday_simulation([], [], [], [], "BTC", years_to_test=1, decision_config={})
        assert entries == []

    def test_empty_daily_returns_empty_list(self):
        _, _, _, raw_m15 = _build_dataset()
        entries = run_intraday_simulation([], [], [], raw_m15, "BTC", years_to_test=1, decision_config={})
        assert entries == []
