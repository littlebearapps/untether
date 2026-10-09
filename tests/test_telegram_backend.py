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


@pytest.mark.anyio
async def test_build_versions_line(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {"claude": "2.1.63", "opencode": "1.1.11"}
    monkeypatch.setattr(
        telegram_backend,
        "_detect_cli_version",
        lambda cmd: versions.get(cmd),
    )
    line = await telegram_backend._build_versions_line(("opencode", "claude", "pi"))
    assert line is not None
    assert line.startswith("py ")
    assert line.endswith("claude 2.1.63 · opencode 1.1.11")


def test_detect_cli_version_reads_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """#951: Pi prints its version to stderr, with an empty stdout."""
    import subprocess as sp

    def fake_run(args, **kwargs):
        return sp.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="0.78.0\n"
        )

    monkeypatch.setattr(sp, "run", fake_run)
    assert telegram_backend._detect_cli_version("pi") == "0.78.0"


def test_detect_cli_version_failed_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """#951: a non-zero exit is still no version, whatever stderr says."""
    import subprocess as sp

    def fake_run(args, **kwargs):
        return sp.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="error: 1.2.3 broke\n"
        )

    monkeypatch.setattr(sp, "run", fake_run)
    assert telegram_backend._detect_cli_version("broken") is None


@pytest.mark.anyio
async def test_build_versions_line_probes_concurrently_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#951: four slow probes take about one probe's time, and the event loop
    keeps running while they do."""
    import time

    import anyio

    def slow_probe(cmd: str) -> str:
        time.sleep(0.3)
        return "1.0.0"

    monkeypatch.setattr(telegram_backend, "_detect_cli_version", slow_probe)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    start = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        line = await telegram_backend._build_versions_line(
            ("claude", "codex", "opencode", "pi")
        )
        tg.cancel_scope.cancel()
    elapsed = time.monotonic() - start

    assert line is not None and "pi 1.0.0" in line
    assert elapsed < 0.9  # serial would be >= 1.2 s
    assert ticks >= 10  # the loop wasn't blocked


@pytest.mark.anyio
async def test_build_versions_line_caches_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#951: a second About within the TTL spawns nothing; after it, re-probes."""
    calls: list[str] = []

    def probe(cmd: str) -> str:
        calls.append(cmd)
        return "1.0.0"

    monkeypatch.setattr(telegram_backend, "_detect_cli_version", probe)
    await telegram_backend._build_versions_line(("claude", "pi"))
    await telegram_backend._build_versions_line(("claude", "pi"))
    assert sorted(calls) == ["claude", "pi"]

    monkeypatch.setattr(telegram_backend, "_CLI_VERSION_TTL_S", 0.0)
    await telegram_backend._build_versions_line(("pi",))
    assert sorted(calls) == ["claude", "pi", "pi"]


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


# ---------------------------------------------------------------------------
# #836: unattended-approval crons in the startup message
# ---------------------------------------------------------------------------


def _claude_runtime(
    *,
    engine_mode: str | None = "plan",
    projects: dict[str, str] | None = None,
    default_engine: str = "claude",
    with_claude: bool = True,
) -> TransportRuntime:
    from untether.config import ProjectConfig, ProjectsConfig
    from untether.runners.claude import ClaudeRunner

    entries = []
    if with_claude:
        entries.append(
            RunnerEntry(
                engine="claude",
                runner=ClaudeRunner(claude_cmd="claude", permission_mode=engine_mode),
            )
        )
    entries.append(RunnerEntry(engine="codex", runner=ScriptRunner([], engine="codex")))
    router = AutoRouter(entries=entries, default_engine=default_engine)
    project_cfgs = {
        alias: ProjectConfig(
            alias=alias,
            path=Path("/tmp") / alias,
            worktrees_dir=Path(".wt"),
            default_engine=engine,
        )
        for alias, engine in (projects or {}).items()
    }
    return TransportRuntime(
        router=router,
        projects=ProjectsConfig(projects=project_cfgs, default_project=None),
        watch_config=True,
    )


def _msg_836(
    crons: list[dict[str, Any]],
    *,
    runtime: TransportRuntime | None = None,
    enabled: bool = True,
    spent: set[str] | None = None,
) -> str:
    full = [{"schedule": "0 0 1 1 *", "prompt": "hi", **c} for c in crons]
    return telegram_backend._build_startup_message(
        runtime or _claude_runtime(),
        chat_id=123,
        topics=TelegramTopicsSettings(),
        trigger_config={"enabled": enabled, "webhooks": [], "crons": full},
        spent_cron_ids=spent or set(),
    )


def _line_836(message: str) -> str | None:
    from untether.permission_audit import UNATTENDED_LINE_LABEL

    for part in message.split("\n\n"):
        if part.startswith(f"_{UNATTENDED_LINE_LABEL}:_"):
            return part
    return None


def test_836_startup_lists_unattended_crons() -> None:
    msg = _msg_836(
        [
            {"id": "c2", "permission_mode": "default"},
            {"id": "c4", "permission_mode": "plan"},
        ]
    )
    assert _line_836(msg) == (
        "_unattended approvals (auto-denied):_ `cron:c2 (default), cron:c4 (plan)`"
    )
    # Below the triggers line.
    assert msg.index("_triggers:_") < msg.index("_unattended approvals")


