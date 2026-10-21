"""
tests/test_engine.py

Tests pour rules/engine.py::resolve_now() -- helper introduit en août 2026
pour permettre au futur backtest intraday d'injecter un horodatage simulé
dans EntryRule/TsqRule (True Day Open, sessions, liquidity pools) SANS
que ces règles n'appellent plus jamais datetime.now(NY_TZ) directement.

Avant ce correctif, rules/entry_rule.py et rules/tsq_rule.py appelaient
datetime.now(NY_TZ) en dur -- un rejeu historique aurait donc fui
l'horloge réelle du conteneur dans la détection de True Day Open et des
liquidity pools, rendant tout backtest intraday incorrect par
construction. context.simulated_now (MarketContext) est None en LIVE
(comportement inchangé, horloge réelle utilisée) -- réservé au backtest.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config.constants import NY_TZ
from models.market_data import MarketData
from rules.engine import MarketContext, resolve_now


def _make_context(simulated_now: datetime | None) -> MarketContext:
    return MarketContext(
        market_data=MarketData(
            price_usd=100, price_change_percentage_24h=0, volume_24h_usd=0, market_cap_usd=0
        ),
        simulated_now=simulated_now,
    )


def test_resolve_now_returns_simulated_now_when_provided():
    """Coeur du correctif backtest : simulated_now, jamais l'horloge réelle, doit être utilisé."""
    simulated = datetime(2020, 3, 15, 10, 30, tzinfo=timezone.utc)
    context = _make_context(simulated_now=simulated)

    result = resolve_now(context)

    assert result == simulated.astimezone(NY_TZ)
    # Bien loin de "maintenant" -- si le code retombait par erreur sur
    # l'horloge réelle, cette assertion échouerait (sauf coïncidence
    # extrêmement improbable).
    assert result.year == 2020


def test_resolve_now_converts_non_ny_timezone_to_ny():
    """simulated_now peut être fourni dans N'IMPORTE QUELLE tz -- doit toujours ressortir en NY_TZ."""
    simulated_utc = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)  # 00:00 NY (hiver, UTC-5)
    context = _make_context(simulated_now=simulated_utc)

    result = resolve_now(context)

    assert result.tzinfo is not None
    assert result.utcoffset() == simulated_utc.astimezone(NY_TZ).utcoffset()
    assert result.hour == 0


def test_resolve_now_falls_back_to_real_clock_when_none():
    """simulated_now=None (défaut, comportement LIVE) -- doit utiliser l'horloge réelle, pas planter."""
    context = _make_context(simulated_now=None)

    before = datetime.now(NY_TZ)
    result = resolve_now(context)
    after = datetime.now(NY_TZ)

    assert before <= result <= after


def test_market_context_simulated_now_defaults_to_none():
    """Défaut du champ lui-même : aucun appelant existant ne le fournissant, le live doit rester inchangé."""
    context = MarketContext(
        market_data=MarketData(
            price_usd=100, price_change_percentage_24h=0, volume_24h_usd=0, market_cap_usd=0
        )
    )
    assert context.simulated_now is None
