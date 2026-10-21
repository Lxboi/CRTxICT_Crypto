"""
rules/narrative_rule.py

Narrative Rule (HTF) : établit le narratif haute timeframe à partir de
plusieurs briques :
  1. Weekly Profiles (CEW1, CEW2, Midweek Reversal, Consolidation
     Reversal) — classification jour par jour de la semaine en cours.
     La détection de "purge" s'appuie sur les zones actives détectées
     par KeyLevelRule (FVG, Old H/L, Order Block, Breaker Block,
     Rejection Block) — les KL et les H/L de la semaine sont donc
     unifiés en une seule notion de "zone active", au lieu d'un simple
     plus haut/bas isolé.
  2. Dealing Range — zone actuelle de "deal" du marché (premium /
     discount / équilibre), basée sur les swings non mitigés encadrant
     le prix.
  3. Calendrier économique — signale les événements macro du jour à
     partir du calendrier fourni (voir _get_economic_calendar_context()
     ci-dessous), limité au FAIT BRUT (aucune analyse d'impact
     haussier/baissier — ce type d'interprétation a été testé via une
     IA dédiée puis abandonné, voir CLAUDE.md).
  4. SMT (Smart Money Divergence) — compare l'actif à sa paire
     corrélée (voir _get_smt_context() ci-dessous et
     rules/correlation.py), purement informatif (n'influence jamais
     entry_recommended/effective_bias/confidence).

Toute la logique temporelle (jours de la semaine) raisonne en heure de
New York (America/New_York), conformément à la convention du projet,
peu importe le fuseau dans lequel les bougies sont stockées (UTC).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar, Literal

from config.constants import NY_TZ
from models.macro_data import MacroEvent
from models.market_data import Candle, CandleSeries
from rules.crt_rule import MAX_ANCHOR_DISTANCE_RATIO
from rules.engine import BaseRule, MarketContext, RuleResult
from rules.keylevel_rule import KeyLevelZone, _filter_unmitigated, _find_swing_points, find_key_levels

# --- News à haut impact à éviter (approche personnelle de l'utilisateur) ---
# Mots-clés recherchés (insensibles à la casse) dans le nom de
# l'événement renvoyé par SoSoValue, pour couvrir les variantes de
# nommage ("CPI" vs "Consumer Price Index", "NFP" vs "Nonfarm Payrolls").
HIGH_IMPACT_EVENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "CPI": ("CPI", "CONSUMER PRICE INDEX"),
    "FOMC": ("FOMC", "FEDERAL FUNDS RATE", "FED INTEREST RATE"),
    "NFP": ("NFP", "NONFARM PAYROLL", "NON-FARM PAYROLL"),
    "PPI": ("PPI", "PRODUCER PRICE INDEX"),
}

# Heures de publication standard (heure NY). Ces événements sont
# généralement publiés à heure fixe par le BLS ou la Fed, mais PEUVENT
# VARIER (jours fériés, changements de calendrier) — l'API SoSoValue ne
# fournit que la date, pas l'heure exacte. À vérifier vous-même si la
# précision est critique pour une décision donnée.
STANDARD_RELEASE_TIMES_NY: dict[str, tuple[int, int]] = {
    "CPI": (8, 30),
    "PPI": (8, 30),
    "NFP": (8, 30),
    "FOMC": (14, 0),  # décision de taux ; la conférence de presse suit généralement à 14h30
}

# Fenêtre à éviter autour de l'heure de publication estimée.
AVOID_WINDOW_MINUTES_BEFORE = 30
AVOID_WINDOW_MINUTES_AFTER = 60

DayRole = Literal["range", "purge", "expansion", "correction", "neutral"]

# --- Paramètres par défaut, surchargeables via config/alerts.yaml ---
DEFAULT_RANGE_THRESHOLD_PCT = 0.5
DEFAULT_EXPANSION_THRESHOLD_PCT = 1.0
DEFAULT_SWING_LOOKBACK = 2
DEFAULT_EQUILIBRIUM_BAND_PCT = 0.1
DEFAULT_MIN_DAYS_FOR_TRIGGER = 2
DEFAULT_MIN_CANDLES_REQUIRED = 10
DEFAULT_PRE_WEEK_LOOKBACK = 30
DEFAULT_PURGE_MIN_EXCURSION_PCT = 0.3

# Signatures des 4 profils hebdomadaires, Lundi -> Vendredi.
# Un profil est retenu tant qu'aucun jour observé ne contredit sa
# signature (correspondance exacte sur le préfixe observé).
WEEKLY_PROFILE_SIGNATURES: dict[str, list[DayRole]] = {
    "CEW1": ["range", "purge", "expansion", "expansion", "correction"],
    "CEW2": ["purge", "expansion", "expansion", "expansion", "correction"],
    "midweek_reversal": ["range", "range", "purge", "expansion", "expansion"],
    "consolidation_reversal": ["range", "range", "range", "purge", "expansion"],
}


@dataclass
class DayRecord:
    """Classification d'une bougie journalière au sein de la semaine NY."""

    ny_date: date
    weekday: int  # 0 = Lundi ... 4 = Vendredi
    role: DayRole
    direction: Literal["bullish", "bearish"]
    body_pct: float
    purged_zone_kind: str | None = None


