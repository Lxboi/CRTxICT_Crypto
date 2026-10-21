"""
tests/test_execution_engine.py

Tests pour execution/engine.py -- en particulier _for_each_user(), qui
n'itérait auparavant que sur des tests d'intégration indirects (jamais
de fichier de test dédié). Simplification architecture (août 2026,
retrait du multi-utilisateur) : _for_each_user() ne boucle plus que sur
VOUS (owner_client) -- ces tests couvrent spécifiquement ce
comportement owner-only et la détection de panne d'authentification
(execution/api_health.py + execution/admin_notifications.py).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from execution import kill_switch
from execution.engine import (
    check_and_repair_position_protection,
    try_close_position_for_all_users,
    try_execute_entry_for_all_users,
    try_move_stop_to_breakeven_for_all_users,
)
from execution.exchange_client import ExchangeClient, OrderResult, Position


class _FakeClient(ExchangeClient):
    """Client factice minimal, comportement personnalisable par test."""

    def __init__(
        self, balance: float = 10_000.0, positions: list[Position] | None = None,
        balance_error: Exception | None = None,
    ) -> None:
        self._balance = balance
        self._positions = positions or []
        self._balance_error = balance_error
        self.close_calls: list[tuple[str, str, float]] = []
        self.move_stop_calls: list[tuple[str, str, float]] = []

    def place_market_order(self, user_id, symbol, side, quantity, stop_loss, leverage, take_profit_levels=None):
        return OrderResult(True, "order-1", 100.0, quantity)

    def move_stop_loss(self, user_id, symbol, new_stop_loss):
        self.move_stop_calls.append((user_id, symbol, new_stop_loss))
        return OrderResult(True, "stop-1", None, None)

    def close_position(self, user_id, symbol, fraction=1.0):
        self.close_calls.append((user_id, symbol, fraction))
        return OrderResult(True, None, None, None)

    def get_open_positions(self):
        return self._positions

    def get_account_balance(self):
        if self._balance_error is not None:
            raise self._balance_error
        return self._balance

    def ensure_take_profit_orders(self, user_id, symbol, side, quantity, take_profit_levels):
        return OrderResult(True, None, None, quantity)


@pytest.fixture(autouse=True)
def _isolate_execution_state(tmp_path, monkeypatch):
    """Isole kill_switch ET api_health dans un dossier temporaire pour chaque test."""
    import execution.api_health as api_health

    monkeypatch.setattr(kill_switch, "STATE_DIR", tmp_path / "execution_state")
    monkeypatch.setattr(api_health, "STATE_DIR", tmp_path / "execution_state" / "api_health")
    yield


class TestOwnerOnlyLoop:
    """
    _for_each_user() -- depuis le retrait du multi-utilisateur, agit
    UNIQUEMENT sur owner_client (plus de boucle sur des comptes tiers).
    """

    def test_no_owner_client_returns_empty_results(self):
        results = try_execute_entry_for_all_users(
            "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, None, None,
        )
        assert results == {}

    def test_owner_client_disabled_kill_switch_returns_none_for_owner(self):
        client = _FakeClient()
        results = try_execute_entry_for_all_users(
            "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, client, "kraken",
        )
        assert results == {kill_switch.OWNER_USER_ID: None}

    def test_owner_client_enabled_kill_switch_executes_for_owner_only(self):
        kill_switch.enable(kill_switch.OWNER_USER_ID)
        client = _FakeClient()

        results = try_execute_entry_for_all_users(
            "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, client, "kraken",
        )

        assert set(results.keys()) == {kill_switch.OWNER_USER_ID}
        assert results[kill_switch.OWNER_USER_ID].success is True

    def test_close_position_for_all_users_is_owner_only(self):
        kill_switch.enable(kill_switch.OWNER_USER_ID)
        client = _FakeClient(positions=[Position("BTC", "long", 100.0, 1.0, 50.0, 5.0)])

        results = try_close_position_for_all_users("BTC", client, "kraken")

        assert set(results.keys()) == {kill_switch.OWNER_USER_ID}
        assert client.close_calls == [(kill_switch.OWNER_USER_ID, "BTC", 1.0)]

    def test_move_stop_to_breakeven_for_all_users_is_owner_only(self):
        kill_switch.enable(kill_switch.OWNER_USER_ID)
        client = _FakeClient()

        results = try_move_stop_to_breakeven_for_all_users("BTC", 100.0, client, "kraken")

        assert set(results.keys()) == {kill_switch.OWNER_USER_ID}
        assert client.move_stop_calls == [(kill_switch.OWNER_USER_ID, "BTC", 100.0)]

    def test_check_and_repair_position_protection_is_owner_only(self):
        kill_switch.enable(kill_switch.OWNER_USER_ID)
        client = _FakeClient(positions=[Position("BTC", "long", 100.0, 1.0, 0.0, 5.0)])
        alert = {"stop_loss": 95.0, "entry_price": 100.0, "highest_tp_hit": 0, "tp_50": 110.0, "tp_100": None, "tp_htf_dol": None}

        repairs = check_and_repair_position_protection("BTC", alert, client, "kraken")

        assert set(repairs.keys()) <= {kill_switch.OWNER_USER_ID}


class TestApiHealthIntegration:
    """
    _for_each_user() détecte un échec d'authentification et notifie via
    execution/admin_notifications.py -- une seule fois par panne.
    """

    def test_auth_failure_flags_broken_and_notifies_once(self):
        import execution.api_health as api_health

        kill_switch.enable(kill_switch.OWNER_USER_ID)
        client = _FakeClient(balance_error=ValueError("EAPI:Invalid key"))

        with patch("execution.admin_notifications.notify_api_key_broken") as mock_notify:
            try_execute_entry_for_all_users(
                "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, client, "kraken",
            )
            assert mock_notify.call_count == 1
            assert api_health.is_flagged_broken(kill_switch.OWNER_USER_ID) is True

            # Deuxième échec identique -- pas de deuxième notification (déjà flaggé).
            try_execute_entry_for_all_users(
                "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, client, "kraken",
            )
            assert mock_notify.call_count == 1

    def test_recovery_clears_flag_and_notifies_restored(self):
        import execution.api_health as api_health

        kill_switch.enable(kill_switch.OWNER_USER_ID)
        api_health.flag_broken(kill_switch.OWNER_USER_ID)
        client = _FakeClient()  # solde OK -> succès

        with patch("execution.admin_notifications.notify_api_key_restored") as mock_notify:
            try_execute_entry_for_all_users(
                "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, client, "kraken",
            )
            assert mock_notify.call_count == 1
            assert api_health.is_flagged_broken(kill_switch.OWNER_USER_ID) is False

    def test_transient_error_does_not_flag_broken(self):
        import execution.api_health as api_health

        kill_switch.enable(kill_switch.OWNER_USER_ID)
        client = _FakeClient(balance_error=ConnectionError("Timeout réseau"))

        with patch("execution.admin_notifications.notify_api_key_broken") as mock_notify:
            try_execute_entry_for_all_users(
                "BTC", "bullish", 100.0, 95.0, 2.0, {"kraken": 10.0}, client, "kraken",
            )
            assert mock_notify.call_count == 0
            assert api_health.is_flagged_broken(kill_switch.OWNER_USER_ID) is False
