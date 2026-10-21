"""
rules/purge_rule.py

Purge Rule : détermine si la purge d'un pattern CRT (visé en LTF) est
"high probability", selon plusieurs filtres de timing :

  1. KillZone (pour un CRT Daily) : la purge doit se produire pendant la
     session de Londres (2h-5h NY) ou de New York (7h-10h NY).
  2. Timing par jour de semaine (pour un CRT Daily, en complément de la
     KillZone) : Lundi -> Mardi et/ou Mercredi ; Vendredi -> Lundi.
     Autres jours d'ancre non référencés = pas de confirmation par ce
     filtre (mais la KillZone reste vérifiée indépendamment).
  3. Timing hebdomadaire (pour un CRT Weekly) : la purge est attendue la
     semaine suivant l'ancre (Week 1 -> Week 2).
  4. Timing horaire précis (pour un CRT 4H) : selon la catégorie d'actif
     (Forex, Indices, Crypto), certaines heures d'ouverture de bougie 4H
     appellent une purge à des heures précises. Le Bitcoin est traité
     par défaut comme un indice (voir justification dans le message
     d'accompagnement), configurable.
  5. Algo Times : fenêtres de quelques minutes (ex: 8h20-8h40) où les
     purges algorithmiques sont réputées plus fréquentes ; vérifié en
     complément des filtres Daily/4H.

LIMITE DE DONNÉES IMPORTANTE : les filtres KillZone/Algo Times ont
besoin de savoir à quel moment précis (idéalement M1) la mèche a
dépassé le niveau de l'ancre. Avec des bougies 1h (granularité intraday
actuelle, via Binance), la précision de détection du "moment de purge"
est bornée à cette granularité — un travail futur sur la source de
données améliorera cette précision. Les filtres par
jour de semaine et hebdomadaire, eux, ne dépendent que des bougies
Daily/Weekly elles-mêmes et ne souffrent pas de cette limite.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal

from loguru import logger

from config.constants import LONDON_SESSION_HOURS_NY, NEW_YORK_SESSION_HOURS_NY, NY_TZ
from models.market_data import Candle
from rules.crt_rule import CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME, CrtPattern, _detect_crt_pattern
from rules.engine import BaseRule, MarketContext, RuleResult

AssetCategory = Literal["forex", "indices", "crypto"]

# --- KillZones (heure NY), pour les CRT Daily ---
# Réutilise les mêmes bornes que narrative_ltf_rule.py (config/constants.py)
# pour Londres/NY, afin d'éviter toute divergence entre les deux.
LONDON_KILLZONE = LONDON_SESSION_HOURS_NY
NEW_YORK_KILLZONE = NEW_YORK_SESSION_HOURS_NY

# --- Timing par jour de semaine, pour les CRT Daily (0=Lundi...4=Vendredi) ---
# Complément de la KillZone : les autres jours d'ancre (Mardi, Mercredi,
# Jeudi) ne sont pas référencés faute de règle donnée — ce filtre ne les
# confirme ni ne les infirme, la KillZone reste seule décisive pour eux.
DAILY_CRT_WEEKDAY_TIMING: dict[int, list[int]] = {
    0: [1, 2],  # Lundi -> Mardi et/ou Mercredi
    4: [0],  # Vendredi -> Lundi (semaine suivante)
}

# Nombre de semaines attendu entre l'ancre et la purge d'un CRT Weekly
# (Week 1 -> Week 2).
WEEKLY_CRT_EXPECTED_GAP_WEEKS = 1

# --- Grilles de bougies 4H (heure d'ouverture NY), par catégorie ---
FOREX_4H_GRID_HOURS: tuple[int, ...] = (1, 5, 9, 13, 17, 21)
INDICES_4H_GRID_HOURS: tuple[int, ...] = (2, 6, 10, 14, 18, 22)
# Le Bitcoin utilise la grille Indices par défaut (voir docstring module).
CRYPTO_4H_GRID_HOURS: tuple[int, ...] = INDICES_4H_GRID_HOURS

# --- Table de timing "ancre 4H -> heure(s) de purge attendue(s)" ---
FOREX_4H_TIMING: dict[int, list[int]] = {
    17: [3],
    21: [1, 5],
    5: [9, 11],
}
INDICES_4H_TIMING: dict[int, list[int]] = {
    14: [2],
    18: [6],
    22: [10],
}
# Table crypto par défaut = alignée sur Indices (même justification que
# la grille). Configurable indépendamment si vous établissez une table
# empirique propre au Bitcoin plus tard.
CRYPTO_4H_TIMING: dict[int, list[int]] = INDICES_4H_TIMING

CATEGORY_GRIDS: dict[AssetCategory, tuple[int, ...]] = {
    "forex": FOREX_4H_GRID_HOURS,
    "indices": INDICES_4H_GRID_HOURS,
    "crypto": CRYPTO_4H_GRID_HOURS,
}
CATEGORY_TIMING_TABLES: dict[AssetCategory, dict[int, list[int]]] = {
    "forex": FOREX_4H_TIMING,
    "indices": INDICES_4H_TIMING,
    "crypto": CRYPTO_4H_TIMING,
}

# --- Algo Times (heure:minute NY, début et fin de chaque fenêtre) ---
ALGO_TIME_WINDOWS: list[tuple[int, int, int, int]] = [
    (8, 20, 8, 40),
    (9, 20, 9, 40),
    (9, 50, 10, 10),
    (10, 20, 10, 40),
    (10, 50, 11, 10),
    (11, 20, 11, 40),
]

DEFAULT_ASSET_CATEGORY: AssetCategory = "crypto"
DEFAULT_TIMING_TOLERANCE_HOURS = 1
DEFAULT_MIN_CANDLES_REQUIRED = 20

# Simplification architecture (août 2026) : le robot ne suit plus que des
# actifs crypto (voir config/alerts.yaml::orchestration.assets) -- IBKR,
# et donc execution/ibkr_contracts.py::CONTRACT_SPECS dont cette fonction
# dérivait auparavant la catégorie par symbole (forex/cfd/future ->
# forex/indices), ont été retirés du projet avec le reste de l'exécution
# IBKR/OKX/Binance et du multi-utilisateur. resolve_asset_category()
# retourne donc systématiquement DEFAULT_ASSET_CATEGORY ("crypto") --
# conservée comme point d'extension (plutôt que supprimée) pour ne pas
# casser ses deux appelants (PurgeRule.evaluate() et
# rules/decision_engine.py::_get_closed_candles_for_scope(), qui doivent
# rester cohérents entre eux sur la même grille 4H) si des actifs
# non-crypto étaient un jour réintroduits.


def resolve_asset_category(symbol: str | None) -> AssetCategory:
    """
    Détermine la catégorie d'actif (forex/indices/crypto) d'un symbole --
    toujours "crypto" depuis la simplification architecture crypto-only
    (août 2026, voir commentaire ci-dessus). Le paramètre `symbol` est
    conservé pour ne pas changer la signature des deux appelants.
    """
    return DEFAULT_ASSET_CATEGORY


@dataclass
class PurgeAssessment:
    """
    Évaluation de la probabilité de purge d'un pattern CRT donné.

    `high_probability` repose uniquement sur le critère de timing propre
    à la timeframe du CRT (Week1->Week2 pour Weekly, jour de semaine
    pour Daily, table horaire par catégorie pour 4H). `killzone` et
    `in_algo_window` sont des confirmations complémentaires (bonus),
    calculées quand le moment exact de purge est disponible, mais ne
    conditionnent pas `high_probability` — elles renforcent seulement la
    sévérité globale quand elles s'ajoutent à une HP déjà confirmée.
    """

    crt_scope: Literal["weekly", "daily", "4h"]
    bias: Literal["bullish", "bearish"]
    dol_price: float
    high_probability: bool
    reason: str
    moment_timestamp: datetime | None
    killzone: str | None
    in_algo_window: bool


# ---------------------------------------------------------------------
# Resampling 4H aligné sur une grille spécifique à la catégorie
# ---------------------------------------------------------------------


def _resample_to_4h(candles: list[Candle], grid_hours: tuple[int, ...]) -> list[Candle]:
    """
    Agrège des bougies fines (intraday) en bougies 4H alignées sur une
    grille d'heures d'ouverture données (heure NY).
    """
    if not candles:
        return []

    sorted_hours = sorted(grid_hours)

    def bucket_start(ny_dt: datetime) -> datetime:
        candidate_hours = [h for h in sorted_hours if h <= ny_dt.hour]
        if candidate_hours:
            anchor_hour = candidate_hours[-1]
            bucket_date = ny_dt.date()
        else:
            anchor_hour = sorted_hours[-1]
            bucket_date = ny_dt.date() - timedelta(days=1)
        return datetime(
            bucket_date.year, bucket_date.month, bucket_date.day, anchor_hour, tzinfo=NY_TZ
        )

    groups: dict[datetime, list[Candle]] = defaultdict(list)
    for candle in candles:
        ny_dt = candle.timestamp.astimezone(NY_TZ)
        groups[bucket_start(ny_dt)].append(candle)

    resampled = []
    for key in sorted(groups.keys()):
        group = sorted(groups[key], key=lambda c: c.timestamp)
        resampled.append(
            Candle(
                timestamp=group[0].timestamp,
                open=group[0].open,
                close=group[-1].close,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
            )
        )

    return resampled


# ---------------------------------------------------------------------
# Localisation du "moment exact" de purge dans les données intraday
# ---------------------------------------------------------------------


def _find_purge_moment(
    anchor_candle: Candle,
    purge_candle: Candle,
    purge_side: Literal["high", "low"],
    purge_candle_duration: timedelta,
    intraday_candles: list[Candle],
) -> Candle | None:
    """
    Retrouve, parmi les bougies intraday couvrant la période de la
    bougie de purge (Daily ou 4H), la première bougie fine qui a
    effectivement dépassé le niveau de l'ancre du côté attendu — c'est
    le "moment exact" de la purge, à la granularité de nos données.
    """
    window_start = purge_candle.timestamp
    window_end = purge_candle.timestamp + purge_candle_duration

    candidates = sorted(
        (c for c in intraday_candles if window_start <= c.timestamp < window_end),
        key=lambda c: c.timestamp,
    )

    for candle in candidates:
        if purge_side == "high" and candle.high > anchor_candle.high:
            return candle
        if purge_side == "low" and candle.low < anchor_candle.low:
            return candle

    return None


# ---------------------------------------------------------------------
# Filtres de timing
# ---------------------------------------------------------------------


def _check_killzone(moment: Candle) -> tuple[bool, str]:
    """Vérifie si un instant tombe dans la KillZone Londres ou New York."""
    ny_hour = moment.timestamp.astimezone(NY_TZ).hour

    if LONDON_KILLZONE[0] <= ny_hour < LONDON_KILLZONE[1]:
        return True, "KillZone Londres"
    if NEW_YORK_KILLZONE[0] <= ny_hour < NEW_YORK_KILLZONE[1]:
        return True, "KillZone New York"
    return False, "hors KillZone"


def _check_4h_timing(
    anchor_hour: int, moment: Candle, timing_table: dict[int, list[int]], tolerance_hours: int
) -> tuple[bool, list[int]]:
    """
    Vérifie si l'heure du moment de purge correspond (à `tolerance_hours`
    près) à l'une des heures attendues pour cette heure d'ancre, selon la
    table de timing de la catégorie d'actif.
    """
    expected_hours = timing_table.get(anchor_hour, [])
    if not expected_hours:
        return False, expected_hours

    ny_hour = moment.timestamp.astimezone(NY_TZ).hour
    matched = any(abs(ny_hour - expected) <= tolerance_hours for expected in expected_hours)
    return matched, expected_hours


def _check_algo_window(moment: Candle) -> bool:
    """Vérifie si un instant tombe dans l'une des fenêtres Algo Times."""
    ny_dt = moment.timestamp.astimezone(NY_TZ)
    minutes_of_day = ny_dt.hour * 60 + ny_dt.minute

    for start_h, start_m, end_h, end_m in ALGO_TIME_WINDOWS:
        window_start = start_h * 60 + start_m
        window_end = end_h * 60 + end_m
        if window_start <= minutes_of_day <= window_end:
            return True

    return False