@dataclass
class DealingRange:
    """
    Zone de "deal" actuelle du marché : bornée par le swing haut non
    mitigé le plus proche au-dessus du prix, et le swing bas non mitigé
    le plus proche en dessous.
    """

    range_low: float
    range_high: float
    equilibrium: float
    zone: Literal["premium", "discount", "equilibrium"]


# ---------------------------------------------------------------------
# Weekly Profiles
# ---------------------------------------------------------------------


def _get_current_week_candles(
    daily_candles: list[Candle], pre_week_lookback: int
) -> tuple[list[Candle], list[Candle]]:
    """
    Sépare `daily_candles` en (bougies de la semaine ISO la plus
    récente, bougies de contexte précédant cette semaine).

    Le contexte précédent sert de référence de "Key Level" pour
    détecter une purge dès le Lundi (premier jour de la semaine), qui
    n'aurait sinon aucun repère interne à la semaine.

    Args:
        daily_candles: Bougies journalières closes, ordonnées
            chronologiquement.
        pre_week_lookback: Nombre de bougies précédant la semaine à
            conserver comme contexte (KL de référence).

    Returns:
        (bougies de la semaine en cours, bougies de contexte précédent).
    """
    if not daily_candles:
        return [], []

    last_ny = daily_candles[-1].timestamp.astimezone(NY_TZ)
    target_key = last_ny.isocalendar()[:2]

    week_candles: list[Candle] = []
    preceding_candles: list[Candle] = []

    for candle in daily_candles:
        ny_dt = candle.timestamp.astimezone(NY_TZ)
        candle_key = ny_dt.isocalendar()[:2]

        if candle_key == target_key and ny_dt.weekday() <= 4:
            week_candles.append(candle)
        elif candle_key < target_key:
            preceding_candles.append(candle)

    week_candles.sort(key=lambda c: c.timestamp)
    preceding_candles.sort(key=lambda c: c.timestamp)

    return week_candles, preceding_candles[-pre_week_lookback:]


def _is_purge_of_zone(candle: Candle, zone: KeyLevelZone, min_excursion_pct: float) -> bool:
    """
    Indique si une bougie réalise une purge d'une zone KeyLevel donnée :
    une mèche dépasse l'extrémité de la zone du côté "actif" (le high
    pour une zone bearish/résistance, le low pour une zone
    bullish/support), tout en clôturant de l'autre côté du NIVEAU
    FRANCHI (rejet du niveau testé), avec une excursion d'au moins
    `min_excursion_pct` pour écarter les dépassements négligeables.

    Le rejet porte sur le niveau franchi (zone_high pour une purge
    bearish, zone_low pour une purge bullish), PAS sur la largeur
    entière de la zone : exiger une clôture au-delà de l'autre bord de
    la zone reviendrait à demander un aller-retour de plusieurs % dans
    une seule bougie pour une zone large (FVG, Order Block...) —
    quasiment impossible en pratique, notamment en hebdomadaire.

    Fonctionne aussi bien pour les zones ponctuelles (Old High/Low, où
    zone_low == zone_high, insensibles à cette distinction) que pour
    les zones à intervalle (FVG, Order Block, Breaker Block, Rejection
    Block).
    """
    if zone.direction == "bearish":
        breaches = candle.high > zone.zone_high
        rejects = candle.close < zone.zone_high
        if not (breaches and rejects):
            return False
        excursion_pct = (candle.high - zone.zone_high) / zone.zone_high * 100
        return excursion_pct >= min_excursion_pct

    breaches = candle.low < zone.zone_low
    rejects = candle.close > zone.zone_low
    if not (breaches and rejects):
        return False
    excursion_pct = (zone.zone_low - candle.low) / zone.zone_low * 100
    return excursion_pct >= min_excursion_pct


