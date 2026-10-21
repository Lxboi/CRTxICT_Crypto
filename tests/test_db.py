"""Tests pour database/db.py : historique, snapshots, déduplication."""

from __future__ import annotations

import os

import pytest

import database.db as db
from models.market_data import MarketData
from rules.decision_engine import DecisionReport, TimeframeSetup


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Utilise une base SQLite temporaire, isolée pour chaque test."""
    test_db_path = tmp_path / "test_alerts.db"
    monkeypatch.setattr(db, "DB_PATH", test_db_path)
    db.init_db()
    yield
    if test_db_path.exists():
        os.remove(test_db_path)


def test_save_market_snapshot():
    market_data = MarketData(
        price_usd=95000, price_change_percentage_24h=-1.5, volume_24h_usd=1e9, market_cap_usd=1.8e12
    )
    db.save_market_snapshot(market_data)  # ne doit pas lever d'exception


def test_save_decision_and_get_recent():
    setup = TimeframeSetup(timeframe="daily", setup_name="BTC Daily CRT (20/05)")
    report = DecisionReport(reasoning_log=["test"], effective_bias=None, confidence="low")

    decision_id = db.save_decision(report, price_at_decision=95000)
    assert decision_id > 0

    recent = db.get_recent_decisions(limit=5)
    assert len(recent) == 1
    assert recent[0]["id"] == decision_id


def test_alert_deduplication():
    setup = TimeframeSetup(
        timeframe="daily",
        setup_name="BTC Daily CRT (20/05)",
        htf_crt={"bias": "bearish", "dol_price": 90000.0, "purge_side": "high"},
        crt_targets={"tp_50": 92500.0, "tp_100": 90000.0, "tp_htf_dol": None},
        entry_recommended=True,
    )
    report = DecisionReport(
        reasoning_log=["test"], effective_bias="bearish", confidence="high",
        entry_recommended=True, timeframe_setups=[setup],
    )

    decision_id_1 = db.save_decision(report, price_at_decision=95000)
    result_1 = db.save_alert_if_new(decision_id_1, setup, "high", current_price=95000)
    assert result_1 is not None
    dedup_key_1, initial_highest_tp_hit_1 = result_1
    assert dedup_key_1 is not None
    assert initial_highest_tp_hit_1 == 0  # aucun TP dépassé à la création dans ce scénario

    # Même setup au cycle suivant -> pas de nouvelle alerte
    decision_id_2 = db.save_decision(report, price_at_decision=94950)
    result_2 = db.save_alert_if_new(decision_id_2, setup, "high", current_price=94950)
    assert result_2 is None

    alerts = db.get_recent_alerts(limit=10)
    assert len(alerts) == 1
