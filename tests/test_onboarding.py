from __future__ import annotations

from pathlib import Path

from untether import engines
from untether.settings import UntetherSettings
from untether.telegram import onboarding


def test_check_setup_marks_missing_codex(monkeypatch, tmp_path: Path) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        onboarding,
        "load_settings",
        lambda: (
            UntetherSettings.model_validate(
                {
                    "transport": "telegram",
                    "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
                }
            ),
            tmp_path / "untether.toml",
        ),
    )

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "install codex" in titles
    assert "create a config" not in titles
    assert result.ok is False


def test_check_setup_marks_missing_config(monkeypatch, tmp_path: Path) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(onboarding, "HOME_CONFIG_PATH", tmp_path / "untether.toml")

    def _raise() -> None:
        raise onboarding.ConfigError("Missing config file")

    monkeypatch.setattr(onboarding, "load_settings", _raise)

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "create a config" in titles
    assert result.config_path == onboarding.HOME_CONFIG_PATH


def test_check_setup_marks_invalid_bot_token(monkeypatch, tmp_path: Path) -> None:
    backend = engines.get_backend("codex")
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/bin/codex")

    def _fail_require(*_args, **_kwargs):
        raise onboarding.ConfigError("Missing bot token")

    monkeypatch.setattr(
        onboarding,
        "load_settings",
        lambda: (
            UntetherSettings.model_validate(
                {
                    "transport": "telegram",
                    "transports": {"telegram": {"bot_token": "token", "chat_id": 123}},
                }
            ),
            tmp_path / "untether.toml",
        ),
    )
    monkeypatch.setattr(onboarding, "require_telegram", _fail_require)

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "configure telegram" in titles
