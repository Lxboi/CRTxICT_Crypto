"""
rules/narrative_ltf_rule.py

Narrative Rule (LTF) : équivalent intraday de narrative_rule.py. Combine :

  1. Intraday Profiles (Normal Protraction, Delay Protraction, NY
     Continuation, NY Reversal, NY Reversal 2) — classification par
     session (Asie, Londres, NY Open, London Close) de la journée de
     trading en cours.
  2. Market Profile LTF — même concept que le Dealing Range HTF
     (narrative_rule.py::_compute_dealing_range), réutilisé tel quel sur
     les bougies intraday.
  3. Cohérence OHLC — indicateur [0,1] mesurant si l'Open/Low/High/Close
     de la session du jour dessine un mouvement propre dans le sens du
     biais DOL courant.
  4. Double Purge LTF — réutilise rules/dol_rule.py::_detect_double_purge.
  5. KeyLevel LTF — réutilise rules/keylevel_rule.py::find_key_levels.
  6. CRT LTF — réutilise rules/crt_rule.py::_detect_crt_pattern.

PRÉREQUIS DE DONNÉES : cette règle a besoin d'une série de bougies
intraday (granularité 1h, via collectors/binance.py — voir
INTERVAL_MAP) exposée sous la clé "intraday" dans MarketContext.candles,
peuplée par main.py à chaque cycle.

NON IMPLÉMENTÉ (reporté, comme convenu) : la règle d'alignement temporel
HTF <-> LTF (Monthly->Weekly LTF, Weekly->Daily LTF, etc.) — nos données
actuelles ne permettent pas cette granularité. Prévu pour une évolution
future avec des données plus poussées.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, ClassVar, Literal

from config.constants import (
    ASIA_SESSION_HOURS_NY,
    LONDON_CLOSE_SESSION_HOURS_NY,
    LONDON_SESSION_HOURS_NY,
    NEW_YORK_SESSION_HOURS_NY,
    NY_TZ,
)
from models.market_data import Candle
from rules.crt_rule import _detect_crt_pattern
from rules.dol_rule import _detect_double_purge
from rules.engine import BaseRule, MarketContext, RuleResult
from rules.keylevel_rule import KeyLevelZone, _filter_unmitigated, find_key_levels
from rules.narrative_rule import _compute_dealing_range, _is_purge_of_zone

SessionRole = Literal["range", "purge", "expansion", "correction", "neutral"]
SessionName = Literal["asia", "london", "ny_open", "lo_close"]
SESSION_ORDER: tuple[SessionName, ...] = ("asia", "london", "ny_open", "lo_close")

# --- Fenêtres de session par défaut, en heure NY (start inclus, end exclu) ---
# Hypothèse documentée : Asie 18h-00h, Londres 2h-5h (donné explicitement),
# NY Open 7h-10h (donné), London Close 10h-12h (donné). Les périodes de
# transition (00h-02h, 05h-07h, 12h-18h) ne sont volontairement pas
# classifiées. À ajuster si votre convention de session diffère.
# Réutilise les mêmes bornes que purge_rule.py (config/constants.py) pour
# Londres/NY/London Close, afin d'éviter toute divergence entre les deux.
DEFAULT_SESSION_WINDOWS: dict[SessionName, tuple[int, int]] = {
    "asia": ASIA_SESSION_HOURS_NY,
    "london": LONDON_SESSION_HOURS_NY,
    "ny_open": NEW_YORK_SESSION_HOURS_NY,
    "lo_close": LONDON_CLOSE_SESSION_HOURS_NY,
}
ASIA_START_HOUR_DEFAULT = ASIA_SESSION_HOURS_NY[0]  # heure de bascule vers le "jour de trading" suivant

# --- Paramètres par défaut, surchargeables via config/alerts.yaml ---
DEFAULT_RANGE_THRESHOLD_PCT = 0.3
DEFAULT_EXPANSION_THRESHOLD_PCT = 0.8
DEFAULT_PURGE_MIN_EXCURSION_PCT = 0.15
DEFAULT_PRE_DAY_LOOKBACK = 20
DEFAULT_SWING_LOOKBACK = 2
DEFAULT_EQUILIBRIUM_BAND_PCT = 0.1
DEFAULT_MIN_DAYS_FOR_TRIGGER = 2
DEFAULT_MIN_CANDLES_REQUIRED = 20
DEFAULT_DOUBLE_PURGE_WINDOW = 5

# Signatures des 5 profils intraday, Asie -> Londres -> NY Open -> LO Close.
# Vocabulaire du projet -> rôles internes : "accumulation" ~ "range",
# "reversal" ~ "correction" (mouvement à contre-sens de la direction
# dominante déjà établie dans la session).
INTRADAY_PROFILE_SIGNATURES: dict[str, list[SessionRole]] = {
    "normal_protraction": ["range", "purge", "expansion", "expansion"],
    "delay_protraction": ["purge", "expansion", "expansion", "expansion"],
    "ny_continuation": ["purge", "expansion", "purge", "expansion"],
    "ny_reversal": ["purge", "expansion", "correction", "correction"],
    "ny_reversal_2": ["range", "range", "expansion", "correction"],
}


@dataclass
class SessionRecord:
    """Classification d'une session intraday pour la journée de trading en cours."""

    session: SessionName
    role: SessionRole
    direction: Literal["bullish", "bearish"]
    body_pct: float
    purged_zone_kind: str | None = None


