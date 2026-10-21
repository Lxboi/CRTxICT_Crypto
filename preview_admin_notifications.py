"""
preview_admin_notifications.py

Script de prévisualisation des notifications D'ADMINISTRATION (pas de
trading — voir preview_notifications.py pour celles-ci) : clé API
cassée/rétablie (voir execution/api_health.py, execution/admin_notifications.py).

Simplification architecture (août 2026) : les scénarios liés à la
gestion de comptes tiers et à la facturation (ajout d'utilisateur,
changement de statut de compte tiers, rapport mensuel de frais) ont été
retirés avec accounts/ -- il ne reste que le scénario clé API
cassée/rétablie, pertinent pour VOTRE compte.

IMPORTANT : ce fichier n'a JAMAIS de préfixe "test_" et n'est jamais
importé automatiquement — il ne se lance que manuellement, via :

    python preview_admin_notifications.py            # aperçu console uniquement (aucun envoi Telegram)
    python preview_admin_notifications.py --send      # envoie réellement chaque message sur Telegram

Toute la logique est protégée par `if __name__ == "__main__":`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

TEST_BANNER = "🧪🧪🧪 CECI EST UN TEST — PAS UNE VRAIE NOTIFICATION 🧪🧪🧪"


def run_preview(actually_send: bool) -> None:
    from execution import kill_switch
    from execution.api_health import _state_path as _api_health_state_path
    from execution.engine import try_execute_entry_for_all_users
    from execution.exchange_client import ExchangeClient, OrderResult

    # État api_health de l'owner nettoyé d'abord -- sans ça, un
    # lancement précédent pourrait laisser croire qu'une panne est déjà
    # signalée, faussant le scénario.
    _owner_health_path = _api_health_state_path(kill_switch.OWNER_USER_ID)
    if _owner_health_path.exists():
        _owner_health_path.unlink()

    captured_messages: list[str] = []

    def fake_send(message: str) -> bool:
        wrapped = f"{TEST_BANNER}\n\n{message}\n\n{TEST_BANNER}"
        captured_messages.append(wrapped)
        if actually_send:
            from notifications.telegram_sender import send_telegram_message

            return send_telegram_message(wrapped)
        return True

    # Patch sur execution.admin_notifications directement (pas
    # notifications.telegram_sender) -- robuste indépendamment de
    # l'ordre d'import, contrairement à patcher le module source.
    with patch("execution.admin_notifications.send_telegram_message", side_effect=fake_send):

        class BrokenExchange(ExchangeClient):
            def place_market_order(self, user_id, symbol, side, quantity, stop_loss, leverage, take_profit_levels=None): pass
            def move_stop_loss(self, user_id, symbol, new_stop_loss): pass
            def close_position(self, user_id, symbol, fraction=1.0): pass
            def get_open_positions(self): return []
            def get_account_balance(self):
                raise ValueError("Erreur API Kraken sur Balance : ['EAPI:Invalid key']")
            def ensure_take_profit_orders(self, user_id, symbol, side, quantity, take_profit_levels):
                return OrderResult(False, None, None, None, error_message="clé invalide")

        class FixedExchange(ExchangeClient):
            def place_market_order(self, user_id, symbol, side, quantity, stop_loss, leverage, take_profit_levels=None):
                return OrderResult(True, "1", 90000.0, quantity)
            def move_stop_loss(self, user_id, symbol, new_stop_loss): pass
            def close_position(self, user_id, symbol, fraction=1.0): pass
            def get_open_positions(self): return []
            def get_account_balance(self): return 1000.0
            def ensure_take_profit_orders(self, user_id, symbol, side, quantity, take_profit_levels):
                return OrderResult(True, None, None, quantity)

        kill_switch.enable(kill_switch.OWNER_USER_ID)
        exchange_leverage_config = {"kraken": 5.0}
        try:
            try_execute_entry_for_all_users(
                "BTC", "bullish", 90000.0, 88200.0, 1.0, exchange_leverage_config, BrokenExchange(), "kraken"
            )  # --- 1a. Panne détectée et notifiée ---
            try_execute_entry_for_all_users(
                "BTC", "bullish", 90000.0, 88200.0, 1.0, exchange_leverage_config, FixedExchange(), "kraken"
            )  # --- 1b. Rétablissement détecté et notifié ---
        finally:
            kill_switch.disable(kill_switch.OWNER_USER_ID)

    labels = [
        "1a. CLÉ API NON FONCTIONNELLE (détection automatique)",
        "1b. CLÉ API DE NOUVEAU FONCTIONNELLE (rétablissement)",
    ]
    for label, message in zip(labels, captured_messages):
        print("=" * 70)
        print(label)
        print("=" * 70)
        print(message)
        print()

    if actually_send:
        print("✅ Chaque message a été réellement envoyé sur Telegram (--send activé).")
    else:
        print(
            "ℹ️  Aperçu console uniquement, aucun message envoyé sur Telegram "
            "(relancez avec --send pour un envoi réel)."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aperçu des notifications d'administration.")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Envoie réellement chaque message sur Telegram (par défaut : aperçu console uniquement).",
    )
    args = parser.parse_args()
    run_preview(actually_send=args.send)
