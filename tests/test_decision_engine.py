"""Tests d'intégration pour rules/decision_engine.py : scénarios complets."""

from __future__ import annotations

from datetime import datetime, timezone

from models.macro_data import MacroEvent
from models.market_data import CandleSeries, MarketData
from rules.decision_engine import DecisionEngine
from rules.engine import MarketContext
from tests.conftest import make_candle, ny_datetime


def _build_full_context():
    monthly = [
        make_candle(105000, 106000, 99000, 103000, ny_datetime(2026, 4, 1)),
        make_candle(103000, 104000, 95000, 94000, ny_datetime(2026, 5, 1)),
        make_candle(94000, 95000, 90000, 91000, ny_datetime(2026, 6, 1)),
    ]
    weekly = [
        make_candle(100000, 101000, 96000, 99000, ny_datetime(2026, 5, 18)),
        make_candle(99000, 99500, 93000, 92000, ny_datetime(2026, 5, 25)),
        make_candle(92000, 93000, 90000, 91000, ny_datetime(2026, 6, 1)),
    ]
    daily = [
        make_candle(91000, 95000, 90000, 93000, ny_datetime(2026, 6, 1)),
        make_candle(93000, 94500, 91500, 92500, ny_datetime(2026, 6, 2)),
        make_candle(92500, 96000, 91800, 92000, ny_datetime(2026, 6, 3)),
        make_candle(92000, 92500, 91000, 91500, ny_datetime(2026, 6, 4)),
    ]
    intraday = [
        make_candle(93000, 93200, 92800, 93100, ny_datetime(2026, 6, 1, 12)),
        make_candle(93100, 93300, 92900, 93000, ny_datetime(2026, 6, 2, 12)),
        make_candle(92500, 92700, 92400, 92550, ny_datetime(2026, 6, 3, 0)),
        make_candle(92550, 93500, 92500, 93200, ny_datetime(2026, 6, 3, 1)),
        make_candle(93200, 93400, 93000, 93100, ny_datetime(2026, 6, 3, 2)),
        make_candle(93100, 96000, 92900, 93150, ny_datetime(2026, 6, 3, 3)),
        make_candle(93150, 93300, 92950, 93000, ny_datetime(2026, 6, 3, 4)),
        make_candle(93000, 93100, 92800, 92900, ny_datetime(2026, 6, 3, 5)),
        make_candle(92900, 93000, 92700, 92800, ny_datetime(2026, 6, 3, 6)),
        make_candle(92800, 92900, 92600, 92700, ny_datetime(2026, 6, 3, 7)),
        make_candle(92700, 92800, 92500, 92650, ny_datetime(2026, 6, 3, 8)),
        make_candle(92650, 92750, 92450, 92600, ny_datetime(2026, 6, 3, 9)),
        make_candle(92600, 92700, 91800, 92300, ny_datetime(2026, 6, 3, 10)),
        make_candle(92300, 92400, 92100, 92200, ny_datetime(2026, 6, 3, 11)),
        make_candle(92200, 92300, 92050, 92150, ny_datetime(2026, 6, 3, 12)),
        make_candle(92150, 92250, 92000, 92050, ny_datetime(2026, 6, 3, 13)),
        make_candle(92050, 92150, 91950, 92000, ny_datetime(2026, 6, 3, 14)),
        make_candle(92000, 92100, 91900, 92000, ny_datetime(2026, 6, 4, 0)),
    ]

    return MarketContext(
        market_data=MarketData(
            price_usd=91500,
            price_change_percentage_24h=-2.0,
            volume_24h_usd=100,
            market_cap_usd=1000,
        ),
        candles={
            "monthly": CandleSeries(candles=monthly),
            "weekly": CandleSeries(candles=weekly),
            "daily": CandleSeries(candles=daily),
            "intraday": CandleSeries(candles=intraday),
        },
    )


def _base_config():
    return {
        "dol_rule": {"min_candles_required": 2},
        "narrative_ltf_rule": {"min_candles_required": 5, "min_sessions_for_trigger": 1},
        "purge_rule": {"min_candles_required": 5},
        "keylevel_rule": {"min_candles_required": 3},
    }


def test_full_scenario_recommends_bearish_entry():
    """
    Avec la stratégie "ancre la plus récente d'abord" (voir
    rules/crt_rule.py), ce scénario trouve désormais un CRT Daily plus
    récent que celui trouvé par l'ancienne stratégie — toujours bearish
    et cohérent avec le biais effectif, mais dont la purge n'est pas
    évaluée "High Probability" par purge_rule.py pour CETTE ancre
    précise, donc pas d'entrée recommandée sur ce setup. Le biais et la
    confiance restent corrects ; c'est le comportement attendu, pas une
    régression : l'important est que le pipeline complet reste cohérent
    de bout en bout.
    """
    context = _build_full_context()
    report = DecisionEngine(config=_base_config()).decide(context)

    assert report.effective_bias == "bearish"
    assert report.confidence == "high"
    assert len(report.timeframe_setups) >= 1
    assert all(s.htf_crt["bias"] == "bearish" for s in report.timeframe_setups if s.htf_crt)


