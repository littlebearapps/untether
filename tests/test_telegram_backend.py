from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from untether import __version__
from untether.config import ProjectConfig, ProjectsConfig
from untether.router import AutoRouter, RunnerEntry
from untether.runners.mock import Return, ScriptRunner
from untether.settings import (
    TelegramFilesSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
)
from untether.telegram import backend as telegram_backend
from untether.transport_runtime import TransportRuntime


def test_build_startup_message_includes_missing_engines(tmp_path: Path) -> None:
    codex = "codex"
    pi = "pi"
    runner = ScriptRunner([Return(answer="ok")], engine=codex)
    missing = ScriptRunner([Return(answer="ok")], engine=pi)
    router = AutoRouter(
        entries=[
            RunnerEntry(engine=codex, runner=runner),
            RunnerEntry(
                engine=pi,
                runner=missing,
                status="missing_cli",
                issue="missing",
            ),
        ],
        default_engine=codex,
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        watch_config=True,
    )

    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )

    assert "untether is ready" in message
    assert "not installed: pi" in message
    assert "_directories:_ `none`" in message


def test_build_startup_message_surfaces_unavailable_engine_reasons(
    tmp_path: Path,
) -> None:
    codex = "codex"
    pi = "pi"
    claude = "claude"
    runner = ScriptRunner([Return(answer="ok")], engine=codex)
    bad_cfg = ScriptRunner([Return(answer="ok")], engine=pi)
    load_err = ScriptRunner([Return(answer="ok")], engine=claude)

    router = AutoRouter(
        entries=[
            RunnerEntry(engine=codex, runner=runner),
            RunnerEntry(engine=pi, runner=bad_cfg, status="bad_config", issue="bad"),
            RunnerEntry(
                engine=claude,
                runner=load_err,
                status="load_error",
                issue="failed",
            ),
        ],
        default_engine=codex,
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        watch_config=True,
    )

    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )

    assert "_installed engines:_" in message and "codex" in message
    assert "misconfigured: pi" in message
    assert "failed to load: claude" in message


def _build_healthy_runtime() -> TransportRuntime:
    """Build a runtime with a single healthy engine and no projects."""
    runner = ScriptRunner([Return(answer="ok")], engine="claude")
    router = AutoRouter(
        entries=[RunnerEntry(engine="claude", runner=runner)],
        default_engine="claude",
    )
    return TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        watch_config=True,
    )


def test_startup_message_includes_version() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )
    assert f"v{__version__}" in message


def test_startup_message_uses_dog_emoji() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )
    assert "\N{DOG}" in message
    assert "\N{OCTOPUS}" not in message


def test_startup_message_core_fields() -> None:
    """When everything is healthy, show core status fields."""
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )
    assert "_default engine:_ `claude`" in message
    assert "_installed engines:_ `claude`" in message
    assert "_directories:_ `none`" in message
    # Disabled topics/triggers should NOT appear
    assert "_topics:_" not in message
    assert "_triggers:_" not in message
    # Quick-start hint and help link
    assert "/config" in message
    assert "help-guides" in message
    assert "report a bug" in message


def test_startup_message_shows_topics_when_enabled() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(enabled=True, scope="main"),
    )
    assert "_topics:_" in message


def test_startup_message_shows_mode_assistant() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
        session_mode="chat",
    )
    assert "_mode:_ `assistant`" in message


def test_startup_message_shows_mode_workspace() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(enabled=True, scope="main"),
        session_mode="chat",
    )
    assert "_mode:_ `workspace`" in message


def test_startup_message_shows_mode_handoff() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
        session_mode="stateless",
    )
    assert "_mode:_ `handoff`" in message


def test_startup_message_shows_triggers_when_enabled() -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
        trigger_config={"enabled": True, "webhooks": [{}], "crons": []},
    )
    assert "_triggers:_" in message
    # #869: singular for a count of 1
    assert "(1 webhook, 0 crons)" in message
    assert "1 webhooks" not in message


def _startup_crons_message(
    crons: list[dict[str, Any]], spent: set[str] | None = None
) -> str:
    runtime = _build_healthy_runtime()
    kwargs: dict[str, Any] = {}
    if spent is not None:
        kwargs["spent_cron_ids"] = spent
    return telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
        trigger_config={"enabled": True, "webhooks": [], "crons": crons},
        **kwargs,
    )


