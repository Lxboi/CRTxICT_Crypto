"""Tests pour rules/crt_rule.py : détection de patterns CRT."""

from __future__ import annotations

from rules.crt_rule import _detect_all_crt_patterns, _detect_crt_pattern
from tests.conftest import make_candle, ny_datetime


def test_crt_pattern_purge_on_later_candle_not_immediate():
    """
    Le cas général : la purge peut survenir sur une bougie D, pas
    seulement B — MAIS avec la stratégie "ancre la plus récente
    d'abord", ça n'est démontrable que si les ancres candidates plus
    récentes (B, C) échouent explicitement AVANT la bougie de purge
    (ici : leur containment casse dès la bougie suivante), forçant la
    recherche à reculer jusqu'à l'ancre A.
    """
    candles = [
        make_candle(100, 110, 90, 105, ny_datetime(2026, 6, 1)),   # A : ancre attendue, mèches larges [90,110]
        make_candle(100, 104, 98, 100, ny_datetime(2026, 6, 2)),    # B : mèches étroites [98,104]
        make_candle(98, 99, 94, 96, ny_datetime(2026, 6, 3)),        # C : close=96 casse le containment de B (96<98)
        make_candle(96, 112, 95, 101, ny_datetime(2026, 6, 4)),      # D : purge de A (high=112>110), contenue par A
    ]
    pattern = _detect_crt_pattern(candles)

    assert pattern is not None
    assert pattern.anchor_candle is candles[0]
    assert pattern.bias == "bearish"
    assert pattern.dol_price == 90


def test_crt_pattern_none_when_containment_breaks():
    """
    Le containment porte sur les MÈCHES de l'ancre (high/low), pas son
    corps (open/close) — un CRT reste valide tant qu'aucune bougie n'a
    clôturé en dehors des mèches de la bougie 1 (règle confirmée par
    l'utilisateur, cas réel SOL). Ici, la clôture de la bougie 2 (85)
    est bien en dehors de [99, 101] (mèches de l'ancre) -> containment
    cassé dès la première bougie candidate, avec une seule ancre
    possible (2 bougies au total) -> aucun pattern ne peut se former.
    """
    candles = [
        make_candle(100, 101, 99, 100.5, ny_datetime(2026, 6, 1)),
        make_candle(100.5, 106, 84, 85, ny_datetime(2026, 6, 2)),  # clôture hors des mèches de l'ancre
    ]
    assert _detect_crt_pattern(candles) is None


def test_crt_pattern_containment_not_required_after_purge():
    """
    Bug historique corrigé : le containment ne doit PAS être exigé
    au-delà de la bougie de purge (le prix continue logiquement vers
    la DOL après confirmation).
    """
    candles = [
        make_candle(95, 110, 90, 105, ny_datetime(2026, 6, 1)),  # ancre
        make_candle(105, 108, 96, 102, ny_datetime(2026, 6, 2)),  # contenue
        make_candle(102, 103, 85, 99, ny_datetime(2026, 6, 3)),  # purge côté low
        make_candle(99, 100, 70, 75, ny_datetime(2026, 6, 4)),  # continuation hors corps de l'ancre
    ]
    pattern = _detect_crt_pattern(candles)

    assert pattern is not None
    assert pattern.bias == "bullish"


def test_detect_all_crt_patterns_finds_multiple():
    candles = [
        make_candle(100, 105, 95, 102, ny_datetime(2026, 6, 1)),  # ancre 1
        make_candle(102, 104, 90, 101, ny_datetime(2026, 6, 2)),  # purge ancre 1 (low)
        make_candle(101, 103, 99, 100, ny_datetime(2026, 6, 3)),
        make_candle(100, 110, 98, 101, ny_datetime(2026, 6, 4)),  # nouvelle purge (high)
    ]
    patterns = _detect_all_crt_patterns(candles)

    assert len(patterns) >= 1