def _check_daily_weekday_timing(
    anchor_candle: Candle, purge_candle: Candle
) -> tuple[bool, list[int]]:
    """
    Vérifie si le jour de semaine de la purge correspond à ceux attendus
    pour le jour de semaine de l'ancre (Lundi -> Mardi/Mercredi ;
    Vendredi -> Lundi). Un jour d'ancre non référencé renvoie
    (False, []) — ni confirmé ni infirmé par ce filtre.
    """
    anchor_weekday = anchor_candle.timestamp.astimezone(NY_TZ).weekday()
    expected_weekdays = DAILY_CRT_WEEKDAY_TIMING.get(anchor_weekday, [])
    if not expected_weekdays:
        return False, expected_weekdays

    purge_weekday = purge_candle.timestamp.astimezone(NY_TZ).weekday()
    return purge_weekday in expected_weekdays, expected_weekdays


def _check_weekly_timing(anchor_candle: Candle, purge_candle: Candle) -> tuple[bool, int]:
    """
    Vérifie si la purge d'un CRT Weekly se produit bien la semaine
    suivant l'ancre (Week 1 -> Week 2). Le nombre de semaines d'écart
    est calculé sur la différence de dates plutôt que sur les numéros
    de semaine ISO, pour éviter les cas limites autour du changement
    d'année.
    """
    gap_days = (purge_candle.timestamp - anchor_candle.timestamp).days
    gap_weeks = round(gap_days / 7)
    return gap_weeks == WEEKLY_CRT_EXPECTED_GAP_WEEKS, gap_weeks


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


