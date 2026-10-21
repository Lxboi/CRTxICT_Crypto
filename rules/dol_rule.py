"""
rules/dol_rule.py

DOL Rule (Draw On Liquidity) : agrège les 3 mécanismes producteurs de DOL
sur chaque timeframe (Monthly, Weekly, Daily) :

  1. CRT (Candle Range Theory) — réutilise rules/crt_rule.py::_detect_crt_pattern.
  2. Bias (close above/below la mèche de la bougie précédente) — logique
     initiale du projet : si la dernière bougie close clôture en dehors
     de la mèche de la précédente, un biais momentum se dessine, avec
     comme DOL la mèche de la bougie précédente. Ce DOL est invalidé si
     le prix courant l'a déjà retesté.
  3. Double Purge — une bougie de référence dont le high ET le low ont
     tous deux été liquidés par d'autres bougies dans une fenêtre courte
     (idéalement 1 bougie, jusqu'à 5). Si trouvée, la DOL est l'extrémité
     opposée du Dealing Range courant (réutilise
     rules/narrative_rule.py::_compute_dealing_range).

Cette règle ne fait AUCUNE hypothèse de priorité entre les 3 mécanismes :
elle les rapporte tous, par timeframe. La confluence (plusieurs
mécanismes et/ou plusieurs timeframes d'accord sur un même biais)
détermine la sévérité.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from config.constants import TIMEFRAME_ORDER
from models.market_data import Candle
from rules.crt_rule import CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME, _detect_crt_pattern
from rules.engine import BaseRule, MarketContext, RuleResult
from rules.narrative_rule import _compute_dealing_range

Bias = Literal["bullish", "bearish"]

# --- Paramètres par défaut, surchargeables via config/alerts.yaml ---
DEFAULT_DOUBLE_PURGE_WINDOW = 5
# Le Monthly est limité par la contrainte API (~3 mois d'historique
# reconstruit), donc une fenêtre de 5 y est presque toujours inatteignable.
# Valeur volontairement plus petite ici ; à corriger le jour où l'historique
# Monthly sera plus profond.
DEFAULT_DOUBLE_PURGE_WINDOW_MONTHLY = 3
DEFAULT_SWING_LOOKBACK = 2
DEFAULT_EQUILIBRIUM_BAND_PCT = 0.1
DEFAULT_MIN_CANDLES_REQUIRED = 5


@dataclass
class DolCandidate:
    """Une DOL candidate produite par l'un des 3 mécanismes, sur une timeframe."""

    source: Literal["crt", "bias", "double_purge"]
    dol_price: float
    bias: Bias
    detail: str


# ---------------------------------------------------------------------
# 2. Bias (close above / below la mèche précédente)
# ---------------------------------------------------------------------


def _detect_bias(candles: list[Candle]) -> DolCandidate | None:
    """
    Détecte le biais momentum le PLUS RÉCENT non invalidé — en
    recherchant vers l'arrière autant de paires de bougies consécutives
    que nécessaire (pas seulement les 2 dernières). Sans cette
    recherche, une cassure réelle et jamais invalidée "sortirait" de la
    mémoire du robot dès qu'une nouvelle bougie s'ouvre, même si aucune
    bougie n'est jamais revenue la contredire.

    Pour chaque paire (bougie précédente, bougie suivante), en partant
    de la plus récente et en remontant : si la bougie suivante clôture
    en dessous du low (la mèche) de la précédente, biais baissier, DOL
    = ce low. Si elle clôture au-dessus du high de la précédente, biais
    haussier, DOL = ce high.

    IMPORTANT — invalidation par CLÔTURE CONFIRMÉE uniquement (jamais
    le prix courant en temps réel) : une cassure trouvée n'est
    invalidée QUE si une bougie CLÔTURÉE ultérieure (parmi `candles`,
    qui exclut déjà la bougie en cours de formation — voir
    DolRule.evaluate()::closed_candles = series.candles[:-1]) a
    elle-même clôturé au-delà du niveau cassé. Le prix courant en
    cours de mois/semaine/jour peut dépasser temporairement ce niveau
    sans invalider le biais : seule une VRAIE clôture compte (juillet
    2026 — cas réel observé sur TRX : le prix courant dépassait de
    justesse un niveau cassé en juin, sans que juillet n'ait encore
    clôturé — le biais devait rester actif).

    Une cassure invalidée par une clôture ultérieure fait continuer la
    recherche plus loin en arrière (une cassure plus ancienne et
    toujours valide peut exister). Renvoie None seulement si tout
    l'historique disponible est épuisé sans trouver de cassure encore
    valide.
    """
    if len(candles) < 2:
        return None

    for i in range(len(candles) - 1, 0, -1):
        previous, last = candles[i - 1], candles[i]

        if last.close < previous.low:
            dol_price = previous.low
            bias: Bias = "bearish"
        elif last.close > previous.high:
            dol_price = previous.high
            bias = "bullish"
        else:
            continue  # cette paire ne montre aucune cassure -- essayer la paire précédente

        # Invalidation : seules les bougies CLÔTURÉES entre la cassure
        # et la fin de l'historique fourni comptent (jamais le prix
        # courant en temps réel, déjà exclu de `candles`).
        subsequent_closed_candles = candles[i + 1:]
        if bias == "bearish" and any(c.close >= dol_price for c in subsequent_closed_candles):
            continue  # une cloture ulterieure a deja invalide cette cassure -- chercher plus loin
        if bias == "bullish" and any(c.close <= dol_price for c in subsequent_closed_candles):
            continue

        candles_back = len(candles) - 1 - i
        age_note = "" if candles_back == 0 else f" (il y a {candles_back} bougie(s))"

        return DolCandidate(
            source="bias",
            dol_price=dol_price,
            bias=bias,
            detail=(
                f"Clôture {'sous' if bias == 'bearish' else 'au-dessus de'} la mèche "
                f"de la bougie précédente{age_note}"
            ),
        )

    return None


