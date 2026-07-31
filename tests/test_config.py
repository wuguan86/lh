from __future__ import annotations

import pytest

from board_screening.config import Settings


def test_settings_require_password_and_strong_session_secret(monkeypatch) -> None:
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)

    with pytest.raises(ValueError, match="APP_PASSWORD"):
        Settings.from_env()


def test_settings_accept_deployment_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_USERNAME", "operator")
    monkeypatch.setenv("APP_PASSWORD", "strong-password")
    monkeypatch.setenv("SESSION_SECRET", "a" * 32)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "screening.db"))
    monkeypatch.setenv("OUTPUT_FILE", str(tmp_path / "latest.csv"))

    settings = Settings.from_env()

    assert settings.username == "operator"
    assert settings.password == "strong-password"
    assert settings.session_secret == "a" * 32