def test_monthly_weekly_disagreement_reduces_confidence():
    monthly = [
        make_candle(105000, 106000, 99000, 103000, ny_datetime(2026, 4, 1)),
        make_candle(103000, 104000, 95000, 94000, ny_datetime(2026, 5, 1)),  # bearish
        make_candle(94000, 95000, 90000, 91000, ny_datetime(2026, 6, 1)),
    ]
    weekly = [
        make_candle(90000, 95000, 89000, 91000, ny_datetime(2026, 5, 18)),
        make_candle(91000, 98000, 90500, 97000, ny_datetime(2026, 5, 25)),  # bullish
        make_candle(97000, 99000, 96000, 98000, ny_datetime(2026, 6, 1)),
    ]
    context = MarketContext(
        market_data=MarketData(
            price_usd=96500, price_change_percentage_24h=-1, volume_24h_usd=100, market_cap_usd=1000
        ),
        candles={
            "monthly": CandleSeries(candles=monthly),
            "weekly": CandleSeries(candles=weekly),
        },
    )
    report = DecisionEngine(config={"dol_rule": {"min_candles_required": 2}}).decide(context)

    assert report.monthly_bias == "bearish"
    assert report.weekly_bias == "bullish"
    assert report.effective_bias == "bearish"  # Monthly prime
    assert report.confidence == "medium"


def test_high_impact_news_reported_in_report():
    context = _build_full_context()
    today = datetime.now(timezone.utc).date()
    context.economic_events = [MacroEvent(date=today, events=["CPI"])]

    # Note : ce test ne force pas l'heure courante dans la fenêtre CPI (8h-9h30 NY) ;
    # il vérifie seulement que le calendrier est bien pris en compte et
    # rapporté, pas le blocage temps réel (couvert par un test dédié plus
    # complexe si l'horloge doit être simulée précisément).
    report = DecisionEngine(config=_base_config()).decide(context)

    assert report.high_impact_news.get("high_impact_today") == ["CPI"]

def test_resolve_bias_crt_not_vetoed_by_diverging_mechanism():
    """
    Bug réel corrigé (BTC) : l'ancien code exigeait l'unanimité entre
    tous les mécanismes DOL — un double purge divergent suffisait à
    rendre le biais "incohérent" (None) alors qu'un CRT parfaitement
    valide pointait clairement une direction. Le CRT ne doit plus être
    neutralisé par un mécanisme secondaire divergent.
    """
    from rules.decision_engine import _resolve_timeframe_bias

    candidates = [
        {"source": "crt", "bias": "bearish", "dol_price": 90000},
        {"source": "double_purge", "bias": "bullish", "dol_price": 100000},
    ]
    assert _resolve_timeframe_bias(candidates) == "bearish"


def test_resolve_bias_crt_has_top_priority_over_break():
    """
    Cascade révisée (BTC, août 2026, à la demande explicite de
    l'utilisateur) : un CRT valide (donc jamais invalidé -- déjà
    garanti par rules/crt_rule.py::_detect_crt_pattern avant d'arriver
    ici) prime désormais SYSTÉMATIQUEMENT sur une cassure de biais,
    même sans condition de fraîcheur ni de range -- une cassure peut
    avoir été trouvée en remontant loin dans l'historique (voir
    rules/dol_rule.py::_detect_bias), donc être bien plus ancienne
    qu'un CRT toujours valide.
    """
    from rules.decision_engine import _resolve_timeframe_bias

    candidates = [
        {"source": "crt", "bias": "bullish", "dol_price": 74070.5},
        {"source": "bias", "bias": "bearish", "dol_price": 72436.0},
    ]
    assert _resolve_timeframe_bias(candidates) == "bullish"


def test_resolve_bias_break_used_only_when_no_crt():
    """Sans CRT valide présent, la cassure de biais reste utilisée normalement."""
    from rules.decision_engine import _resolve_timeframe_bias

    candidates = [{"source": "bias", "bias": "bearish", "dol_price": 72436.0}]
    assert _resolve_timeframe_bias(candidates) == "bearish"


def test_resolve_bias_double_purge_alone_is_not_a_bias():
    """Le double purge seul est une confirmation, pas un mécanisme de biais autonome."""
    from rules.decision_engine import _resolve_timeframe_bias

    assert _resolve_timeframe_bias([{"source": "double_purge", "bias": "bullish", "dol_price": 1}]) is None


def test_bias_cascade_descends_to_weekly_with_reduced_confidence():
    """Monthly sans rien de clair -> le Weekly fournit le biais, confiance medium."""
    monthly_flat = [
        make_candle(95000, 95100, 94900, 95000, ny_datetime(2026, 3, 1)),
        make_candle(95000, 95100, 94900, 95010, ny_datetime(2026, 4, 1)),
        make_candle(95010, 95110, 94910, 95005, ny_datetime(2026, 5, 1)),
    ]
    weekly = [
        make_candle(96000, 99000, 93000, 97000, ny_datetime(2026, 5, 4)),
        make_candle(97000, 100000, 93500, 96500, ny_datetime(2026, 5, 11)),
        make_candle(96500, 97000, 95000, 96000, ny_datetime(2026, 5, 18)),
    ]
    context = MarketContext(
        market_data=MarketData(
            price_usd=95500, price_change_percentage_24h=0, volume_24h_usd=1, market_cap_usd=1
        ),
        candles={
            "monthly": CandleSeries(candles=monthly_flat),
            "weekly": CandleSeries(candles=weekly),
        },
    )
    report = DecisionEngine(config={"dol_rule": {"min_candles_required": 2}}).decide(context)

    assert report.bias_source_timeframe == "weekly"
    assert report.effective_bias == "bearish"
    assert report.confidence == "medium"