# ---------------------------------------------------------------------
# Regroupement par session (journée de trading NY)
# ---------------------------------------------------------------------


def _get_trading_day(ny_dt) -> date:
    """
    Détermine la "journée de trading" d'un horodatage NY : la journée
    commence à l'ouverture de la session asiatique (18h NY) et se
    termine à la London Close du lendemain. Un candle à 21h lundi
    appartient donc à la journée de trading de mardi.
    """
    if ny_dt.hour >= ASIA_START_HOUR_DEFAULT:
        return ny_dt.date() + timedelta(days=1)
    return ny_dt.date()


def _get_session_for_hour(hour: int, session_windows: dict[SessionName, tuple[int, int]]) -> SessionName | None:
    for session_name, (start, end) in session_windows.items():
        if start <= hour < end:
            return session_name
    return None


def _get_current_trading_day_candles(
    intraday_candles: list[Candle],
    session_windows: dict[SessionName, tuple[int, int]],
    pre_day_lookback: int,
) -> tuple[dict[SessionName, list[Candle]], list[Candle]]:
    """
    Sépare les bougies intraday en (bougies de la journée de trading la
    plus récente, regroupées par session ; bougies de contexte précédent).

    Returns:
        (dict {session: [candles]}, bougies de contexte précédant la
        journée de trading, utilisées comme référence de KL pour la
        détection de purge dès la session Asie).
    """
    if not intraday_candles:
        return {name: [] for name in SESSION_ORDER}, []

    last_ny = intraday_candles[-1].timestamp.astimezone(NY_TZ)
    target_day = _get_trading_day(last_ny)

    sessions: dict[SessionName, list[Candle]] = {name: [] for name in SESSION_ORDER}
    preceding: list[Candle] = []

    for candle in intraday_candles:
        ny_dt = candle.timestamp.astimezone(NY_TZ)
        candle_day = _get_trading_day(ny_dt)

        if candle_day == target_day:
            session = _get_session_for_hour(ny_dt.hour, session_windows)
            if session is not None:
                sessions[session].append(candle)
        elif candle_day < target_day:
            preceding.append(candle)

    for name in SESSION_ORDER:
        sessions[name].sort(key=lambda c: c.timestamp)
    preceding.sort(key=lambda c: c.timestamp)

    return sessions, preceding[-pre_day_lookback:]


# ---------------------------------------------------------------------
# Classification des sessions (même principe que _classify_week en HTF)
# ---------------------------------------------------------------------


