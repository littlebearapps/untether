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
                    "transports": {
                        "telegram": {
                            "bot_token": "token",
                            "chat_id": 123,
                            "allow_any_user": True,
                        }
                    },
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
                    "transports": {
                        "telegram": {
                            "bot_token": "token",
                            "chat_id": 123,
                            "allow_any_user": True,
                        }
                    },
                }
            ),
            tmp_path / "untether.toml",
        ),
    )
    monkeypatch.setattr(onboarding, "require_telegram", _fail_require)

    result = onboarding.check_setup(backend)

    titles = {issue.title for issue in result.issues}
    assert "configure telegram" in titles


# --- #1089: the wizard looks up the agy the runner will spawn ---------------


def _agy_env(monkeypatch, tmp_path: Path) -> Path:
    """PATH without agy and a home we control; returns that home."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    return home


def _fake_agy(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _agy_settings(monkeypatch, tmp_path: Path, antigravity: dict | None) -> None:
    raw: dict = {
        "transport": "telegram",
        "transports": {
            "telegram": {"bot_token": "token", "chat_id": 123, "allow_any_user": True}
        },
    }
    if antigravity is not None:
        raw["antigravity"] = antigravity
    settings = UntetherSettings.model_validate(raw)
    monkeypatch.setattr(
        onboarding, "load_settings", lambda: (settings, tmp_path / "untether.toml")
    )
    monkeypatch.setattr(
        onboarding,
        "load_settings_if_exists",
        lambda: (settings, tmp_path / "untether.toml"),
    )


def _agy_rows(*, installed: bool) -> list[tuple[str, bool, str | None]]:
    rows = onboarding.LiveServices().list_engines()
    return [row for row in rows if row[0] == "antigravity" and row[1] is installed]


def test_1089_check_setup_uses_configured_agy_cmd(monkeypatch, tmp_path: Path) -> None:
    _agy_env(monkeypatch, tmp_path)
    agy = _fake_agy(tmp_path / "opt" / "agy")
    _agy_settings(monkeypatch, tmp_path, {"cmd": str(agy)})

    result = onboarding.check_setup(engines.get_backend("antigravity"))

    assert "install agy" not in {issue.title for issue in result.issues}
    assert _agy_rows(installed=True)


def test_1089_check_setup_uses_local_bin_fallback(monkeypatch, tmp_path: Path) -> None:
    home = _agy_env(monkeypatch, tmp_path)
    _fake_agy(home / ".local" / "bin" / "agy")
    _agy_settings(monkeypatch, tmp_path, None)

    result = onboarding.check_setup(engines.get_backend("antigravity"))

    assert "install agy" not in {issue.title for issue in result.issues}
    assert _agy_rows(installed=True)


def test_1089_check_setup_reports_missing_agy(monkeypatch, tmp_path: Path) -> None:
    _agy_env(monkeypatch, tmp_path)
    _agy_settings(monkeypatch, tmp_path, None)

    result = onboarding.check_setup(engines.get_backend("antigravity"))

    assert "install agy" in {issue.title for issue in result.issues}
    assert _agy_rows(installed=False)