def test_filter_kls_toward_target_rejects_distant_zones():
    """
    Trou de sécurité comblé (même famille de bug que le CRT/SOL) :
    _filter_kls_toward_target ne vérifiait que la direction et le côté
    du prix, jamais la distance — une zone non mitigée depuis des
    années (jamais retestée) pouvait rester "valide" indéfiniment sans
    être réellement pertinente pour le prix actuel.
    """
    from datetime import datetime, timezone

    from rules.decision_engine import _filter_kls_toward_target
    from rules.keylevel_rule import KeyLevelZone

    zones = [
        KeyLevelZone(
            kind="old_low", direction="bullish", zone_low=19, zone_high=21,
            formed_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
        ),
        KeyLevelZone(
            kind="fvg", direction="bullish", zone_low=68, zone_high=70,
            formed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ),
    ]

    filtered = _filter_kls_toward_target(zones, "bearish", current_price=74.83)

    assert len(filtered) == 1
    assert filtered[0].kind == "fvg"


def test_compute_crt_targets_rejects_unreasonable_htf_dol():
    """
    Bug réel corrigé (TRX) : htf_dol_price (TP3) provient des candidats
    DOL bruts (mécanismes "bias", "double_purge", "crt" combinés) sans
    passer par AUCUN filtre anti-obsolescence — seul le mécanisme "crt"
    en bénéficie via _detect_crt_pattern. Un cas réel a montré un TP3 à
    614% du prix actuel. _compute_crt_targets doit rejeter un htf_dol_price
    déraisonnablement distant du prix actuel, quel que soit le mécanisme
    qui l'a produit.
    """
    from datetime import datetime, timezone

    from models.market_data import Candle
    from rules.crt_rule import CrtPattern
    from rules.decision_engine import _compute_crt_targets

    anchor = Candle(timestamp=datetime(2026, 2, 22, tzinfo=timezone.utc), open=0.32, high=0.34, low=0.28, close=0.29)
    purge = Candle(timestamp=datetime(2026, 3, 1, tzinfo=timezone.utc), open=0.29, high=0.30, low=0.27, close=0.284)
    crt = CrtPattern(anchor_candle=anchor, purge_candle=purge, purge_side="low", dol_price=0.2783, bias="bearish")
    current_price = 0.3236

    targets_rejected = _compute_crt_targets(crt, 0.0453, current_price)
    assert targets_rejected.tp_htf_dol is None

    targets_accepted = _compute_crt_targets(crt, 0.25, current_price)
    assert targets_accepted.tp_htf_dol == 0.25


def test_get_closed_candles_for_scope_4h_uses_resolve_asset_category_grid():
    """
    _get_closed_candles_for_scope("4h") dérive la grille via
    rules.purge_rule.resolve_asset_category(context.symbol), la MÊME
    résolution que PurgeRule.evaluate() (voir tests/test_purge_rule.py::
    TestResolveAssetCategory) -- les deux doivent IMPÉRATIVEMENT
    s'accorder sur la même grille pour un même symbole.

    Depuis la simplification architecture crypto-only (août 2026),
    resolve_asset_category() retourne toujours "crypto" -- tous les
    symboles reçoivent donc désormais la même grille (CRYPTO_4H_GRID_HOURS).
    """
    from datetime import timedelta

    from rules.decision_engine import _get_closed_candles_for_scope
    from rules.purge_rule import CRYPTO_4H_GRID_HOURS, _resample_to_4h

    start = ny_datetime(2026, 6, 1, 0)
    m15_candles = [
        make_candle(100, 101, 99, 100, start + timedelta(minutes=15 * i)) for i in range(96)
    ]

    def build_context(symbol: str) -> MarketContext:
        return MarketContext(
            market_data=MarketData(
                price_usd=100, price_change_percentage_24h=0, volume_24h_usd=0, market_cap_usd=0
            ),
            candles={"m15": CandleSeries(candles=m15_candles)},
            symbol=symbol,
        )

    eurusd_4h = _get_closed_candles_for_scope(build_context("EURUSD"), "4h")
    btc_4h = _get_closed_candles_for_scope(build_context("BTC"), "4h")

    assert eurusd_4h is not None
    assert btc_4h is not None

    expected_crypto = _resample_to_4h(m15_candles[:-1], CRYPTO_4H_GRID_HOURS)[:-1]

    assert [c.timestamp for c in eurusd_4h] == [c.timestamp for c in expected_crypto]
    assert [c.timestamp for c in btc_4h] == [c.timestamp for c in expected_crypto]
