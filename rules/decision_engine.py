"""
rules/decision_engine.py

DecisionEngine : orchestre toutes les règles métier selon la
méthodologie du projet (4 phases) :

  1. Cascade de biais Monthly -> Weekly -> Daily (_resolve_timeframe_bias) :
     à chaque niveau, priorité cassure de biais > CRT > double purge. On
     ne descend d'une timeframe que si la précédente n'offre rien de
     clair, avec une confiance réduite à chaque descente (voir
     bias_source_timeframe sur DecisionReport).
  2. Narrative HTF (Weekly Profiles) + catalogue des KL majeurs
     (Monthly/Weekly/Daily) dans le sens du biais effectif.
  3. CRT indépendants et nommés sur CHAQUE timeframe (Monthly, Weekly,
     Daily, 4H — voir CRT_SCAN_TIMEFRAMES) : chaque CRT valide et
     aligné avec le biais effectif est un setup à part entière (voir
     _build_crt_setup), avec son propre nom stable pour le suivi à
     travers les notifications. Filtres anti-obsolescence détaillés
     dans rules/crt_rule.py (mèches, âge, distance, SL déjà franchi).
  4. Purge Rule — évalue si la purge de chaque CRT (Weekly/Daily/4H) est
     High Probability (KillZone/Algo Times) ; conditionne
     entry_recommended pour ce setup précis.
  5. Entrée précise unique — parmi toutes celles trouvées (Distribution,
     Modèles 1-4, étapes TSQ), une seule est retenue selon l'ordre de
     priorité méthodologique (voir rules/refined_entry.py).

Ce module ne remplace pas RuleEngine (rules/engine.py) : il l'utilise en
appelant directement les règles individuelles et en interprétant leurs
résultats/métadonnées selon la logique métier ci-dessus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from config.constants import NY_TZ
from rules.crt_rule import (
    CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME,
    MAX_ANCHOR_DISTANCE_RATIO,
    CrtPattern,
    _detect_crt_pattern,
)
from rules.dol_rule import DolRule
from rules.engine import MarketContext
from rules.entry_rule import EntryRule
from rules.keylevel_rule import _filter_unmitigated, find_key_levels
from rules.narrative_ltf_rule import NarrativeLtfRule
from rules.narrative_rule import NarrativeRule
from rules.purge_rule import CATEGORY_GRIDS, CRYPTO_4H_GRID_HOURS, PurgeRule, _resample_to_4h, resolve_asset_category
from rules.refined_entry import RefinedEntry, collect_refined_entries, select_priority_entry
from rules.tsq_rule import TsqRule
from models.market_data import Candle, format_price_adaptive

Bias = Literal["bullish", "bearish"]
Confidence = Literal["high", "medium", "low"]

# Paires HTF -> LTF évaluées en parallèle. Chacune produit son propre
# CRT + TPs + compatibilité LTF, rapportés ensemble dans la notification.
# Monthly->Weekly n'est volontairement pas incluse : la contrainte du
# projet limite le CRT "exploitable pour une entrée" au Daily au plus
# bas, et au Weekly au plus haut avec nos données actuelles.
CRT_SCAN_TIMEFRAMES: list[str] = ["monthly", "weekly", "daily", "4h"]


@dataclass
class CrtTargets:
    """
    Objectifs provisoires dérivés d'un CRT.

    stop_loss : H/L de la bougie de confirmation/purge (la "bougie 2"),
    même convention que rules/entry_rule.py — Low pour un setup
    bullish, High pour un setup bearish.
    """

    tp_50: float
    tp_100: float
    tp_htf_dol: float | None
    stop_loss: float


@dataclass
class TimeframeSetup:
    """
    Un CRT valide et indépendant, détecté à une timeframe donnée
    (monthly/weekly/daily/4h), aligné avec le biais effectif. Chaque
    CRT valide est un setup à part entière, suivi et notifié
    indépendamment — remplace l'ancienne notion de paire HTF/LTF fixe
    (weekly->daily, daily->intraday), qui ne correspondait pas à la
    méthodologie réelle (un CRT Monthly, Weekly, Daily ou 4H valide et
    aligné est TOUJOURS un setup à considérer, pas seulement quand il
    forme une paire avec une timeframe inférieure précise).

    setup_name : identifiant lisible et stable pour reconnaître ce
    setup à travers ses notifications successives (nouveau signal,
    TP touché, SL touché) — voir _make_setup_name().
    """

    timeframe: str  # "monthly" / "weekly" / "daily" / "4h"
    setup_name: str = ""
    htf_crt: dict[str, Any] | None = None
    crt_targets: dict[str, Any] | None = None
    purge_checked: bool = False
    purge_high_probability: bool = False
    entry_recommended: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class DecisionReport:
    """
    Rapport complet de la décision, destiné à la fois à un usage
    programmatique (métadonnées structurées) et à la notification finale
    (via to_text()).
    """

    reasoning_log: list[str] = field(default_factory=list)

    monthly_bias: Bias | None = None
    monthly_dol_candidates: list[dict[str, Any]] = field(default_factory=list)
    monthly_liquidated_zones: list[dict[str, Any]] = field(default_factory=list)

    weekly_bias: Bias | None = None
    daily_bias: Bias | None = None
    bias_source_timeframe: str | None = None  # "monthly"/"weekly"/"daily" : quelle timeframe a determiné effective_bias
    weekly_dol_candidates: list[dict[str, Any]] = field(default_factory=list)
    daily_dol_candidates: list[dict[str, Any]] = field(default_factory=list)
    weekly_narrative_profiles: dict[str, float] = field(default_factory=dict)
    weekly_day_roles: list[dict[str, Any]] = field(default_factory=list)
    smt_context: dict[str, Any] | None = None

    monthly_weekly_aligned: bool = False
    effective_bias: Bias | None = None
    confidence: Confidence = "low"

    major_kls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    weekly_kls_in_monthly_direction: list[dict[str, Any]] = field(default_factory=list)
    liquidated_kls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    narrative_validated_by_liquidation: bool = False

    timeframe_setups: list[TimeframeSetup] = field(default_factory=list)
    narrative_ltf_profiles: dict[str, float] = field(default_factory=dict)
    dealing_range_ltf: dict[str, Any] | None = None
    crt_ltf: dict[str, Any] | None = None
    double_purge_ltf: dict[str, Any] | None = None
    keylevel_ltf_touched: list[dict[str, Any]] = field(default_factory=list)
    ohlc_coherence: float | None = None

    economic_calendar: dict[str, Any] = field(default_factory=dict)
    high_impact_news: dict[str, Any] = field(default_factory=dict)
    quiet_day: dict[str, Any] = field(default_factory=dict)
    holiday: dict[str, Any] = field(default_factory=dict)

    purge_assessments: list[dict[str, Any]] = field(default_factory=list)

    refined_entries: list[RefinedEntry] = field(default_factory=list)
    priority_entry: RefinedEntry | None = None

    entry_recommended: bool = False

    def to_text(self) -> str:
        """Résumé lisible du raisonnement complet, pour la notification."""
        lines = ["=== Analyse du marché ===", ""]
        lines.extend(f"- {entry}" for entry in self.reasoning_log)
        lines.append("")

        if not self.timeframe_setups:
            lines.append(">>> Pas d'entrée recommandée actuellement")
            return "\n".join(lines)

        for setup in self.timeframe_setups:
            label = setup.setup_name or setup.timeframe.capitalize()
            if setup.entry_recommended and setup.htf_crt:
                lines.append(
                    f">>> [{label}] ENTRÉE {setup.htf_crt['bias'].upper()} recommandée "
                    f"(confiance: {self.confidence})"
                )
                targets = setup.crt_targets or {}
                if targets.get("stop_loss") is not None:
                    lines.append(f"    SL : {format_price_adaptive(targets['stop_loss'])}")
                if targets.get("tp_50") is not None:
                    lines.append(f"    TP1 (50% CRT) : {format_price_adaptive(targets['tp_50'])}")
                if targets.get("tp_100") is not None:
                    lines.append(f"    TP2 (100% CRT / DOL) : {format_price_adaptive(targets['tp_100'])}")
                if targets.get("tp_htf_dol") is not None:
                    lines.append(f"    TP3 (DOL HTF) : {format_price_adaptive(targets['tp_htf_dol'])}")
            else:
                lines.append(f">>> [{label}] Pas d'entrée recommandée")
            for note in setup.notes:
                lines.append(f"    Note : {note}")

        return "\n".join(lines)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _resolve_timeframe_bias(candidates: list[dict[str, Any]], current_price: float | None = None) -> Bias | None:
    """
    Résout le biais d'une timeframe selon la cascade de priorité
    (révisée août 2026, cas réel BTC) :
      1. CRT valide (source="crt") — priorité maximale. La validité est
         DÉJÀ garantie par rules/crt_rule.py::_detect_crt_pattern avant
         même d'arriver ici (cible pas encore atteinte, ancre pas trop
         ancienne en temps ni trop loin en prix — voir sa docstring) :
         un CRT qui arrive jusqu'ici n'a donc jamais besoin d'être
         re-vérifié. Un seul CRT possible par timeframe de toute façon
         (le plus récent valide, _detect_crt_pattern scanne déjà du
         plus récent vers le plus ancien et s'arrête au premier trouvé).
      2. Si aucun CRT valide : cassure de biais simple (close above/below
         la mèche précédente, source="bias").
    Le Double Purge (source="double_purge") n'est PAS utilisé ici comme
    mécanisme de biais autonome — c'est une confirmation qui accompagne
    le CRT et/ou la cassure de biais, pas un vote à égalité qui pourrait
    neutraliser un signal par ailleurs clair (c'était le bug précédent :
    exiger l'unanimité entre tous les mécanismes rendait le résultat
    "incohérent" dès qu'un seul mécanisme plus simple divergeait).

    Historique : la cascade était auparavant "cassure > CRT" (priorité
    inverse), avec une exception limitée aux CRT formés sur les 2
    dernières bougies. Simplifiée ici à la demande explicite de
    l'utilisateur : dès lors qu'un CRT est présent (donc valide, par
    construction), il prime systématiquement — une cassure de biais
    peut avoir été trouvée en remontant loin dans l'historique (voir
    rules/dol_rule.py::_detect_bias), donc être bien plus ancienne
    qu'un CRT toujours valide et non invalidé.

    Args:
        current_price: Conservé dans la signature pour compatibilité
            des appelants existants — plus utilisé par cette fonction
            depuis la simplification (la validité du CRT ne dépend
            plus d'une vérification de range ici, déjà faite en amont).

    Returns:
        Le biais du CRT si présent, sinon celui de la cassure si
        présente, sinon None (rien de clair à cette timeframe — le
        code appelant doit alors descendre à la timeframe suivante).
    """
    crt_candidate = next((c for c in candidates if c.get("source") == "crt"), None)
    if crt_candidate is not None:
        return crt_candidate["bias"]

    bias_candidate = next((c for c in candidates if c.get("source") == "bias"), None)
    if bias_candidate is not None:
        return bias_candidate["bias"]

    return None


def _detect_recently_liquidated_zones(
    candles: list[Candle], keylevel_config: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Détecte les zones KeyLevel qui étaient actives juste avant la
    dernière bougie close, mais qui viennent d'être mitigées par elle —
    signe qu'une liquidité vient d'être prise. Contrairement à une
    simple liste de types, on conserve ici le détail complet de chaque
    zone (direction, bornes) : ces zones liquidées doivent être
    retenues, pas juste ignorées, car une prise de liquidité dans le
    bon sens peut valider un scénario de narrative HTF ou LTF.
    """
    if len(candles) < 3:
        return []

    before = _filter_unmitigated(find_key_levels(candles[:-1], keylevel_config), candles[:-1])
    after = _filter_unmitigated(find_key_levels(candles, keylevel_config), candles)

    def _zone_key(zone) -> tuple[str, float, float]:
        return (zone.kind, round(zone.zone_low, 2), round(zone.zone_high, 2))

    after_keys = {_zone_key(z) for z in after}
    liquidated = [z for z in before if _zone_key(z) not in after_keys]

    return [
        {
            "kind": zone.kind,
            "direction": zone.direction,
            "zone_low": zone.zone_low,
            "zone_high": zone.zone_high,
        }
        for zone in liquidated
    ]


MAX_KL_DISTANCE_RATIO = 0.5  # zone à plus de 50% du prix actuel -> jugée trop lointaine pour être pertinente


def _filter_kls_toward_target(
    zones: list, bias: Bias, current_price: float
) -> list:
    """
    Filtre une liste de zones KeyLevel pour ne garder que celles
    compatibles avec un biais directionnel donné : les pools de
    liquidité que le marché devrait "consommer" en chemin vers la
    cible.

    Pour un biais bearish (marché attendu à la baisse vers une DOL plus
    basse), on garde les zones de support/demande ("bullish", situées
    en dessous du prix actuel) : ce sont elles que le marché va balayer
    en descendant. Symétriquement pour un biais bullish, on garde les
    zones de résistance/offre ("bearish", au-dessus du prix).

    Écarte aussi les zones trop éloignées du prix actuel (au-delà de
    MAX_KL_DISTANCE_RATIO) — même non mitigée, une zone à des années de
    distance de prix n'est plus une liquidité "sur le chemin" réaliste
    (même bug de fond que celui corrigé sur les CRT : une zone jamais
    retestée peut rester "valide" indéfiniment sans jamais redevenir
    pertinente).
    """
    if bias == "bearish":
        candidates = [
            zone for zone in zones if zone.direction == "bullish" and zone.midpoint < current_price
        ]
    else:
        candidates = [
            zone for zone in zones if zone.direction == "bearish" and zone.midpoint > current_price
        ]

    return [
        zone
        for zone in candidates
        if abs(current_price - zone.midpoint) / zone.midpoint <= MAX_KL_DISTANCE_RATIO
    ]


def _compute_crt_targets(
    crt: CrtPattern, htf_dol_price: float | None, current_price: float | None = None
) -> CrtTargets:
    """
    Calcule les 3 objectifs provisoires d'un CRT : 50% de son range,
    100% (= sa DOL), et la DOL HTF précédemment repérée si elle est
    compatible en direction (au-delà de la DOL du CRT dans le même sens)
    ET raisonnable en distance de prix.

    htf_dol_price provient des candidats DOL bruts (mécanismes "bias",
    "double_purge", "crt" combinés — voir rules/dol_rule.py), sélectionné
    comme le plus extrême parmi eux compatibles en biais. Ce choix ne
    passe PAR AUCUN des filtres anti-obsolescence de _detect_crt_pattern
    (mèches, âge, distance) : seul le mécanisme "crt" en bénéficie
    directement, "bias" et "double_purge" n'ont pas d'équivalent. Un cas
    réel (TRX) a montré un TP3 à 614% du prix actuel, provenant d'un de
    ces mécanismes non filtrés. Pour s'en prémunir sans devoir traquer
    chaque mécanisme séparément, on valide ici directement le résultat
    final (htf_dol_price) par sa distance au prix actuel — quel que soit
    le mécanisme qui l'a produit.
    """
    anchor_midpoint = (crt.anchor_candle.high + crt.anchor_candle.low) / 2

    compatible_htf_dol = None
    if htf_dol_price is not None:
        direction_compatible = (crt.bias == "bullish" and htf_dol_price > crt.dol_price) or (
            crt.bias == "bearish" and htf_dol_price < crt.dol_price
        )
        reasonable_distance = (
            current_price is None
            or htf_dol_price <= 0
            or abs(current_price - htf_dol_price) / htf_dol_price <= MAX_ANCHOR_DISTANCE_RATIO
        )
        if direction_compatible and reasonable_distance:
            compatible_htf_dol = htf_dol_price

    stop_loss = crt.purge_candle.low if crt.bias == "bullish" else crt.purge_candle.high

    return CrtTargets(
        tp_50=anchor_midpoint,
        tp_100=crt.dol_price,
        tp_htf_dol=compatible_htf_dol,
        stop_loss=stop_loss,
    )


def _get_closed_candles_for_scope(context: MarketContext, scope: str) -> list[Candle] | None:
    """
    Bougies closes pour une timeframe de scan CRT. "4h" est un cas
    particulier : n'existe pas nativement dans MarketContext.candles —
    resamplée depuis "m15" (ou "intraday" en repli) via
    rules/purge_rule.py::_resample_to_4h, sur la grille propre à la
    catégorie d'actif du symbole (resolve_asset_category) -- TOUJOURS la
    même grille que celle utilisée par PurgeRule.evaluate() pour ce même
    symbole (voir rules/purge_rule.py, correctif août 2026), sans quoi le
    CRT 4H détecté ici et la purge évaluée par PurgeRule porteraient sur
    des bougies 4H différentes.
    """
    if scope == "4h":
        fine_series = (
            (context.candles.get("m15") or context.candles.get("intraday"))
            if context.candles
            else None
        )
        if fine_series is None or len(fine_series.candles) < 2:
            return None
        grid_hours = CATEGORY_GRIDS.get(resolve_asset_category(context.symbol), CRYPTO_4H_GRID_HOURS)
        four_h_candles = _resample_to_4h(fine_series.candles[:-1], grid_hours)
        return four_h_candles[:-1] if len(four_h_candles) >= 3 else None

    series = context.candles.get(scope) if context.candles else None
    if series is None or len(series.candles) < 3:
        return None
    return series.candles[:-1]


def _make_setup_name(symbol: str | None, timeframe: str, anchor_timestamp: datetime) -> str:
    """
    Identifiant lisible et stable pour reconnaître ce setup à travers
    ses notifications successives (nouveau signal, TP touché, SL
    touché) — basé sur la timeframe et la date de la bougie d'ancrage,
    qui identifie ce CRT de façon unique.
    """
    local = anchor_timestamp.astimezone(NY_TZ)
    if timeframe == "monthly":
        date_label = local.strftime("%m/%Y")
    elif timeframe == "4h":
        date_label = local.strftime("%d/%m %Hh")
    else:
        date_label = local.strftime("%d/%m")
    prefix = f"{symbol} " if symbol else ""
    return f"{prefix}{timeframe.capitalize()} CRT ({date_label})"


def _build_crt_setup(
    timeframe: str,
    context: MarketContext,
    htf_dol_reference_candidates: list[dict[str, Any]],
    effective_bias: Bias | None,
    purge_assessments: list[dict[str, Any]],
    log: list[str],
) -> TimeframeSetup | None:
    """
    Détecte un CRT valide sur `timeframe` et, s'il est aligné avec le
    biais effectif, construit son setup indépendant. Renvoie None si
    aucun CRT valide n'existe à cette timeframe, ou s'il existe mais
    est en désaccord avec le biais effectif — dans ces deux cas, il n'y
    a simplement pas de setup à créer pour cette timeframe ce cycle
    (contrairement à l'ancien comportement, qui renvoyait toujours un
    TimeframeSetup, même vide).
    """
    closed_candles = _get_closed_candles_for_scope(context, timeframe)
    if closed_candles is None:
        log.append(f"Historique {timeframe} indisponible -> pas de scan CRT")
        return None

    crt_pattern = _detect_crt_pattern(
        closed_candles,
        current_price=context.market_data.price_usd,
        max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get(timeframe),
    )
    if crt_pattern is None:
        log.append(f"Aucun CRT {timeframe.capitalize()} détecté")
        return None

    if effective_bias is not None and crt_pattern.bias != effective_bias:
        log.append(
            f"CRT {timeframe.capitalize()} détecté (biais {crt_pattern.bias}) mais en désaccord "
            f"avec le biais effectif ({effective_bias}) -> ignoré"
        )
        return None

    compatible_dols = [
        c["dol_price"] for c in htf_dol_reference_candidates if c["bias"] == crt_pattern.bias
    ]
    htf_dol_price = None
    if compatible_dols:
        htf_dol_price = (
            max(compatible_dols) if crt_pattern.bias == "bullish" else min(compatible_dols)
        )

    targets = _compute_crt_targets(crt_pattern, htf_dol_price, context.market_data.price_usd)
    setup_name = _make_setup_name(context.symbol, timeframe, crt_pattern.anchor_candle.timestamp)

    setup = TimeframeSetup(
        timeframe=timeframe,
        setup_name=setup_name,
        htf_crt={
            "bias": crt_pattern.bias,
            "dol_price": crt_pattern.dol_price,
            "purge_side": crt_pattern.purge_side,
            "anchor_timestamp": crt_pattern.anchor_candle.timestamp.isoformat(),
            "purge_timestamp": crt_pattern.purge_candle.timestamp.isoformat(),
        },
        crt_targets={
            "tp_50": targets.tp_50,
            "tp_100": targets.tp_100,
            "tp_htf_dol": targets.tp_htf_dol,
            "stop_loss": targets.stop_loss,
        },
    )

    log.append(
        f"{setup_name} : biais {crt_pattern.bias}, "
        f"TP1(50%)={format_price_adaptive(targets.tp_50)}, "
        f"TP2(100%)={format_price_adaptive(targets.tp_100)}"
        + (f", TP3(DOL HTF)={format_price_adaptive(targets.tp_htf_dol)}" if targets.tp_htf_dol else "")
    )

    # Le filtre de qualité de purge (KillZone/Algo Times, timing
    # Week1->Week2) existe pour Weekly, Daily et 4H — voir
    # rules/purge_rule.py. Monthly n'a pas d'équivalent défini : entrée
    # basée uniquement sur l'alignement de biais (déjà garanti ci-dessus).
    if timeframe in ("weekly", "daily", "4h"):
        setup.purge_checked = True
        setup.purge_high_probability = any(
            a["high_probability"] for a in purge_assessments if a.get("crt_scope") == timeframe
        )
    else:
        setup.notes.append(
            f"Pas de filtre de qualité de purge défini pour un CRT {timeframe} "
            "-> entrée basée uniquement sur l'alignement de biais"
        )

    setup.entry_recommended = setup.purge_high_probability if setup.purge_checked else True

    if setup.entry_recommended:
        log.append(f"-> ENTRÉE potentielle {crt_pattern.bias.upper()} au marché ({setup_name})")
    else:
        log.append(f"-> Pas d'entrée pour {setup_name} (purge non High Probability)")

    return setup


# ---------------------------------------------------------------------
# DecisionEngine
# ---------------------------------------------------------------------


class DecisionEngine:
    """
    Point d'entrée unique pour produire une décision complète à partir
    d'un MarketContext peuplé (monthly, weekly, daily, et idéalement
    intraday).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}

    def decide(self, context: MarketContext) -> DecisionReport:
        cfg = self._config
        keylevel_cfg: dict[str, Any] = cfg.get("keylevel_rule", {})
        log: list[str] = []

        # --- Étape 1 & 2 : DOL Monthly/Weekly + Narrative HTF ---
        dol_result = DolRule().evaluate(context, cfg)
        narrative_result = NarrativeRule().evaluate(context, cfg)

        high_impact_news: dict[str, Any] = narrative_result.metadata.get(
            "high_impact_news", {}
        )
        news_risk_active = bool(high_impact_news.get("currently_in_avoid_window"))
        if high_impact_news.get("high_impact_today"):
            events_label = ", ".join(high_impact_news["high_impact_today"])
            if news_risk_active:
                log.append(
                    f"⚠️ Fenêtre à risque active pour news à haut impact ({events_label}) "
                    "-> entrée toujours notifiée, mais volatilité accrue possible"
                )
            else:
                log.append(f"News à haut impact prévue aujourd'hui : {events_label}")

        monthly_candidates = dol_result.metadata.get("monthly", [])
        monthly_bias = _resolve_timeframe_bias(monthly_candidates, context.market_data.price_usd)
        if monthly_candidates:
            log.append(
                f"DOL Monthly : {len(monthly_candidates)} candidat(s), "
                f"biais {'retenu (' + monthly_bias + ')' if monthly_bias else 'aucun (ni cassure, ni CRT clair)'}"
            )
        else:
            log.append("Aucune DOL Monthly active actuellement")

        # Zones Monthly récemment liquidées : purement informatif — ne
        # doit PAS être confondu avec "la cible du CRT Monthly a été
        # atteinte" (déjà géré par le filtre anti-obsolescence de
        # rules/crt_rule.py::_detect_crt_pattern via current_price).
        # Une zone KL générique balayée n'invalide pas en soi le biais
        # Monthly retenu ci-dessus.
        monthly_series = context.candles.get("monthly") if context.candles else None
        monthly_liquidated: list[dict[str, Any]] = []
        if monthly_series is not None:
            monthly_liquidated = _detect_recently_liquidated_zones(
                monthly_series.candles[:-1], keylevel_cfg
            )
            if monthly_liquidated:
                kinds = ", ".join(z["kind"] for z in monthly_liquidated)
                log.append(f"Zone(s) Monthly récemment liquidée(s) (contexte) : {kinds}")

        weekly_candidates = dol_result.metadata.get("weekly", [])
        weekly_bias = _resolve_timeframe_bias(weekly_candidates, context.market_data.price_usd)
        weekly_profiles: dict[str, float] = narrative_result.metadata.get("weekly_profiles", {})
        weekly_day_roles: list[dict[str, Any]] = narrative_result.metadata.get("day_roles", [])
        smt_context: dict[str, Any] | None = narrative_result.metadata.get("smt_context")
        if weekly_profiles:
            log.append(f"Narrative HTF potentielle : {weekly_profiles}")
        else:
            log.append("Aucune narrative HTF claire identifiée cette semaine")

        daily_candidates_for_bias = dol_result.metadata.get("daily", [])
        daily_bias = _resolve_timeframe_bias(daily_candidates_for_bias, context.market_data.price_usd)

        # --- Étape 3 : cascade de biais Monthly -> Weekly -> Daily ---
        # Priorité stricte : on ne descend d'une timeframe que si celle
        # du dessus n'offre RIEN de clair (ni cassure de biais, ni CRT
        # valide) — à chaque niveau descendu, la confiance baisse d'un
        # cran, conformément à la méthode décrite.
        aligned = bool(monthly_bias and weekly_bias and monthly_bias == weekly_bias)

        if monthly_bias:
            effective_bias = monthly_bias
            bias_source_timeframe = "monthly"
            confidence: Confidence = "high"
            if weekly_bias and not aligned:
                log.append(
                    f"Weekly ({weekly_bias}) en désaccord avec Monthly ({monthly_bias}) "
                    "-> Weekly ignoré, sauf ses KL déjà alignés avec Monthly"
                )
                confidence = "medium"
            elif aligned:
                log.append(f"Weekly aligné avec Monthly (biais {monthly_bias}) -> pris en compte")
        elif weekly_bias:
            effective_bias = weekly_bias
            bias_source_timeframe = "weekly"
            confidence = "medium"
            log.append(
                f"Monthly sans biais clair -> on descend à Weekly (biais {weekly_bias}), "
                "confiance réduite (medium)"
            )
        elif daily_bias:
            effective_bias = daily_bias
            bias_source_timeframe = "daily"
            confidence = "low"
            log.append(
                f"Monthly et Weekly sans biais clair -> on descend à Daily (biais {daily_bias}), "
                "confiance réduite (low)"
            )
        else:
            effective_bias = None
            bias_source_timeframe = None
            confidence = "low"
            log.append(
                "Aucun biais clair sur Monthly, Weekly ni Daily -> pas de scénario "
                "directionnel fort pour l'instant"
            )

        # --- Étape 4 : catalogue des KL majeurs HTF + KL liquidés (toutes timeframes) ---
        major_kls: dict[str, list[dict[str, Any]]] = {}
        raw_zones_by_timeframe: dict[str, list] = {}
        liquidated_kls: dict[str, list[dict[str, Any]]] = {}

        for timeframe in ("monthly", "weekly", "daily", "intraday"):
            series = context.candles.get(timeframe) if context.candles else None
            if series is None or len(series.candles) < 3:
                continue
            closed = series.candles[:-1]
            zones = _filter_unmitigated(find_key_levels(closed, keylevel_cfg), closed)
            raw_zones_by_timeframe[timeframe] = zones
            major_kls[timeframe] = [
                {
                    "kind": zone.kind,
                    "direction": zone.direction,
                    "zone_low": zone.zone_low,
                    "zone_high": zone.zone_high,
                }
                for zone in zones
            ]
            liquidated_kls[timeframe] = (
                monthly_liquidated
                if timeframe == "monthly"
                else _detect_recently_liquidated_zones(closed, keylevel_cfg)
            )

        log.append(
            "KL majeurs catalogués : "
            + ", ".join(f"{tf}={len(zones)}" for tf, zones in major_kls.items())
        )

        # --- KL Weekly filtrés dans le sens du biais effectif (quelle
        # que soit la timeframe qui l'a fourni via la cascade) :
        # uniquement les pools de liquidité que le marché devrait
        # consommer en allant vers la cible ---
        current_price = context.market_data.price_usd
        weekly_kls_in_monthly_direction: list[dict[str, Any]] = []
        narrative_validated_by_liquidation = False

        if effective_bias:
            weekly_zones = raw_zones_by_timeframe.get("weekly", [])
            filtered = _filter_kls_toward_target(weekly_zones, effective_bias, current_price)
            weekly_kls_in_monthly_direction = [
                {
                    "kind": zone.kind,
                    "direction": zone.direction,
                    "zone_low": zone.zone_low,
                    "zone_high": zone.zone_high,
                }
                for zone in filtered
            ]
            log.append(
                f"KL Weekly dans le sens du biais {bias_source_timeframe} ({effective_bias}) : "
                f"{len(weekly_kls_in_monthly_direction)} zone(s) à surveiller pour validation"
            )

            expected_liquidation_direction = "bullish" if effective_bias == "bearish" else "bearish"
            weekly_liquidated = liquidated_kls.get("weekly", [])
            relevant_liquidations = [
                z for z in weekly_liquidated if z["direction"] == expected_liquidation_direction
            ]
            if relevant_liquidations:
                narrative_validated_by_liquidation = True
                kinds = ", ".join(z["kind"] for z in relevant_liquidations)
                log.append(
                    f"Prise de liquidité Weekly sur {kinds}, dans le sens du biais "
                    f"({effective_bias}) -> scénario narratif validé"
                )
                if confidence == "medium":
                    confidence = "high"
                    log.append("Confiance renforcée suite à cette validation (medium -> high)")


        # --- Étape 7 : Purge Rule (High Probability ?) — calculée une
        # fois ; seule la paire Daily->intraday s'en sert comme filtre
        # (voir _build_timeframe_setup) ---
        purge_result = PurgeRule().evaluate(context, cfg)
        purge_assessments: list[dict[str, Any]] = purge_result.metadata.get("assessments", [])
        if purge_assessments:
            high_probability_found = any(a["high_probability"] for a in purge_assessments)
            log.append(
                "Évaluation de la purge : "
                + (
                    "HIGH probability trouvée"
                    if high_probability_found
                    else "aucune purge HP actuellement"
                )
            )
        else:
            log.append("Aucun CRT Daily/4H à évaluer pour la Purge Rule actuellement")

        # --- Étape 6 : Narrative LTF (indépendante des paires HTF/LTF) ---
        # Regroupe tout ce qui doit apparaître dans le même "onglet" que
        # la narrative dans la notification (méthodologie utilisateur) :
        # profils intraday, dealing range, KL LTF proches du prix,
        # double purge LTF, CRT LTF, cohérence OHLC.
        narrative_ltf_result = NarrativeLtfRule().evaluate(context, cfg)
        narrative_ltf_profiles: dict[str, float] = narrative_ltf_result.metadata.get(
            "intraday_profiles", {}
        )
        if narrative_ltf_profiles:
            log.append(f"Narrative LTF potentielle : {narrative_ltf_profiles}")

        dealing_range_ltf = narrative_ltf_result.metadata.get("dealing_range_ltf")
        double_purge_ltf = narrative_ltf_result.metadata.get("double_purge_ltf")
        keylevel_ltf_touched = narrative_ltf_result.metadata.get("keylevel_ltf_touched", [])
        ohlc_coherence = narrative_ltf_result.metadata.get("ohlc_coherence")

        # CRT LTF : uniquement s'il est dans le sens de la DOL HTF
        # (biais effectif) — un CRT LTF opposé n'est qu'un bruit de
        # marché à cette granularité, pas une confirmation utile.
        raw_crt_ltf = narrative_ltf_result.metadata.get("crt_ltf")
        crt_ltf = (
            raw_crt_ltf
            if raw_crt_ltf is not None
            and effective_bias is not None
            and raw_crt_ltf.get("bias") == effective_bias
            else None
        )
        if raw_crt_ltf is not None and crt_ltf is None:
            log.append(
                f"CRT LTF détecté (biais {raw_crt_ltf.get('bias')}) mais pas dans le sens "
                f"de la DOL HTF ({effective_bias}) -> ignoré"
            )

        # Prise de liquidité intraday dans le sens du biais effectif ->
        # validation possible du scénario narratif LTF (même logique
        # que pour le Weekly côté HTF).
        if effective_bias is not None:
            expected_ltf_direction = "bullish" if effective_bias == "bearish" else "bearish"
            intraday_liquidated = liquidated_kls.get("intraday", [])
            relevant_ltf_liquidations = [
                z for z in intraday_liquidated if z["direction"] == expected_ltf_direction
            ]
            if relevant_ltf_liquidations:
                narrative_validated_by_liquidation = True
                kinds = ", ".join(z["kind"] for z in relevant_ltf_liquidations)
                log.append(
                    f"Prise de liquidité LTF sur {kinds}, dans le sens du biais "
                    f"({effective_bias}) -> scénario narratif LTF validé"
                )

        # --- CRT indépendants sur Monthly/Weekly/Daily/4H : chaque CRT
        # valide et aligné avec le biais effectif est un setup à part
        # entière (méthodologie utilisateur) — plus de paire HTF/LTF
        # fixe. La référence DOL "HTF" pour le TP3 s'élargit à chaque
        # timeframe descendue (Monthly n'a pas de référence au-dessus).
        htf_dol_reference_by_timeframe: dict[str, list[dict[str, Any]]] = {
            "monthly": [],
            "weekly": monthly_candidates,
            "daily": monthly_candidates + weekly_candidates,
            "4h": monthly_candidates + weekly_candidates + daily_candidates_for_bias,
        }
        timeframe_setups: list[TimeframeSetup] = []
        for timeframe in CRT_SCAN_TIMEFRAMES:
            setup = _build_crt_setup(
                timeframe,
                context,
                htf_dol_reference_by_timeframe[timeframe],
                effective_bias,
                purge_assessments,
                log,
            )
            if setup is not None:
                timeframe_setups.append(setup)

        if news_risk_active:
            events_label = ", ".join(high_impact_news.get("high_impact_today", []))
            for setup in timeframe_setups:
                if setup.entry_recommended:
                    setup.notes.append(
                        f"⚠️ Fenêtre à risque active pour une news à haut impact ({events_label}) "
                        "— volatilité accrue possible, entrée quand même notifiée"
                    )

        entry_recommended = any(setup.entry_recommended for setup in timeframe_setups)

        # Une fois un CRT confirmé (entrée recommandée sur au moins une
        # paire HTF/LTF), on va chercher un point d'entrée plus précis
        # via EntryRule (modèles 1-4, distribution) et TsqRule (CISD,
        # OTE, Continuation, KOD) — toutes deux opèrent sur l'intraday,
        # indépendamment de la paire HTF/LTF à l'origine du signal.
        refined_entries: list[RefinedEntry] = []
        priority_entry: RefinedEntry | None = None
        if entry_recommended:
            entry_result = EntryRule().evaluate(context, cfg)
            tsq_result = TsqRule().evaluate(context, cfg)
            all_refined_entries = collect_refined_entries(entry_result.metadata, tsq_result.metadata)

            # Ne garder que les entrées précises dont la direction
            # concorde avec le biais effectif du trade recommandé : un
            # signal LTF (Turtle Soup / TSQ) peut techniquement exister
            # dans le sens OPPOSÉ au CRT HTF (ex: un Turtle Soup bullish
            # intraday pendant qu'un CRT weekly recommande un short) —
            # EntryRule/TsqRule ne connaissent pas le biais HTF, elles
            # analysent l'intraday tel quel. Afficher une entrée en
            # contradiction avec le sens du trade serait incohérent et
            # pourrait induire en erreur.
            refined_entries = [e for e in all_refined_entries if e.direction == effective_bias]
            discarded_count = len(all_refined_entries) - len(refined_entries)
            if discarded_count:
                log.append(
                    f"{discarded_count} entrée(s) précise(s) écartée(s) car en contradiction "
                    f"avec le biais du trade ({effective_bias})"
                )

            # Une seule entrée est retenue au final : la première selon
            # l'ordre méthodologique (Distribution -> Modèle 1 -> 2 -> 3
            # -> 4 -> TSQ). Pas la peine de notifier toutes les
            # entrées trouvées, seulement la plus prioritaire.
            priority_entry = select_priority_entry(refined_entries)
            if priority_entry is not None:
                log.append(f"Entrée précise retenue (priorité) : {priority_entry.source}")

            if refined_entries:
                sources_label = ", ".join(e.source for e in refined_entries)
                log.append(f"Entrée(s) précise(s) trouvée(s) : {sources_label}")
            else:
                log.append(
                    "Entrée recommandée, mais aucun point d'entrée précis "
                    "(modèle CRT ou TSQ) n'a encore confirmé"
                )

        return DecisionReport(
            reasoning_log=log,
            monthly_bias=monthly_bias,
            monthly_dol_candidates=monthly_candidates,
            monthly_liquidated_zones=monthly_liquidated,
            weekly_bias=weekly_bias,
            daily_bias=daily_bias,
            bias_source_timeframe=bias_source_timeframe,
            weekly_dol_candidates=weekly_candidates,
            daily_dol_candidates=daily_candidates_for_bias,
            weekly_narrative_profiles=weekly_profiles,
            weekly_day_roles=weekly_day_roles,
            smt_context=smt_context,
            monthly_weekly_aligned=aligned,
            effective_bias=effective_bias,
            confidence=confidence,
            major_kls=major_kls,
            weekly_kls_in_monthly_direction=weekly_kls_in_monthly_direction,
            liquidated_kls=liquidated_kls,
            narrative_validated_by_liquidation=narrative_validated_by_liquidation,
            timeframe_setups=timeframe_setups,
            narrative_ltf_profiles=narrative_ltf_profiles,
            dealing_range_ltf=dealing_range_ltf,
            crt_ltf=crt_ltf,
            double_purge_ltf=double_purge_ltf,
            keylevel_ltf_touched=keylevel_ltf_touched,
            ohlc_coherence=ohlc_coherence,
            economic_calendar=narrative_result.metadata.get("economic_calendar", {}),
            high_impact_news=high_impact_news,
            quiet_day=narrative_result.metadata.get("quiet_day", {}),
            holiday=narrative_result.metadata.get("holiday", {}),
            purge_assessments=purge_assessments,
            refined_entries=refined_entries,
            priority_entry=priority_entry,
            entry_recommended=entry_recommended,
        )