# ---------------------------------------------------------------------
# 3. Double Purge
# ---------------------------------------------------------------------


def _detect_double_purge(candles: list[Candle], window_size: int) -> Candle | None:
    """
    Cherche, dans les `window_size` dernières bougies, une bougie de
    référence dont le high ET le low ont tous deux été dépassés par
    d'autres bougies de la fenêtre (pas nécessairement la même bougie
    pour les deux côtés) — une "liquidation" complète de son range.

    Returns:
        La bougie de référence liquidée des deux côtés, ou None.
    """
    if len(candles) < 2:
        return None

    recent = candles[-window_size:]

    for ref_index in range(len(recent) - 1):
        reference = recent[ref_index]
        subsequent = recent[ref_index + 1 :]

        high_swept = any(c.high > reference.high for c in subsequent)
        low_swept = any(c.low < reference.low for c in subsequent)

        if high_swept and low_swept:
            return reference

    return None


def _build_double_purge_candidate(
    reference_candle: Candle,
    closed_candles: list[Candle],
    current_price: float,
    swing_lookback: int,
    equilibrium_band_pct: float,
) -> DolCandidate | None:
    """
    Construit la DOL candidate issue d'un Double Purge confirmé : la
    cible est l'extrémité opposée du Dealing Range courant par rapport
    à la position actuelle du prix (discount -> cible en haut du range,
    premium -> cible en bas).
    """
    dealing_range = _compute_dealing_range(
        closed_candles, current_price, swing_lookback, equilibrium_band_pct
    )
    if dealing_range is None:
        return None

    if dealing_range.zone in ("discount", "equilibrium"):
        dol_price = dealing_range.range_high
        bias: Bias = "bullish"
    else:
        dol_price = dealing_range.range_low
        bias = "bearish"

    return DolCandidate(
        source="double_purge",
        dol_price=dol_price,
        bias=bias,
        detail=(
            f"Double purge détectée (bougie du {reference_candle.timestamp.date()} "
            f"liquidée des 2 côtés) → cible à l'opposé du dealing range "
            f"[${dealing_range.range_low:,.2f} - ${dealing_range.range_high:,.2f}]"
        ),
    )


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