def _classify_sessions(
    sessions: dict[SessionName, list[Candle]],
    preceding_candles: list[Candle],
    range_threshold_pct: float,
    expansion_threshold_pct: float,
    keylevel_config: dict[str, Any],
    purge_min_excursion_pct: float,
) -> list[SessionRecord]:
    """
    Classifie chaque session (dans l'ordre Asie -> Londres -> NY Open ->
    London Close) en un rôle, en agrégeant les bougies de la session en
    une bougie synthétique (open de la première, close de la dernière,
    max des highs, min des lows). Une session vide (pas de bougies) est
    ignorée (absente du résultat).

    La détection de purge réutilise les zones KeyLevel (FVG, Old H/L,
    Order Block, Breaker Block, Rejection Block) sur une fenêtre qui
    grandit session après session — même mécanisme que narrative_rule.py.
    """
    records: list[SessionRecord] = []
    dominant_direction: Literal["bullish", "bearish"] | None = None
    context_candles: list[Candle] = list(preceding_candles)

    for session_name in SESSION_ORDER:
        session_candles = sessions.get(session_name, [])
        if not session_candles:
            continue

        synthetic = Candle(
            timestamp=session_candles[0].timestamp,
            open=session_candles[0].open,
            close=session_candles[-1].close,
            high=max(c.high for c in session_candles),
            low=min(c.low for c in session_candles),
        )

        body_pct = abs(synthetic.close - synthetic.open) / synthetic.open * 100
        direction: Literal["bullish", "bearish"] = (
            "bullish" if synthetic.close >= synthetic.open else "bearish"
        )

        active_zones: list[KeyLevelZone] = []
        if len(context_candles) >= 2:
            raw_zones = find_key_levels(context_candles, keylevel_config)
            active_zones = _filter_unmitigated(raw_zones, context_candles)

        purged_zone = next(
            (
                zone
                for zone in active_zones
                if _is_purge_of_zone(synthetic, zone, purge_min_excursion_pct)
            ),
            None,
        )

        if purged_zone is not None:
            role: SessionRole = "purge"
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
            SessionRecord(
                session=session_name,
                role=role,
                direction=direction,
                body_pct=body_pct,
                purged_zone_kind=purged_zone.kind if purged_zone else None,
            )
        )

        context_candles.extend(session_candles)

    return records


def _match_intraday_profiles(session_records: list[SessionRecord]) -> dict[str, float]:
    """
    Compare les rôles de session observés aux 5 signatures connues,
    même logique de correspondance stricte de préfixe que pour les
    Weekly Profiles HTF (un profil reste "en vie" tant qu'aucune session
    observée ne le contredit).
    """
    observed_roles = [record.role for record in session_records]
    matches: dict[str, float] = {}

    for profile_name, signature in INTRADAY_PROFILE_SIGNATURES.items():
        compare_length = min(len(observed_roles), len(signature))
        if compare_length == 0:
            continue

        is_still_valid = all(
            observed_roles[i] == signature[i] for i in range(compare_length)
        )
        if is_still_valid:
            matches[profile_name] = compare_length / len(signature)

    return matches


# ---------------------------------------------------------------------
# Cohérence OHLC
# ---------------------------------------------------------------------


def _compute_ohlc_coherence(
    session_records: list[SessionRecord],
    sessions: dict[SessionName, list[Candle]],
    dol_bias: Literal["bullish", "bearish"] | None,
) -> float | None:
    """
    Mesure si l'Open/Low/High/Close de la journée de trading en cours
    (du début de la première session observée à la fin de la dernière)
    dessine un mouvement propre dans le sens du biais DOL fourni.

    Principe : pour un biais haussier, on veut un Open proche du Low de
    la journée et un Close proche du High (une bougie journalière bien
    "structurée" dans le sens haussier) ; l'inverse pour un biais
    baissier. Le score va de 0 (incohérent / mouvement inverse) à 1
    (parfaitement cohérent).

    Returns:
        Un score [0, 1], ou None si aucun biais DOL n'est fourni ou si
        aucune session n'a encore de données.
    """
    if dol_bias is None:
        return None

    all_candles = [c for name in SESSION_ORDER for c in sessions.get(name, [])]
    if not all_candles:
        return None

    day_open = all_candles[0].open
    day_close = all_candles[-1].close
    day_high = max(c.high for c in all_candles)
    day_low = min(c.low for c in all_candles)

    range_size = day_high - day_low
    if range_size <= 0:
        return None

    if dol_bias == "bullish":
        open_position = (day_open - day_low) / range_size
        close_position = (day_close - day_low) / range_size
    else:
        open_position = (day_high - day_open) / range_size
        close_position = (day_high - day_close) / range_size

    coherence = close_position - open_position
    return max(0.0, min(1.0, coherence))


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


