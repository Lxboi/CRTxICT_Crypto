"""
rules/tsq_rule.py

Suit la séquence LTF "TSQ" (Turtle Soup Sequence), documentée comme
l'exécution en LTF d'un CRT HTF : Turtle Soup -> CISD -> OTE ->
Continuation -> KOD. Chaque étape peut servir de point d'entrée
indépendant sur un LTF CRT, une fois confirmée.

Définitions retenues (vérifiées auprès de sources externes sur la
méthodologie ICT, résumées ici) :

- CISD (Change in State of Delivery) : repère la "jambe de livraison"
  (bougies consécutives de même direction, opposée au renversement
  attendu) juste avant la bougie du Turtle Soup. Confirme quand une
  bougie CLÔTURE (jamais la mèche) au-delà de l'OPEN de la première
  bougie de cette jambe.
- OTE (Optimal Trade Entry) : une fois CISD confirmé, retracement
  Fibonacci de la jambe d'impulsion (du niveau balayé par le TS jusqu'à
  l'extrême atteint après CISD) ; zone d'entrée = 62%-79%, avec 70.5%
  comme centre ("sweet spot").
- Continuation : après un passage dans la zone OTE, le prix reprend la
  direction du CISD (nouvelle clôture au-delà du point de confirmation
  CISD) — confirme la poursuite du mouvement.
- KOD (Kiss of Death) : variante spécifique du Turtle Soup où la bougie
  du sweep a un GRAND corps dans le sens OPPOSÉ au renversement attendu
  (un piège visuel qui semble confirmer la continuation du mouvement
  balayé, avant le vrai retournement). Entrée à la clôture de cette
  bougie, dans le sens du renversement.

Ne calcule AUCUN stop-loss ni take-profit — signale uniquement des
points d'entrée et leur contexte, comme rules/entry_rule.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from models.market_data import Candle
from rules.crt_rule import CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME, _detect_crt_pattern
from rules.engine import BaseRule, MarketContext, RuleResult, resolve_now
from rules.entry_rule import Bias, TurtleSoupEvent, _get_liquidity_pools, _detect_turtle_soup

DEFAULT_MIN_CANDLES_REQUIRED = 20

# Seuil (corps / range total de la bougie) au-delà duquel une bougie
# est considérée comme ayant un "grand corps", condition du piège KOD.
DEFAULT_KOD_BODY_RATIO_THRESHOLD = 0.5

# Zone OTE : 62% à 79% de retracement, 70.5% = centre ("sweet spot").
OTE_ZONE_START = 0.62
OTE_ZONE_END = 0.79
OTE_SWEET_SPOT = 0.705


# ---------------------------------------------------------------------
# CISD
# ---------------------------------------------------------------------


@dataclass
class CISDEvent:
    """
    Change in State of Delivery confirmé : une bougie a clôturé
    au-delà de l'open de la jambe de livraison opposée au renversement
    attendu par le Turtle Soup.

    stop_loss : même swing que celui du Turtle Soup (H/L de la bougie
    du TS) — le CISD confirme le même renversement, pas un nouveau
    swing, donc même référence de risque.
    """

    direction: Bias
    delivery_leg_open: float
    confirmed_at_index: int
    stop_loss: float


def _get_delivery_leg_open(candles: list[Candle], ts_event: TurtleSoupEvent) -> float | None:
    """
    Repère la jambe de livraison : la série de bougies consécutives
    (juste avant la bougie du TS) de même direction, opposée au
    renversement attendu — et renvoie l'open de sa PREMIÈRE bougie
    (référence pour le CISD).

    Returns:
        None si la bougie immédiatement avant le TS n'est pas déjà de
        la direction opposée attendue (pas de jambe de livraison
        identifiable).
    """
    opposite_direction: Bias = "bearish" if ts_event.direction == "bullish" else "bullish"

    leg_open: float | None = None
    index = ts_event.sweep_candle_index - 1
    while index >= 0:
        candle = candles[index]
        candle_direction: Bias = "bullish" if candle.close > candle.open else "bearish"
        if candle_direction != opposite_direction:
            break
        leg_open = candle.open
        index -= 1

    return leg_open


def _detect_cisd(candles: list[Candle], ts_event: TurtleSoupEvent) -> CISDEvent | None:
    """
    Cherche, après la bougie du TS (incluse), une bougie dont la
    CLÔTURE (jamais la mèche) dépasse l'open de la jambe de livraison
    opposée — confirmant le changement de délivrance de prix.
    """
    leg_open = _get_delivery_leg_open(candles, ts_event)
    if leg_open is None:
        return None

    for index in range(ts_event.sweep_candle_index, len(candles)):
        candle = candles[index]
        stop_loss = ts_event.sweep_candle.low if ts_event.direction == "bullish" else ts_event.sweep_candle.high
        if ts_event.direction == "bullish" and candle.close > leg_open:
            return CISDEvent(
                direction="bullish", delivery_leg_open=leg_open, confirmed_at_index=index, stop_loss=stop_loss
            )
        if ts_event.direction == "bearish" and candle.close < leg_open:
            return CISDEvent(
                direction="bearish", delivery_leg_open=leg_open, confirmed_at_index=index, stop_loss=stop_loss
            )

    return None


# ---------------------------------------------------------------------
# OTE (Optimal Trade Entry)
# ---------------------------------------------------------------------


@dataclass
class OTEZone:
    """
    Zone de retracement Fibonacci 62%-79% de la jambe d'impulsion post-CISD.

    stop_loss : le niveau d'origine de l'impulsion (le point balayé par
    le TS, "swing" de départ du retracement — niveau 100% du Fibonacci)
    conformément à la pratique standard ICT (stop juste au-delà du
    100%).
    """

    zone_low: float
    zone_high: float
    sweet_spot: float
    direction: Bias
    stop_loss: float


def _compute_ote_zone(
    candles: list[Candle], ts_event: TurtleSoupEvent, cisd_event: CISDEvent
) -> OTEZone | None:
    """
    Calcule la zone OTE de la jambe d'impulsion : du niveau balayé par
    le TS (origine du mouvement) jusqu'à l'extrême le plus favorable
    atteint depuis la confirmation CISD.

    Returns:
        None si la jambe d'impulsion a une amplitude nulle ou négative
        (aucune donnée exploitable après la confirmation CISD).
    """
    post_cisd_candles = candles[cisd_event.confirmed_at_index :]
    if not post_cisd_candles:
        return None

    impulse_origin = ts_event.swept_level_price

    if cisd_event.direction == "bullish":
        impulse_extreme = max(c.high for c in post_cisd_candles)
        leg_range = impulse_extreme - impulse_origin
        if leg_range <= 0:
            return None
        zone_high = impulse_extreme - leg_range * OTE_ZONE_START
        zone_low = impulse_extreme - leg_range * OTE_ZONE_END
        sweet_spot = impulse_extreme - leg_range * OTE_SWEET_SPOT
    else:
        impulse_extreme = min(c.low for c in post_cisd_candles)
        leg_range = impulse_origin - impulse_extreme
        if leg_range <= 0:
            return None
        zone_low = impulse_extreme + leg_range * OTE_ZONE_START
        zone_high = impulse_extreme + leg_range * OTE_ZONE_END
        sweet_spot = impulse_extreme + leg_range * OTE_SWEET_SPOT

    return OTEZone(
        zone_low=zone_low,
        zone_high=zone_high,
        sweet_spot=sweet_spot,
        direction=cisd_event.direction,
        stop_loss=impulse_origin,
    )


def _check_ote_touched(candles: list[Candle], cisd_event: CISDEvent, ote_zone: OTEZone) -> bool:
    """
    Vérifie si la DERNIÈRE bougie disponible touche la zone OTE —
    cohérent avec le reste du fichier, qui détecte des événements "à
    l'instant" plutôt qu'un historique complet depuis le CISD.

    Note : vérifier tout l'historique post-CISD serait trompeur, car la
    bougie qui a elle-même défini l'extrême de l'impulsion chevauche
    presque toujours sa propre zone OTE par construction (son low/high
    couvre nécessairement une bonne partie du retracement) — ce qui
    déclencherait un faux "touché" dès la formation de l'impulsion,
    avant même un vrai retracement.
    """
    if len(candles) <= cisd_event.confirmed_at_index:
        return False
    last = candles[-1]
    return last.low <= ote_zone.zone_high and last.high >= ote_zone.zone_low


# ---------------------------------------------------------------------
# Continuation
# ---------------------------------------------------------------------


@dataclass
class ContinuationEvent:
    """
    Continuation confirmée : après un passage dans la zone OTE, le prix
    reprend la direction du CISD.

    stop_loss : le swing de retracement dans l'OTE — l'extrême (low
    pour un setup bullish, high pour un setup bearish) atteint APRÈS le
    sommet/creux de l'impulsion, pendant le pullback qui a précédé la
    reprise. Plus serré que le stop du TS d'origine, puisque le marché
    a déjà validé une reprise (donc moins de marge nécessaire).
    """

    direction: Bias
    entry_price: float
    stop_loss: float


def _compute_continuation_stop_loss(
    candles: list[Candle], cisd_event: CISDEvent, direction: Bias
) -> float | None:
    """
    Isole la phase de retracement (les bougies APRÈS le sommet/creux de
    l'impulsion post-CISD, pas l'impulsion elle-même) et renvoie son
    extrême — le swing qui a marqué le pullback avant la reprise.

    Returns:
        None si aucune bougie n'est disponible après la confirmation
        CISD (ne devrait pas arriver si une continuation a été
        détectée, mais vérifié explicitement par prudence).
    """
    post_cisd_candles = candles[cisd_event.confirmed_at_index :]
    if not post_cisd_candles:
        return None

    if direction == "bullish":
        extreme_index = max(range(len(post_cisd_candles)), key=lambda i: post_cisd_candles[i].high)
        retracement_candles = post_cisd_candles[extreme_index + 1 :]
        return min(c.low for c in retracement_candles) if retracement_candles else None

    extreme_index = min(range(len(post_cisd_candles)), key=lambda i: post_cisd_candles[i].low)
    retracement_candles = post_cisd_candles[extreme_index + 1 :]
    return max(c.high for c in retracement_candles) if retracement_candles else None


def _detect_continuation(
    candles: list[Candle], cisd_event: CISDEvent, ote_zone: OTEZone
) -> ContinuationEvent | None:
    """
    Confirme la continuation : après un passage dans la zone OTE, la
    dernière bougie disponible clôture à nouveau au-delà du point de
    confirmation CISD (delivery_leg_open), dans le sens du CISD —
    signe que le mouvement reprend après la pause OTE.
    """
    if not _check_ote_touched(candles, cisd_event, ote_zone):
        return None

    last = candles[-1]
    confirmed = (
        last.close > cisd_event.delivery_leg_open
        if cisd_event.direction == "bullish"
        else last.close < cisd_event.delivery_leg_open
    )
    if not confirmed:
        return None

    stop_loss = _compute_continuation_stop_loss(candles, cisd_event, cisd_event.direction)
    if stop_loss is None:
        return None

    return ContinuationEvent(direction=cisd_event.direction, entry_price=last.close, stop_loss=stop_loss)


# ---------------------------------------------------------------------
# KOD (Kiss of Death)
# ---------------------------------------------------------------------


@dataclass
class KODEvent:
    """
    Modèle KOD confirmé : la bougie du Turtle Soup avait un grand corps
    dans le sens opposé au renversement attendu (piège visuel), et le
    renversement est donc anticipé à sa clôture.
    """

    direction: Bias
    entry_price: float
    stop_loss: float  # le niveau balayé par le TS ("le point du turtle soup")


def _detect_kod(
    candles: list[Candle],
    ts_event: TurtleSoupEvent,
    body_ratio_threshold: float = DEFAULT_KOD_BODY_RATIO_THRESHOLD,
) -> KODEvent | None:
    """
    Détecte un KOD : la bougie du TS doit avoir un corps représentant
    au moins `body_ratio_threshold` de son range total, ET ce corps
    doit être dans le sens OPPOSÉ au renversement attendu (le "piège" —
    ex: pour un KOD bearish, la bougie qui balaie le high doit clôturer
    haussière, rassurant faussement les acheteurs avant le retournement).
    """
    ts_candle = ts_event.sweep_candle
    candle_range = ts_candle.high - ts_candle.low
    if candle_range == 0:
        return None

    body = abs(ts_candle.close - ts_candle.open)
    is_large_body = (body / candle_range) >= body_ratio_threshold

    trap_direction: Bias = "bullish" if ts_candle.close > ts_candle.open else "bearish"
    expected_trap_direction: Bias = "bullish" if ts_event.direction == "bearish" else "bearish"

    if not is_large_body or trap_direction != expected_trap_direction:
        return None

    return KODEvent(
        direction=ts_event.direction,
        entry_price=ts_candle.close,
        stop_loss=ts_event.swept_level_price,
    )


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Take-profit : swing opposé (+ DOL du CRT HTF si disponible)
# ---------------------------------------------------------------------

# Appariement swing opposé, pour construire une cible à partir du
# niveau balayé par le TS (le marché vise typiquement la liquidité
# opposée une fois le renversement engagé).
_OPPOSITE_POOL_NAME: dict[str, str] = {
    "PDH": "PDL",
    "PDL": "PDH",
    "Asia High": "Asia Low",
    "Asia Low": "Asia High",
}


def _compute_opposite_swing_target(
    ts_event: TurtleSoupEvent, liquidity_pools: dict[str, float]
) -> float | None:
    """
    Cible (TP) au swing opposé au TS : si le sweep a eu lieu sur un
    plus bas (PDL/Asia Low), la cible naturelle est le plus haut
    correspondant (PDH/Asia High), et inversement — le marché vise
    typiquement la liquidité du côté opposé une fois le renversement
    engagé.

    Returns:
        None si le nom du niveau balayé n'a pas de correspondance
        connue, ou si cette correspondance n'est pas dans les pools
        fournis (ex: Asia High/Low absent des données).
    """
    opposite_name = _OPPOSITE_POOL_NAME.get(ts_event.swept_level_name)
    if opposite_name is None:
        return None
    return liquidity_pools.get(opposite_name)


def _compute_crt_dol_target(
    daily_candles: list[Candle] | None, current_price: float, direction: Bias
) -> float | None:
    """
    Cible (TP) additionnelle : la DOL du CRT Daily, si un CRT compatible
    en direction est détecté sur cette timeframe — une cible plus
    lointaine que le simple swing opposé intraday, cohérente avec la
    consigne "TP vers ... CRT et DOL".

    Returns:
        None si aucun CRT Daily n'est détecté, ou si son biais ne
        correspond pas à `direction`.
    """
    if not daily_candles or len(daily_candles) < 3:
        return None

    crt = _detect_crt_pattern(
        daily_candles,
        current_price=current_price,
        max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get("daily"),
    )
    if crt is None or crt.bias != direction:
        return None

    return crt.dol_price


class TsqRule(BaseRule):
    """
    Règle de suivi de la séquence TSQ (Turtle Soup -> CISD -> OTE ->
    Continuation -> KOD) sur la timeframe intraday — chaque étape
    confirmée est un point d'entrée LTF possible sur un CRT HTF.

    SL : le swing marquant chaque étape (voir chaque dataclass).
    TP : le swing opposé au TS (autre pool de liquidité), complété par
    la DOL du CRT Daily si un CRT compatible en direction est détecté
    (cible plus lointaine). Opère sur "intraday" comme
    rules/entry_rule.py (True Day Open, sessions, liquidity pools n'ont
    de sens qu'à cette granularité).
    """

    name: ClassVar[str] = "tsq_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("tsq_rule", {})
        min_candles_required: int = cfg.get("min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED)
        body_ratio_threshold: float = cfg.get(
            "kod_body_ratio_threshold", DEFAULT_KOD_BODY_RATIO_THRESHOLD
        )

        if not context.candles or "intraday" not in context.candles:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Historique intraday indisponible pour la séquence TSQ",
            )

        closed_candles = context.candles["intraday"].candles[:-1]

        if len(closed_candles) < min_candles_required:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Historique intraday insuffisant pour la séquence TSQ",
            )

        # resolve_now() : context.simulated_now si fourni (backtest), sinon
        # l'horloge réelle (live, comportement inchangé) -- voir rules/engine.py.
        today = resolve_now(context).date()
        liquidity_pools = _get_liquidity_pools(closed_candles, today)
        ts_event = _detect_turtle_soup(closed_candles, liquidity_pools) if liquidity_pools else None

        cisd_event: CISDEvent | None = None
        kod_event: KODEvent | None = None
        ote_zone: OTEZone | None = None
        ote_touched = False
        continuation_event: ContinuationEvent | None = None
        opposite_swing_target: float | None = None
        crt_dol_target: float | None = None

        if ts_event is not None:
            cisd_event = _detect_cisd(closed_candles, ts_event)
            kod_event = _detect_kod(closed_candles, ts_event, body_ratio_threshold)
            opposite_swing_target = _compute_opposite_swing_target(ts_event, liquidity_pools)

            daily_series = context.candles.get("daily")
            daily_closed = daily_series.candles[:-1] if daily_series is not None else None
            crt_dol_target = _compute_crt_dol_target(
                daily_closed, context.market_data.price_usd, ts_event.direction
            )

            if cisd_event is not None:
                ote_zone = _compute_ote_zone(closed_candles, ts_event, cisd_event)
                if ote_zone is not None:
                    ote_touched = _check_ote_touched(closed_candles, cisd_event, ote_zone)
                    continuation_event = _detect_continuation(closed_candles, cisd_event, ote_zone)

        triggered = any([cisd_event, ote_touched, continuation_event, kod_event])

        message_parts = []
        if ts_event is not None:
            message_parts.append(f"TS ({ts_event.swept_level_name}) détecté")
        if cisd_event is not None:
            message_parts.append(f"CISD {cisd_event.direction} confirmé")
        if ote_zone is not None:
            message_parts.append(
                f"OTE [{ote_zone.zone_low:,.2f} - {ote_zone.zone_high:,.2f}]"
                + (" touchée" if ote_touched else " non touchée")
            )
        if continuation_event is not None:
            message_parts.append(f"Continuation confirmée @ ${continuation_event.entry_price:,.2f}")
        if kod_event is not None:
            message_parts.append(f"KOD {kod_event.direction} @ ${kod_event.entry_price:,.2f}")

        message = " | ".join(message_parts) if message_parts else "Aucune étape TSQ détectée ce cycle"

        return RuleResult(
            rule_name=self.name,
            triggered=triggered,
            message=message,
            severity="warning" if triggered else "info",
            metadata={
                "turtle_soup": (
                    {
                        "direction": ts_event.direction,
                        "swept_level_name": ts_event.swept_level_name,
                        "entry_price": ts_event.sweep_candle.close,
                        "stop_loss": (
                            ts_event.sweep_candle.low
                            if ts_event.direction == "bullish"
                            else ts_event.sweep_candle.high
                        ),
                    }
                    if ts_event
                    else None
                ),
                "cisd": (
                    {
                        "direction": cisd_event.direction,
                        "delivery_leg_open": cisd_event.delivery_leg_open,
                        "entry_price": closed_candles[cisd_event.confirmed_at_index].close,
                        "stop_loss": cisd_event.stop_loss,
                    }
                    if cisd_event
                    else None
                ),
                "targets": {
                    "opposite_swing": opposite_swing_target,
                    "crt_dol": crt_dol_target,
                },
                "ote_zone": (
                    {
                        "zone_low": ote_zone.zone_low,
                        "zone_high": ote_zone.zone_high,
                        "sweet_spot": ote_zone.sweet_spot,
                        "entry_price": ote_zone.sweet_spot,
                        "touched": ote_touched,
                        "stop_loss": ote_zone.stop_loss,
                    }
                    if ote_zone
                    else None
                ),
                "continuation": (
                    {
                        "direction": continuation_event.direction,
                        "entry_price": continuation_event.entry_price,
                        "stop_loss": continuation_event.stop_loss,
                    }
                    if continuation_event
                    else None
                ),
                "kod": (
                    {
                        "direction": kod_event.direction,
                        "entry_price": kod_event.entry_price,
                        "stop_loss": kod_event.stop_loss,
                    }
                    if kod_event
                    else None
                ),
            },
        )