class DolRule(BaseRule):
    """
    Règle DOL (Draw On Liquidity) : agrège CRT, Bias et Double Purge sur
    chaque timeframe disponible. Se déclenche dès qu'au moins une DOL
    est identifiée. La sévérité reflète la confluence : plusieurs
    mécanismes et/ou plusieurs timeframes d'accord sur le même biais
    renforcent le signal.
    """

    name: ClassVar[str] = "dol_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("dol_rule", {})
        double_purge_window: int = cfg.get(
            "double_purge_window", DEFAULT_DOUBLE_PURGE_WINDOW
        )
        double_purge_window_monthly: int = cfg.get(
            "double_purge_window_monthly", DEFAULT_DOUBLE_PURGE_WINDOW_MONTHLY
        )
        swing_lookback: int = cfg.get("swing_lookback", DEFAULT_SWING_LOOKBACK)
        equilibrium_band_pct: float = cfg.get(
            "equilibrium_band_pct", DEFAULT_EQUILIBRIUM_BAND_PCT
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
        candidates_by_timeframe: dict[str, list[DolCandidate]] = {}

        for timeframe in TIMEFRAME_ORDER:
            series = context.candles.get(timeframe)
            if series is None or len(series) < min_candles_required:
                continue

            closed_candles = series.candles[:-1]
            if len(closed_candles) < 2:
                continue

            timeframe_candidates: list[DolCandidate] = []

            # 1. CRT
            crt_pattern = _detect_crt_pattern(
                closed_candles,
                current_price=context.market_data.price_usd,
                max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get(timeframe),
            )
            if crt_pattern is not None:
                timeframe_candidates.append(
                    DolCandidate(
                        source="crt",
                        dol_price=crt_pattern.dol_price,
                        bias=crt_pattern.bias,
                        detail=(
                            f"Pattern CRT (purge côté {crt_pattern.purge_side} "
                            f"de l'ancre du {crt_pattern.anchor_candle.timestamp.date()})"
                        ),
                    )
                )

            # 2. Bias
            bias_candidate = _detect_bias(closed_candles)
            if bias_candidate is not None:
                timeframe_candidates.append(bias_candidate)

            # 3. Double Purge
            window = (
                double_purge_window_monthly if timeframe == "monthly" else double_purge_window
            )
            purged_reference = _detect_double_purge(closed_candles, window)
            if purged_reference is not None:
                double_purge_candidate = _build_double_purge_candidate(
                    purged_reference,
                    closed_candles,
                    current_price,
                    swing_lookback,
                    equilibrium_band_pct,
                )
                if double_purge_candidate is not None:
                    timeframe_candidates.append(double_purge_candidate)

            if timeframe_candidates:
                candidates_by_timeframe[timeframe] = timeframe_candidates

        if not candidates_by_timeframe:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Aucune DOL identifiée actuellement",
            )

        message = self._build_message(candidates_by_timeframe)
        severity = self._compute_severity(candidates_by_timeframe)
        metadata = self._build_metadata(candidates_by_timeframe)

        return RuleResult(
            rule_name=self.name,
            triggered=True,
            message=message,
            severity=severity,
            metadata=metadata,
        )

    @staticmethod
    def _build_message(candidates_by_timeframe: dict[str, list[DolCandidate]]) -> str:
        parts = []
        for timeframe in TIMEFRAME_ORDER:
            candidates = candidates_by_timeframe.get(timeframe)
            if not candidates:
                continue
            for candidate in candidates:
                parts.append(
                    f"{timeframe}/{candidate.source} : DOL {candidate.bias} à "
                    f"${candidate.dol_price:,.2f} ({candidate.detail})"
                )
        return "DOL identifiée(s) : " + " | ".join(parts)

    @staticmethod
    def _compute_severity(
        candidates_by_timeframe: dict[str, list[DolCandidate]]
    ) -> Literal["info", "warning", "critical"]:
        """
        Sévérité basée sur la confluence : nombre de timeframes
        distinctes ayant produit au moins une DOL, ET accord sur le
        biais parmi TOUTES les DOL candidates (tous mécanismes, toutes
        timeframes confondus).
        """
        all_biases = {
            candidate.bias
            for candidates in candidates_by_timeframe.values()
            for candidate in candidates
        }
        aligned = len(all_biases) == 1
        timeframe_count = len(candidates_by_timeframe)

        if aligned and timeframe_count == len(TIMEFRAME_ORDER):
            return "critical"
        if timeframe_count >= 2 or sum(len(c) for c in candidates_by_timeframe.values()) >= 2:
            return "warning"
        return "info"

    @staticmethod
    def _build_metadata(candidates_by_timeframe: dict[str, list[DolCandidate]]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}

        for timeframe, candidates in candidates_by_timeframe.items():
            metadata[timeframe] = [
                {
                    "source": candidate.source,
                    "dol_price": candidate.dol_price,
                    "bias": candidate.bias,
                    "detail": candidate.detail,
                }
                for candidate in candidates
            ]

        all_biases = {
            candidate.bias
            for candidates in candidates_by_timeframe.values()
            for candidate in candidates
        }
        metadata["aligned"] = len(all_biases) == 1
        metadata["aligned_bias"] = next(iter(all_biases)) if len(all_biases) == 1 else None
        metadata["timeframes_with_dol"] = list(candidates_by_timeframe.keys())

        return metadata
