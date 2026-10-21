"""
models/macro_data.py

Modèle de données pour le calendrier macro-économique (CPI, Nonfarm
Payrolls, etc.) via collectors/macro.py.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Any

from pydantic import BaseModel, Field


class MacroEvent(BaseModel):
    """
    Un jour du calendrier macro-économique, avec les événements prévus
    ce jour-là (ex: "CPI", "Nonfarm Payrolls"). Ne porte aucune analyse
    d'impact — juste le fait brut qu'un événement est prévu à cette
    date.
    """

    date: date_type
    events: list[str] = Field(default_factory=list)

    @classmethod
    def from_collector_dict(cls, data: dict[str, Any]) -> "MacroEvent":
        """Construit un MacroEvent à partir du dict brut de collectors/macro.py::get_macro_events()."""
        return cls(date=data["date"], events=data.get("events", []))