def test_startup_message_excludes_spent_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#809: a fired run_once cron is not counted as scheduled.

    Driven through ``build_and_run`` so the ``run_once_fired.json`` read
    (sibling of the toml) is exercised, not just the formatter.
    """
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'transport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 321\n\n"
        "[triggers]\n"
        "enabled = true\n\n"
        "[[triggers.crons]]\n"
        'id = "daily"\n'
        'schedule = "0 9 * * *"\n'
        'prompt = "hi"\n\n'
        "[[triggers.crons]]\n"
        'id = "once"\n'
        'schedule = "0 10 * * *"\n'
        'prompt = "hi"\n'
        "run_once = true\n",
        encoding="utf-8",
    )
    (tmp_path / "run_once_fired.json").write_text(
        '{"fired": {"once": "2026-09-29T10:00:00+10:00"}}', encoding="utf-8"
    )

    captured: dict[str, Any] = {}

    async def fake_run_main_loop(cfg, **kwargs) -> None:
        captured["cfg"] = cfg

    class _FakeClient:
        def __init__(self, token: str, **kwargs: Any) -> None:
            self.token = token

        async def close(self) -> None:
            return None

    monkeypatch.setattr(telegram_backend, "run_main_loop", fake_run_main_loop)
    monkeypatch.setattr(telegram_backend, "TelegramClient", _FakeClient)

    telegram_backend.TelegramBackend().build_and_run(
        transport_config=TelegramTransportSettings(
            bot_token="token", chat_id=321, allowed_user_ids=[7]
        ),
        config_path=config_path,
        runtime=_build_healthy_runtime(),
        final_notify=False,
        default_engine_override=None,
    )

    startup = captured["cfg"].startup_msg
    assert "_triggers:_ `enabled (0 webhooks, 1 cron, 1 spent one-shot)`" in startup


def test_startup_message_no_spent_suffix_when_none() -> None:
    """#809: with nothing fired the suffix is omitted entirely."""
    crons = [{"id": "a"}, {"id": "b", "run_once": True}]
    for spent in (None, set(), {"not-configured"}):
        message = _startup_crons_message(crons, spent)
        assert "_triggers:_ `enabled (0 webhooks, 2 crons)`" in message
        assert "spent" not in message


def test_startup_message_all_crons_spent() -> None:
    """#809: every cron spent → 0 scheduled, the rest reported as spent."""
    crons = [
        {"id": "a", "run_once": True},
        {"id": "b", "run_once": True},
    ]
    message = _startup_crons_message(crons, {"a", "b"})
    assert "_triggers:_ `enabled (0 webhooks, 0 crons, 2 spent one-shots)`" in message


@pytest.mark.parametrize(
    ("n_webhooks", "n_crons", "expected"),
    [
        (0, 0, "0 webhooks, 0 crons"),
        (1, 1, "1 webhook, 1 cron"),
        (2, 13, "2 webhooks, 13 crons"),
    ],
)
def test_869_startup_message_trigger_counts_pluralise(
    n_webhooks: int, n_crons: int, expected: str
) -> None:
    message = telegram_backend._build_startup_message(
        _build_healthy_runtime(),
        chat_id=123,
        topics=TelegramTopicsSettings(),
        trigger_config={
            "enabled": True,
            "webhooks": [{}] * n_webhooks,
            "crons": [{"id": f"c{i}"} for i in range(n_crons)],
        },
    )
    assert f"_triggers:_ `enabled ({expected})`" in message


def test_startup_message_project_count(tmp_path: Path) -> None:
    runner = ScriptRunner([Return(answer="ok")], engine="claude")
    router = AutoRouter(
        entries=[RunnerEntry(engine="claude", runner=runner)],
        default_engine="claude",
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(
            projects={
                "proj-a": ProjectConfig(
                    alias="proj-a",
                    path=Path("/a"),
                    worktrees_dir=Path(".worktrees"),
                    chat_id=1,
                ),
                "proj-b": ProjectConfig(
                    alias="proj-b",
                    path=Path("/b"),
                    worktrees_dir=Path(".worktrees"),
                    chat_id=2,
                ),
            },
            default_project=None,
        ),
        watch_config=True,
    )
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )
    assert "_directories:_ `proj-a, proj-b`" in message


