"""
rules/keylevel_rule.py

KeyLevel Rule : repère les zones de "Key Levels" (KL) sur chaque
timeframe — FVG, anciens plus hauts/plus bas (Old H/L), Order Block,
Breaker Block, Rejection Block.

Principe métier : lorsque le prix atteint un KL, le marché est
généralement attiré vers un KL opposé encore non mitigé, ce qui sert
d'objectif et de confirmation de trade. Cette règle se déclenche donc
quand le prix teste un KL actif, et expose dans ses métadonnées la
cible opposée la plus proche si elle existe.

Ces 5 concepts viennent du vocabulaire ICT / Smart Money Concepts, où
les définitions varient légèrement d'un éducateur à l'autre. Les choix
retenus sont documentés dans chaque fonction de détection ci-dessous ;
à ajuster si votre définition diffère.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal

from models.market_data import Candle
from rules.engine import BaseRule, MarketContext, RuleResult

Direction = Literal["bullish", "bearish"]
KeyLevelKind = Literal[
    "fvg", "old_high", "old_low", "order_block", "breaker_block", "rejection_block"
]

# --- Paramètres par défaut, surchargeables via config/alerts.yaml ---
DEFAULT_SWING_LOOKBACK = 2
DEFAULT_DISPLACEMENT_MULTIPLIER = 1.5
DEFAULT_REJECTION_WICK_RATIO = 0.5
DEFAULT_PROXIMITY_THRESHOLD_PCT = 0.3
DEFAULT_MIN_CANDLES_REQUIRED = 8


@dataclass
class KeyLevelZone:
    """
    Représente une zone de Key Level identifiée sur une timeframe.

    Attributes:
        kind: Type de zone (fvg, old_high, old_low, order_block,
            breaker_block, rejection_block).
        direction: "bullish" si la zone agit comme support/demande
            (on attend un rebond en la testant), "bearish" si elle agit
            comme résistance/offre (on attend un rejet en la testant).
        zone_low: Borne basse de la zone.
        zone_high: Borne haute de la zone.
        formed_at: Horodatage de la bougie qui a formé la zone.
    """

    kind: KeyLevelKind
    direction: Direction
    zone_low: float
    zone_high: float
    formed_at: datetime

    def contains(self, price: float) -> bool:
        """Indique si un prix se trouve à l'intérieur de la zone."""
        return self.zone_low <= price <= self.zone_high

    def distance_pct(self, price: float) -> float:
        """Distance relative (%) entre un prix et le bord le plus proche de la zone."""
        if self.contains(price):
            return 0.0
        nearest_edge = self.zone_low if price < self.zone_low else self.zone_high
        return abs(price - nearest_edge) / nearest_edge * 100

    def overlaps(self, low: float, high: float) -> bool:
        """Indique si un intervalle [low, high] chevauche cette zone."""
        return low <= self.zone_high and high >= self.zone_low

    @property
    def midpoint(self) -> float:
        """Prix médian de la zone, utilisé pour l'affichage et le ciblage."""
        return (self.zone_low + self.zone_high) / 2


# ---------------------------------------------------------------------
# Détection des swings (plus hauts / plus bas locaux) — brique commune
# à Old High/Low, Order Block et Rejection Block.
# ---------------------------------------------------------------------


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: Literal["high", "low"]
    candle: Candle


def _find_swing_points(candles: list[Candle], lookback: int) -> list[SwingPoint]:
    """
    Détecte les swing highs/lows : une bougie dont le high (resp. low)
    est le plus élevé (resp. le plus bas) parmi les `lookback` bougies
    de chaque côté (fenêtre glissante de type "fractal").
    """
    swings: list[SwingPoint] = []
    n = len(candles)

    for i in range(lookback, n - lookback):
        window = candles[i - lookback : i + lookback + 1]
        current = candles[i]

        if current.high == max(c.high for c in window):
            swings.append(SwingPoint(index=i, price=current.high, kind="high", candle=current))
        if current.low == min(c.low for c in window):
            swings.append(SwingPoint(index=i, price=current.low, kind="low", candle=current))

    return swings