def test_detect_crt_pattern_ignores_stale_pattern_beyond_current_price():
    """
    Bug réel corrigé (silver XAGUSD) : avec l'ancienne stratégie de scan
    ("ancre la plus ancienne d'abord"), sur un long historique, le
    premier pattern valide trouvé pouvait dater d'il y a des années,
    avec une cible déjà largement dépassée par le prix actuel — inutile
    et trompeur pour une décision de trading aujourd'hui.

    Avec la stratégie actuelle ("ancre la plus récente d'abord"), ce cas
    est déjà résolu SANS même avoir besoin du filtre de prix — le scan
    trouve naturellement le pattern récent en premier. Le filtre de
    prix (current_price) reste une protection supplémentaire pour les
    cas où le pattern le plus récent trouvé serait lui-même obsolète en
    prix (voir test_anchor_distance_threshold_rejects_sol_case).
    """
    old_pattern = [
        make_candle(25, 29.44, 24, 29, ny_datetime(2020, 1, 1)),
        make_candle(29, 29.2, 26.5, 27, ny_datetime(2020, 1, 8)),
        make_candle(27, 27.5, 23, 28, ny_datetime(2020, 1, 15)),  # purge -> DOL=29.44 (bullish)
    ]
    filler = [
        make_candle(
            40 + i * 0.1, 40.05 + i * 0.1, 39.95 + i * 0.1, 40 + i * 0.1,
            ny_datetime(2020, 2, 1) + __import__("datetime").timedelta(weeks=i),
        )
        for i in range(50)
    ]
    recent_pattern = [
        make_candle(58, 59, 57, 58.5, ny_datetime(2026, 5, 1)),
        make_candle(58.5, 58.8, 58.2, 58.3, ny_datetime(2026, 5, 8)),
        make_candle(58.3, 61, 58.1, 58.4, ny_datetime(2026, 5, 15)),  # purge -> DOL=57 (bearish)
    ]
    candles = old_pattern + filler + recent_pattern
    current_price = 58.6  # proche du groupe recent, sans franchir son propre SL (61)

    # Même sans filtre de prix, la stratégie "plus récent d'abord" trouve
    # déjà un pattern récent et pertinent, jamais l'ancien de 2020 —
    # l'ancre retenue est la PLUS RÉCENTE possible satisfaisant
    # containment+purge (ici, la 2e bougie du groupe récent).
    pattern_without_filter = _detect_crt_pattern(candles)
    assert pattern_without_filter is not None
    assert pattern_without_filter.dol_price != 29.44
    assert pattern_without_filter.anchor_candle.timestamp.year == 2026

    pattern_with_filter = _detect_crt_pattern(candles, current_price=current_price)
    assert pattern_with_filter is not None
    assert pattern_with_filter.dol_price != 29.44
    assert pattern_with_filter.bias == "bearish"


def test_detect_crt_pattern_ignores_anchor_far_from_price_even_if_target_never_crossed():
    """
    Bug réel corrigé (ETH) : une ancre ancienne dont la cible est dans
    la direction OPPOSÉE au mouvement de fond de l'actif (ex: cible
    bearish très en dessous, alors que le prix n'a fait que monter
    depuis) n'est jamais "dépassée" au sens strict — le filtre
    précédent (cible déjà atteinte) ne suffit donc pas à l'exclure. Un
    second critère (distance de l'ancre elle-même au prix actuel) est
    nécessaire.
    """
    old_pattern = [
        make_candle(200, 350, 144.21, 292.34, ny_datetime(2019, 1, 1)),
        make_candle(292, 300, 200, 250, ny_datetime(2019, 1, 8)),
        make_candle(250, 260, 100, 230, ny_datetime(2019, 1, 15)),  # purge -> DOL=144.21 (bearish)
    ]
    filler = [
        make_candle(1000 + i, 1000.05 + i, 999.95 + i, 1000 + i, ny_datetime(2019, 2, 1) + __import__("datetime").timedelta(weeks=i))
        for i in range(200)
    ]
    recent_pattern = [
        make_candle(1850, 1900, 1800, 1870, ny_datetime(2026, 5, 1)),
        make_candle(1870, 1880, 1830, 1860, ny_datetime(2026, 5, 8)),
        make_candle(1860, 1950, 1820, 1855, ny_datetime(2026, 5, 15)),  # purge -> DOL=1800 (bearish)
    ]
    candles = old_pattern + filler + recent_pattern
    current_price = 1884.75

    pattern = _detect_crt_pattern(candles, current_price=current_price)
    assert pattern is not None
    assert pattern.dol_price != 144.21  # l'ancre de 2019 est bien ignorée
    # Avec la stratégie "ancre la plus récente d'abord", l'ancre retenue
    # est la plus récente possible du groupe récent (pas forcément la
    # toute première du groupe) — ce qui compte ici est qu'elle reste
    # raisonnable et proche du prix actuel, pas l'ancre de 2019.
    assert abs(current_price - pattern.dol_price) / pattern.dol_price < 0.1


