"""
tests/test_api_health.py

Tests pour execution/api_health.py -- déplacé depuis accounts/api_health.py
(simplification architecture, août 2026, retrait du multi-utilisateur).
Aucun test n'existait encore pour ce module avant ce déplacement.
"""

from __future__ import annotations

import pytest

from execution.api_health import (
    AUTH_FAILURE_PATTERNS,
    clear_broken_flag,
    flag_broken,
    is_auth_failure,
    is_flagged_broken,
)


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    """Isole chaque test dans un dossier d'état temporaire -- jamais execution_state/ réel."""
    import execution.api_health as api_health

    monkeypatch.setattr(api_health, "STATE_DIR", tmp_path / "api_health")
    yield


class TestIsAuthFailure:
    def test_none_or_empty_is_not_an_auth_failure(self):
        assert is_auth_failure(None) is False
        assert is_auth_failure("") is False

    @pytest.mark.parametrize("pattern", AUTH_FAILURE_PATTERNS)
    def test_each_known_pattern_is_detected(self, pattern):
        assert is_auth_failure(f"Erreur Kraken : {pattern} quelque chose") is True

    def test_transient_error_is_not_an_auth_failure(self):
        assert is_auth_failure("Timeout de connexion au serveur") is False
        assert is_auth_failure("Rate limit dépassé (429)") is False


class TestBrokenFlagLifecycle:
    def test_not_flagged_by_default(self):
        assert is_flagged_broken("_owner") is False

    def test_flag_broken_then_is_flagged(self):
        flag_broken("_owner")
        assert is_flagged_broken("_owner") is True

    def test_clear_broken_flag_resets_state(self):
        flag_broken("_owner")
        clear_broken_flag("_owner")
        assert is_flagged_broken("_owner") is False

    def test_clear_broken_flag_is_safe_when_never_flagged(self):
        """Ne doit jamais lever d'exception même si aucun fichier d'état n'existe encore."""
        clear_broken_flag("_owner")
        assert is_flagged_broken("_owner") is False

    def test_state_is_isolated_per_user_id(self):
        flag_broken("_owner")
        assert is_flagged_broken("_owner") is True
        assert is_flagged_broken("other_id") is False