def test_telegram_backend_build_and_run_wires_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'watch_config = true\ntransport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 321\n"
        "allow_any_user = true\n",
        encoding="utf-8",
    )

    codex = "codex"
    runner = ScriptRunner([Return(answer="ok")], engine=codex)
    router = AutoRouter(
        entries=[RunnerEntry(engine=codex, runner=runner)],
        default_engine=codex,
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects={}, default_project=None),
        watch_config=True,
    )

    captured: dict[str, Any] = {}

    async def fake_run_main_loop(cfg, **kwargs) -> None:
        captured["cfg"] = cfg
        captured["kwargs"] = kwargs

    class _FakeClient:
        def __init__(self, token: str, **kwargs: Any) -> None:
            self.token = token

        async def close(self) -> None:
            return None

    monkeypatch.setattr(telegram_backend, "run_main_loop", fake_run_main_loop)
    monkeypatch.setattr(telegram_backend, "TelegramClient", _FakeClient)

    transport_config = TelegramTransportSettings(
        bot_token="token",
        chat_id=321,
        allowed_user_ids=[7, 8],
        voice_transcription=True,
        voice_max_bytes=1234,
        voice_transcription_model="whisper-1",
        voice_transcription_base_url="http://localhost:8000/v1",
        voice_transcription_api_key="local",
        voice_show_transcription=False,
        files=TelegramFilesSettings(enabled=True, allowed_user_ids=[1, 2]),
        topics=TelegramTopicsSettings(enabled=True, scope="main"),
    )

    telegram_backend.TelegramBackend().build_and_run(
        transport_config=transport_config,
        config_path=config_path,
        runtime=runtime,
        final_notify=False,
        default_engine_override=None,
    )

    cfg = captured["cfg"]
    kwargs = captured["kwargs"]
    assert cfg.chat_id == 321
    assert cfg.voice_transcription is True
    assert cfg.voice_max_bytes == 1234
    assert cfg.voice_transcription_model == "whisper-1"
    assert cfg.voice_transcription_base_url == "http://localhost:8000/v1"
    # #378: voice_transcription_api_key is now SecretStr — compare via .get_secret_value()
    assert cfg.voice_transcription_api_key is not None
    assert cfg.voice_transcription_api_key.get_secret_value() == "local"
    assert cfg.voice_show_transcription is False
    assert cfg.allowed_user_ids == (7, 8)
    assert cfg.files.enabled is True
    assert cfg.files.allowed_user_ids == [1, 2]
    assert cfg.topics.enabled is True
    assert cfg.bot.token == "token"
    assert kwargs["watch_config"] is True
    assert kwargs["transport_id"] == "telegram"


def test_detect_cli_version_returns_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version detection extracts version from CLI output."""
    import subprocess as sp

    def fake_run(args, **kwargs):
        return sp.CompletedProcess(
            args=args, returncode=0, stdout="myengine v1.2.3\n", stderr=""
        )

    monkeypatch.setattr(sp, "run", fake_run)
    result = telegram_backend._detect_cli_version("myengine")
    assert result == "1.2.3"


def test_detect_cli_version_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing CLI returns None gracefully."""
    import subprocess as sp

    def fake_run(args, **kwargs):
        raise FileNotFoundError("not found")

    monkeypatch.setattr(sp, "run", fake_run)
    assert telegram_backend._detect_cli_version("missing") is None


def test_build_versions_line(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {"claude": "2.1.63", "opencode": "1.1.11"}
    monkeypatch.setattr(
        telegram_backend,
        "_detect_cli_version",
        lambda cmd: versions.get(cmd),
    )
    line = telegram_backend._build_versions_line(("claude", "opencode"))
    assert line is not None
    assert "py " in line
    assert "claude 2.1.63" in line
    assert "opencode 1.1.11" in line


def test_startup_message_excludes_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Versions moved to /config home page — no longer in startup."""
    runtime = _build_healthy_runtime()
    monkeypatch.setattr(
        telegram_backend,
        "_detect_cli_version",
        lambda cmd: "1.0.0",
    )
    message = telegram_backend._build_startup_message(
        runtime,
        chat_id=123,
        topics=TelegramTopicsSettings(),
    )
    assert "versions:" not in message


def test_telegram_files_settings_defaults() -> None:
    cfg = TelegramFilesSettings()

    assert cfg.enabled is False
    assert cfg.auto_put is True
    assert cfg.auto_put_mode == "upload"
    assert cfg.uploads_dir == "incoming"
    assert cfg.allowed_user_ids == []
