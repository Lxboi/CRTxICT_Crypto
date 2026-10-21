"""
tests/test_tsq_rule.py

Vérifie uniquement le câblage du correctif "simulated_now" (août 2026,
voir tests/test_engine.py) dans TsqRule.evaluate() -- PAS la logique
métier complète de la séquence TSQ (Turtle Soup -> CISD -> OTE ->
Continuation -> KOD), qui n'a pas de suite de tests dédiée à ce jour et
sort du périmètre de ce correctif.

Même régression que rules/entry_rule.py (voir tests/test_entry_rule.py) :
TsqRule.evaluate() appelait datetime.now(NY_TZ) en dur.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models.market_data import CandleSeries, MarketData
from rules.engine import MarketContext
from rules.tsq_rule import TsqRule
from tests.conftest import make_candle, ny_datetime


def _build_context(simulated_now=None) -> MarketContext:
    start = ny_datetime(2026, 6, 1, 0)
    intraday = [
        make_candle(100, 101, 99, 100, start + timedelta(hours=i)) for i in range(22)
    ]
    return MarketContext(
        market_data=MarketData(
            price_usd=100, price_change_percentage_24h=0, volume_24h_usd=0, market_cap_usd=0
        ),
        candles={"intraday": CandleSeries(candles=intraday)},
        simulated_now=simulated_now,
    )


def test_evaluate_uses_resolve_now_with_the_given_context(monkeypatch):
    """
    TsqRule.evaluate() doit dériver "aujourd'hui" via rules.engine.
    resolve_now(context) -- jamais via un appel direct à datetime.now().
    """
    calls: list[MarketContext] = []

    def fake_resolve_now(context: MarketContext) -> datetime:
        calls.append(context)
        return datetime(2026, 6, 3, 10, tzinfo=timezone.utc)

    monkeypatch.setattr("rules.tsq_rule.resolve_now", fake_resolve_now)

    context = _build_context()
    TsqRule().evaluate(context, config={})

    assert len(calls) == 1
    assert calls[0] is context


def test_evaluate_does_not_crash_with_simulated_now_far_in_the_past():
    """Sanity check bout-en-bout : un simulated_now éloigné ne doit produire aucune erreur."""
    simulated_now = datetime(2020, 1, 15, 10, tzinfo=timezone.utc)
    context = _build_context(simulated_now=simulated_now)

    result = TsqRule().evaluate(context, config={})

    assert result.rule_name == "tsq_rule"