# ---------------------------------------------------------------------
# 1. FVG (Fair Value Gap)
# ---------------------------------------------------------------------


def _detect_fvgs(candles: list[Candle]) -> list[KeyLevelZone]:
    """
    Un FVG est un déséquilibre sur 3 bougies consécutives : la bougie du
    milieu réalise un mouvement assez fort pour laisser un "vide" entre
    le high de la bougie 1 et le low de la bougie 3 (FVG haussier), ou
    entre le low de la bougie 1 et le high de la bougie 3 (FVG baissier).
    """
    zones: list[KeyLevelZone] = []

    for i in range(1, len(candles) - 1):
        left, middle, right = candles[i - 1], candles[i], candles[i + 1]

        if left.high < right.low:
            zones.append(
                KeyLevelZone(
                    kind="fvg",
                    direction="bullish",
                    zone_low=left.high,
                    zone_high=right.low,
                    formed_at=right.timestamp,
                )
            )
        elif left.low > right.high:
            zones.append(
                KeyLevelZone(
                    kind="fvg",
                    direction="bearish",
                    zone_low=right.high,
                    zone_high=left.low,
                    formed_at=right.timestamp,
                )
            )

    return zones


# ---------------------------------------------------------------------
# 2. Old High / Old Low
# ---------------------------------------------------------------------


def _detect_old_highs_lows(candles: list[Candle], lookback: int) -> list[KeyLevelZone]:
    """
    Transforme les swing points en zones "Old High" (résistance,
    direction=bearish) et "Old Low" (support, direction=bullish). Un
    swing déjà "balayé" par une bougie ultérieure (dont le high/low est
    allé au-delà) est exclu : il ne représente plus une cible valide.
    """
    swings = _find_swing_points(candles, lookback)
    zones: list[KeyLevelZone] = []

    for swing in swings:
        subsequent = candles[swing.index + 1 :]

        if swing.kind == "high":
            already_swept = any(c.high > swing.price for c in subsequent)
            if already_swept:
                continue
            zones.append(
                KeyLevelZone(
                    kind="old_high",
                    direction="bearish",
                    zone_low=swing.price,
                    zone_high=swing.price,
                    formed_at=swing.candle.timestamp,
                )
            )
        else:
            already_swept = any(c.low < swing.price for c in subsequent)
            if already_swept:
                continue
            zones.append(
                KeyLevelZone(
                    kind="old_low",
                    direction="bullish",
                    zone_low=swing.price,
                    zone_high=swing.price,
                    formed_at=swing.candle.timestamp,
                )
            )

    return zones


# ---------------------------------------------------------------------
# 3. Order Block
# ---------------------------------------------------------------------


def _average_body_size(candles: list[Candle]) -> float:
    if not candles:
        return 0.0
    return sum(abs(c.close - c.open) for c in candles) / len(candles)


