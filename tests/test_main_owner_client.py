"""
tests/test_main_owner_client.py

Tests pour main.py::_build_owner_client() -- remplace l'ancien
_build_owner_clients()/_owner_client_for_symbol() (multi-exchange,
IBKR inclus, voir tests/test_main_owner_clients.py, supprimé) par un
client Kraken unique depuis la simplification architecture crypto-only
(août 2026).
"""

from __future__ import annotations

import pytest

import main


@pytest.fixture(autouse=True)
def _reset_owner_client_cache():
    """Isole chaque test de l'état module-level de main.py."""
    original = main._owner_client_cache
    main._owner_client_cache = None
    yield
    main._owner_client_cache = original


class _EmptySettings:
    kraken_api_key = None
    kraken_api_secret = None


class _RealSettings:
    kraken_api_key = "cle-demo"
    kraken_api_secret = "secret-demo"


class TestBuildOwnerClient:
    def test_missing_credentials_returns_none_none(self, monkeypatch):
        monkeypatch.setattr("config.settings.get_settings", lambda: _EmptySettings())

        client, exchange = main._build_owner_client({})

        assert client is None
        assert exchange is None

    def test_valid_credentials_build_kraken_client(self, monkeypatch):
        monkeypatch.setattr("config.settings.get_settings", lambda: _RealSettings())

        client, exchange = main._build_owner_client({"execution": {"exchange": "kraken"}})

        assert client is not None
        assert exchange == "kraken"

    def test_client_is_cached_across_calls(self, monkeypatch):
        monkeypatch.setattr("config.settings.get_settings", lambda: _RealSettings())

        client_a, _ = main._build_owner_client({"execution": {"exchange": "kraken"}})
        client_b, _ = main._build_owner_client({"execution": {"exchange": "kraken"}})

        assert client_a is client_b

    def test_unsupported_exchange_returns_none_none(self, monkeypatch):
        monkeypatch.setattr("config.settings.get_settings", lambda: _RealSettings())

        client, exchange = main._build_owner_client({"execution": {"exchange": "okx"}})

        assert client is None
        assert exchange is None
