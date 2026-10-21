"""
execution/kill_switch.py

Interrupteur marche/arrêt de l'automatisation des prises de position —
la pièce la plus critique de tout le module execution/.

PAR UTILISATEUR : chaque utilisateur (vous inclus, sous l'identifiant
"_owner") a son propre état, dans un fichier séparé — désactiver un
utilisateur problématique n'affecte jamais les autres. Sans quoi, avec
plusieurs utilisateurs connectés, un seul kill-switch global forcerait
à tout couper pour désactiver un seul compte à problème.

Principes de sécurité non négociables :
  1. L'état est PERSISTANT (fichier sur disque), jamais un simple
     booléen en mémoire — un redémarrage du robot (crash, coupure,
     mise à jour) ne doit JAMAIS réactiver l'exécution automatique par
     défaut. Le défaut est TOUJOURS désactivé (fail-safe).
  2. Activer nécessite une action explicite (commande CLI dédiée),
     jamais une conséquence indirecte d'une autre action.
  3. Désactiver doit être instantané et infaillible — même si le
     fichier d'état est corrompu ou illisible, le kill-switch doit être
     considéré comme DÉSACTIVÉ (toute ambiguïté = arrêt), jamais activé
     par défaut en cas de doute.

Usage :
    python manage_execution.py --enable                    # active pour vous (_owner)
    python manage_execution.py --disable                   # désactive pour vous
    python manage_execution.py --status                    # votre état
    python manage_execution.py --enable --user alice        # active pour l'utilisateur alice
    python manage_execution.py --disable --user alice       # désactive pour alice uniquement
    python manage_execution.py --status --user alice        # état d'alice
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).parent.parent / "execution_state"
OWNER_USER_ID = "_owner"


def _state_path(user_id: str) -> Path:
    """
    Un fichier d'état par utilisateur — user_id assaini (alphanumériques
    uniquement) pour éviter tout risque de traversée de chemin, même si
    user_id provient d'une saisie utilisateur ailleurs dans le projet.
    """
    safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{safe_user_id}.json"


def is_enabled(user_id: str = OWNER_USER_ID) -> bool:
    """
    Indique si l'exécution automatique est activée POUR CET UTILISATEUR.
    Fail-safe strict : toute erreur de lecture, fichier absent, ou
    contenu invalide est traité comme DÉSACTIVÉ — jamais l'inverse.
    """
    path = _state_path(user_id)
    if not path.exists():
        return False

    try:
        with open(path) as f:
            state = json.load(f)
        return bool(state.get("enabled", False))
    except (json.JSONDecodeError, OSError, ValueError):
        # Fichier corrompu ou illisible -> on considère l'exécution
        # désactivée, jamais l'inverse (ambiguïté = arrêt).
        return False


def get_state(user_id: str = OWNER_USER_ID) -> dict[str, Any]:
    """Renvoie l'état complet pour cet utilisateur, pour affichage --status."""
    path = _state_path(user_id)
    if not path.exists():
        return {"enabled": False, "changed_at": None, "reason": "Aucun état enregistré (jamais activé)."}

    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {"enabled": False, "changed_at": None, "reason": "Fichier d'état corrompu -> traité comme désactivé."}


def list_enabled_users() -> list[str]:
    """
    Liste tous les user_id (y compris _owner) actuellement activés —
    utile pour la boucle d'exécution multi-utilisateurs (voir
    execution/engine.py), qui ne doit agir que pour les utilisateurs
    réellement activés, jamais tout le monde par défaut.
    """
    if not STATE_DIR.exists():
        return []

    enabled = []
    for state_file in STATE_DIR.glob("*.json"):
        user_id = state_file.stem
        if is_enabled(user_id):
            enabled.append(user_id)
    return enabled


def enable(user_id: str = OWNER_USER_ID) -> None:
    """
    Active l'exécution automatique POUR CET UTILISATEUR uniquement.
    À n'appeler que depuis manage_execution.py, après confirmation
    explicite — jamais automatiquement par le robot lui-même.
    """
    _write_state(user_id, enabled=True, reason=f"Activé manuellement via manage_execution.py --enable --user {user_id}")


def disable(user_id: str = OWNER_USER_ID, reason: str | None = None) -> None:
    """
    Désactive l'exécution automatique pour CET UTILISATEUR immédiatement
    — n'affecte jamais les autres utilisateurs. Peut être appelée depuis
    n'importe où (CLI, ou automatiquement par le robot en cas d'anomalie
    détectée pour ce compte précis).
    """
    _write_state(user_id, enabled=False,
                 reason=reason or f"Désactivé manuellement via manage_execution.py --disable --user {user_id}")


def _write_state(user_id: str, enabled: bool, reason: str) -> None:
    state = {
        "user_id": user_id,
        "enabled": enabled,
        "changed_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    with open(_state_path(user_id), "w") as f:
        json.dump(state, f, indent=2)

