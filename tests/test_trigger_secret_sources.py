"""#1219: webhook secrets from env/file, and private config files.

``secret_env`` / ``secret_file`` resolve into ``secret`` at parse, a missing
or unsafe source skips only that webhook (#1117), and no value ever reaches
an error or a log line. ``config_permission_fixes`` and the loop's warning
flag a readable config that holds a secret, or a readable backup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from untether.config import config_permission_fixes
from untether.config_reload_notification import format_config_permissions_notice
from untether.triggers.settings import (
    WebhookConfig,
    log_invalid_triggers,
    parse_trigger_config_isolated,
)

SECRET = "f3c1-very-secret-value-0123456789abcdef"


def _hook(**kw: Any) -> dict[str, Any]:
    return {
        "id": "gh",
        "path": "/hooks/gh",
        "auth": "hmac-sha256",
        "prompt_template": "x",
        **kw,
    }


def _secret_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "wh.secret"
    path.write_text(SECRET + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _skipped_error(**kw: Any) -> str:
    parsed = parse_trigger_config_isolated({"enabled": True, "webhooks": [_hook(**kw)]})
    assert not parsed.settings.webhooks
    (bad,) = parsed.invalid
    with capture_logs() as logs:
        log_invalid_triggers(parsed.invalid, phase="reload")
    assert SECRET not in bad.error and SECRET not in str(logs)
    return bad.error


# --- secret_env ------------------------------------------------------------


def test_secret_env_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R_WH_SECRET", SECRET)
    hook = WebhookConfig.model_validate(_hook(secret_env="R_WH_SECRET"))
    assert hook.secret is not None
    assert hook.secret.get_secret_value() == SECRET
    assert SECRET not in repr(hook)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_secret_env_missing_skips_the_webhook(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("R_WH_SECRET", raising=False)
    else:
        monkeypatch.setenv("R_WH_SECRET", value)
    assert "R_WH_SECRET is not set or empty" in _skipped_error(secret_env="R_WH_SECRET")


def test_secret_env_bad_name_rejected() -> None:
    assert "secret_env" in _skipped_error(secret_env="BAD-NAME")


# --- secret_file -----------------------------------------------------------


def test_secret_file_resolves_stripped(tmp_path: Path) -> None:
    hook = WebhookConfig.model_validate(_hook(secret_file=str(_secret_file(tmp_path))))
    assert hook.secret is not None
    assert hook.secret.get_secret_value() == SECRET


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_secret_file_readable_by_others_refused(tmp_path: Path, mode: int) -> None:
    path = _secret_file(tmp_path, mode)
    error = _skipped_error(secret_file=str(path))
    assert f"chmod 600 {path}" in error


def test_secret_file_relative_missing_dir_empty(tmp_path: Path) -> None:
    assert "absolute path" in _skipped_error(secret_file="wh.secret")
    assert "can't be read" in _skipped_error(secret_file=str(tmp_path / "nope"))
    assert "not a regular file" in _skipped_error(secret_file=str(tmp_path))
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    empty.chmod(0o600)
    assert "is empty" in _skipped_error(secret_file=str(empty))


def test_only_one_secret_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("R_WH_SECRET", SECRET)
    error = _skipped_error(secret=SECRET, secret_env="R_WH_SECRET")
    assert "set only one of secret, secret_env" in error


def test_strict_short_secret_applies_to_resolved_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("R_WH_SECRET", "short")
    parsed = parse_trigger_config_isolated(
        {
            "enabled": True,
            "webhook_defaults": "strict",
            "webhooks": [_hook(project="app", secret_env="R_WH_SECRET")],
        }
    )
    (bad,) = parsed.invalid
    assert "at least 32 characters" in bad.error


# --- config_permission_fixes -------------------------------------------------


def _config(tmp_path: Path, mode: int = 0o600, dir_mode: int = 0o700) -> Path:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(mode=0o700)
    path = cfg_dir / "untether.toml"
    path.write_text("", encoding="utf-8")
    path.chmod(mode)
    cfg_dir.chmod(dir_mode)
    return path


INLINE_TOKEN = {"transports": {"telegram": {"bot_token": "t"}}}


def test_private_config_needs_nothing(tmp_path: Path) -> None:
    assert config_permission_fixes(_config(tmp_path), INLINE_TOKEN) == ()


def test_readable_config_with_inline_token(tmp_path: Path) -> None:
    path = _config(tmp_path, 0o664, 0o775)
    assert config_permission_fixes(path, INLINE_TOKEN) == (
        f"chmod 600 {path}",
        f"chmod 700 {path.parent}",
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"bot_token": "legacy"},
        {"triggers": {"webhooks": [{"id": "gh", "secret": "s"}]}},
    ],
)
def test_other_inline_secrets_count(tmp_path: Path, raw: dict[str, Any]) -> None:
    path = _config(tmp_path, 0o644)
    assert config_permission_fixes(path, raw) == (f"chmod 600 {path}",)


def test_no_inline_secret_no_file_warning(tmp_path: Path) -> None:
    raw = {"triggers": {"webhooks": [{"id": "gh", "secret_env": "X"}]}}
    assert config_permission_fixes(_config(tmp_path, 0o644, 0o755), raw) == ()


def test_readable_backup_always_flagged(tmp_path: Path) -> None:
    path = _config(tmp_path)
    backup = path.parent / "untether.toml.bak-20261105"
    backup.write_text("", encoding="utf-8")
    backup.chmod(0o644)
    assert config_permission_fixes(path, {}) == (
        f"chmod 600 {path.parent / 'untether.toml'}.bak*",
    )
    backup.chmod(0o600)
    assert config_permission_fixes(path, {}) == ()


def test_permissions_notice_names_commands_only() -> None:
    text = format_config_permissions_notice(
        ["chmod 600 /home/u/.untether/untether.toml"]
    )
    assert text.startswith("⚠️ Config file permissions")
    assert "`chmod 600 /home/u/.untether/untether.toml`" in text
    assert "UNTETHER__TRANSPORTS__TELEGRAM__BOT_TOKEN" in text


# --- the loop's warning ----------------------------------------------------


@pytest.mark.anyio
async def test_loop_warns_once_per_finding(tmp_path: Path) -> None:
    from tests.telegram_fakes import FakeTransport, make_cfg
    from untether.telegram.loop import TelegramLoopState, _warn_config_permissions

    path = _config(tmp_path, 0o644)
    path.write_text(
        f'[transports.telegram]\nbot_token = "{SECRET}"\n', encoding="utf-8"
    )
    transport = FakeTransport()
    cfg = make_cfg(transport)
    state = TelegramLoopState.__new__(TelegramLoopState)
    state.config_permission_fixes = ()

    with capture_logs() as logs:
        await _warn_config_permissions(cfg, state, path)
        await _warn_config_permissions(cfg, state, path)  # unchanged reload
    warns = [e for e in logs if e["event"] == "config.file_permissions"]
    assert len(warns) == 1 and warns[0]["log_level"] == "warning"
    assert len(transport.send_calls) == 1
    assert SECRET not in str(logs)
    assert SECRET not in transport.send_calls[0]["message"].text

    path.chmod(0o600)
    await _warn_config_permissions(cfg, state, path)  # fixed: quiet, forgotten
    assert state.config_permission_fixes == ()
    path.chmod(0o644)
    await _warn_config_permissions(cfg, state, path)  # broken again: warns
    assert len(transport.send_calls) == 2
    await _warn_config_permissions(cfg, state, None)
    assert len(transport.send_calls) == 2