def test_containment_uses_wicks_not_body():
    """
    Bug réel corrigé (SOL) : le containment vérifiait la clôture des
    bougies contre le CORPS de l'ancre (open/close), une fourchette
    bien trop étroite qui casse quasi immédiatement — l'algorithme
    "trouvait" alors des CRT sur de pures coïncidences (deux bougies
    isolées dont les prix se chevauchent brièvement par hasard),
    n'importe où dans l'historique, sans représenter une vraie
    consolidation soutenue (donc invisibles sur un vrai graphique).

    Le containment doit porter sur les MÈCHES de l'ancre (high/low) —
    règle confirmée par l'utilisateur : "un CRT devient invalide dès
    lors qu'une bougie a clôturé son corps à l'extérieur des mèches de
    la bougie 1".
    """
    # Ancre : corps étroit [100, 100.5], mèches larges [95, 106]
    anchor = make_candle(100, 106, 95, 100.5, ny_datetime(2026, 6, 1))
    # Clôture (99) hors du CORPS [100, 100.5] mais DANS les mèches [95, 106]
    # -> doit rester "contenue" avec la correction (mèches), et purge
    # simultanément (low=90 < anchor.low=95) -> pattern bullish direct,
    # sans bougie intermédiaire (qui formerait sinon sa propre ancre plus
    # récente, avec la stratégie "plus récent d'abord").
    purge_candle = make_candle(100.5, 101, 90, 99, ny_datetime(2026, 6, 2))

    candles = [anchor, purge_candle]

    pattern = _detect_crt_pattern(candles)
    assert pattern is not None
    assert pattern.anchor_candle is anchor
    assert pattern.bias == "bullish"


def test_anchor_distance_threshold_rejects_sol_case():
    """
    Reproduit le cas réel signalé : SOL à $74,83, un CRT Weekly avec
    une ancre autour de $28-50 (distance ~92% du prix actuel). Avec le
    seuil resserré à 50%, cette ancre doit être jugée obsolète.
    """
    from rules.crt_rule import _is_anchor_too_far_from_price

    anchor = make_candle(45, 50, 28, 42, ny_datetime(2022, 1, 1))
    assert _is_anchor_too_far_from_price(anchor, current_price=74.83) is True