def _classify_week(
    week_candles: list[Candle],
    preceding_candles: list[Candle],
    range_threshold_pct: float,
    expansion_threshold_pct: float,
    keylevel_config: dict[str, Any],
    purge_min_excursion_pct: float = DEFAULT_PURGE_MIN_EXCURSION_PCT,
) -> list[DayRecord]:
    """
    Classifie chaque jour de la semaine en cours selon 4 rôles :

    - "purge" : la bougie a réalisé une purge (cf. `_is_purge_of_zone`)
      d'au moins une zone KeyLevel active (FVG, Old High/Low, Order
      Block, Breaker Block, Rejection Block — KL et H/L sont donc
      unifiés ici, ce sont les mêmes zones que celles détectées par
      KeyLevelRule). Les zones actives sont recalculées à chaque jour
      sur une fenêtre qui grandit au fil de la semaine (contexte
      précédent + jours déjà écoulés), pour capter aussi bien les
      niveaux hérités d'avant la semaine que ceux qui se forment
      pendant la semaine elle-même.
    - "range" : mouvement faible (corps <= range_threshold_pct).
    - "expansion" : mouvement fort (corps >= expansion_threshold_pct)
      dans le sens de la direction dominante de la semaine (le premier
      jour d'expansion fixe cette direction).
    - "correction" : mouvement notable mais dans le sens opposé à la
      direction dominante déjà établie.
    - "neutral" : ne rentre dans aucune des catégories ci-dessus.

    Note : la détection de swing (utilisée pour Old H/L, Order Block et
    Rejection Block) nécessite des bougies de confirmation après le
    point de swing. Un swing formé dans les tout derniers jours avant
    la semaine peut donc ne se confirmer que quelques jours après le
    début de la semaine — limite inhérente à ce type de détection,
    à garder en tête plutôt qu'un bug.
    """
    records: list[DayRecord] = []
    dominant_direction: Literal["bullish", "bearish"] | None = None
    context_candles: list[Candle] = list(preceding_candles)

    for candle in week_candles:
        body_pct = abs(candle.close - candle.open) / candle.open * 100
        direction: Literal["bullish", "bearish"] = (
            "bullish" if candle.close >= candle.open else "bearish"
        )
        ny_dt = candle.timestamp.astimezone(NY_TZ)

        active_zones: list[KeyLevelZone] = []
        if len(context_candles) >= 2:
            raw_zones = find_key_levels(context_candles, keylevel_config)
            active_zones = _filter_unmitigated(raw_zones, context_candles)

        purged_zone = next(
            (
                zone
                for zone in active_zones
                if _is_purge_of_zone(candle, zone, purge_min_excursion_pct)
            ),
            None,
        )

        if purged_zone is not None:
            role: DayRole = "purge"
        elif body_pct <= range_threshold_pct:
            role = "range"
        elif dominant_direction is not None and direction != dominant_direction:
            role = "correction"
        elif body_pct >= expansion_threshold_pct:
            role = "expansion"
            if dominant_direction is None:
                dominant_direction = direction
        else:
            role = "neutral"

        records.append(
            DayRecord(
                ny_date=ny_dt.date(),
                weekday=ny_dt.weekday(),
                role=role,
                direction=direction,
                body_pct=body_pct,
                purged_zone_kind=purged_zone.kind if purged_zone else None,
            )
        )

        context_candles.append(candle)

    return records


def _match_weekly_profiles(day_records: list[DayRecord]) -> dict[str, float]:
    """
    Compare les rôles observés cette semaine aux 4 signatures connues.
    Un profil n'est retenu que si AUCUN jour observé ne contredit sa
    signature (le profil reste "en vie"). La confiance est le ratio de
    jours observés (avec un jour de marché réel, donc en excluant les
    jours fériés) sur le total de la semaine (5).

    La comparaison se fait par JOUR DE SEMAINE RÉEL (record.weekday,
    0=lundi...4=vendredi), pas par position dans la liste : essentiel
    sur Forex/Indices, où un jour férié (Noël, Jour de l'An...) peut
    créer un trou dans la semaine. Comparer par position décalerait
    silencieusement tous les jours suivants d'un cran (ex: jeudi
    comparé à tort à la position attendue pour mercredi). En comparant
    par jour de semaine réel, un jour manquant est simplement ignoré
    (ni confirmé ni contredit), sans décaler les jours suivants.
    """
    roles_by_weekday: dict[int, str] = {
        record.weekday: record.role for record in day_records if record.weekday <= 4
    }
    matches: dict[str, float] = {}

    for profile_name, signature in WEEKLY_PROFILE_SIGNATURES.items():
        observed_weekdays = sorted(roles_by_weekday)
        if not observed_weekdays:
            continue

        is_still_valid = all(
            roles_by_weekday[weekday] == signature[weekday]
            for weekday in observed_weekdays
            if weekday < len(signature)
        )
        if is_still_valid:
            matches[profile_name] = len(observed_weekdays) / len(signature)

    return matches


# ---------------------------------------------------------------------
# Dealing Range
# ---------------------------------------------------------------------


