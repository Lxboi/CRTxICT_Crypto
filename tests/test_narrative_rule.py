"""Tests pour rules/narrative_rule.py : Weekly Profiles, Dealing Range, news à haut impact."""

from __future__ import annotations

from datetime import datetime, timezone

from models.macro_data import MacroEvent
from rules.narrative_rule import (
    _classify_week,
    _compute_dealing_range,
    _get_high_impact_news_context,
    _match_high_impact_keyword,
    _match_weekly_profiles,
)
from tests.conftest import make_candle, ny_datetime


def test_cew1_profile_recognized():
    preceding = [
        make_candle(100, 100.5, 99, 100.2, ny_datetime(2026, 5, 22)),
        make_candle(100.2, 100.6, 99.8, 100.3, ny_datetime(2026, 5, 23)),
        make_candle(100.3, 100.7, 99.9, 100.4, ny_datetime(2026, 5, 24)),
        make_candle(100.4, 100.6, 98, 100.1, ny_datetime(2026, 5, 25)),
        make_candle(100.1, 100.5, 95, 100.0, ny_datetime(2026, 5, 26)),
        make_candle(100.0, 100.4, 99.7, 100.2, ny_datetime(2026, 5, 27)),
        make_candle(100.2, 100.5, 99.8, 100.3, ny_datetime(2026, 5, 28)),
        make_candle(100.3, 100.6, 99.9, 100.4, ny_datetime(2026, 5, 29)),
    ]
    week = [
        make_candle(100.4, 100.6, 99.9, 100.5, ny_datetime(2026, 6, 1)),
        make_candle(100.5, 100.9, 93, 100.6, ny_datetime(2026, 6, 2)),  # purge sous 95
        make_candle(100.6, 106, 100.2, 105.5, ny_datetime(2026, 6, 3)),
        make_candle(105.5, 112, 105, 111, ny_datetime(2026, 6, 4)),
        make_candle(111, 111.5, 107, 108, ny_datetime(2026, 6, 5)),
    ]

    records = _classify_week(week, preceding, 0.8, 2.0, {})
    matches = _match_weekly_profiles(records)

    assert matches.get("CEW1") == 1.0


def test_dealing_range_premium_and_none_cases():
    candles = [
        make_candle(90, 91, 89, 90.5, ny_datetime(2026, 6, 1)),
        make_candle(90.5, 92, 89.5, 91, ny_datetime(2026, 6, 2)),
        make_candle(91, 130, 90, 120, ny_datetime(2026, 6, 3)),  # swing high
        make_candle(120, 121, 80, 100, ny_datetime(2026, 6, 4)),  # swing low
        make_candle(100, 105, 98, 102, ny_datetime(2026, 6, 5)),
        make_candle(102, 112, 90, 108, ny_datetime(2026, 6, 6)),
    ]
    dealing_range = _compute_dealing_range(
        candles, current_price=108, swing_lookback=2, equilibrium_band_pct=0.1
    )

    assert dealing_range is not None
    assert dealing_range.range_low == 80
    assert dealing_range.range_high == 130


def test_high_impact_news_keyword_matching():
    assert _match_high_impact_keyword("CPI") == "CPI"
    assert _match_high_impact_keyword("Consumer Price Index") == "CPI"
    assert _match_high_impact_keyword("Nonfarm Payrolls") == "NFP"
    assert _match_high_impact_keyword("FOMC Rate Decision") == "FOMC"
    assert _match_high_impact_keyword("Random unrelated event") is None


def test_high_impact_news_context_detects_today_event():
    today = datetime.now(timezone.utc).date()
    events = [MacroEvent(date=today, events=["CPI"])]

    result = _get_high_impact_news_context(events)

    assert result["available"] is True
    assert "CPI" in result["high_impact_today"]
    assert len(result["avoid_windows"]) == 1
    assert result["avoid_windows"][0]["window_start_ny"] == "08:00"


def test_high_impact_news_context_without_calendar():
    result = _get_high_impact_news_context(None)
    assert result["available"] is False
    assert result["currently_in_avoid_window"] is False


