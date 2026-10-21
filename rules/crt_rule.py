"""
rules/crt_rule.py

CRT Rule (Candle Range Theory) : détecte un pattern où une bougie "ancre"
(A) définit un range, une ou plusieurs bougies suivantes clôturent à
l'intérieur du corps de A (containment), et l'une d'elles réalise une
"purge" de liquidité (une mèche dépasse le high ou le low de A sans que la
clôture ne sorte du corps de A). Le DOL résultant est l'extrême opposé de A.

La stratégie étant fractale, cette règle s'exécute sur les 3 timeframes
disponibles dans MarketContext.candles (monthly, weekly, daily). Un
alignement du même biais sur plusieurs timeframes renforce la sévérité de
l'alerte (Monthly domine Weekly domine Daily, mais le déclenchement reste
informatif dès qu'un pattern est trouvé, même sur une seule timeframe).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from config.constants import TIMEFRAME_ORDER
from models.market_data import Candle
from rules.engine import BaseRule, MarketContext, RuleResult

DEFAULT_MIN_CANDLES_REQUIRED = 3


@dataclass
class CrtPattern:
    """
    Représente un pattern CRT confirmé sur une timeframe donnée.

    Attributes:
        anchor_candle: La bougie A qui définit le range.
        purge_candle: La bougie (B, C, ...) qui a réalisé la purge.
        purge_side: Côté de la purge : "high" si la mèche a dépassé le
            high de A, "low" si elle a dépassé le low de A.
        dol_price: Prix du DOL résultant (extrême opposé de A).
        bias: Biais directionnel impliqué par ce DOL — "bearish" si la
            purge s'est faite côté high (le marché est censé redescendre
            vers le low de A), "bullish" si elle s'est faite côté low.
    """

    anchor_candle: Candle
    purge_candle: Candle
    purge_side: Literal["high", "low"]
    dol_price: float
    bias: Literal["bullish", "bearish"]


MAX_ANCHOR_DISTANCE_RATIO = 0.5  # ancre à plus de 50% du prix actuel -> jugée obsolète

# Âge maximum d'une ancre CRT, en nombre de bougies de sa propre
# timeframe — indépendant de la distance de prix (voir
# _detect_crt_pattern, paramètre max_anchor_age_candles). Cas réel
# corrigé (BTC) : une ancre Monthly de ~24 mois avait une cible restée
# sous le seuil de distance de prix (~31%, sous 50%) mais était
# clairement trop ancienne pour rester pertinente. Valeurs choisies pour
# représenter un horizon raisonnable par timeframe (~1 an Monthly, ~6
# mois Weekly, ~2 mois Daily, ~10 jours 4H) — ajustables si l'usage réel
# suggère un horizon différent. Partagée par tous les appelants de
# _detect_crt_pattern (rules/decision_engine.py, entry_rule.py,
# tsq_rule.py, dol_rule.py, purge_rule.py) pour une cohérence garantie.
CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME: dict[str, int] = {
    "monthly": 12,
    "weekly": 26,
    "daily": 60,
    "4h": 60,
}


def _is_anchor_too_far_from_price(anchor: Candle, current_price: float | None) -> bool:
    """
    Détecte une ancre dont le NIVEAU DE PRIX lui-même est anormalement
    éloigné du prix actuel — pas seulement une cible déjà atteinte, mais
    une ancre venant d'un tout autre régime de prix (ex : un CRT ancré
    autour de $150-350 retrouvé alors qu'un actif vaut aujourd'hui
    $1 800+ : la cible n'a jamais été "dépassée" au sens strict car le
    prix n'a fait que monter depuis, mais l'ancre reste totalement hors
    de propos pour une décision de trading actuelle).

    Sans ce filtre, sur un historique de plusieurs années, une ancre
    ancienne dont la cible est dans la direction opposée au mouvement de
    fond de l'actif peut passer le filtre "cible déjà atteinte" (qui ne
    détecte que les cibles dans le sens du mouvement) tout en étant
    tout aussi absurde.
    """
    if current_price is None:
        return False
    anchor_reference_price = (anchor.high + anchor.low) / 2
    if anchor_reference_price <= 0:
        return False
    distance_ratio = abs(current_price - anchor_reference_price) / anchor_reference_price
    return distance_ratio > MAX_ANCHOR_DISTANCE_RATIO


def _detect_crt_pattern(
    candles: list[Candle],
    current_price: float | None = None,
    max_anchor_age_candles: int | None = None,
) -> CrtPattern | None:
    """
    Cherche un pattern CRT dans une liste de bougies closes, ordonnées
    chronologiquement.

    Stratégie : on cherche l'ancre A la PLUS RÉCENTE possible telle que
    les bougies suivantes clôturent à l'intérieur de ses mèches (voir
    plus bas) JUSQU'À CE QU'UNE PURGE SOIT CONFIRMÉE. On scanne donc les
    indices du plus récent vers le plus ancien ; pour chaque ancre
    candidate, on avance bougie par bougie (dans l'ordre chronologique)
    tant que le containment tient, et on retourne immédiatement le
    pattern dès qu'une purge est trouvée — sans exiger de containment
    au-delà de la bougie de purge, puisque le prix est alors censé
    continuer vers la DOL (donc sortir logiquement de la fourchette de
    l'ancre). Si le containment casse avant toute purge pour une ancre
    donnée, on l'abandonne au profit d'une ancre encore plus ancienne
    (on continue de reculer dans le temps jusqu'à en trouver une qui
    fonctionne).

    Ce choix (le plus récent d'abord, pas le plus ancien avec la
    couverture maximale) a remplacé une stratégie précédente après
    plusieurs cas réels (SOL, ETH, BTC, TRX) ayant montré que même avec
    des filtres anti-obsolescence sur l'ancre trouvée, scanner du plus
    ancien vers le plus récent pouvait encore renvoyer un pattern bien
    plus ancien et moins pertinent que celui qu'un trader repérerait
    immédiatement sur un graphique réel.

    Args:
        candles: Bougies closes (bougie en cours de formation déjà exclue),
            ordonnées de la plus ancienne à la plus récente.
        current_price: Si fourni, filtre les patterns OBSOLÈTES en PRIX,
            selon TROIS critères combinés :
            1. La cible (DOL) a déjà été liquidée à N'IMPORTE QUEL moment
               depuis la purge (pas seulement par le prix ACTUEL) — une
               ancre à très large mèche peut "contenir" le prix pendant
               des mois malgré plusieurs allers-retours entre-temps ; si
               la cible a déjà été atteinte puis que le prix est revenu en
               arrière, elle reste consommée (ex : un CRT bearish dont la
               cible est à $29, atteinte il y a plusieurs semaines, même
               si le prix est repassé au-dessus depuis).
            2. Le niveau de l'ANCRE elle-même est anormalement éloigné du
               prix actuel (voir _is_anchor_too_far_from_price) — capture
               les ancres anciennes dont la cible n'a jamais été
               "dépassée" au sens strict (car dans la direction opposée
               au mouvement de fond de l'actif) mais qui restent hors de
               propos.
            3. La cible réelle (dol_price) est trop éloignée du prix
               actuel, même si le milieu de l'ancre entière semblait
               proche (ancre à mèche large et asymétrique).
        max_anchor_age_candles: Si fourni, filtre en plus les ancres
            OBSOLÈTES EN TEMPS — une ancre à plus de N bougies de la fin
            de la série est ignorée, MÊME SI sa cible reste raisonnablement
            proche en prix (cas réel BTC : une ancre Monthly de juillet
            2024, ~24 bougies d'écart, dont la cible n'est qu'à ~31% du
            prix actuel — sous le seuil de distance, donc pas rejetée par
            les critères 1-3 ci-dessus, mais clairement trop ancienne pour
            rester pertinente). Contrairement aux filtres de prix, celui-ci
            ne dépend pas du mouvement de l'actif : il borne directement
            "à quel point on a le droit de remonter dans l'historique"
            pour cette timeframe (valeur différente selon Monthly/Weekly/
            Daily/4H, fixée par l'appelant — voir
            rules/decision_engine.py::CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME).
            Sans ce filtre, sur un long historique (des centaines de
            bougies), le premier pattern valide trouvé peut dater d'il y
            a des années et n'avoir plus aucune pertinence pour une
            décision de trading actuelle. Un pattern obsolète est ignoré
            et la recherche continue avec une ancre plus récente. Laissez
            à None pour l'ancien comportement (aucun filtrage) — utile
            par exemple en LTF où l'historique est court et ce cas de
            figure ne se présente pas.

    Returns:
        Le CrtPattern trouvé (non obsolète si current_price/
        max_anchor_age_candles sont fournis), ou None si aucun pattern
        valide n'existe dans cette fenêtre de bougies.
    """
    if len(candles) < 2:
        return None

    # Scan de la bougie la PLUS RÉCENTE vers la plus ancienne (et non
    # l'inverse) : on veut le CRT le plus actuel/pertinent pour une
    # décision de trading maintenant, pas celui qui a tenu le plus
    # longtemps historiquement. Plusieurs cas réels (SOL, ETH, BTC, TRX)
    # ont montré que même avec les filtres anti-obsolescence (âge,
    # distance), un scan "plus ancienne ancre d'abord" pouvait encore
    # renvoyer un pattern bien plus ancien que le CRT visuellement le
    # plus évident, simplement parce qu'il satisfaisait containment+purge
    # AVANT une ancre plus récente et plus pertinente.
    for anchor_index in range(len(candles) - 2, -1, -1):
        anchor = candles[anchor_index]

        if max_anchor_age_candles is not None:
            age_in_candles = (len(candles) - 1) - anchor_index
            if age_in_candles > max_anchor_age_candles:
                # On scanne désormais du plus récent au plus ancien :
                # toute ancre suivante sera encore plus vieille -> inutile
                # de continuer, on arrête la recherche ici.
                break

        # Containment sur les MÈCHES de l'ancre (high/low), pas son corps
        # (open/close) : un CRT reste valide tant qu'aucune bougie n'a
        # clôturé en dehors des mèches de la bougie 1 (règle rappelée
        # par l'utilisateur). Un containment sur le corps, bien plus
        # étroit, casse presque immédiatement — l'algorithme avance
        # alors d'ancre en ancre en permanence, et peut "trouver" par
        # pure coïncidence deux bougies isolées, n'importe où dans
        # l'historique, dont les prix se chevauchent brièvement sans
        # représenter une vraie consolidation soutenue (ce qui produisait
        # des CRT invisibles visuellement sur le graphique réel).
        anchor_low = anchor.low
        anchor_high = anchor.high

        if _is_anchor_too_far_from_price(anchor, current_price):
            # Ancre hors de propos (régime de prix trop différent) :
            # inutile même d'explorer les purges possibles depuis cette
            # ancre, on passe directement à la suivante.
            continue

        for candidate_offset, candidate in enumerate(candles[anchor_index + 1 :]):
            candidate_index = anchor_index + 1 + candidate_offset
            is_contained = anchor_low <= candidate.close <= anchor_high
            if not is_contained:
                # Containment cassé avant toute purge : cette ancre est
                # abandonnée, on essaie une ancre plus récente.
                break

            if candidate.high > anchor.high:
                # "Déjà liquidée" : la cible (anchor.low) doit être vérifiée
                # sur TOUT l'historique depuis la purge, pas seulement le
                # prix actuel — une ancre à très large mèche peut "contenir"
                # le prix pendant des mois malgré plusieurs allers-retours
                # entre-temps ; si le prix a déjà atteint la cible à un
                # moment donné puis est revenu en arrière, la cible reste
                # consommée même si le prix ACTUEL ne la dépasse plus.
                post_purge = candles[candidate_index + 1 :]
                target_already_reached = current_price is not None and any(
                    c.low <= anchor.low for c in post_purge
                )
                target_too_far = (
                    current_price is not None
                    and anchor.low > 0
                    and abs(current_price - anchor.low) / anchor.low > MAX_ANCHOR_DISTANCE_RATIO
                )
                # SL déjà franchi : le SL d'un setup bearish (short) est le
                # high de la bougie de purge — le niveau de rejet. Si le
                # prix a ENSUITE dépassé ce niveau (cas réel ETH : SL
                # proposé sous le prix actuel, entrée déjà "stoppée" avant
                # même d'exister), le setup n'a plus de sens : on
                # n'entrerait jamais un short avec un stop déjà franchi.
                sl_already_breached = current_price is not None and (
                    current_price > candidate.high or any(c.high > candidate.high for c in post_purge)
                )
                if not target_already_reached and not target_too_far and not sl_already_breached:
                    return CrtPattern(
                        anchor_candle=anchor,
                        purge_candle=candidate,
                        purge_side="high",
                        dol_price=anchor.low,
                        bias="bearish",
                    )
                # Cible déjà liquidée à un moment donné (même si le prix est
                # depuis revenu en arrière), trop éloignée du prix actuel
                # pour être réaliste, OU SL déjà franchi depuis la purge ->
                # pattern obsolète, on abandonne cette ancre pour une plus
                # récente.
                break
            if candidate.low < anchor.low:
                post_purge = candles[candidate_index + 1 :]
                target_already_reached = current_price is not None and any(
                    c.high >= anchor.high for c in post_purge
                )
                target_too_far = (
                    current_price is not None
                    and anchor.high > 0
                    and abs(current_price - anchor.high) / anchor.high > MAX_ANCHOR_DISTANCE_RATIO
                )
                # SL déjà franchi : le SL d'un setup bullish (long) est le
                # low de la bougie de purge. Si le prix est ENSUITE repassé
                # sous ce niveau, le setup n'a plus de sens (on n'entrerait
                # jamais un long avec un stop déjà franchi).
                sl_already_breached = current_price is not None and (
                    current_price < candidate.low or any(c.low < candidate.low for c in post_purge)
                )
                if not target_already_reached and not target_too_far and not sl_already_breached:
                    return CrtPattern(
                        anchor_candle=anchor,
                        purge_candle=candidate,
                        purge_side="low",
                        dol_price=anchor.high,
                        bias="bullish",
                    )
                break

        # Containment valide sur toute la fenêtre mais aucune purge
        # trouvée pour cette ancre : on continue à chercher une ancre
        # plus récente, qui pourrait produire un pattern différent.

    return None


def _detect_all_crt_patterns(candles: list[Candle]) -> list[CrtPattern]:
    """
    Variante de _detect_crt_pattern qui renvoie TOUS les patterns CRT
    valides trouvés (une ancre par pattern), plutôt que de s'arrêter au
    premier trouvé. Utile en LTF où plusieurs CRT peuvent coexister à
    différentes ancres — chacun doit être analysé séparément par le
    decision_engine.

    Note : les patterns renvoyés peuvent se chevaucher dans le temps
    (une bougie peut être "contenue" par plusieurs ancres différentes).
    C'est volontaire : c'est au niveau supérieur (decision_engine) de
    prioriser ou dédupliquer si besoin.
    """
    patterns: list[CrtPattern] = []

    if len(candles) < 2:
        return patterns

    for anchor_index in range(len(candles) - 1):
        anchor = candles[anchor_index]
        # Voir _detect_crt_pattern : containment sur les mèches
        # (high/low) de l'ancre, pas son corps.
        anchor_low = anchor.low
        anchor_high = anchor.high

        for candidate in candles[anchor_index + 1 :]:
            is_contained = anchor_low <= candidate.close <= anchor_high
            if not is_contained:
                break

            if candidate.high > anchor.high:
                patterns.append(
                    CrtPattern(
                        anchor_candle=anchor,
                        purge_candle=candidate,
                        purge_side="high",
                        dol_price=anchor.low,
                        bias="bearish",
                    )
                )
                break
            if candidate.low < anchor.low:
                patterns.append(
                    CrtPattern(
                        anchor_candle=anchor,
                        purge_candle=candidate,
                        purge_side="low",
                        dol_price=anchor.high,
                        bias="bullish",
                    )
                )
                break

    return patterns


class CrtRule(BaseRule):
    """
    Règle de détection du pattern CRT (Candle Range Theory), exécutée sur
    les timeframes Monthly, Weekly et Daily.
    """

    name: ClassVar[str] = "crt_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("crt_rule", {})
        min_candles_required: int = cfg.get(
            "min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED
        )

        if not context.candles:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Aucun historique de bougies multi-timeframe fourni",
            )

        detected_patterns: dict[str, CrtPattern] = {}

        for timeframe in TIMEFRAME_ORDER:
            series = context.candles.get(timeframe)
            if series is None or len(series) < min_candles_required:
                continue

            # On exclut la dernière bougie : potentiellement en cours de
            # formation, elle n'est pas encore "clôturée".
            closed_candles = series.candles[:-1]
            pattern = _detect_crt_pattern(closed_candles, current_price=context.market_data.price_usd)

            if pattern is not None:
                detected_patterns[timeframe] = pattern

        if not detected_patterns:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Aucun pattern CRT détecté sur les timeframes disponibles",
            )

        severity = self._compute_severity(detected_patterns)
        message = self._build_message(detected_patterns)
        metadata = self._build_metadata(detected_patterns)

        return RuleResult(
            rule_name=self.name,
            triggered=True,
            message=message,
            severity=severity,
            metadata=metadata,
        )

    @staticmethod
    def _compute_severity(
        detected_patterns: dict[str, CrtPattern]
    ) -> Literal["info", "warning", "critical"]:
        """
        Détermine la sévérité en fonction du nombre de timeframes ayant
        détecté un pattern CRT ET partageant le même biais directionnel.

        Une timeframe seule = info. Deux timeframes alignées (même biais)
        = warning. Les trois timeframes alignées = critical (signal fort,
        conforme à la description : "si les trois sont coordonnées alors
        on a un indicateur puissant"). Des biais contradictoires entre
        timeframes restent en warning au maximum, car le signal est
        ambigu malgré la multiplicité des détections.
        """
        biases = {pattern.bias for pattern in detected_patterns.values()}
        aligned = len(biases) == 1
        count = len(detected_patterns)

        if aligned and count == len(TIMEFRAME_ORDER):
            return "critical"
        if count >= 2:
            return "warning"
        return "info"

    @staticmethod
    def _build_message(detected_patterns: dict[str, CrtPattern]) -> str:
        """Construit un message lisible résumant les patterns détectés."""
        parts = []
        for timeframe in TIMEFRAME_ORDER:
            pattern = detected_patterns.get(timeframe)
            if pattern is None:
                continue
            direction_label = "haussier" if pattern.bias == "bullish" else "baissier"
            parts.append(
                f"{timeframe} : purge côté {pattern.purge_side} → DOL à "
                f"${pattern.dol_price:,.2f} (biais {direction_label})"
            )

        biases = {pattern.bias for pattern in detected_patterns.values()}
        alignment_note = ""
        if len(detected_patterns) > 1:
            alignment_note = (
                " — timeframes alignées, signal renforcé"
                if len(biases) == 1
                else " — biais contradictoires entre timeframes, signal à confirmer"
            )

        return "Pattern(s) CRT détecté(s) : " + " | ".join(parts) + alignment_note

    @staticmethod
    def _build_metadata(detected_patterns: dict[str, CrtPattern]) -> dict[str, Any]:
        """
        Sérialise les patterns détectés en métadonnées scalaires,
        exploitables par la sauvegarde en base de données
        (database/db.py) et par messaging/formatter.py pour le message
        final.
        """
        metadata: dict[str, Any] = {}

        for timeframe, pattern in detected_patterns.items():
            metadata[timeframe] = {
                "anchor_timestamp": pattern.anchor_candle.timestamp.isoformat(),
                "anchor_open": pattern.anchor_candle.open,
                "anchor_close": pattern.anchor_candle.close,
                "anchor_high": pattern.anchor_candle.high,
                "anchor_low": pattern.anchor_candle.low,
                "purge_timestamp": pattern.purge_candle.timestamp.isoformat(),
                "purge_side": pattern.purge_side,
                "dol_price": pattern.dol_price,
                "bias": pattern.bias,
            }

        biases = {pattern.bias for pattern in detected_patterns.values()}
        metadata["timeframes_detected"] = list(detected_patterns.keys())
        metadata["aligned"] = len(biases) == 1
        metadata["aligned_bias"] = next(iter(biases)) if len(biases) == 1 else None

        return metadata