def test_836_inherited_mode_cron_listed_when_engine_default_asks() -> None:
    """A Claude cron with no permission_mode falls back to the engine default
    (`plan`) — the slip the line exists to surface."""
    msg = _msg_836([{"id": "slip"}, {"id": "ok", "permission_mode": "auto"}])
    assert _line_836(msg) == (
        "_unattended approvals (auto-denied):_ `cron:slip (inherits plan)`"
    )
    quiet = _msg_836([{"id": "slip"}], runtime=_claude_runtime(engine_mode="auto"))
    assert _line_836(quiet) is None
    bypass = _claude_runtime(engine_mode="plan")
    runner = bypass.resolve_runner(resume_token=None, engine_override="claude").runner
    runner.dangerously_skip_permissions = True  # type: ignore[attr-defined]
    assert _line_836(_msg_836([{"id": "slip"}], runtime=bypass)) is None


@pytest.mark.parametrize("mode", ["plan-auto", "bypassPermissions", "auto", "dontAsk"])
def test_836_no_line_when_set_empty(mode: str) -> None:
    msg = _msg_836([{"id": "c", "permission_mode": mode}])
    assert _line_836(msg) is None
    assert "unattended" not in msg


def test_836_no_line_when_triggers_disabled() -> None:
    msg = _msg_836([{"id": "c", "permission_mode": "default"}], enabled=False)
    assert "unattended" not in msg


def test_836_no_line_for_non_claude_crons() -> None:
    msg = _msg_836(
        [
            {"id": "x", "engine": "codex", "permission_mode": "default"},
            {"id": "y", "project": "cx"},
        ],
        runtime=_claude_runtime(projects={"cx": "codex"}),
    )
    assert _line_836(msg) is None


def test_836_project_default_engine_resolves_to_claude() -> None:
    """#862: no engine, project default claude, global default codex."""
    msg = _msg_836(
        [{"id": "p", "project": "cl", "permission_mode": "acceptEdits"}],
        runtime=_claude_runtime(projects={"cl": "claude"}, default_engine="codex"),
    )
    assert _line_836(msg) == (
        "_unattended approvals (auto-denied):_ `cron:p (acceptEdits)`"
    )


def test_836_no_line_when_claude_not_configured() -> None:
    msg = _msg_836(
        [{"id": "c", "permission_mode": "default"}],
        runtime=_claude_runtime(with_claude=False, default_engine="codex"),
    )
    assert "unattended" not in msg


def test_836_spent_run_once_excluded() -> None:
    msg = _msg_836(
        [
            {"id": "gone", "permission_mode": "default", "run_once": True},
            {"id": "gone2", "run_once": True},
        ],
        spent={"gone", "gone2"},
    )
    assert _line_836(msg) is None


def test_836_more_than_three_shows_plus_n() -> None:
    msg = _msg_836([{"id": f"c{i}", "permission_mode": "default"} for i in range(5)])
    line = _line_836(msg)
    assert line is not None
    assert line.endswith("` +2 more")
    assert "cron:c2 (default)`" in line
    assert "c3" not in line


def test_836_no_line_for_auto_crons() -> None:
    msg = _msg_836([{"id": "a", "permission_mode": "auto"}])
    assert "unattended" not in msg
    assert "auto_semantics" not in msg


def test_836_bad_trigger_section_skips_line() -> None:
    msg = telegram_backend._build_startup_message(
        _claude_runtime(),
        chat_id=123,
        topics=TelegramTopicsSettings(),
        trigger_config={"enabled": True, "crons": [{"id": 1}]},
    )
    assert "_triggers:_" in msg
    assert "unattended" not in msg


def test_836_backtick_in_cron_id_sanitised() -> None:
    msg = _msg_836([{"id": "we`ird*_[id", "permission_mode": "default"}])
    line = _line_836(msg)
    assert line == (
        "_unattended approvals (auto-denied):_ `cron:we'ird*_[id (default)`"
    )
    assert line.count("`") == 2


def test_836_no_audit_log_from_startup_message() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        _msg_836([{"id": "c", "permission_mode": "default"}])
    names = {e["event"] for e in logs}
    assert "trigger.unattended_approval_risk" not in names
    assert "startup.unattended_line_failed" not in names


def test_836_failure_never_breaks_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    import untether.permission_audit as audit_mod

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(audit_mod, "audit_claude_permission_modes", _boom)
    with capture_logs() as logs:
        msg = _msg_836([{"id": "c", "permission_mode": "default"}])
    assert "_triggers:_" in msg and "unattended" not in msg
    assert any(e["event"] == "startup.unattended_line_failed" for e in logs)


@pytest.mark.anyio
async def test_versions_line_probes_backend_cli_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#993 (08 §3): the probe uses the backend's ``cli_cmd`` (agy for
    antigravity) while the label stays the engine id."""
    probed: list[str] = []

    def probe(cmd: str) -> str | None:
        probed.append(cmd)
        return {"agy": "1.3.2", "claude": "2.1.300"}.get(cmd)

    monkeypatch.setattr(telegram_backend, "_detect_cli_version", probe)
    line = await telegram_backend._build_versions_line(("antigravity", "claude"))
    assert "agy" in probed and "antigravity" not in probed
    assert line is not None
    assert line.endswith("antigravity 1.3.2 · claude 2.1.300")


@pytest.mark.anyio
async def test_versions_line_existing_engines_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []

    def probe(cmd: str) -> str | None:
        probed.append(cmd)
        return None

    monkeypatch.setattr(telegram_backend, "_detect_cli_version", probe)
    await telegram_backend._build_versions_line(
        ("claude", "codex", "opencode", "pi", "not-an-engine")
    )
    assert sorted(probed) == ["claude", "codex", "not-an-engine", "opencode", "pi"]