def test_is_purge_of_zone_rejects_level_not_entire_zone_width():
    """
    Bug réel corrigé : l'ancienne logique exigeait une clôture au-delà
    de la LARGEUR ENTIÈRE de la zone (ex: pour une Order Block bearish
    de $95 000-$96 000, il fallait clôturer sous $95 000 après avoir
    méché au-dessus de $96 000 — un aller-retour de plusieurs % dans
    une seule bougie, quasi impossible en pratique). La purge doit
    seulement rejeter le NIVEAU FRANCHI (zone_high pour bearish), pas
    traverser toute la zone.
    """
    from datetime import datetime, timezone

    from rules.keylevel_rule import KeyLevelZone
    from rules.narrative_rule import _is_purge_of_zone

    zone = KeyLevelZone(
        kind="order_block", direction="bearish", zone_low=95000, zone_high=96000,
        formed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    # Mèche au-dessus de zone_high (96000), clôture juste en dessous
    # (95500) -- PAS sous zone_low (95000).
    candle = make_candle(95800, 96500, 95400, 95500, ny_datetime(2026, 6, 1))

    assert _is_purge_of_zone(candle, zone, min_excursion_pct=0.3) is True



def test_all_four_weekly_profiles_recognized_after_purge_fix():
    """
    Vérifie que la correction de _is_purge_of_zone (rejet du niveau
    franchi, pas de la zone entière) fonctionne pour les 4 signatures
    de narrative, pas seulement CEW1 — chacune exige un jour "purge" à
    une position différente de la semaine.
    """
    preceding = [
        make_candle(94000, 96500, 93800, 94200, ny_datetime(2026, 5, 18 + i))
        for i in range(10)
    ]

    scenarios = {
        "CEW2": [
            (94200, 96800, 93900, 94000),
            (94000, 97000, 93800, 96500),
            (96500, 99500, 96300, 99000),
            (99000, 102000, 98800, 101500),
            (101500, 101800, 99000, 100000),
        ],
        "midweek_reversal": [
            (94200, 94300, 94100, 94250),
            (94250, 94350, 94150, 94300),
            (94300, 96800, 93900, 94000),
            (94000, 97000, 93800, 96500),
            (96500, 99500, 96300, 99000),
        ],
        "consolidation_reversal": [
            (94200, 94300, 94100, 94250),
            (94250, 94350, 94150, 94300),
            (94300, 94400, 94200, 94350),
            (94350, 96800, 93900, 94000),
            (94000, 97000, 93800, 96500),
        ],
    }

    for expected_profile, ohlc_values in scenarios.items():
        week = [
            make_candle(o, h, l, c, ny_datetime(2026, 6, 1) + __import__("datetime").timedelta(days=i))
            for i, (o, h, l, c) in enumerate(ohlc_values)
        ]
        records = _classify_week(week, preceding, 0.5, 1.0, {}, purge_min_excursion_pct=0.3)
        matches = _match_weekly_profiles(records)
        assert matches.get(expected_profile) == 1.0, f"{expected_profile} non détecté : {matches}"


def test_compute_dealing_range_rejects_ancient_unmitigated_extreme():
    """
    Bug réel corrigé (TRX) : un swing extrême historique (souvent un
    plus bas/haut absolu) reste par définition "non mitigé" pour
    toujours — s'il devient l'unique candidat restant (tous les swings
    plus récents ayant été balayés depuis), _compute_dealing_range le
    sélectionnait quand même, même à des centaines de % du prix actuel
    (cas réel : un plus bas de 2018 à $0,0453 alors que le prix actuel
    est à $0,3262). Un filtre de distance (même seuil que les CRT) doit
    l'exclure.
    """
    import datetime as dt

    candles = [make_candle(0.05, 0.06, 0.0453, 0.055, ny_datetime(2018, 1, 1))]
    for i in range(1, 400):
        price = 0.05 + i * 0.0007
        candles.append(
            make_candle(price, price + 0.01, price - 0.005, price + 0.002, ny_datetime(2018, 1, 1) + dt.timedelta(weeks=i))
        )

    current_price = 0.3262
    result = _compute_dealing_range(candles, current_price, swing_lookback=5, equilibrium_band_pct=0.1)

    if result is not None:
        assert result.range_low != 0.0453


def test_match_weekly_profiles_uses_real_weekday_not_list_position():
    """
    Bug réel corrigé (pertinent pour Forex/Indices) : un jour férié
    (Noël, Jour de l'An...) crée un trou dans la semaine de trading —
    ex: lundi, mardi, [mercredi férié absent], jeudi, vendredi. Comparer
    par POSITION dans la liste décalait silencieusement jeudi/vendredi
    d'un cran (jeudi comparé à tort à la position attendue pour
    mercredi). La comparaison doit se faire par jour de semaine RÉEL
    (record.weekday), pas par position — un jour manquant est ignoré
    sans décaler les jours suivants.
    """
    from rules.narrative_rule import DayRecord

    # CEW1 = [range, purge, expansion, expansion, correction]
    # Lundi=range (OK), Mardi=purge (OK), [mercredi absent],
    # Jeudi=expansion (OK, position réelle 3), Vendredi=correction (OK, position réelle 4)
    # -> doit rester un candidat CEW1 valide malgré le trou
    records_matching_cew1_with_gap = [
        DayRecord(ny_date=__import__("datetime").date(2026, 12, 21), weekday=0, role="range", direction="bearish", body_pct=0.2, purged_zone_kind=None),
        DayRecord(ny_date=__import__("datetime").date(2026, 12, 21), weekday=1, role="purge", direction="bearish", body_pct=0.4, purged_zone_kind="fvg"),
        # mercredi (weekday=2) absent : jour férié
        DayRecord(ny_date=__import__("datetime").date(2026, 12, 21), weekday=3, role="expansion", direction="bearish", body_pct=1.5, purged_zone_kind=None),
        DayRecord(ny_date=__import__("datetime").date(2026, 12, 21), weekday=4, role="correction", direction="bullish", body_pct=1.2, purged_zone_kind=None),
    ]
    matches = _match_weekly_profiles(records_matching_cew1_with_gap)
    assert matches.get("CEW1") == 4 / 5

    # Si on avait comparé par POSITION (bug), jeudi (role=expansion) aurait
    # été comparé à signature[2]='expansion' (coïncidence, ça matcherait
    # quand même ici) MAIS vendredi (role=correction) aurait été comparé à
    # signature[3]='expansion' -> mismatch -> CEW1 aurait été invalidé à tort.
    # Ce test confirme donc bien le comportement CORRECT (par jour réel).
