"""
execution/api_health.py

Suit si VOTRE clé API Kraken est actuellement considérée comme "cassée"
(échec d'authentification détecté) — état PERSISTANT (fichier, même
principe que execution/kill_switch.py), pour ne notifier qu'UNE SEULE
FOIS par panne, pas à chaque cycle tant que le problème n'est pas résolu
(le robot tourne toutes les quelques minutes ; sans ce mécanisme, ce
serait une notification toutes les 5 minutes en continu).

Déplacé depuis accounts/api_health.py (simplification architecture, août
2026, retrait du multi-utilisateur) — contenu inchangé, toujours
indexé par user_id (aujourd'hui toujours execution.kill_switch.OWNER_USER_ID,
"_owner") pour rester cohérent avec le reste de execution/ (kill_switch,
stop_order_registry, tp_order_registry, déjà tous indexés ainsi).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STATE_DIR = Path(__file__).parent.parent / "execution_state" / "api_health"

# Motifs d'erreur indiquant un problème d'AUTHENTIFICATION (clé
# révoquée/invalide/permissions retirées), par opposition à un souci
# transitoire (réseau, timeout, rate-limit) qui ne doit PAS déclencher
# cette notification -- mêmes motifs que check_kraken_connection.py.
AUTH_FAILURE_PATTERNS = ("Invalid key", "Permission denied", "Invalid signature", "EAPI:", "EGeneral:")


def is_auth_failure(error_message: str | None) -> bool:
    """Détecte si un message d'erreur correspond à un problème d'authentification, pas un souci transitoire."""
    if not error_message:
        return False
    return any(pattern in error_message for pattern in AUTH_FAILURE_PATTERNS)


def _state_path(user_id: str) -> Path:
    safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{safe_user_id}.json"


def is_flagged_broken(user_id: str) -> bool:
    """Indique si on a déjà notifié ce problème (donc à ne pas renotifier)."""
    path = _state_path(user_id)
    if not path.exists():
        return False
    try:
        with open(path) as f:
            return bool(json.load(f).get("broken", False))
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def flag_broken(user_id: str) -> None:
    """Marque un échec d'authentification en cours -- à appeler avant de notifier."""
    with open(_state_path(user_id), "w") as f:
        json.dump({"broken": True}, f)


def clear_broken_flag(user_id: str) -> None:
    """
    Efface le marqueur -- à appeler dès qu'un appel réussit à nouveau,
    pour permettre une nouvelle notification si le problème revient
    plus tard (et notifier optionnellement que c'est résolu).
    """
    path = _state_path(user_id)
    if path.exists():
        path.unlink()
