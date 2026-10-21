"""
models/alert.py

Représente une alerte finalisée : la version distillée et publique d'un
TimeframeSetup (issu de rules.decision_engine.DecisionReport), prête à
être stockée, affichée, ou exposée par une future API.

Pourquoi un modèle séparé plutôt que de réutiliser TimeframeSetup
directement : TimeframeSetup porte des détails de raisonnement interne
(notes, chevauchements de règles) qui n'ont pas vocation à être exposés
tels quels à ce qui consomme une alerte (Telegram, la base de données,
un futur tableau de bord). Ce module fait office de "façade" publique.

Note : ce modèle n'est pas encore câblé dans database/db.py ni main.py
(qui utilisent directement TimeframeSetup) — c'est un utilitaire
disponible pour un futur refactor, introduit sans modifier le
comportement déjà testé du pipeline actuel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from rules.decision_engine import TimeframeSetup

Bias = Literal["bullish", "bearish"]
Confidence = Literal["high", "medium", "low"]


class Alert(BaseModel):
    """
    Représentation publique d'une alerte de trading.

    Attributes:
        htf_timeframe: Timeframe haute (ex: "daily", "weekly").
        ltf_timeframe: Timeframe basse associée (ex: "intraday").
        bias: Biais directionnel ("bullish" ou "bearish").
        dol_price: Prix de la DOL (Draw On Liquidity) du CRT à l'origine.
        tp_50: Take-profit à 50% du CRT, si calculé.
        tp_100: Take-profit à 100% du CRT (= DOL), si calculé.
        tp_htf_dol: Take-profit sur une DOL HTF plus large, si compatible.
        confidence: Niveau de confiance global ("high"/"medium"/"low").
        message: Message final (déjà enrichi par l'IA et formaté).
        created_at: Horodatage UTC de création de l'alerte.
        sent_to_telegram: Statut d'envoi.
    """

    htf_timeframe: str
    ltf_timeframe: str
    bias: Bias
    dol_price: float
    tp_50: float | None = None
    tp_100: float | None = None
    tp_htf_dol: float | None = None
    confidence: Confidence
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sent_to_telegram: bool = False

    @classmethod
    def from_timeframe_setup(
        cls, setup: "TimeframeSetup", confidence: Confidence, message: str
    ) -> "Alert":
        """
        Construit une Alert à partir d'un TimeframeSetup dont
        `entry_recommended` est True.

        Raises:
            ValueError: Si `setup.htf_crt` est None (le setup ne porte
                pas de CRT confirmé — ne devrait pas arriver pour un
                setup dont entry_recommended=True, mais vérifié
                explicitement plutôt que de lever un KeyError confus).
        """
        if setup.htf_crt is None:
            raise ValueError(
                "Impossible de construire une Alert : setup.htf_crt est None "
                f"(paire {setup.htf_timeframe}->{setup.ltf_timeframe})"
            )

        targets = setup.crt_targets or {}

        return cls(
            htf_timeframe=setup.htf_timeframe,
            ltf_timeframe=setup.ltf_timeframe,
            bias=setup.htf_crt["bias"],
            dol_price=setup.htf_crt["dol_price"],
            tp_50=targets.get("tp_50"),
            tp_100=targets.get("tp_100"),
            tp_htf_dol=targets.get("tp_htf_dol"),
            confidence=confidence,
            message=message,
        )

    def dedup_key(self) -> str:
        """
        Même logique de clé que database/db.py::_build_dedup_key,
        dupliquée ici volontairement pour que ce modèle reste autonome
        (ne dépend pas de database/db.py). À terme, si ce modèle est
        câblé dans db.py, une seule implémentation devra faire
        autorité.
        """
        return "|".join(
            [self.htf_timeframe, self.ltf_timeframe, self.bias, str(self.dol_price)]
        )