def _detect_order_blocks(
    candles: list[Candle], lookback: int, displacement_multiplier: float
) -> list[KeyLevelZone]:
    """
    Un Order Block est la dernière bougie de sens opposé avant un
    mouvement de "displacement" (forte impulsion) qui casse un swing
    récent (Break of Structure).

    Choix retenu : une bougie est une "displacement" si son corps
    dépasse `displacement_multiplier` fois le corps moyen des 10
    bougies précédentes, ET qu'elle clôture au-delà du dernier swing
    high (cas haussier) ou du dernier swing low (cas baissier). La
    bougie immédiatement avant elle, de couleur opposée, devient
    l'Order Block. Zone retenue = range complet de cette bougie
    (low à high).
    """
    if len(candles) < lookback * 2 + 3:
        return []

    swings = _find_swing_points(candles, lookback)
    zones: list[KeyLevelZone] = []

    for i in range(1, len(candles)):
        displacement_candle = candles[i]
        preceding = candles[max(0, i - 10) : i]
        avg_body = _average_body_size(preceding)
        if avg_body == 0:
            continue

        body_size = abs(displacement_candle.close - displacement_candle.open)
        if body_size < displacement_multiplier * avg_body:
            continue

        prior_swings = [s for s in swings if s.index < i]
        if not prior_swings:
            continue

        ob_candle = candles[i - 1]
        is_bullish_displacement = displacement_candle.close > displacement_candle.open

        if is_bullish_displacement:
            recent_highs = [s.price for s in prior_swings if s.kind == "high"]
            if not recent_highs:
                continue
            last_swing_high = recent_highs[-1]
            breaks_structure = displacement_candle.close > last_swing_high
            is_opposite_candle = ob_candle.close < ob_candle.open

            if breaks_structure and is_opposite_candle:
                zones.append(
                    KeyLevelZone(
                        kind="order_block",
                        direction="bullish",
                        zone_low=ob_candle.low,
                        zone_high=ob_candle.high,
                        # formed_at pointe sur la bougie de displacement (pas
                        # l'OB lui-même) : le pattern n'est confirmé qu'une
                        # fois la displacement réalisée, et cela évite que
                        # la displacement candle (qui chevauche souvent la
                        # zone de l'OB) ne soit comptée comme une mitigation
                        # immédiate lors du filtrage.
                        formed_at=displacement_candle.timestamp,
                    )
                )
        else:
            recent_lows = [s.price for s in prior_swings if s.kind == "low"]
            if not recent_lows:
                continue
            last_swing_low = recent_lows[-1]
            breaks_structure = displacement_candle.close < last_swing_low
            is_opposite_candle = ob_candle.close > ob_candle.open

            if breaks_structure and is_opposite_candle:
                zones.append(
                    KeyLevelZone(
                        kind="order_block",
                        direction="bearish",
                        zone_low=ob_candle.low,
                        zone_high=ob_candle.high,
                        formed_at=displacement_candle.timestamp,
                    )
                )

    return zones


# ---------------------------------------------------------------------
# 4. Breaker Block
# ---------------------------------------------------------------------


def _detect_breaker_blocks(
    order_blocks: list[KeyLevelZone], candles: list[Candle]
) -> list[KeyLevelZone]:
    """
    Un Breaker Block est un Order Block invalidé : le prix a clôturé
    au-delà de son extrémité opposée (contre le sens de l'OB), ce qui
    inverse sa polarité. La zone géométrique reste la même, mais son
    rôle s'inverse : un Order Block haussier cassé à la baisse devient
    une résistance (breaker baissier) ; un Order Block baissier cassé à
    la hausse devient un support (breaker haussier).
    """
    timestamp_to_index = {c.timestamp: idx for idx, c in enumerate(candles)}
    breakers: list[KeyLevelZone] = []

    for ob in order_blocks:
        ob_index = timestamp_to_index.get(ob.formed_at)
        if ob_index is None:
            continue

        subsequent = candles[ob_index + 1 :]

        if ob.direction == "bullish":
            invalidated = any(c.close < ob.zone_low for c in subsequent)
            if invalidated:
                breakers.append(
                    KeyLevelZone(
                        kind="breaker_block",
                        direction="bearish",
                        zone_low=ob.zone_low,
                        zone_high=ob.zone_high,
                        formed_at=ob.formed_at,
                    )
                )
        else:
            invalidated = any(c.close > ob.zone_high for c in subsequent)
            if invalidated:
                breakers.append(
                    KeyLevelZone(
                        kind="breaker_block",
                        direction="bullish",
                        zone_low=ob.zone_low,
                        zone_high=ob.zone_high,
                        formed_at=ob.formed_at,
                    )
                )

    return breakers


# ---------------------------------------------------------------------
# 5. Rejection Block
# ---------------------------------------------------------------------


