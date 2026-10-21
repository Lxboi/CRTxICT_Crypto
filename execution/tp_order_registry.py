"""
execution/tp_order_registry.py

Registre PERSISTANT (fichier, même principe que execution/kill_switch.py
et execution/stop_order_registry.py) des ordres TAKE-PROFIT actifs par
utilisateur et par symbole — une LISTE d'identifiants, contrairement au
stop-loss qui n'en a qu'un seul à la fois (plusieurs TP peuvent être
actifs simultanément sur la même position).

Un fichier par utilisateur (execution_state/tp_orders/{user_id}.json),
contenant {"BTC": ["order-id-1", "order-id-2"], ...}.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STATE_DIR = Path(__file__).parent.parent / "execution_state" / "tp_orders"


def _state_path(user_id: str) -> Path:
    safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{safe_user_id}.json"


def _read_all(user_id: str) -> dict[str, list[str]]:
    path = _state_path(user_id)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def save_tp_order_ids(user_id: str, symbol: str, order_ids: list[str]) -> None:
    """À appeler juste après avoir placé tous les ordres TP d'une nouvelle entrée, pour ce symbole."""
    data = _read_all(user_id)
    data[symbol] = order_ids
    with open(_state_path(user_id), "w") as f:
        json.dump(data, f, indent=2)


def get_tp_order_ids(user_id: str, symbol: str) -> list[str]:
    """Renvoie la liste des identifiants d'ordres TP actifs pour cet utilisateur et ce symbole (vide si aucun connu)."""
    return _read_all(user_id).get(symbol, [])


def clear_tp_order_ids(user_id: str, symbol: str) -> None:
    """À appeler dès que la position correspondante est entièrement clôturée (plus besoin de suivre ses TP)."""
    data = _read_all(user_id)
    if symbol in data:
        del data[symbol]
        with open(_state_path(user_id), "w") as f:
            json.dump(data, f, indent=2)
