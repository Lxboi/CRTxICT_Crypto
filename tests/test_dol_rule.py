"""Tests pour rules/dol_rule.py : Bias, Double Purge, agrégation multi-mécanismes."""

from __future__ import annotations

from models.market_data import CandleSeries, MarketData
from rules.dol_rule import DolRule, _detect_bias, _detect_double_purge
from rules.engine import MarketContext
from tests.conftest import make_candle, ny_datetime


def test_detect_bias_bearish_and_invalidation():
    candles = [
        make_candle(100, 102, 98, 101, ny_datetime(2026, 6, 1)),
        make_candle(101, 102, 97, 96, ny_datetime(2026, 6, 2)),  # close(96) < low_prev(98)
    ]

    bias = _detect_bias(candles)
    assert bias is not None
    assert bias.bias == "bearish"
    assert bias.dol_price == 98

    # Une bougie ULTÉRIEURE CLÔTURÉE revient sur la DOL -> plus de momentum valide
    # (jamais le prix courant en temps réel -- voir docstring de _detect_bias)
    candles_invalidated = candles + [make_candle(96, 99, 95, 99, ny_datetime(2026, 6, 3))]  # close(99) >= dol(98)
    assert _detect_bias(candles_invalidated) is None


def test_detect_bias_ignores_current_price_waits_for_confirmed_close():
    """
    Reproduit un cas réel observé (TRX, juillet 2026) : le prix COURANT
    (bougie en cours de formation, PAS encore clôturée) dépasse
    temporairement le niveau cassé -- ne doit PAS invalider le biais.
    Seule une bougie CLÔTURÉE ultérieure compte (voir DolRule.evaluate()
    ::closed_candles = series.candles[:-1], qui exclut déjà la bougie
    en cours avant même d'appeler _detect_bias).
    """
    closed_candles = [
        make_candle(0.286490, 0.291120, 0.266450, 0.281640, ny_datetime(2026, 2, 1)),
        make_candle(0.281640, 0.324470, 0.278680, 0.313150, ny_datetime(2026, 3, 1)),
        make_candle(0.313150, 0.336590, 0.312230, 0.326490, ny_datetime(2026, 4, 1)),
        make_candle(0.326480, 0.377160, 0.325090, 0.351250, ny_datetime(2026, 5, 1)),
        make_candle(0.351250, 0.352510, 0.308420, 0.315010, ny_datetime(2026, 6, 1)),  # close(0.315010) < low_mai(0.325090) -> bearish
    ]

    # Juillet (en cours, PAS clôturé) n'est jamais passé à _detect_bias
    # -- le biais bearish de juin doit rester actif, peu importe où le
    # prix courant se trouve actuellement.
    result = _detect_bias(closed_candles)
    assert result is not None
    assert result.bias == "bearish"
    assert result.dol_price == 0.325090

    # Si juillet CLÔTURE effectivement au-dessus du niveau de mai (une
    # fois le mois terminé, cette bougie rejoint closed_candles) -> la
    # cassure de juin est ALORS invalidée. Juin clôturant bas (0.315010)
    # invalide EN CASCADE aussi les cassures bullish d'avril et de mai
    # (toutes deux au-dessus de 0.315010) -> la recherche remonte
    # jusqu'à mars/février (0.291120), jamais contredite depuis.
    closed_candles_with_july_closed = closed_candles + [
        make_candle(0.315020, 0.333490, 0.313850, 0.326040, ny_datetime(2026, 7, 1))
    ]
    result_after_july_closes = _detect_bias(closed_candles_with_july_closed)
    assert result_after_july_closes is not None
    assert result_after_july_closes.bias == "bullish"
    assert result_after_july_closes.dol_price == 0.291120


def test_detect_bias_returns_none_when_all_history_invalidated():
    """Si TOUTES les cassures de l'historique disponible sont invalidées par des clôtures, aucun biais -- pas de plantage."""
    candles = [
        make_candle(100, 102, 98, 101, ny_datetime(2026, 6, 1)),
        make_candle(101, 102, 97, 96, ny_datetime(2026, 6, 2)),  # close(96) < low_prev(98) -> bearish, dol=98
        make_candle(96, 101, 96, 100, ny_datetime(2026, 6, 3)),  # close(100) : ni <97 ni >102 (pas de nouvelle cassure), mais >=98 -> invalide la precedente
    ]
    assert _detect_bias(candles) is None

def test_detect_double_purge_single_candle():
    candles = [
        make_candle(100, 105, 95, 102, ny_datetime(2026, 6, 1)),
        make_candle(102, 110, 90, 108, ny_datetime(2026, 6, 2)),  # dépasse les 2 côtés
    ]
    reference = _detect_double_purge(candles, window_size=5)

    assert reference is not None
    assert reference.high == 105
    assert reference.low == 95


def test_detect_double_purge_none_when_only_one_side():
    candles = [
        make_candle(100, 105, 95, 102, ny_datetime(2026, 6, 1)),
        make_candle(102, 110, 98, 108, ny_datetime(2026, 6, 2)),  # seul le high est dépassé
    ]
    assert _detect_double_purge(candles, window_size=5) is None


def test_dol_rule_end_to_end_triggers_on_bias():
    candles = [
        make_candle(100, 101, 99, 100.5, ny_datetime(2026, 6, 1)),
        make_candle(100.5, 101, 98, 100.3, ny_datetime(2026, 6, 2)),
        make_candle(100.3, 100.6, 99.8, 100.4, ny_datetime(2026, 6, 3)),
        make_candle(100.4, 100.6, 99, 96, ny_datetime(2026, 6, 4)),  # bias baissier
        make_candle(96, 97, 94, 95, ny_datetime(2026, 6, 5)),  # bougie en cours
    ]
    context = MarketContext(
        market_data=MarketData(
            price_usd=95, price_change_percentage_24h=-3.0, volume_24h_usd=100, market_cap_usd=1000
        ),
        candles={"daily": CandleSeries(candles=candles)},
    )
    result = DolRule().evaluate(context, {})

    assert result.triggered is True
    assert any(c["source"] == "bias" for c in result.metadata["daily"])