def _compute_dealing_range(
    candles: list[Candle],
    current_price: float,
    swing_lookback: int,
    equilibrium_band_pct: float,
) -> DealingRange | None:
    """
    Calcule le Dealing Range actuel : encadré par le swing high non
    mitigé le plus proche au-dessus du prix, et le swing low non mitigé
    le plus proche en dessous.

    Conditions nécessaires (d'après le concept ICT de Dealing Range) :
    il faut qu'un swing high non balayé existe au-dessus du prix ET
    qu'un swing low non balayé existe en dessous. Si l'un des deux
    manque, il n'y a pas de dealing range valide actuellement — le
    marché est en phase de "price discovery" / expansion hors range.

    Args:
        candles: Bougies closes, ordonnées chronologiquement.
        current_price: Prix actuel à situer dans le range.
        swing_lookback: Fenêtre de détection des swings.
        equilibrium_band_pct: Largeur (en % du range) de la bande
            centrale considérée comme "équilibre" plutôt que premium ou
            discount.

    Returns:
        Un DealingRange, ou None si les conditions ne sont pas réunies.
    """
    swings = _find_swing_points(candles, swing_lookback)

    candidate_highs: list[float] = []
    for swing in swings:
        if swing.kind != "high" or swing.price <= current_price:
            continue
        subsequent = candles[swing.index + 1 :]
        swept = any(c.high > swing.price for c in subsequent)
        if not swept:
            candidate_highs.append(swing.price)

    candidate_lows: list[float] = []
    for swing in swings:
        if swing.kind != "low" or swing.price >= current_price:
            continue
        subsequent = candles[swing.index + 1 :]
        swept = any(c.low < swing.price for c in subsequent)
        if not swept:
            candidate_lows.append(swing.price)

    # Écarte les swings trop éloignés du prix actuel avant la sélection
    # finale : un extrême historique (souvent un plus bas/haut absolu)
    # reste par définition "non mitigé" indéfiniment — s'il devient
    # l'unique candidat restant (tous les swings plus récents ayant été
    # balayés depuis), il resterait sélectionné pour toujours, même
    # totalement hors de propos (cas réel TRX : un plus bas de 2018 à
    # $0,0453 alors que le prix actuel est à $0,32). Même filtre de
    # distance que pour les CRT (MAX_ANCHOR_DISTANCE_RATIO), pour rester
    # cohérent dans tout le projet.
    candidate_highs = [
        price
        for price in candidate_highs
        if price > 0 and abs(current_price - price) / price <= MAX_ANCHOR_DISTANCE_RATIO
    ]
    candidate_lows = [
        price
        for price in candidate_lows
        if price > 0 and abs(current_price - price) / price <= MAX_ANCHOR_DISTANCE_RATIO
    ]

    if not candidate_highs or not candidate_lows:
        return None

    range_high = min(candidate_highs)
    range_low = max(candidate_lows)
    range_size = range_high - range_low

    if range_size <= 0:
        return None

    equilibrium = (range_high + range_low) / 2
    position_ratio = (current_price - range_low) / range_size

    half_band = equilibrium_band_pct / 2
    if abs(position_ratio - 0.5) <= half_band:
        zone: Literal["premium", "discount", "equilibrium"] = "equilibrium"
    elif position_ratio > 0.5:
        zone = "premium"
    else:
        zone = "discount"

    return DealingRange(
        range_low=range_low, range_high=range_high, equilibrium=equilibrium, zone=zone
    )


# ---------------------------------------------------------------------
# Calendrier économique (SoSoValue) & SMT (stub restant)
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Calendrier économique (SoSoValue) & SMT (stub restant)
# ---------------------------------------------------------------------


DEFAULT_QUIET_DAY_LOOKBACK = 10
DEFAULT_QUIET_DAY_RATIO_THRESHOLD = 0.3  # amplitude < 30% de la moyenne récente -> jour "blanc"