def test_target_already_reached_checked_over_full_history_not_just_current_price():
    """
    Bug réel corrigé (BTC) : une ancre à très large mèche peut "contenir"
    le prix pendant des mois malgré plusieurs allers-retours entre-temps
    (le containment sur les mèches, corrigé plus tôt, ne casse jamais
    pour une ancre suffisamment large). Si la cible a déjà été liquidée
    À N'IMPORTE QUEL MOMENT depuis la purge, elle doit rester "consommée"
    même si le prix est ensuite revenu du bon côté — l'ancienne logique
    ne comparait qu'au prix ACTUEL, laissant passer ce cas à tort.
    """
    anchor = make_candle(70000, 76000, 19000, 65000, ny_datetime(2025, 1, 1))
    purge = make_candle(65500, 77000, 64500, 65000, ny_datetime(2025, 1, 8))
    zigzag = [
        make_candle(65500, 70000, 60000, 68000, ny_datetime(2025, 1, 15)),
        make_candle(68000, 72000, 65000, 70000, ny_datetime(2025, 1, 22)),
        make_candle(70000, 71000, 25000, 30000, ny_datetime(2025, 1, 29)),
        # Liquide la cible de l'ancre ORIGINALE (18500 < 19000), puis remonte fort :
        make_candle(30000, 60000, 18500, 55000, ny_datetime(2025, 2, 5)),
        make_candle(55000, 62000, 50000, 60000, ny_datetime(2025, 2, 12)),
        make_candle(60000, 65000, 58000, 63000, ny_datetime(2025, 2, 19)),
        make_candle(63000, 68000, 61000, 64000, ny_datetime(2025, 2, 26)),
    ]
    candles = [anchor, purge] + zigzag

    # Avec la stratégie "ancre la plus récente d'abord", ce cas est déjà
    # résolu naturellement : un pattern récent et raisonnable est trouvé
    # (basé sur la fin du zigzag), jamais l'ancre originale de 19000 —
    # que ce soit avec ou sans current_price fourni.
    pattern_no_price = _detect_crt_pattern(candles)
    assert pattern_no_price is not None
    assert pattern_no_price.dol_price != 19000

    pattern_with_price = _detect_crt_pattern(candles, current_price=64000.0)
    assert pattern_with_price is not None
    assert pattern_with_price.dol_price != 19000


def test_max_anchor_age_candles_rejects_old_anchor_within_price_threshold():
    """
    Bug réel corrigé (BTC) : une ancre Monthly de juillet 2024 (~24
    bougies d'écart) avait une cible restée à ~31% du prix actuel — SOUS
    le seuil de distance de prix (50%), donc non rejetée par les
    filtres existants, mais clairement trop ancienne en TEMPS pour
    rester pertinente. Un filtre indépendant de la distance de prix,
    basé uniquement sur l'âge (en nombre de bougies), est nécessaire.
    """
    anchor = make_candle(65659.78, 68000, 49000, 49000, ny_datetime(2024, 7, 1))
    purge = make_candle(49000, 66498, 48000, 55000, ny_datetime(2024, 8, 1))
    filler = [
        make_candle(
            55000 + i * 300, 68000, 49500, 60000 + i * 200,
            ny_datetime(2024, 9, 1) + __import__("datetime").timedelta(days=30 * i),
        )
        for i in range(22)
    ]
    candles = [anchor, purge] + filler
    current_price = 64020.54

    # Sans le filtre d'âge : le pattern (obsolète en temps) est encore trouvé
    pattern_no_age_filter = _detect_crt_pattern(candles, current_price=current_price)
    assert pattern_no_age_filter is not None

    # Avec le filtre d'âge (12 bougies, cohérent avec Monthly) : rejeté
    pattern_with_age_filter = _detect_crt_pattern(
        candles, current_price=current_price, max_anchor_age_candles=12
    )
    assert pattern_with_age_filter is None


def test_sl_already_breached_rejects_pattern():
    """
    Bug réel corrigé (ETH) : le SL d'un setup bearish (short) est le
    high de la bougie de purge — le niveau de rejet. Si le prix est
    ENSUITE remonté au-delà de ce niveau (cas réel : SL proposé à
    $1 833,40 alors que le prix actuel était à $1 874,92, largement
    au-dessus), le setup n'a plus de sens : on n'entrerait jamais un
    short avec un stop déjà dépassé avant même d'entrer. Un tel pattern
    doit être rejeté comme obsolète.
    """
    anchor = make_candle(1650, 1800, 1550.20, 1708.20, ny_datetime(2026, 6, 28))
    purge = make_candle(1708, 1833.40, 1700, 1750, ny_datetime(2026, 6, 29))
    recovery = [
        make_candle(1750, 1780, 1740, 1770, ny_datetime(2026, 6, 30)),
        make_candle(1770, 1850, 1760, 1840, ny_datetime(2026, 7, 1)),  # dépasse déjà 1833.40
        make_candle(1840, 1880, 1820, 1874.92, ny_datetime(2026, 7, 2)),
    ]
    candles = [anchor, purge] + recovery

    pattern = _detect_crt_pattern(candles, current_price=1874.92)
    assert pattern is None
