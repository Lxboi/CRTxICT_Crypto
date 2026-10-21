"""
tests/conftest.py

Fixtures et helpers partagés par tous les tests. Centralise notamment
la construction de bougies (Candle) et la gestion de l'heure NY, pour
éviter de dupliquer ces utilitaires dans chaque fichier de test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from models.market_data import Candle

NY_TZ = ZoneInfo("America/New_York")


def ny_datetime(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Construit un datetime en heure NY, converti en UTC (comme les Candle le stockent)."""
    return datetime(year, month, day, hour, minute, tzinfo=NY_TZ).astimezone(timezone.utc)


def make_candle(
    open_: float, high: float, low: float, close: float, timestamp: datetime
) -> Candle:
    """Raccourci pour construire une Candle sans répéter les noms de champs."""
    return Candle(timestamp=timestamp, open=open_, high=high, low=low, close=close)


@pytest.fixture
def ny_dt():
    """Expose ny_datetime() comme fixture, pour l'injecter dans les tests qui le préfèrent."""
    return ny_datetime


@pytest.fixture
def candle():
    """Expose make_candle() comme fixture."""
    return make_candle