def _compute_easter_date(year: int) -> date:
    """
    Calcule la date de Pâques (dimanche) pour une année donnée, via
    l'algorithme de Meeus/Jones/Butcher (calendrier grégorien) — évite
    d'ajouter une dépendance externe (ex: python-dateutil) juste pour
    ce calcul, plusieurs jours fériés US et français en dépendant
    (Good Friday, Lundi de Pâques, Ascension, Lundi de Pentecôte).
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Date du n-ième jour `weekday` (0=lundi...6=dimanche) d'un mois donné."""
    first_day = date(year, month, 1)
    days_until_first = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_first + (n - 1) * 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Date du DERNIER jour `weekday` d'un mois donné."""
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def _get_us_holidays(year: int) -> dict[date, str]:
    """
    Jours fériés fédéraux US majeurs affectant les marchés financiers
    (fermeture ou séance réduite au NYSE) — liste volontairement limitée
    aux jours ayant un vrai impact sur la liquidité, pas la liste
    complète des jours fériés administratifs.
    """
    easter = _compute_easter_date(year)
    return {
        date(year, 1, 1): "New Year's Day",
        _nth_weekday_of_month(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday_of_month(year, 2, 0, 3): "Presidents' Day",
        easter - timedelta(days=2): "Good Friday",
        _last_weekday_of_month(year, 5, 0): "Memorial Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        _nth_weekday_of_month(year, 9, 0, 1): "Labor Day",
        _nth_weekday_of_month(year, 11, 3, 4): "Thanksgiving Day",
        date(year, 12, 25): "Christmas Day",
    }


def _get_france_holidays(year: int) -> dict[date, str]:
    """Jours fériés légaux français majeurs."""
    easter = _compute_easter_date(year)
    return {
        date(year, 1, 1): "Jour de l'An",
        easter + timedelta(days=1): "Lundi de Pâques",
        date(year, 5, 1): "Fête du Travail",
        date(year, 5, 8): "Victoire 1945",
        easter + timedelta(days=39): "Ascension",
        easter + timedelta(days=50): "Lundi de Pentecôte",
        date(year, 7, 14): "Fête Nationale",
        date(year, 8, 15): "Assomption",
        date(year, 11, 1): "Toussaint",
        date(year, 11, 11): "Armistice",
        date(year, 12, 25): "Noël",
    }


def _check_holiday(today: date) -> dict[str, Any]:
    """
    Vérifie si `today` est un jour férié majeur aux US et/ou en France.
    Purement informatif — ne bloque rien, signale juste le fait pour
    contextualiser une éventuelle faible liquidité.

    Returns:
        Dict {"us_holiday": nom du jour férié ou None,
        "france_holiday": nom du jour férié ou None}.
    """
    return {
        "us_holiday": _get_us_holidays(today.year).get(today),
        "france_holiday": _get_france_holidays(today.year).get(today),
    }


def _detect_quiet_day(
    daily_candles: list[Candle],
    lookback: int = DEFAULT_QUIET_DAY_LOOKBACK,
    ratio_threshold: float = DEFAULT_QUIET_DAY_RATIO_THRESHOLD,
) -> dict[str, Any]:
    """
    Détecte une journée "blanche" : une amplitude (High - Low)
    anormalement faible sur la dernière bougie daily close, comparée à
    la moyenne des `lookback` bougies précédentes — signe possible d'un
    jour férié, d'une grève affectant les marchés, ou plus généralement
    d'une liquidité très réduite.

    Ne fait AUCUNE distinction entre les causes possibles (jour férié
    connu, grève, actualité simplement calme) : signale seulement le
    FAIT d'une volatilité anormalement basse, à charge de l'IA d'en
    évoquer la cause probable dans son commentaire.

    Args:
        daily_candles: Bougies daily CLOSES (bougie en cours déjà exclue).
        lookback: Nombre de bougies précédentes utilisées pour la moyenne.
        ratio_threshold: Seuil (amplitude dernière bougie / moyenne)
            en dessous duquel la journée est jugée "blanche".

    Returns:
        Dict avec `available`, `is_quiet`, et `range_ratio` (amplitude
        de la dernière bougie / moyenne récente, utile pour juger de la
        sévérité même quand is_quiet est proche du seuil).
    """
    if len(daily_candles) < lookback + 1:
        return {"available": False, "is_quiet": False, "range_ratio": None}

    recent = daily_candles[-(lookback + 1) : -1]
    last = daily_candles[-1]

    avg_range = sum(c.high - c.low for c in recent) / len(recent)
    if avg_range <= 0:
        return {"available": False, "is_quiet": False, "range_ratio": None}

    last_range = last.high - last.low
    ratio = last_range / avg_range

    return {
        "available": True,
        "is_quiet": ratio < ratio_threshold,
        "range_ratio": ratio,
    }


def _get_economic_calendar_context(
    economic_events: list[MacroEvent] | None,
) -> dict[str, Any]:
    """
    Signale les événements macro-économiques du jour, à partir du
    calendrier fourni (collectors/macro.py::get_macro_events(), via
    MarketContext.economic_events).

    Volontairement limité au FAIT BRUT ("tel événement est prévu
    aujourd'hui") : cette fonction ne tente aucune analyse d'impact
    haussier/baissier — ce type d'interprétation avait été envisagé via
    une IA dédiée, testée puis entièrement retirée du pipeline (jugée
    trop imprécise, voir CLAUDE.md) ; aucune IA ne l'effectue donc plus
    nulle part dans le projet.

    Returns:
        Dict avec `available` (le calendrier a-t-il été fourni),
        `today_events` (liste des noms d'événements prévus aujourd'hui,
        éventuellement vide), et `has_event_today` (bool pratique pour
        un filtrage rapide côté IA ou décision).
    """
    if not economic_events:
        return {
            "available": False,
            "today_events": [],
            "has_event_today": False,
        }

    today = datetime.now(timezone.utc).date()
    today_events = next(
        (event.events for event in economic_events if event.date == today), []
    )

    return {
        "available": True,
        "today_events": today_events,
        "has_event_today": bool(today_events),
    }


def _match_high_impact_keyword(event_name: str) -> str | None:
    """
    Renvoie le mot-clé haut-impact (CPI, FOMC, NFP, PPI) trouvé dans le
    nom de l'événement, ou None si l'événement n'en fait pas partie.
    """
    upper_name = event_name.upper()
    for keyword, variants in HIGH_IMPACT_EVENT_KEYWORDS.items():
        if any(variant in upper_name for variant in variants):
            return keyword
    return None


def _get_high_impact_news_context(
    economic_events: list[MacroEvent] | None,
) -> dict[str, Any]:
    """
    Signale les événements macro à haut impact (CPI, FOMC, NFP, PPI)
    prévus aujourd'hui, avec une fenêtre indicative à éviter pour une
    entrée — reproduit l'approche personnelle décrite : "je ne
    m'intéresse qu'aux news rouges qui peuvent entraîner de fortes
    fluctuations, et j'évite de rentrer pendant cette news".

    La fenêtre est basée sur des heures de publication standard
    (STANDARD_RELEASE_TIMES_NY), pas sur l'heure exacte de l'événement
    (non fournie par l'API SoSoValue, qui ne donne que la date).

    Ne fait aucune analyse d'impact haussier/baissier — seulement le
    signal factuel "tel événement à risque est prévu, fenêtre à éviter
    HH:MM-HH:MM heure NY, actuellement dans la fenêtre : oui/non".
    """
    if not economic_events:
        return {
            "available": False,
            "high_impact_today": [],
            "avoid_windows": [],
            "currently_in_avoid_window": False,
        }

    today = datetime.now(timezone.utc).date()
    today_events = next(
        (event.events for event in economic_events if event.date == today), []
    )

    now_ny = datetime.now(NY_TZ)
    high_impact_today: list[str] = []
    avoid_windows: list[dict[str, Any]] = []

    for event_name in today_events:
        keyword = _match_high_impact_keyword(event_name)
        if keyword is None:
            continue

        high_impact_today.append(event_name)

        release_time = STANDARD_RELEASE_TIMES_NY.get(keyword)
        if release_time is None:
            continue

        release_hour, release_minute = release_time
        release_dt_ny = now_ny.replace(
            hour=release_hour, minute=release_minute, second=0, microsecond=0
        )
        window_start = release_dt_ny - timedelta(minutes=AVOID_WINDOW_MINUTES_BEFORE)
        window_end = release_dt_ny + timedelta(minutes=AVOID_WINDOW_MINUTES_AFTER)
        currently_active = window_start <= now_ny <= window_end

        avoid_windows.append(
            {
                "event": event_name,
                "keyword": keyword,
                "window_start_ny": window_start.strftime("%H:%M"),
                "window_end_ny": window_end.strftime("%H:%M"),
                "currently_active": currently_active,
            }
        )

    return {
        "available": True,
        "high_impact_today": high_impact_today,
        "avoid_windows": avoid_windows,
        "currently_in_avoid_window": any(w["currently_active"] for w in avoid_windows),
    }


def _get_smt_context(
    primary_candles: dict[str, CandleSeries] | None,
    secondary_candles: dict[str, CandleSeries] | None,
    secondary_symbol: str | None,
    timeframe: str = "daily",
    swing_lookback: int = 2,
) -> dict[str, Any]:
    """
    Détecte une divergence SMT (Smart Money Technique) entre l'actif
    principal et une paire corrélée (ex: BTC vs ETH) : l'un dessine un
    nouveau plus haut/plus bas swing tandis que l'autre ne confirme
    pas — signe classique de divergence smart money, potentiel signal
    de retournement.

    Ne prend aucune décision de trading à elle seule : signale la
    divergence en tant que fait, à charge des autres règles/de l'IA de
    la mettre en contexte.

    Args:
        primary_candles: context.candles de l'actif principal.
        secondary_candles: context.secondary_candles (paire corrélée),
            même structure par timeframe que `primary_candles`.
        secondary_symbol: Nom de la paire corrélée (ex: "ETH"), pour
            construire un message lisible.
        timeframe: Timeframe utilisée pour la comparaison des swings
            (défaut "daily" — un choix pragmatique ; le SMT peut en
            théorie se vérifier sur n'importe quelle timeframe).
        swing_lookback: Fenêtre de détection des swings (voir
            keylevel_rule.py::_find_swing_points).

    Returns:
        Dict avec `available` (les deux séries de bougies étaient-elles
        fournies), `secondary_symbol`, et `divergence` (None si aucune
        divergence détectée, sinon {"type": "bullish_smt"|"bearish_smt",
        "detail": str}).
    """
    if not primary_candles or not secondary_candles or not secondary_symbol:
        return {"available": False, "secondary_symbol": secondary_symbol, "divergence": None}

    primary_series = primary_candles.get(timeframe)
    secondary_series = secondary_candles.get(timeframe)
    if primary_series is None or secondary_series is None or len(primary_series) < 2 or len(secondary_series) < 2:
        return {"available": False, "secondary_symbol": secondary_symbol, "divergence": None}

    primary_swings = _find_swing_points(primary_series.candles[:-1], swing_lookback)
    secondary_swings = _find_swing_points(secondary_series.candles[:-1], swing_lookback)

    primary_highs = [s.price for s in primary_swings if s.kind == "high"]
    secondary_highs = [s.price for s in secondary_swings if s.kind == "high"]
    primary_lows = [s.price for s in primary_swings if s.kind == "low"]
    secondary_lows = [s.price for s in secondary_swings if s.kind == "low"]

    divergence: dict[str, str] | None = None

    if len(primary_highs) >= 2 and len(secondary_highs) >= 2:
        primary_new_high = primary_highs[-1] > primary_highs[-2]
        secondary_new_high = secondary_highs[-1] > secondary_highs[-2]
        if primary_new_high and not secondary_new_high:
            divergence = {
                "type": "bearish_smt",
                "detail": (
                    f"Nouveau plus haut confirmé, mais pas sur {secondary_symbol} "
                    "-> divergence baissière potentielle (SMT)"
                ),
            }

    if divergence is None and len(primary_lows) >= 2 and len(secondary_lows) >= 2:
        primary_new_low = primary_lows[-1] < primary_lows[-2]
        secondary_new_low = secondary_lows[-1] < secondary_lows[-2]
        if primary_new_low and not secondary_new_low:
            divergence = {
                "type": "bullish_smt",
                "detail": (
                    f"Nouveau plus bas confirmé, mais pas sur {secondary_symbol} "
                    "-> divergence haussière potentielle (SMT)"
                ),
            }

    return {
        "available": True,
        "secondary_symbol": secondary_symbol,
        "divergence": divergence,
    }


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


class NarrativeRule(BaseRule):
    """
    Règle de narrative HTF : combine Weekly Profiles et Dealing Range
    pour donner un contexte directionnel de haut niveau. Se déclenche
    dès qu'un profil hebdomadaire reste compatible avec au moins
    `min_days_for_trigger` jours observés cette semaine.

    Le Dealing Range est toujours calculé et inclus dans les métadonnées
    (contexte utile pour l'IA et les autres règles), même s'il ne
    déclenche pas l'alerte à lui seul — sinon, la règle se déclencherait
    en permanence, un dealing range existant la plupart du temps.
    """

    name: ClassVar[str] = "narrative_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("narrative_rule", {})
        range_threshold_pct: float = cfg.get(
            "range_threshold_pct", DEFAULT_RANGE_THRESHOLD_PCT
        )
        expansion_threshold_pct: float = cfg.get(
            "expansion_threshold_pct", DEFAULT_EXPANSION_THRESHOLD_PCT
        )
        swing_lookback: int = cfg.get("swing_lookback", DEFAULT_SWING_LOOKBACK)
        equilibrium_band_pct: float = cfg.get(
            "equilibrium_band_pct", DEFAULT_EQUILIBRIUM_BAND_PCT
        )
        min_days_for_trigger: int = cfg.get(
            "min_days_for_trigger", DEFAULT_MIN_DAYS_FOR_TRIGGER
        )
        min_candles_required: int = cfg.get(
            "min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED
        )
        pre_week_lookback: int = cfg.get("pre_week_lookback", DEFAULT_PRE_WEEK_LOOKBACK)
        purge_min_excursion_pct: float = cfg.get(
            "purge_min_excursion_pct", DEFAULT_PURGE_MIN_EXCURSION_PCT
        )

        # Le calendrier économique, les news à haut impact et les jours
        # fériés ne dépendent d'aucune bougie -> calculés AVANT tout
        # early-return lié à l'absence/l'insuffisance de données daily,
        # pour ne jamais être accidentellement coupés par ces conditions.
        economic_calendar = _get_economic_calendar_context(context.economic_events)
        high_impact_news = _get_high_impact_news_context(context.economic_events)
        holiday = _check_holiday(datetime.now(timezone.utc).date())
        holiday_names = [name for name in (holiday.get("us_holiday"), holiday.get("france_holiday")) if name]

        if not context.candles or "daily" not in context.candles:
            return RuleResult(
                rule_name=self.name,
                triggered=bool(high_impact_news["high_impact_today"]) or bool(holiday_names),
                message="Historique daily indisponible pour la narrative HTF",
                severity=(
                    "critical"
                    if high_impact_news["currently_in_avoid_window"]
                    else "warning" if holiday_names else "info"
                ),
                metadata={
                    "economic_calendar": economic_calendar,
                    "high_impact_news": high_impact_news,
                    "quiet_day": {"available": False, "is_quiet": False, "range_ratio": None},
                    "holiday": holiday,
                },
            )

        daily_series = context.candles["daily"]
        closed_candles = daily_series.candles[:-1]
        quiet_day = _detect_quiet_day(closed_candles)

        if len(closed_candles) < min_candles_required:
            return RuleResult(
                rule_name=self.name,
                triggered=bool(high_impact_news["high_impact_today"]) or bool(holiday_names),
                message="Historique daily insuffisant pour la narrative HTF",
                severity=(
                    "critical"
                    if high_impact_news["currently_in_avoid_window"]
                    else "warning" if holiday_names else "info"
                ),
                metadata={
                    "economic_calendar": economic_calendar,
                    "high_impact_news": high_impact_news,
                    "quiet_day": quiet_day,
                    "holiday": holiday,
                },
            )

        dealing_range = _compute_dealing_range(
            closed_candles, context.market_data.price_usd, swing_lookback, equilibrium_band_pct
        )

        week_candles, preceding_candles = _get_current_week_candles(
            closed_candles, pre_week_lookback
        )
        keylevel_config: dict[str, Any] = config.get("keylevel_rule", {})
        day_records = _classify_week(
            week_candles,
            preceding_candles,
            range_threshold_pct,
            expansion_threshold_pct,
            keylevel_config,
            purge_min_excursion_pct,
        )
        matched_profiles = _match_weekly_profiles(day_records)

        significant_matches = (
            matched_profiles if len(day_records) >= min_days_for_trigger else {}
        )

        message_parts: list[str] = []
        if significant_matches:
            profiles_label = ", ".join(
                f"{name} ({confidence * 100:.0f}%)"
                for name, confidence in significant_matches.items()
            )
            message_parts.append(f"Profil(s) hebdomadaire(s) en formation : {profiles_label}")

        if dealing_range:
            message_parts.append(
                f"Dealing range [${dealing_range.range_low:,.2f} - "
                f"${dealing_range.range_high:,.2f}], prix en zone {dealing_range.zone}"
            )
        else:
            message_parts.append("Pas de dealing range valide (marché en price discovery)")

        if high_impact_news["high_impact_today"]:
            events_label = ", ".join(high_impact_news["high_impact_today"])
            if high_impact_news["currently_in_avoid_window"]:
                message_parts.append(
                    f"⚠️ Dans la fenêtre d'évitement d'une news à haut impact ({events_label})"
                )
            else:
                message_parts.append(f"News à haut impact prévue aujourd'hui : {events_label}")

        if quiet_day["is_quiet"]:
            message_parts.append(
                f"⚠️ Journée blanche détectée (amplitude à {quiet_day['range_ratio']:.0%} de la "
                "moyenne récente) — possible jour férié, grève, ou liquidité très réduite"
            )

        if holiday_names:
            message_parts.append(f"⚠️ Jour férié aujourd'hui : {', '.join(holiday_names)}")

        triggered = (
            bool(significant_matches)
            or bool(high_impact_news["high_impact_today"])
            or quiet_day["is_quiet"]
            or bool(holiday_names)
        )

        severity: Literal["info", "warning", "critical"] = "info"
        if significant_matches:
            max_confidence = max(significant_matches.values())
            if max_confidence >= 1.0:
                severity = "critical"
            elif max_confidence >= 0.6:
                severity = "warning"
        if high_impact_news["currently_in_avoid_window"]:
            severity = "critical"
        elif high_impact_news["high_impact_today"] and severity == "info":
            severity = "warning"
        if quiet_day["is_quiet"] and severity == "info":
            severity = "warning"
        if holiday_names and severity == "info":
            severity = "warning"

        metadata: dict[str, Any] = {
            "weekly_profiles": significant_matches,
            "smt_context": _get_smt_context(
                context.candles, context.secondary_candles, context.secondary_symbol
            ),
            "days_observed_this_week": len(day_records),
            "day_roles": [
                {
                    "date": record.ny_date.isoformat(),
                    "weekday": record.weekday,
                    "role": record.role,
                    "direction": record.direction,
                    "body_pct": round(record.body_pct, 3),
                    "purged_zone_kind": record.purged_zone_kind,
                }
                for record in day_records
            ],
            "dealing_range": (
                {
                    "range_low": dealing_range.range_low,
                    "range_high": dealing_range.range_high,
                    "equilibrium": dealing_range.equilibrium,
                    "zone": dealing_range.zone,
                }
                if dealing_range
                else None
            ),
            "economic_calendar": economic_calendar,
            "high_impact_news": high_impact_news,
            "quiet_day": quiet_day,
            "holiday": holiday,
        }

        return RuleResult(
            rule_name=self.name,
            triggered=triggered,
            message=" | ".join(message_parts),
            severity=severity,
            metadata=metadata,
        )