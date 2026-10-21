"""
execution/stop_order_registry.py

Registre PERSISTANT (fichier, même principe que execution/kill_switch.py)
de l'ordre stop-loss actif par utilisateur et par symbole — remplace
les dict en mémoire (`_active_stop_txid`/`_active_stop_order_id`) qui
existaient précédemment dans chaque client d'exchange, perdus si le
processus redémarrait entre une entrée et un déplacement au Breakeven.

Un fichier par utilisateur (execution_state/stop_orders/{user_id}.json),
contenant {"BTC": "12345", "ETH": "67890", ...} — un ordre stop actif
par symbole ouvert pour cet utilisateur.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STATE_DIR = Path(__file__).parent.parent / "execution_state" / "stop_orders"


def _state_path(user_id: str) -> Path:
    safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{safe_user_id}.json"


def _read_all(user_id: str) -> dict[str, str]:
    path = _state_path(user_id)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        # Fichier corrompu -> traité comme vide plutôt que de faire
        # planter tout le cycle sur un souci de persistance annexe.
        return {}


def save_stop_order_id(user_id: str, symbol: str, order_id: str) -> None:
    """À appeler juste après avoir placé (ou remplacé) l'ordre stop-loss d'une position, pour ce symbole."""
    data = _read_all(user_id)
    data[symbol] = order_id
    with open(_state_path(user_id), "w") as f:
        json.dump(data, f, indent=2)


def get_stop_order_id(user_id: str, symbol: str) -> str | None:
    """
    Renvoie l'identifiant de l'ordre stop actif pour cet utilisateur et
    ce symbole, ou None si aucun n'est connu (jamais créé, ou processus
    précédent redémarré avant d'avoir persisté — voir move_stop_loss()
    dans chaque client concret, qui gère ce cas proprement plutôt que
    de planter).
    """
    return _read_all(user_id).get(symbol)


def clear_stop_order_id(user_id: str, symbol: str) -> None:
    """À appeler dès que la position correspondante est fermée (plus besoin de suivre son stop)."""
    data = _read_all(user_id)
    if symbol in data:
        del data[symbol]
        with open(_state_path(user_id), "w") as f:
            json.dump(data, f, indent=2)