def _detect_rejection_blocks(
    candles: list[Candle], lookback: int, wick_ratio_threshold: float
) -> list[KeyLevelZone]:
    """
    Un Rejection Block se forme sur une bougie de swing dont la mèche
    représente une part importante du range total (rejet marqué). La
    zone couvre la mèche : mèche haute pour un swing high (résistance),
    mèche basse pour un swing low (support).
    """
    swings = _find_swing_points(candles, lookback)
    zones: list[KeyLevelZone] = []

    for swing in swings:
        candle = swing.candle
        total_range = candle.high - candle.low
        if total_range <= 0:
            continue

        body_top = max(candle.open, candle.close)
        body_bottom = min(candle.open, candle.close)

        if swing.kind == "high":
            upper_wick = candle.high - body_top
            if upper_wick / total_range >= wick_ratio_threshold:
                zones.append(
                    KeyLevelZone(
                        kind="rejection_block",
                        direction="bearish",
                        zone_low=body_top,
                        zone_high=candle.high,
                        formed_at=candle.timestamp,
                    )
                )
        else:
            lower_wick = body_bottom - candle.low
            if lower_wick / total_range >= wick_ratio_threshold:
                zones.append(
                    KeyLevelZone(
                        kind="rejection_block",
                        direction="bullish",
                        zone_low=candle.low,
                        zone_high=body_bottom,
                        formed_at=candle.timestamp,
                    )
                )

    return zones


# ---------------------------------------------------------------------
# Agrégation + filtrage des zones déjà mitigées
# ---------------------------------------------------------------------


def find_key_levels(candles: list[Candle], config: dict[str, Any]) -> list[KeyLevelZone]:
    """
    Détecte les 5 types de Key Levels sur une série de bougies closes.

    Args:
        candles: Bougies closes, ordonnées chronologiquement.
        config: Section de configuration (ex: config["keylevel_rule"]).

    Returns:
        La liste combinée de toutes les zones détectées (avant filtrage
        de mitigation).
    """
    swing_lookback = config.get("swing_lookback", DEFAULT_SWING_LOOKBACK)
    displacement_multiplier = config.get(
        "displacement_multiplier", DEFAULT_DISPLACEMENT_MULTIPLIER
    )
    rejection_wick_ratio = config.get("rejection_wick_ratio", DEFAULT_REJECTION_WICK_RATIO)

    zones: list[KeyLevelZone] = []
    zones += _detect_fvgs(candles)
    zones += _detect_old_highs_lows(candles, swing_lookback)

    order_blocks = _detect_order_blocks(candles, swing_lookback, displacement_multiplier)
    zones += order_blocks
    zones += _detect_breaker_blocks(order_blocks, candles)
    zones += _detect_rejection_blocks(candles, swing_lookback, rejection_wick_ratio)

    return zones


def _filter_unmitigated(zones: list[KeyLevelZone], candles: list[Candle]) -> list[KeyLevelZone]:
    """
    Exclut les zones déjà "mitigées" : celles dont une bougie ultérieure
    à leur formation a déjà chevauché leur intervalle de prix. Une zone
    déjà retouchée est moins pertinente comme cible fraîche.

    Note : old_high/old_low sont déjà filtrées (sweep) au moment de leur
    détection ; elles passent donc telles quelles.
    """
    timestamp_to_index = {c.timestamp: idx for idx, c in enumerate(candles)}
    unmitigated: list[KeyLevelZone] = []

    for zone in zones:
        if zone.kind in ("old_high", "old_low"):
            unmitigated.append(zone)
            continue

        zone_index = timestamp_to_index.get(zone.formed_at)
        if zone_index is None:
            unmitigated.append(zone)
            continue

        subsequent = candles[zone_index + 1 :]
        already_touched = any(zone.overlaps(c.low, c.high) for c in subsequent)
        if not already_touched:
            unmitigated.append(zone)

    return unmitigated


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


