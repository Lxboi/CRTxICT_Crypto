"""
tests/test_admin_notifications.py

Tests pour execution/admin_notifications.py -- déplacé et allégé depuis
accounts/admin_notifications.py (simplification architecture, août
2026, retrait du multi-utilisateur/facturation : notify_user_added,
notify_user_removed, notify_user_status_changed et
notify_monthly_fee_report ont été retirés avec accounts/). Aucun test
n'existait encore pour ce module avant ce déplacement.

Vérifie uniquement que chaque fonction (a) envoie bien UN message,
(b) préfixe systématiquement le bandeau administrateur (ADMIN_BANNER),
et (c) inclut les informations clés attendues -- pas le contenu exact
du texte, qui peut évoluer librement.
"""

from __future__ import annotations

from unittest.mock import patch

import execution.admin_notifications as admin_notifications


def _capture_send():
    """Patch send_telegram_message et renvoie la liste des messages envoyés."""
    sent: list[str] = []

    def fake_send(message: str) -> bool:
        sent.append(message)
        return True

    return patch.object(admin_notifications, "send_telegram_message", side_effect=fake_send), sent


class TestNotifyApiKeyBrokenAndRestored:
    def test_notify_api_key_broken_includes_exchange_and_detail(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_api_key_broken("_owner", "kraken", "EAPI:Invalid key")

        assert len(sent) == 1
        assert admin_notifications.ADMIN_BANNER in sent[0]
        assert "kraken" in sent[0]
        assert "Invalid key" in sent[0]

    def test_notify_api_key_restored_includes_exchange(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_api_key_restored("_owner", "kraken")

        assert len(sent) == 1
        assert admin_notifications.ADMIN_BANNER in sent[0]
        assert "kraken" in sent[0]


class TestNotifyPositionProtectionRepaired:
    def test_mentions_only_what_was_actually_repaired(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_position_protection_repaired("_owner", "BTC", sl_repaired=True, tp_repaired=False)

        assert "stop-loss" in sent[0]
        assert "take-profit" not in sent[0]

    def test_mentions_both_when_both_repaired(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_position_protection_repaired("_owner", "BTC", sl_repaired=True, tp_repaired=True)

        assert "stop-loss" in sent[0]
        assert "take-profit" in sent[0]


class TestExecutionFailureNotifications:
    def test_notify_execution_failed_includes_symbol_and_reason(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_execution_failed("_owner", "ETH", "solde insuffisant")

        assert "ETH" in sent[0]
        assert "solde insuffisant" in sent[0]

    def test_notify_breakeven_failed_includes_symbol_and_reason(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_breakeven_failed("_owner", "ETH", "panne Kraken 503")

        assert "ETH" in sent[0]
        assert "panne Kraken 503" in sent[0]

    def test_notify_breakeven_repaired_includes_symbol(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_breakeven_repaired("_owner", "ETH")

        assert "ETH" in sent[0]

    def test_notify_close_failed_includes_symbol_and_reason(self):
        patcher, sent = _capture_send()
        with patcher:
            admin_notifications.notify_close_failed("_owner", "ETH", "timeout exchange")

        assert "ETH" in sent[0]
        assert "timeout exchange" in sent[0]


class TestRemovedMultiUserNotifications:
    """
    Régression négative : les notifications propres à la gestion de
    comptes tiers/facturation ne doivent PLUS exister sur ce module
    (retirées avec accounts/, voir plan de simplification architecture).
    """

    def test_user_management_and_fee_notifications_are_gone(self):
        for removed_name in (
            "notify_user_added", "notify_user_removed",
            "notify_user_status_changed", "notify_monthly_fee_report",
        ):
            assert not hasattr(admin_notifications, removed_name)
