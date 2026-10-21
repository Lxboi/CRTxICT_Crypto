"""
collectors/macro.py

Collecte du calendrier des événements macro-économiques (CPI, Nonfarm
Payrolls, etc.) via SoSoValue. Nécessite SOSOVALUE_API_KEY dans .env —
inscription sur https://sosovalue.com/developer/dashboard
"""

from __future__ import annotations

import requests

from config.settings import get_settings

SOSOVALUE_BASE_URL = "https://openapi.sosovalue.com/openapi/v1"


# ---------------------------------------------------------------------
# Calendrier macro (SoSoValue)
# ---------------------------------------------------------------------


def _get_sosovalue_headers() -> dict[str, str]:
    """
    Construit les en-têtes d'authentification SoSoValue (SOSOVALUE_API_KEY).

    Raises:
        ValueError: Si SOSOVALUE_API_KEY est absente de l'environnement.
    """
    api_key = get_settings().sosovalue_api_key
    if not api_key:
        raise ValueError(
            "SOSOVALUE_API_KEY absente de l'environnement -> impossible "
            "d'appeler l'API SoSoValue"
        )
    return {"x-soso-api-key": api_key}


def _unwrap_sosovalue_response(payload: dict | list) -> list:
    """
    L'API SoSoValue enveloppe ses réponses sous la forme
    {"code": 0, "message": "success", "data": [...]} plutôt que de
    renvoyer la liste directement. Cette fonction extrait `data` quand
    l'enveloppe est présente, et renvoie `payload` tel quel sinon (au
    cas où un endpoint ne l'utiliserait pas).
    """
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def get_macro_events() -> list[dict]:
    """
    Récupère le calendrier des événements macro-économiques
    (CPI, Nonfarm Payrolls, décisions de taux, etc.).

    Returns:
        Liste de dicts {"date": "YYYY-MM-DD", "events": [nom, ...]}.

    Raises:
        ValueError: Si SOSOVALUE_API_KEY est absente.
        requests.HTTPError: Si l'appel échoue.
    """
    url = f"{SOSOVALUE_BASE_URL}/macro/events"

    response = requests.get(url, headers=_get_sosovalue_headers(), timeout=10)
    response.raise_for_status()

    return _unwrap_sosovalue_response(response.json())