class KeyLevelRule(BaseRule):
    """
    Règle de détection des Key Levels (FVG, Old H/L, Order Block,
    Breaker Block, Rejection Block), exécutée sur chaque timeframe
    disponible dans le contexte.

    Se déclenche lorsque le prix actuel teste une zone active (non
    mitigée). Expose dans les métadonnées la zone opposée la plus
    proche comme objectif potentiel de trade.
    """

    name: ClassVar[str] = "keylevel_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("keylevel_rule", {})
        proximity_threshold_pct: float = cfg.get(
            "proximity_threshold_pct", DEFAULT_PROXIMITY_THRESHOLD_PCT
        )
        min_candles_required: int = cfg.get(
            "min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED
        )

        if not context.candles:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Aucun historique de bougies multi-timeframe fourni",
            )

        current_price = context.market_data.price_usd
        touched_by_timeframe: dict[str, list[KeyLevelZone]] = {}
        active_zones_by_timeframe: dict[str, list[KeyLevelZone]] = {}

        for timeframe, series in context.candles.items():
            closed_candles = series.candles[:-1]
            if len(closed_candles) < min_candles_required:
                continue

            raw_zones = find_key_levels(closed_candles, cfg)
            active_zones = _filter_unmitigated(raw_zones, closed_candles)
            active_zones_by_timeframe[timeframe] = active_zones

            touched = [
                zone
                for zone in active_zones
                if zone.distance_pct(current_price) <= proximity_threshold_pct
            ]
            if touched:
                touched_by_timeframe[timeframe] = touched

        if not touched_by_timeframe:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Aucun Key Level testé actuellement",
            )

        message_parts: list[str] = []
        metadata: dict[str, Any] = {}

        for timeframe, touched_zones in touched_by_timeframe.items():
            timeframe_metadata = []

            for zone in touched_zones:
                opposite_target = self._find_opposite_target(
                    zone, active_zones_by_timeframe.get(timeframe, []), current_price
                )
                target_note = (
                    f" → cible opposée potentielle : {opposite_target.kind} "
                    f"à ${opposite_target.midpoint:,.2f}"
                    if opposite_target
                    else ""
                )
                message_parts.append(
                    f"{timeframe}/{zone.kind} ({zone.direction}) testé à "
                    f"${current_price:,.2f}{target_note}"
                )
                timeframe_metadata.append(
                    {
                        "kind": zone.kind,
                        "direction": zone.direction,
                        "zone_low": zone.zone_low,
                        "zone_high": zone.zone_high,
                        "opposite_target_kind": (
                            opposite_target.kind if opposite_target else None
                        ),
                        "opposite_target_price": (
                            opposite_target.midpoint if opposite_target else None
                        ),
                    }
                )

            metadata[timeframe] = timeframe_metadata

        distinct_kinds_touched = {
            zone.kind for zones in touched_by_timeframe.values() for zone in zones
        }
        severity: Literal["info", "warning", "critical"]
        if len(distinct_kinds_touched) >= 3:
            severity = "critical"
        elif len(distinct_kinds_touched) >= 2:
            severity = "warning"
        else:
            severity = "info"

        return RuleResult(
            rule_name=self.name,
            triggered=True,
            message="Key Level(s) testé(s) : " + " | ".join(message_parts),
            severity=severity,
            metadata=metadata,
        )

    @staticmethod
    def _find_opposite_target(
        zone: KeyLevelZone, candidates: list[KeyLevelZone], current_price: float
    ) -> KeyLevelZone | None:
        """
        Cherche, parmi les zones actives de la même timeframe, la zone
        de direction opposée la plus pertinente comme objectif : en
        priorité une zone située du bon côté du prix actuel (au-dessus
        si la zone testée est bullish, en dessous si bearish) ; à
        défaut, la zone opposée la plus proche tout court.
        """
        opposite_direction: Direction = (
            "bearish" if zone.direction == "bullish" else "bullish"
        )
        opposites = [c for c in candidates if c.direction == opposite_direction and c is not zone]
        if not opposites:
            return None

        if zone.direction == "bullish":
            in_expected_direction = [c for c in opposites if c.midpoint > current_price]
        else:
            in_expected_direction = [c for c in opposites if c.midpoint < current_price]

        pool = in_expected_direction or opposites
        return min(pool, key=lambda c: abs(c.midpoint - current_price))