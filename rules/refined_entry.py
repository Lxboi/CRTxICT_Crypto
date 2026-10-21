"""
rules/refined_entry.py

Consolide les signaux d'entrée précis produits par EntryRule (modèles
1-4, entrée distribution) et TsqRule (CISD, OTE, Continuation, KOD) en
une liste unique de RefinedEntry — chacune explicitement étiquetée par
sa source, pour répondre à "donne-moi l'entrée, le TP, le SL, et si
c'est un modèle 1/2/3/4 ou une condition du TSQ".

Ne prend aucune décision et ne calcule rien de nouveau : se contente
d'agréger ce que EntryRule.evaluate() et TsqRule.evaluate() ont déjà
produit dans leurs metadata respectifs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Bias = Literal["bullish", "bearish"]


@dataclass
class RefinedEntry:
    """
    Un point d'entrée précis, quelle que soit sa source (modèle CRT
    classique ou étape TSQ), avec son SL et ses TP disponibles.

    Attributes:
        source: Étiquette lisible de la source exacte du signal — ex:
            "Modèle 1 (Order Block)", "Distribution (bougie 3)",
            "TSQ - CISD", "TSQ - OTE", "TSQ - Continuation", "TSQ - KOD".
        direction: "bullish" (long) ou "bearish" (short).
        entry_price: Prix d'entrée proposé.
        stop_loss: Niveau de stop associé à cette source précise (voir
            rules/entry_rule.py et rules/tsq_rule.py pour le détail de
            calcul par source).
        take_profits: Cibles disponibles pour cette entrée — peut être
            vide si aucune cible n'a pu être calculée pour cette source
            (ex: les modèles 1-4 n'ont pas de TP propre à ce stade,
            seul le TSQ calcule un swing opposé + DOL CRT).
    """

    source: str
    direction: Bias
    entry_price: float
    stop_loss: float
    take_profits: list[float] = field(default_factory=list)


def collect_refined_entries(
    entry_metadata: dict[str, Any], tsq_metadata: dict[str, Any]
) -> list[RefinedEntry]:
    """
    Construit la liste consolidée de RefinedEntry à partir des metadata
    de EntryRule.evaluate() et TsqRule.evaluate().

    Args:
        entry_metadata: `RuleResult.metadata` renvoyé par
            rules.entry_rule.EntryRule.evaluate().
        tsq_metadata: `RuleResult.metadata` renvoyé par
            rules.tsq_rule.TsqRule.evaluate().

    Returns:
        Liste de RefinedEntry, dans l'ordre : entrée distribution (si
        présente), modèles 1 à 4 (si présents), puis étapes TSQ dans
        l'ordre de la séquence (CISD, OTE, Continuation, KOD).
    """
    entries: list[RefinedEntry] = []

    distribution = entry_metadata.get("distribution_entry")
    if distribution is not None:
        timing_label = "anticipée" if distribution["predicted"] else "confirmée"
        entries.append(
            RefinedEntry(
                source=f"Distribution (bougie 3, {timing_label})",
                direction=distribution["direction"],
                entry_price=distribution["entry_price"],
                stop_loss=distribution["stop_loss"],
            )
        )

    for model in entry_metadata.get("entry_models", []):
        entries.append(
            RefinedEntry(
                source=f"Modèle {model['model_number']} ({model['model_name']})",
                direction=model["direction"],
                entry_price=model["entry_price"],
                stop_loss=model["stop_loss"],
            )
        )

    tsq_targets = tsq_metadata.get("targets", {})
    tsq_take_profits = [
        target
        for target in (tsq_targets.get("opposite_swing"), tsq_targets.get("crt_dol"))
        if target is not None
    ]

    cisd = tsq_metadata.get("cisd")
    if cisd is not None:
        entries.append(
            RefinedEntry(
                source="TSQ - CISD",
                direction=cisd["direction"],
                entry_price=cisd["entry_price"],
                stop_loss=cisd["stop_loss"],
                take_profits=list(tsq_take_profits),
            )
        )

    ote_zone = tsq_metadata.get("ote_zone")
    if ote_zone is not None and ote_zone.get("touched") and cisd is not None:
        # Le signal OTE n'est un point d'entrée exploitable qu'une fois
        # la zone effectivement touchée — sinon ce n'est qu'une cible
        # calculée pour un retracement qui n'a pas encore eu lieu.
        # La direction est celle du CISD (l'OTE ne se calcule que si un
        # CISD a été confirmé au préalable — voir rules/tsq_rule.py).
        entries.append(
            RefinedEntry(
                source="TSQ - OTE",
                direction=cisd["direction"],
                entry_price=ote_zone["entry_price"],
                stop_loss=ote_zone["stop_loss"],
                take_profits=list(tsq_take_profits),
            )
        )

    continuation = tsq_metadata.get("continuation")
    if continuation is not None:
        entries.append(
            RefinedEntry(
                source="TSQ - Continuation",
                direction=continuation["direction"],
                entry_price=continuation["entry_price"],
                stop_loss=continuation["stop_loss"],
                take_profits=list(tsq_take_profits),
            )
        )

    kod = tsq_metadata.get("kod")
    if kod is not None:
        entries.append(
            RefinedEntry(
                source="TSQ - KOD (Kiss of Death)",
                direction=kod["direction"],
                entry_price=kod["entry_price"],
                stop_loss=kod["stop_loss"],
                take_profits=list(tsq_take_profits),
            )
        )

    return entries


# Ordre de priorité méthodologique : Distribution (bougie 3, l'entrée
# la plus sûre) -> Modèle 1 -> 2 -> 3 (confirmation structurelle,
# croissant en agressivité) -> Modèle 4 (agressif, sans confirmation,
# dès qu'un Turtle Soup existe) -> étapes TSQ, dans leur ordre naturel
# de séquence (CISD -> OTE -> Continuation -> KOD). Un nombre plus petit
# = priorité plus haute.
_ENTRY_PRIORITY_PREFIXES: list[str] = [
    "Distribution",
    "Modèle 1",
    "Modèle 2",
    "Modèle 3",
    "Modèle 4",
    "TSQ - CISD",
    "TSQ - OTE",
    "TSQ - Continuation",
    "TSQ - KOD",
]


def _entry_priority_rank(source: str) -> int:
    """Rang de priorité d'une RefinedEntry (plus petit = prioritaire) selon _ENTRY_PRIORITY_PREFIXES."""
    for rank, prefix in enumerate(_ENTRY_PRIORITY_PREFIXES):
        if source.startswith(prefix):
            return rank
    return len(_ENTRY_PRIORITY_PREFIXES)  # source inconnue -> priorité la plus basse


def select_priority_entry(entries: list[RefinedEntry]) -> RefinedEntry | None:
    """
    Sélectionne UNE SEULE entrée parmi celles trouvées : la première
    selon l'ordre de priorité méthodologique (voir
    _ENTRY_PRIORITY_PREFIXES) — pas une liste de toutes les entrées
    disponibles. Conforme à la consigne : "le robot devra m'indiquer le
    premier modèle d'entrée qu'il trouve dans l'ordre : modèle 1-2-3
    puis TS puis TSQ".

    Args:
        entries: Liste de RefinedEntry (déjà filtrée par direction, voir
            rules/decision_engine.py).

    Returns:
        La RefinedEntry de plus haute priorité, ou None si la liste est vide.
    """
    if not entries:
        return None
    return min(entries, key=lambda e: _entry_priority_rank(e.source))