class PurgeRule(BaseRule):
    """
    Évalue la probabilité de la purge d'un CRT Daily et/ou 4H, selon les
    filtres KillZone, timing 4H par catégorie, et Algo Times.
    """

    name: ClassVar[str] = "purge_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("purge_rule", {})
        # Résolu automatiquement par symbole (voir resolve_asset_category)
        # -- plus de réglage global unique appliqué à tort à tous les
        # actifs (correctif août 2026, voir commentaire au-dessus de
        # _CONTRACT_TYPE_TO_ASSET_CATEGORY).
        asset_category: AssetCategory = resolve_asset_category(context.symbol)
        timing_tolerance_hours: int = cfg.get(
            "timing_tolerance_hours", DEFAULT_TIMING_TOLERANCE_HOURS
        )
        min_candles_required: int = cfg.get(
            "min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED
        )

        grid_hours = CATEGORY_GRIDS.get(asset_category, CRYPTO_4H_GRID_HOURS)
        timing_table = CATEGORY_TIMING_TABLES.get(asset_category, CRYPTO_4H_TIMING)

        assessments: list[PurgeAssessment] = []

        # Préfère la série fine "m15" (15 min, dédiée au resampling 4H
        # précis) si disponible ; repli sur "intraday" (1h) sinon — ex:
        # actifs Polygon, qui n'ont pas encore cette série séparée. Le
        # repli dégrade la précision du "moment de purge" mais ne fait
        # jamais échouer complètement l'évaluation.
        fine_series = (
            context.candles.get("m15") or context.candles.get("intraday")
            if context.candles
            else None
        )
        intraday_closed = fine_series.candles[:-1] if fine_series is not None else []
        intraday_available = len(intraday_closed) >= min_candles_required

        # Le CRT Weekly ne dépend pas de l'intraday pour son critère
        # principal (Week1->Week2) : évalué même si l'intraday est
        # indisponible. Si elle est disponible, elle sert uniquement au
        # bonus KillZone/Algo Time.
        weekly_series = context.candles.get("weekly") if context.candles else None
        if weekly_series is not None and len(weekly_series.candles) >= 3:
            weekly_crt = _detect_crt_pattern(
                weekly_series.candles[:-1],
                current_price=context.market_data.price_usd,
                max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get("weekly"),
            )
            if weekly_crt is not None:
                assessments.append(self._assess_weekly_crt(weekly_crt, intraday_closed))

        if intraday_available:
            # CRT Daily -> KillZone + jour de semaine
            daily_series = context.candles.get("daily")
            if daily_series is not None:
                daily_closed = daily_series.candles[:-1]
                daily_crt = _detect_crt_pattern(
                    daily_closed,
                    current_price=context.market_data.price_usd,
                    max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get("daily"),
                )
                if daily_crt is not None:
                    assessment = self._assess_daily_crt(daily_crt, intraday_closed)
                    if assessment is not None:
                        assessments.append(assessment)

            # CRT 4H -> timing par catégorie
            four_h_candles = _resample_to_4h(intraday_closed, grid_hours)
            if len(four_h_candles) >= 2:
                four_h_crt = _detect_crt_pattern(
                    four_h_candles[:-1],
                    current_price=context.market_data.price_usd,
                    max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get("4h"),
                )
                if four_h_crt is not None:
                    assessment = self._assess_4h_crt(
                        four_h_crt, intraday_closed, timing_table, timing_tolerance_hours
                    )
                    if assessment is not None:
                        assessments.append(assessment)

        if not assessments:
            message = "Aucun CRT Weekly/Daily/4H à évaluer actuellement"
            if not intraday_available:
                message += " (intraday indisponible ou insuffisant pour Daily/4H)"
            return RuleResult(rule_name=self.name, triggered=False, message=message)

        message = self._build_message(assessments)
        severity = self._compute_severity(assessments)
        metadata = self._build_metadata(assessments, asset_category)

        return RuleResult(
            rule_name=self.name,
            triggered=True,
            message=message,
            severity=severity,
            metadata=metadata,
        )

    @staticmethod
    def _assess_daily_crt(
        daily_crt: CrtPattern, intraday_candles: list[Candle]
    ) -> PurgeAssessment:
        """
        HP basée uniquement sur le jour de semaine ancre/purge (Lundi ->
        Mardi/Mercredi ; Vendredi -> Lundi). KillZone et Algo Time sont
        des bonus rapportés en plus, calculés si le moment exact de
        purge est retrouvé dans les données intraday.
        """
        weekday_confirmed, expected_weekdays = _check_daily_weekday_timing(
            daily_crt.anchor_candle, daily_crt.purge_candle
        )
        high_probability = bool(expected_weekdays) and weekday_confirmed
        reason = (
            f"jour de semaine {'confirmé' if weekday_confirmed else 'non confirmé'} "
            f"(attendu : {expected_weekdays})"
            if expected_weekdays
            else "jour d'ancre non référencé dans la table de timing"
        )

        moment = _find_purge_moment(
            daily_crt.anchor_candle,
            daily_crt.purge_candle,
            daily_crt.purge_side,
            timedelta(days=1),
            intraday_candles,
        )

        killzone_label: str | None = None
        in_algo = False
        if moment is not None:
            in_killzone, label = _check_killzone(moment)
            killzone_label = label if in_killzone else None
            in_algo = _check_algo_window(moment)
            if killzone_label:
                reason += f" ; + {killzone_label}"
            if in_algo:
                reason += " ; + Algo Time"

        return PurgeAssessment(
            crt_scope="daily",
            bias=daily_crt.bias,
            dol_price=daily_crt.dol_price,
            high_probability=high_probability,
            reason=reason,
            moment_timestamp=moment.timestamp if moment else None,
            killzone=killzone_label,
            in_algo_window=in_algo,
        )

    @staticmethod
    def _assess_weekly_crt(
        weekly_crt: CrtPattern, intraday_candles: list[Candle]
    ) -> PurgeAssessment:
        """
        HP basée uniquement sur l'écart ancre/purge (Week 1 -> Week 2).
        KillZone et Algo Time sont des bonus rapportés en plus, si le
        moment exact de purge est retrouvé dans l'intraday disponible
        (rarement le cas vu la profondeur nécessaire sur une semaine
        entière, mais vérifié si possible).
        """
        on_time, gap_weeks = _check_weekly_timing(
            weekly_crt.anchor_candle, weekly_crt.purge_candle
        )
        reason = (
            f"purge {gap_weeks} semaine(s) après l'ancre "
            f"(attendu : {WEEKLY_CRT_EXPECTED_GAP_WEEKS})"
        )

        moment = None
        if intraday_candles:
            moment = _find_purge_moment(
                weekly_crt.anchor_candle,
                weekly_crt.purge_candle,
                weekly_crt.purge_side,
                timedelta(weeks=1),
                intraday_candles,
            )

        killzone_label: str | None = None
        in_algo = False
        if moment is not None:
            in_killzone, label = _check_killzone(moment)
            killzone_label = label if in_killzone else None
            in_algo = _check_algo_window(moment)
            if killzone_label:
                reason += f" ; + {killzone_label}"
            if in_algo:
                reason += " ; + Algo Time"

        return PurgeAssessment(
            crt_scope="weekly",
            bias=weekly_crt.bias,
            dol_price=weekly_crt.dol_price,
            high_probability=on_time,
            reason=reason,
            moment_timestamp=moment.timestamp if moment else weekly_crt.purge_candle.timestamp,
            killzone=killzone_label,
            in_algo_window=in_algo,
        )

    @staticmethod
    def _assess_4h_crt(
        four_h_crt: CrtPattern,
        intraday_candles: list[Candle],
        timing_table: dict[int, list[int]],
        timing_tolerance_hours: int,
    ) -> PurgeAssessment | None:
        """
        HP basée uniquement sur la table horaire par catégorie d'actif
        (Forex/Indices/Crypto). KillZone et Algo Time sont des bonus
        rapportés en plus.
        """
        moment = _find_purge_moment(
            four_h_crt.anchor_candle,
            four_h_crt.purge_candle,
            four_h_crt.purge_side,
            timedelta(hours=4),
            intraday_candles,
        )
        if moment is None:
            return PurgeAssessment(
                crt_scope="4h",
                bias=four_h_crt.bias,
                dol_price=four_h_crt.dol_price,
                high_probability=False,
                reason="moment exact de purge non retrouvé dans les données intraday disponibles",
                moment_timestamp=None,
                killzone=None,
                in_algo_window=False,
            )

        anchor_hour = four_h_crt.anchor_candle.timestamp.astimezone(NY_TZ).hour
        on_time, expected_hours = _check_4h_timing(
            anchor_hour, moment, timing_table, timing_tolerance_hours
        )
        in_killzone, killzone_label_raw = _check_killzone(moment)
        killzone_label = killzone_label_raw if in_killzone else None
        in_algo = _check_algo_window(moment)

        reason = (
            f"ancre {anchor_hour}h -> purge attendue {expected_hours}h, "
            f"{'confirmée' if on_time else 'non confirmée'}"
            if expected_hours
            else f"ancre {anchor_hour}h non référencée dans la table de timing"
        )
        if killzone_label:
            reason += f" ; + {killzone_label}"
        if in_algo:
            reason += " ; + Algo Time"

        return PurgeAssessment(
            crt_scope="4h",
            bias=four_h_crt.bias,
            dol_price=four_h_crt.dol_price,
            high_probability=on_time,
            reason=reason,
            moment_timestamp=moment.timestamp,
            killzone=killzone_label,
            in_algo_window=in_algo,
        )

    @staticmethod
    def _build_message(assessments: list[PurgeAssessment]) -> str:
        parts = []
        for assessment in assessments:
            probability_label = "HIGH probability" if assessment.high_probability else "low probability"
            parts.append(
                f"CRT {assessment.crt_scope} ({assessment.bias}, DOL ${assessment.dol_price:,.2f}) : "
                f"{probability_label} ({assessment.reason})"
            )
        return " | ".join(parts)

    @staticmethod
    def _compute_severity(
        assessments: list[PurgeAssessment],
    ) -> Literal["info", "warning", "critical"]:
        """
        "warning" dès qu'au moins une HP simple (critère propre à la
        timeframe) est confirmée. "critical" quand une HP est en plus
        renforcée par au moins un bonus (KillZone et/ou Algo Time).
        """
        high_probability_count = sum(1 for a in assessments if a.high_probability)
        bonus_confirmed_count = sum(
            1 for a in assessments if a.high_probability and (a.killzone or a.in_algo_window)
        )

        if bonus_confirmed_count >= 1:
            return "critical"
        if high_probability_count >= 1:
            return "warning"
        return "info"

    @staticmethod
    def _build_metadata(
        assessments: list[PurgeAssessment], asset_category: AssetCategory
    ) -> dict[str, Any]:
        return {
            "asset_category": asset_category,
            "assessments": [
                {
                    "crt_scope": a.crt_scope,
                    "bias": a.bias,
                    "dol_price": a.dol_price,
                    "high_probability": a.high_probability,
                    "reason": a.reason,
                    "moment_timestamp": a.moment_timestamp.isoformat() if a.moment_timestamp else None,
                    "killzone": a.killzone,
                    "in_algo_window": a.in_algo_window,
                }
                for a in assessments
            ],
            "data_precision_note": (
                "moment de purge résolu à la granularité intraday disponible "
                "(1h actuellement, via Binance), pas M1"
            ),
        }
