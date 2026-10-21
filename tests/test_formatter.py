"""Tests pour messaging/formatter.py (aucune IA — analyse déterministe uniquement)."""

from __future__ import annotations

from rules.decision_engine import DecisionReport, TimeframeSetup
from messaging.formatter import format_telegram_message


def _sample_report() -> DecisionReport:
    setup = TimeframeSetup(
        timeframe="daily",
        setup_name="BTC Daily CRT (20/05)",
        htf_crt={"bias": "bearish", "dol_price": 90000.0, "purge_side": "high"},
        crt_targets={"tp_50": 92500.0, "tp_100": 90000.0, "tp_htf_dol": 85000.0, "stop_loss": 93500.0},
        entry_recommended=True,
    )
    return DecisionReport(
        reasoning_log=["DOL Monthly bearish", "CRT Daily confirmé"],
        effective_bias="bearish",
        confidence="high",
        entry_recommended=True,
        timeframe_setups=[setup],
    )


def test_format_telegram_message_includes_exact_tps():
    report = _sample_report()
    message = format_telegram_message(report, current_price=91500, symbol="BTC")

    assert "92,500.00" in message
    assert "90,000.00" in message
    assert "85,000.00" in message
    assert "93,500.00" in message  # SL
    assert "SHORT" in message


def test_format_telegram_message_no_ai_section():
    """Confirme qu'aucune trace du raisonnement brut (report.to_text()) n'apparaît."""
    report = _sample_report()
    message = format_telegram_message(report, current_price=91500, symbol="BTC")

    assert "=== Analyse du marché ===" not in message


def test_format_telegram_message_shows_market_alerts_when_present():
    report = _sample_report()
    report.high_impact_news = {
        "available": True,
        "high_impact_today": ["CPI"],
        "currently_in_avoid_window": True,
    }
    report.holiday = {"us_holiday": "Independence Day", "france_holiday": None}

    message = format_telegram_message(report, current_price=91500, symbol="BTC")

    assert "CPI" in message
    assert "Independence Day" in message


def test_format_telegram_message_no_market_alerts_when_nothing_to_report():
    report = _sample_report()
    message = format_telegram_message(report, current_price=91500, symbol="BTC")

    assert "⚠️ News" not in message
    assert "⚠️ Jour férié" not in message
    assert "⚠️ Journée blanche" not in message
