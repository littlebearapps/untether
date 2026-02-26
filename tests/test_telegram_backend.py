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
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="stateless",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )

    assert "untether" in message and "is ready" in message
    assert "engines: `codex (not installed: pi)`" in message
    assert "projects: `0`" in message


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
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="stateless",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )

    assert "engines:" in message and "codex" in message
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


def test_startup_message_includes_version(tmp_path: Path) -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="stateless",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )
    assert f"v{__version__}" in message


def test_startup_message_uses_dog_emoji(tmp_path: Path) -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="stateless",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )
    assert "\N{DOG}" in message
    assert "\N{OCTOPUS}" not in message


def test_startup_message_minimal_when_healthy(tmp_path: Path) -> None:
    """When everything is healthy, only show primary info and working dir."""
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="stateless",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )
    assert "mode:" not in message
    assert "topics:" not in message
    assert "triggers:" not in message
    assert "engines:" not in message
    assert "resume lines:" not in message
    assert "working in:" in message


def test_startup_message_shows_mode_chat(tmp_path: Path) -> None:
    runtime = _build_healthy_runtime()
    message = telegram_backend._build_startup_message(
        runtime,
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="chat",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )
    assert "mode: `chat`" in message


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
        startup_pwd=str(tmp_path),
        chat_id=123,
        session_mode="stateless",
        show_resume_line=True,
        topics=TelegramTopicsSettings(),
    )
    assert "projects: `2`" in message


def test_telegram_backend_build_and_run_wires_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "untether.toml"
    config_path.write_text(
        'watch_config = true\ntransport = "telegram"\n\n'
        "[transports.telegram]\n"
        'bot_token = "token"\n'
        "chat_id = 321\n",
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
        def __init__(self, token: str) -> None:
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
    assert cfg.voice_transcription_api_key == "local"
    assert cfg.allowed_user_ids == (7, 8)
    assert cfg.files.enabled is True
    assert cfg.files.allowed_user_ids == [1, 2]
    assert cfg.topics.enabled is True
    assert cfg.bot.token == "token"
    assert kwargs["watch_config"] is True
    assert kwargs["transport_id"] == "telegram"


def test_telegram_files_settings_defaults() -> None:
    cfg = TelegramFilesSettings()

    assert cfg.enabled is False
    assert cfg.auto_put is True
    assert cfg.auto_put_mode == "upload"
    assert cfg.uploads_dir == "incoming"
    assert cfg.allowed_user_ids == []