class NarrativeLtfRule(BaseRule):
    """
    Règle de narrative LTF : combine Intraday Profiles, Market Profile
    (Dealing Range LTF), cohérence OHLC, Double Purge, KeyLevel et CRT,
    tous appliqués sur la série de bougies intraday.
    """

    name: ClassVar[str] = "narrative_ltf_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("narrative_ltf_rule", {})
        range_threshold_pct: float = cfg.get(
            "range_threshold_pct", DEFAULT_RANGE_THRESHOLD_PCT
        )
        expansion_threshold_pct: float = cfg.get(
            "expansion_threshold_pct", DEFAULT_EXPANSION_THRESHOLD_PCT
        )
        purge_min_excursion_pct: float = cfg.get(
            "purge_min_excursion_pct", DEFAULT_PURGE_MIN_EXCURSION_PCT
        )
        pre_day_lookback: int = cfg.get("pre_day_lookback", DEFAULT_PRE_DAY_LOOKBACK)
        swing_lookback: int = cfg.get("swing_lookback", DEFAULT_SWING_LOOKBACK)
        equilibrium_band_pct: float = cfg.get(
            "equilibrium_band_pct", DEFAULT_EQUILIBRIUM_BAND_PCT
        )
        min_sessions_for_trigger: int = cfg.get(
            "min_sessions_for_trigger", DEFAULT_MIN_DAYS_FOR_TRIGGER
        )
        min_candles_required: int = cfg.get(
            "min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED
        )
        double_purge_window: int = cfg.get(
            "double_purge_window", DEFAULT_DOUBLE_PURGE_WINDOW
        )
        session_windows: dict[SessionName, tuple[int, int]] = cfg.get(
            "session_windows", DEFAULT_SESSION_WINDOWS
        )

        if not context.candles or "intraday" not in context.candles:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message=(
                    "Historique intraday indisponible pour la narrative LTF "
                    "(nécessite context.candles['intraday'], non encore câblé "
                    "dans le pipeline principal)"
                ),
            )

        intraday_series = context.candles["intraday"]
        closed_candles = intraday_series.candles[:-1]

        if len(closed_candles) < min_candles_required:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Historique intraday insuffisant pour la narrative LTF",
            )

        current_price = context.market_data.price_usd
        keylevel_config: dict[str, Any] = config.get("keylevel_rule", {})

        # 1. Market Profile LTF (Dealing Range)
        dealing_range = _compute_dealing_range(
            closed_candles, current_price, swing_lookback, equilibrium_band_pct
        )

        # 2. KeyLevel LTF
        kl_zones = _filter_unmitigated(
            find_key_levels(closed_candles, keylevel_config), closed_candles
        )
        touched_kl_zones = [
            zone for zone in kl_zones if zone.distance_pct(current_price) <= 0.3
        ]

        # 3. CRT LTF
        crt_pattern = _detect_crt_pattern(closed_candles, current_price=current_price)

        # 4. Double Purge LTF
        double_purge_ref = _detect_double_purge(closed_candles, double_purge_window)

        # 5. Intraday Profiles
        sessions, preceding_candles = _get_current_trading_day_candles(
            closed_candles, session_windows, pre_day_lookback
        )
        session_records = _classify_sessions(
            sessions,
            preceding_candles,
            range_threshold_pct,
            expansion_threshold_pct,
            keylevel_config,
            purge_min_excursion_pct,
        )
        matched_profiles = (
            _match_intraday_profiles(session_records)
            if len(session_records) >= min_sessions_for_trigger
            else {}
        )

        # 6. Cohérence OHLC (biais déduit du CRT LTF si disponible, sinon None)
        dol_bias_hint = crt_pattern.bias if crt_pattern else None
        ohlc_coherence = _compute_ohlc_coherence(session_records, sessions, dol_bias_hint)

        triggered = bool(
            matched_profiles
            or crt_pattern is not None
            or double_purge_ref is not None
            or touched_kl_zones
        )

        if not triggered:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Aucun signal LTF significatif actuellement",
                metadata={
                    "dealing_range": self._serialize_dealing_range(dealing_range),
                    "ohlc_coherence": ohlc_coherence,
                },
            )

        message = self._build_message(
            matched_profiles, crt_pattern, double_purge_ref, touched_kl_zones, ohlc_coherence
        )
        severity = self._compute_severity(
            matched_profiles, crt_pattern, double_purge_ref, touched_kl_zones
        )
        metadata = self._build_metadata(
            session_records,
            matched_profiles,
            dealing_range,
            crt_pattern,
            double_purge_ref,
            touched_kl_zones,
            ohlc_coherence,
        )

        return RuleResult(
            rule_name=self.name,
            triggered=True,
            message=message,
            severity=severity,
            metadata=metadata,
        )

    @staticmethod
    def _serialize_dealing_range(dealing_range) -> dict[str, Any] | None:
        if dealing_range is None:
            return None
        return {
            "range_low": dealing_range.range_low,
            "range_high": dealing_range.range_high,
            "equilibrium": dealing_range.equilibrium,
            "zone": dealing_range.zone,
        }

    @staticmethod
    def _build_message(matched_profiles, crt_pattern, double_purge_ref, touched_kl_zones, ohlc_coherence) -> str:
        parts: list[str] = []

        if matched_profiles:
            profiles_label = ", ".join(
                f"{name} ({confidence * 100:.0f}%)" for name, confidence in matched_profiles.items()
            )
            parts.append(f"Profil(s) intraday en formation : {profiles_label}")

        if crt_pattern is not None:
            parts.append(
                f"CRT LTF détecté (biais {crt_pattern.bias}, DOL à ${crt_pattern.dol_price:,.2f})"
            )

        if double_purge_ref is not None:
            parts.append("Double purge LTF détectée")

        if touched_kl_zones:
            kinds = ", ".join(sorted({zone.kind for zone in touched_kl_zones}))
            parts.append(f"KL LTF testé(s) : {kinds}")

        if ohlc_coherence is not None:
            parts.append(f"Cohérence OHLC : {ohlc_coherence * 100:.0f}%")

        return " | ".join(parts)

    @staticmethod
    def _compute_severity(
        matched_profiles, crt_pattern, double_purge_ref, touched_kl_zones
    ) -> Literal["info", "warning", "critical"]:
        signal_count = sum(
            [
                bool(matched_profiles),
                crt_pattern is not None,
                double_purge_ref is not None,
                bool(touched_kl_zones),
            ]
        )
        full_profile_match = any(conf >= 1.0 for conf in matched_profiles.values())

        if full_profile_match and signal_count >= 3:
            return "critical"
        if signal_count >= 2:
            return "warning"
        return "info"

    def _build_metadata(
        self,
        session_records,
        matched_profiles,
        dealing_range,
        crt_pattern,
        double_purge_ref,
        touched_kl_zones,
        ohlc_coherence,
    ) -> dict[str, Any]:
        return {
            "intraday_profiles": matched_profiles,
            "sessions_observed": len(session_records),
            "session_roles": [
                {
                    "session": record.session,
                    "role": record.role,
                    "direction": record.direction,
                    "body_pct": round(record.body_pct, 3),
                    "purged_zone_kind": record.purged_zone_kind,
                }
                for record in session_records
            ],
            "dealing_range_ltf": self._serialize_dealing_range(dealing_range),
            "crt_ltf": (
                {
                    "bias": crt_pattern.bias,
                    "dol_price": crt_pattern.dol_price,
                    "purge_side": crt_pattern.purge_side,
                }
                if crt_pattern
                else None
            ),
            "double_purge_ltf": (
                {
                    "reference_timestamp": double_purge_ref.timestamp.isoformat(),
                    "reference_high": double_purge_ref.high,
                    "reference_low": double_purge_ref.low,
                    "timeframe": "intraday (1h)",
                }
                if double_purge_ref is not None
                else None
            ),
            "keylevel_ltf_touched": [
                {"kind": zone.kind, "direction": zone.direction}
                for zone in touched_kl_zones
            ],
            "ohlc_coherence": ohlc_coherence,
            "htf_ltf_time_alignment": "non implémenté - reporté à une évolution future",
        }
