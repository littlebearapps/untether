import contextlib
import json
import time
from datetime import UTC
from pathlib import Path
from typing import cast

import anyio
import pytest

import untether.runners.claude as claude_runner
from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners.claude import (
    ENGINE,
    ClaudeRunner,
    ClaudeStreamState,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema


def _load_fixture(
    name: str, *, session_id: str | None = None
) -> list[claude_schema.StreamJsonMessage]:
    path = Path(__file__).parent / "fixtures" / name
    events = [
        claude_schema.decode_stream_json_line(line)
        for line in path.read_bytes().splitlines()
        if line.strip()
    ]
    if session_id is None:
        return events
    return [
        event for event in events if getattr(event, "session_id", None) == session_id
    ]


def _decode_event(payload: dict) -> claude_schema.StreamJsonMessage:
    data_payload = dict(payload)
    data_payload.setdefault("uuid", "uuid")
    data_payload.setdefault("session_id", "session")
    match data_payload.get("type"):
        case "assistant":
            message = dict(data_payload.get("message", {}))
            message.setdefault("role", "assistant")
            message.setdefault("content", [])
            message.setdefault("model", "claude")
            data_payload["message"] = message
        case "user":
            message = dict(data_payload.get("message", {}))
            message.setdefault("role", "user")
            message.setdefault("content", [])
            data_payload["message"] = message
    data = json.dumps(data_payload).encode("utf-8")
    return claude_schema.decode_stream_json_line(data)


# ---------------------------------------------------------------------------
# #350 — pre-spawn RAM guard on the shared JsonlSubprocessRunner base class
# ---------------------------------------------------------------------------


def test_prespawn_ram_guard_blocks_when_below_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When MemAvailable < block_mb, _check_prespawn_ram_guard returns a
    CompletedEvent(ok=False) so run_impl yields it and returns early before
    spawning the subprocess."""
    runner = ClaudeRunner(claude_cmd="claude")

    # Stub mem_available_kb to return 100 MB (well below the 500 MB block)
    from untether.utils import proc_diag

    monkeypatch.setattr(proc_diag, "mem_available_kb", lambda: 100 * 1024)

    # Stub load_settings_if_exists to return default watchdog settings
    from untether import settings as settings_module
    from untether.settings import WatchdogSettings

    class _Fake:
        watchdog = WatchdogSettings()  # defaults: warn=2000, block=500

    monkeypatch.setattr(
        settings_module,
        "load_settings_if_exists",
        lambda: (_Fake(), tmp_path / "untether.toml"),
    )

    result = runner._check_prespawn_ram_guard(resume=None)
    assert result is not None
    assert result.ok is False
    assert "Insufficient RAM" in (result.error or "")
    assert "100 MB" in (result.error or "")
    assert "500 MB" in (result.error or "")


def test_prespawn_ram_guard_allows_when_above_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Healthy host → guard returns None → normal spawn proceeds."""
    runner = ClaudeRunner(claude_cmd="claude")

    from untether import settings as settings_module
    from untether.settings import WatchdogSettings
    from untether.utils import proc_diag

    monkeypatch.setattr(proc_diag, "mem_available_kb", lambda: 8 * 1024 * 1024)

    class _Fake:
        watchdog = WatchdogSettings()

    monkeypatch.setattr(
        settings_module,
        "load_settings_if_exists",
        lambda: (_Fake(), tmp_path / "untether.toml"),
    )

    assert runner._check_prespawn_ram_guard(resume=None) is None


def test_prespawn_ram_guard_disabled_when_both_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """warn=0 and block=0 disables the guard entirely — no /proc read."""
    runner = ClaudeRunner(claude_cmd="claude")

    from untether import settings as settings_module
    from untether.settings import WatchdogSettings
    from untether.utils import proc_diag

    called = {"mem": False}

    def _fail_if_called() -> int | None:
        called["mem"] = True
        return 10

    monkeypatch.setattr(proc_diag, "mem_available_kb", _fail_if_called)

    class _Fake:
        watchdog = WatchdogSettings(prespawn_ram_warn_mb=0, prespawn_ram_block_mb=0)

    monkeypatch.setattr(
        settings_module,
        "load_settings_if_exists",
        lambda: (_Fake(), tmp_path / "untether.toml"),
    )

    assert runner._check_prespawn_ram_guard(resume=None) is None
    assert called["mem"] is False  # guard short-circuited before proc read


def test_prespawn_ram_guard_warn_only_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Between block and warn thresholds → logged warning but guard returns
    None so the spawn proceeds."""
    runner = ClaudeRunner(claude_cmd="claude")

    from untether import settings as settings_module
    from untether.settings import WatchdogSettings
    from untether.utils import proc_diag

    monkeypatch.setattr(proc_diag, "mem_available_kb", lambda: 1500 * 1024)

    class _Fake:
        watchdog = WatchdogSettings()  # warn=2000, block=500

    monkeypatch.setattr(
        settings_module,
        "load_settings_if_exists",
        lambda: (_Fake(), tmp_path / "untether.toml"),
    )

    # 1500 < 2000 warn threshold, but >= 500 block — should warn, not block
    assert runner._check_prespawn_ram_guard(resume=None) is None


# ---------------------------------------------------------------------------
# #478 / #205 — claude runner.start log must NOT carry prompt content at INFO
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_runner_start_does_not_log_prompt_at_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#478: ClaudeRunner.run_impl emits ``runner.start`` at INFO with only
    ``prompt_len`` + ``args`` (no ``prompt`` field). The prompt preview
    moves to a DEBUG ``runner.start_prompt`` companion event so credentials
    or PII never surface at the broadly-accessible INFO tier (#205).
    Regression-locks the duplicate INFO call inside the claude override
    that was missed when the base runner was fixed.
    """
    from structlog.testing import capture_logs

    class _BoomManager:
        async def __aenter__(self) -> object:
            raise RuntimeError("stop_after_log")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_manage_subprocess(*args: object, **kwargs: object) -> _BoomManager:
        _ = args, kwargs
        return _BoomManager()

    monkeypatch.setattr(claude_runner, "manage_subprocess", fake_manage_subprocess)

    # Force control-channel mode (production default). Without a
    # permission_mode, build_args falls back to legacy ``-p <prompt>``
    # which puts the prompt into argv — covered separately below.
    runner = ClaudeRunner(claude_cmd="claude", permission_mode="acceptEdits")
    # Distinctive sentinel that won't collide with legitimate env var names
    # (e.g., GEMINI_API_KEY) which appear redacted in args=[...].
    sentinel = "ZAPHOD-PROMPT-SECRET-XYZZY-9876"
    secret_prompt = f"sensitive content: {sentinel} run my task"

    with capture_logs() as logs, contextlib.suppress(RuntimeError):
        async for _evt in runner.run_impl(secret_prompt, None):
            pass

    start_events = [r for r in logs if r.get("event") == "runner.start"]
    assert start_events, "runner.start INFO event must fire"
    for record in start_events:
        # Prompt content must NOT appear in the INFO log under any field name.
        assert "prompt" not in record, (
            f"runner.start at INFO leaked 'prompt' field: {record!r}"
        )
        assert "prompt_preview" not in record
        # But length should be there for ops visibility.
        assert record.get("prompt_len") == len(secret_prompt)
        # ``args`` is part of the base-runner contract — claude override
        # should mirror it so subprocess invocation is visible.
        assert "args" in record
        # The literal prompt sentinel must not appear anywhere in the record.
        assert sentinel not in str(record), (
            f"runner.start INFO leaked prompt sentinel: {record!r}"
        )
        # And `env -i KEY=VAL` pairs in args must be redacted (#361) so
        # secrets passed via env-wrap don't surface even when ``args`` is
        # logged. Spot-check on a known-redacted name from the env policy.
        args_str = str(record.get("args"))
        if "BWS_ACCESS_TOKEN" in args_str:
            assert "BWS_ACCESS_TOKEN=***" in args_str, (
                f"env -i pair should be redacted: {args_str}"
            )


@pytest.mark.anyio
async def test_runner_start_redacts_legacy_mode_prompt_in_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#478: in legacy ``-p <prompt>`` mode (no permission_mode set), the
    prompt sits as the last argv element after ``--``. The runner.start INFO
    log must redact at the ``--`` boundary so prompt content still doesn't
    reach INFO. Covers the path where _effective_permission_mode() is None.
    """
    from structlog.testing import capture_logs

    class _BoomManager:
        async def __aenter__(self) -> object:
            raise RuntimeError("stop_after_log")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_manage_subprocess(*args: object, **kwargs: object) -> _BoomManager:
        _ = args, kwargs
        return _BoomManager()

    monkeypatch.setattr(claude_runner, "manage_subprocess", fake_manage_subprocess)

    # No permission_mode → legacy ``-p`` path, prompt lands in argv.
    runner = ClaudeRunner(claude_cmd="claude")
    sentinel = "ZAPHOD-LEGACY-SECRET-XYZZY-9876"
    secret_prompt = f"top-secret legacy: {sentinel} run the task"

    with capture_logs() as logs, contextlib.suppress(RuntimeError):
        async for _evt in runner.run_impl(secret_prompt, None):
            pass

    start_events = [r for r in logs if r.get("event") == "runner.start"]
    assert start_events, "runner.start INFO event must fire"
    for record in start_events:
        # The literal prompt sentinel must NOT leak through args.
        assert sentinel not in str(record), (
            f"runner.start INFO leaked prompt sentinel via legacy args: {record!r}"
        )
        args = record.get("args") or []
        # Legacy mode appends ``--`` then the prompt; we replace the prompt
        # with a placeholder string so reviewers can still tell the run was
        # in legacy mode without exposing prompt content.
        assert "--" in args
        assert "<prompt redacted>" in args


# ---------------------------------------------------------------------------
# #347 — background-task tracking (Monitor / Bash-bg / Agent-bg /
# ScheduleWakeup / RemoteTrigger)
# ---------------------------------------------------------------------------


def _make_tool_use_event(
    name: str, tool_id: str, tool_input: dict | None = None
) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": tool_input or {},
                }
            ],
        },
    }


def _make_tool_result_event(tool_use_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "id": "msg_r",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "ok",
                    "is_error": False,
                }
            ],
        },
    }


def test_monitor_tool_registers_live_monitor() -> None:
    """Monitor with timeout_ms registers a dated entry in live_monitors."""
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("Monitor", "toolu_M1", {"timeout_ms": 60_000})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_M1" in state.live_monitors
    # Deadline should be in the future by ~60s
    assert state.live_monitors["toolu_M1"] > 0


def test_monitor_tool_clears_on_tool_result() -> None:
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("Monitor", "toolu_M1", {"timeout_ms": 60_000})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_M1" in state.live_monitors

    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_M1")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_M1" not in state.live_monitors


def test_bash_bg_registers_when_run_in_background_true() -> None:
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event(
                "Bash", "toolu_B1", {"command": "sleep 60", "run_in_background": True}
            )
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_B1" in state.live_bg_bashes


def test_bash_without_run_in_background_is_not_tracked() -> None:
    """A foreground Bash call must NOT land in live_bg_bashes — otherwise
    every Claude command would pollute the background set."""
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(_make_tool_use_event("Bash", "toolu_B2", {"command": "ls"})),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_B2" not in state.live_bg_bashes


def test_agent_bg_tracked_only_when_run_in_background() -> None:
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event(
                "Agent", "toolu_A1", {"task": "...", "run_in_background": True}
            )
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_A1" in state.live_bg_agents

    state2 = ClaudeStreamState()
    translate_claude_event(
        _decode_event(_make_tool_use_event("Agent", "toolu_A2", {"task": "..."})),
        title="claude",
        state=state2,
        factory=state2.factory,
    )
    assert "toolu_A2" not in state2.live_bg_agents


def test_schedule_wakeup_tracked_with_deadline() -> None:
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W1", {"delay_ms": 120_000})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_W1" in state.live_wakeups


def test_schedule_wakeup_reads_delaySeconds_field() -> None:
    """#481: real Claude Code stream-json emits ``delaySeconds`` (#289).

    Previous code only read ``delay_ms``/``timeout_ms`` so production
    deadlines fell to 0.0 (countdown rendering broken). Verify the new
    code path: a 60s wakeup yields a ``deadline`` ~60s in the future.
    """
    import time

    state = ClaudeStreamState()
    before = time.monotonic()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W2", {"delaySeconds": 60})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    after = time.monotonic()
    deadline = state.live_wakeups["toolu_W2"]
    # 60s wakeup → deadline between (before + 60) and (after + 60).
    assert before + 60.0 <= deadline <= after + 60.0


def test_schedule_wakeup_delay_ms_fallback_still_works() -> None:
    """Backward-compat: delay_ms fallback still produces a valid deadline."""
    import time

    state = ClaudeStreamState()
    before = time.monotonic()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W3", {"delay_ms": 30_000})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    deadline = state.live_wakeups["toolu_W3"]
    # 30s wakeup via delay_ms fallback.
    assert before + 30.0 <= deadline <= time.monotonic() + 30.0


def test_post_result_closing_state_initial_values() -> None:
    """#470: ClaudeStreamState carries the new closing-message signal fields."""
    state = ClaudeStreamState()
    assert state.post_result_closed_at is None
    assert state.post_result_idle_minutes == 0.0
    assert state.post_result_closing_sent is False


def test_remote_trigger_tracked_as_set_member() -> None:
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("RemoteTrigger", "toolu_R1", {"target": "other-chat"})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_R1" in state.live_remote_triggers


def test_has_live_background_work_empty() -> None:
    from untether.runners.claude import has_live_background_work

    state = ClaudeStreamState()
    assert has_live_background_work(state) is False


def test_has_live_background_work_with_bg_bash() -> None:
    from untether.runners.claude import has_live_background_work

    state = ClaudeStreamState()
    state.live_bg_bashes.add("toolu_X")
    assert has_live_background_work(state) is True


def test_has_live_background_work_expired_monitor() -> None:
    """A monitor whose deadline has passed should be treated as no longer live."""
    import time

    from untether.runners.claude import has_live_background_work

    state = ClaudeStreamState()
    # deadline 10s in the past
    state.live_monitors["toolu_expired"] = time.monotonic() - 10.0
    assert has_live_background_work(state) is False


def test_background_task_summary_formatting() -> None:
    from untether.runners.claude import background_task_summary

    state = ClaudeStreamState()
    assert background_task_summary(state) is None

    state.live_monitors["a"] = 0.0
    state.live_bg_bashes.add("b")
    summary = background_task_summary(state)
    assert summary is not None
    assert "⏳" in summary
    assert "1 watcher" in summary
    assert "1 bg task" in summary

    state.live_monitors["c"] = 0.0
    state.live_bg_agents.add("d")
    summary = background_task_summary(state)
    assert summary is not None
    assert "2 watchers" in summary
    assert "2 bg tasks" in summary


# ---------------------------------------------------------------------------
# #365 MCP catalog observability + proactive refresh
# ---------------------------------------------------------------------------


def _make_system_init_event(
    session_id: str,
    *,
    mcp_servers: list[dict] | None = None,
    tools: list[str] | None = None,
) -> dict:
    payload: dict = {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "uuid": "uuid",
        "cwd": "/tmp",
        "model": "claude-sonnet",
        "tools": tools or ["Bash", "Read"],
        "permissionMode": "default",
        "apiKeySource": "none",
    }
    if mcp_servers is not None:
        payload["mcp_servers"] = mcp_servers
    return payload


def test_catalog_init_snapshots_mcp_servers_when_all_connected() -> None:
    """All-connected system.init captures the snapshot but emits no warning."""
    from structlog.testing import capture_logs

    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-1")
    event = _decode_event(
        _make_system_init_event(
            "sess-1",
            mcp_servers=[
                {"name": "pal", "status": "connected"},
                {"name": "github", "status": "connected"},
            ],
        )
    )
    with capture_logs() as logs:
        translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )

    assert state.initial_mcp_servers == [
        {"name": "pal", "status": "connected"},
        {"name": "github", "status": "connected"},
    ]
    assert state.catalog_staleness_logged == set()
    assert [r for r in logs if r.get("event") == "catalog_staleness.detected"] == []


def test_catalog_init_logs_staleness_warning_for_non_connected() -> None:
    """Any non-``connected`` MCP at init emits a catalog_staleness WARNING."""
    from structlog.testing import capture_logs

    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-2")
    event = _decode_event(
        _make_system_init_event(
            "sess-2",
            mcp_servers=[
                {"name": "pal", "status": "connected"},
                {"name": "github", "status": "failed"},
                {"name": "jina", "status": "pending"},
            ],
        )
    )
    with capture_logs() as logs:
        translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )

    warnings = [r for r in logs if r.get("event") == "catalog_staleness.detected"]
    assert len(warnings) == 2
    by_server = {r["server"]: r for r in warnings}
    assert by_server["github"]["status"] == "failed"
    assert by_server["github"]["session_id"] == "sess-2"
    assert by_server["github"]["source"] == "system.init"
    assert by_server["jina"]["status"] == "pending"
    # "pal" connected must NOT appear
    assert "pal" not in by_server
    # Dedup set mirrors the emitted warnings
    assert ("sess-2", "github", "failed") in state.catalog_staleness_logged
    assert ("sess-2", "jina", "pending") in state.catalog_staleness_logged
    assert ("sess-2", "pal", "connected") not in state.catalog_staleness_logged


def test_catalog_staleness_dedups_repeated_init() -> None:
    """Re-fired init with same server+status only logs once per session."""
    from structlog.testing import capture_logs

    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-3")
    event = _decode_event(
        _make_system_init_event(
            "sess-3", mcp_servers=[{"name": "pal", "status": "error"}]
        )
    )
    with capture_logs() as logs:
        translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )
        translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )

    matches = [r for r in logs if r.get("event") == "catalog_staleness.detected"]
    assert len(matches) == 1


def test_catalog_staleness_disabled_emits_no_warning() -> None:
    """detect_catalog_staleness=False suppresses the warning entirely."""
    from structlog.testing import capture_logs

    state = ClaudeStreamState()
    state.detect_catalog_staleness = False
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-4")
    event = _decode_event(
        _make_system_init_event(
            "sess-4", mcp_servers=[{"name": "pal", "status": "failed"}]
        )
    )
    with capture_logs() as logs:
        translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )

    assert [r for r in logs if r.get("event") == "catalog_staleness.detected"] == []
    # Snapshot still captured — it's free and future-useful
    assert state.initial_mcp_servers == [{"name": "pal", "status": "failed"}]


def test_tool_result_queues_mcp_status_when_notify_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With notify_catalog_refresh on, each tool_result batch queues one request
    once the per-session debounce window has elapsed (#497)."""
    state = ClaudeStreamState()
    state.notify_catalog_refresh = True
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-5")

    # Drive monotonic time deterministically to skip past the 5s debounce.
    fake_now = [1000.0]
    monkeypatch.setattr(claude_runner.time, "monotonic", lambda: fake_now[0])

    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_1")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(state.pending_catalog_refresh_ids) == 1
    assert state.pending_catalog_refresh_ids[0].startswith("ut_catalog_refresh_sess-5_")

    fake_now[0] += state.catalog_refresh_min_interval_s + 0.1
    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_2")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    # Second batch queues a second distinct ID after debounce elapses
    assert len(state.pending_catalog_refresh_ids) == 2
    assert state.pending_catalog_refresh_ids[0] != state.pending_catalog_refresh_ids[1]


def test_tool_result_debounces_back_to_back_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#497: rapid-fire tool_results within the debounce window yield ONE refresh.

    Reproduces the conditions of the 'scout' storm (count=183) — without the
    debounce, every tool_result batch queues a fresh request. With it, only
    the first batch in each interval window fires.
    """
    state = ClaudeStreamState()
    state.notify_catalog_refresh = True
    state.catalog_refresh_min_interval_s = 5.0
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-debounce")

    fake_now = [2000.0]
    monkeypatch.setattr(claude_runner.time, "monotonic", lambda: fake_now[0])

    # Fire 10 tool_result batches 100 ms apart — all within the 5 s window.
    for i in range(10):
        translate_claude_event(
            _decode_event(_make_tool_result_event(f"toolu_burst_{i}")),
            title="claude",
            state=state,
            factory=state.factory,
        )
        fake_now[0] += 0.1

    assert len(state.pending_catalog_refresh_ids) == 1, (
        f"Expected 1 refresh queued under debounce, got "
        f"{len(state.pending_catalog_refresh_ids)} — debounce broken"
    )
    first_ts = state.last_catalog_refresh_queued_at
    assert first_ts == 2000.0

    # Advance past the window — the next tool_result fires a second refresh.
    fake_now[0] = 2000.0 + 5.0 + 0.05
    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_after_window")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(state.pending_catalog_refresh_ids) == 2
    assert state.last_catalog_refresh_queued_at == 2005.05


def test_tool_result_debounce_disabled_with_zero_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#497: catalog_refresh_min_interval_s = 0 restores pre-debounce behaviour."""
    state = ClaudeStreamState()
    state.notify_catalog_refresh = True
    state.catalog_refresh_min_interval_s = 0.0
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-no-debounce")

    fake_now = [3000.0]
    monkeypatch.setattr(claude_runner.time, "monotonic", lambda: fake_now[0])

    for i in range(5):
        translate_claude_event(
            _decode_event(_make_tool_result_event(f"toolu_z_{i}")),
            title="claude",
            state=state,
            factory=state.factory,
        )
        fake_now[0] += 0.01

    assert len(state.pending_catalog_refresh_ids) == 5


def test_tool_result_does_not_queue_when_notify_disabled() -> None:
    """Default notify_catalog_refresh=False produces no queued requests."""
    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-6")

    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_1")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.pending_catalog_refresh_ids == []


def test_tool_result_without_resume_skips_queue() -> None:
    """Factory with no resume → no request_id can be minted → queue stays empty."""
    state = ClaudeStreamState()
    state.notify_catalog_refresh = True
    # factory.resume deliberately None — defensive: tool_result before init

    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_1")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.pending_catalog_refresh_ids == []


@pytest.mark.anyio
async def test_drain_catalog_refresh_sends_mcp_status_jsonl() -> None:
    """_drain_catalog_refresh serialises one control_request per queued ID."""

    class _FakeStdin:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        async def send(self, payload: bytes) -> None:
            self.sent.append(payload)

    runner = ClaudeRunner(claude_cmd="claude")
    state = ClaudeStreamState()
    state.pending_catalog_refresh_ids = [
        "ut_catalog_refresh_s_1",
        "ut_catalog_refresh_s_2",
    ]

    fake_stdin = _FakeStdin()
    await runner._drain_catalog_refresh(state, stdin=fake_stdin)

    assert len(fake_stdin.sent) == 2
    for payload in fake_stdin.sent:
        msg = json.loads(payload.decode().rstrip("\n"))
        assert msg["type"] == "control_request"
        assert msg["request"]["subtype"] == "mcp_status"
        assert msg["request_id"].startswith("ut_catalog_refresh_")
    # Queue is cleared after drain
    assert state.pending_catalog_refresh_ids == []


@pytest.mark.anyio
async def test_drain_catalog_refresh_no_op_when_queue_empty() -> None:
    """Empty queue → no stdin writes, no log calls."""

    class _FakeStdin:
        def __init__(self) -> None:
            self.send_count = 0

        async def send(self, payload: bytes) -> None:
            self.send_count += 1

    runner = ClaudeRunner(claude_cmd="claude")
    state = ClaudeStreamState()
    fake_stdin = _FakeStdin()
    await runner._drain_catalog_refresh(state, stdin=fake_stdin)
    assert fake_stdin.send_count == 0


@pytest.mark.anyio
async def test_drain_catalog_refresh_handles_closed_pipe() -> None:
    """Closed stdin logs a warning and clears the queue instead of crashing."""

    class _ClosedStdin:
        async def send(self, payload: bytes) -> None:
            raise anyio.ClosedResourceError()

    runner = ClaudeRunner(claude_cmd="claude")
    state = ClaudeStreamState()
    state.pending_catalog_refresh_ids = ["ut_catalog_refresh_s_1"]

    await runner._drain_catalog_refresh(state, stdin=_ClosedStdin())
    # Queue cleared (drain doesn't retry); no exception propagates
    assert state.pending_catalog_refresh_ids == []


def test_new_state_propagates_watchdog_settings(monkeypatch) -> None:
    """ClaudeRunner.new_state pulls catalog settings from WatchdogSettings."""
    from untether import settings as settings_module
    from untether.settings import WatchdogSettings

    class _Fake:
        watchdog = WatchdogSettings(
            detect_catalog_staleness=False,
            notify_catalog_refresh=True,
        )

    monkeypatch.setattr(
        settings_module,
        "load_settings_if_exists",
        lambda: (_Fake(), Path("untether.toml")),
    )
    monkeypatch.setattr(
        claude_runner,
        "load_settings_if_exists",
        lambda: (_Fake(), Path("untether.toml")),
    )

    runner = ClaudeRunner(claude_cmd="claude")
    state = runner.new_state("prompt", None)
    assert state.detect_catalog_staleness is False
    assert state.notify_catalog_refresh is True


def test_claude_resume_format_and_extract() -> None:
    runner = ClaudeRunner(claude_cmd="claude")
    token = ResumeToken(engine=ENGINE, value="sid")

    assert runner.format_resume(token) == "`claude --resume sid`"
    assert runner.extract_resume("`claude --resume sid`") == token
    assert runner.extract_resume("claude -r other") == ResumeToken(
        engine=ENGINE, value="other"
    )
    assert runner.extract_resume("`codex resume sid`") is None


def test_build_runner_uses_shutil_which(monkeypatch) -> None:
    expected = r"C:\Tools\claude.cmd"
    called: dict[str, str] = {}

    def fake_which(name: str) -> str | None:
        called["name"] = name
        return expected

    monkeypatch.setattr(claude_runner.shutil, "which", fake_which)
    runner = cast(ClaudeRunner, claude_runner.build_runner({}, Path("untether.toml")))

    assert called["name"] == "claude"
    assert runner.claude_cmd == expected


def test_translate_success_fixture() -> None:
    state = ClaudeStreamState()
    events: list = []
    for event in _load_fixture(
        "claude_stream_json_session.jsonl",
        session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ):
        events.extend(
            translate_claude_event(
                event,
                title="claude",
                state=state,
                factory=state.factory,
            )
        )

    assert isinstance(events[0], StartedEvent)
    started = next(evt for evt in events if isinstance(evt, StartedEvent))

    action_events = [evt for evt in events if isinstance(evt, ActionEvent)]
    assert len(action_events) == 4

    started_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "started"
    }
    assert (
        started_actions[("toolu_01BASH_LS_EXAMPLE", "started")].action.kind == "command"
    )
    write_action = started_actions[("toolu_02", "started")].action
    assert write_action.kind == "file_change"
    assert write_action.detail["changes"][0]["path"] == "notes.md"

    completed_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "completed"
    }
    assert completed_actions[("toolu_01BASH_LS_EXAMPLE", "completed")].ok is True
    assert completed_actions[("toolu_02", "completed")].ok is True

    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert events[-1] == completed
    assert completed.ok is True
    assert completed.resume == started.resume
    assert completed.answer == "I see README.md, pyproject.toml, and src/."


def test_translate_error_fixture_permission_denials() -> None:
    state = ClaudeStreamState()
    events: list = []
    for event in _load_fixture(
        "claude_stream_json_session.jsonl",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ):
        events.extend(
            translate_claude_event(
                event,
                title="claude",
                state=state,
                factory=state.factory,
            )
        )

    started = next(evt for evt in events if isinstance(evt, StartedEvent))
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is False
    assert completed.error is not None
    assert "Claude Code run failed" in completed.error
    assert completed.resume == started.resume


def test_tool_results_pop_pending_actions() -> None:
    state = ClaudeStreamState()

    tool_use_event = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "echo hi"},
                }
            ],
        },
    }
    tool_result_event = {
        "type": "user",
        "message": {
            "id": "msg_2",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "ok",
                    "is_error": False,
                }
            ],
        },
    }

    translate_claude_event(
        _decode_event(tool_use_event),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_1" in state.pending_actions

    translate_claude_event(
        _decode_event(tool_result_event),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert not state.pending_actions


def test_translate_rate_limit_event_surfaces_as_action() -> None:
    """#349: rate_limit_event JSONL translates to a visible action event.

    Previously this event fell through to the empty-list default, so the user
    saw silent inactivity on Telegram while Claude Code waited for the
    Anthropic API to unthrottle. Now it renders as a progress note with a
    ⏳ prefix and the retry-after hint.
    """
    state = ClaudeStreamState()
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "tokens_limit": 1_000_000,
            "tokens_remaining": 0,
            "retry_after_ms": 47_000,
        },
    }
    events = translate_claude_event(
        _decode_event(event),
        title="claude",
        state=state,
        factory=state.factory,
    )
    # started + completed so the progress bar shows a finished note
    assert len(events) == 2
    assert all(isinstance(e, ActionEvent) for e in events)
    assert events[0].phase == "started"
    assert events[1].phase == "completed"
    assert events[0].action.kind == "note"
    assert "⏳" in events[0].action.title
    assert "retrying in 47s" in events[0].action.title
    assert events[1].ok is True
    assert events[1].level == "info"
    # Cumulative counter feeds the future footer annotation / /stats surface
    assert state.rate_limit_count == 1
    assert state.rate_limit_total_s == 47.0
    # Detail dict preserves the raw retry_after_ms for callers that want it
    assert events[0].action.detail.get("retry_after_ms") == 47_000
    assert events[0].action.detail.get("tokens_remaining") == 0


def test_translate_rate_limit_event_accumulates_across_throttles() -> None:
    """Multiple rate_limit_events in one session accumulate into a single total."""
    state = ClaudeStreamState()
    for retry_ms in (10_000, 30_000, 5_000):
        translate_claude_event(
            _decode_event(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {"retry_after_ms": retry_ms},
                }
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
    assert state.rate_limit_count == 3
    assert state.rate_limit_total_s == 45.0


def test_translate_rate_limit_event_handles_missing_retry() -> None:
    """Rate-limit without a retry hint still surfaces, just without the seconds."""
    state = ClaudeStreamState()
    events = translate_claude_event(
        _decode_event({"type": "rate_limit_event"}),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(events) == 2
    assert "⏳" in events[0].action.title
    assert "waiting to retry" in events[0].action.title
    # cumulative stays at 0 when we have no retry_after_ms to accrue
    assert state.rate_limit_count == 1
    assert state.rate_limit_total_s == 0.0


def test_translate_rate_limit_event_derives_retry_after_from_reset_ts() -> None:
    """#518: when `retry_after_ms` is missing but `requests_reset` is present
    as an ISO timestamp, derive `retry_after_s` from the reset window. This
    is the subscription-cap pattern the rc13 audit observed — bare events
    that left users with no actionable wait time."""
    from datetime import datetime, timedelta

    state = ClaudeStreamState()
    # Reset 90 seconds from now
    reset_ts = (datetime.now(UTC) + timedelta(seconds=90)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    events = translate_claude_event(
        _decode_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"requests_reset": reset_ts},
            }
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(events) == 2
    # Should now show "retrying in Ns" (not the generic "waiting to retry"),
    # and cumulative should accumulate the derived seconds.
    assert "retrying in" in events[0].action.title
    assert state.rate_limit_count == 1
    # Allow ±2s wiggle for clock drift between the test's setup and translate
    assert 88 <= state.rate_limit_total_s <= 92, (
        f"Expected ~90s cumulative, got {state.rate_limit_total_s}"
    )


def test_translate_rate_limit_event_prefers_earlier_reset_when_both_present() -> None:
    """#518: when both `requests_reset` and `tokens_reset` are present, derive
    from the EARLIER of the two — the rate limit lifts as soon as either
    budget refills."""
    from datetime import datetime, timedelta

    state = ClaudeStreamState()
    now = datetime.now(UTC)
    earlier = (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (now + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = translate_claude_event(
        _decode_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "requests_reset": later,
                    "tokens_reset": earlier,
                },
            }
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(events) == 2
    # Should pick ~30s, not ~600s
    assert 28 <= state.rate_limit_total_s <= 32, (
        f"Expected ~30s (earlier reset), got {state.rate_limit_total_s}"
    )


def test_translate_rate_limit_event_retry_after_ms_takes_precedence() -> None:
    """#518: explicit `retry_after_ms` is preferred over derived reset_ts so we
    don't double-account or override the upstream value."""
    from datetime import datetime, timedelta

    state = ClaudeStreamState()
    # retry_after_ms says 10s, reset_ts says 60s — we should use 10s
    later = (datetime.now(UTC) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = translate_claude_event(
        _decode_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "retry_after_ms": 10_000,
                    "requests_reset": later,
                },
            }
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(events) == 2
    assert state.rate_limit_total_s == 10.0


def test_translate_rate_limit_event_handles_unparseable_reset_ts() -> None:
    """#518: garbage `requests_reset` is silently ignored — we fall back to the
    "waiting to retry" copy rather than crashing the runner."""
    state = ClaudeStreamState()
    events = translate_claude_event(
        _decode_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"requests_reset": "not-a-timestamp"},
            }
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(events) == 2
    assert "waiting to retry" in events[0].action.title
    assert state.rate_limit_total_s == 0.0


def test_translate_thinking_block() -> None:
    state = ClaudeStreamState()
    event = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Consider the options.",
                    "signature": "sig",
                }
            ],
        },
    }

    events = translate_claude_event(
        _decode_event(event),
        title="claude",
        state=state,
        factory=state.factory,
    )

    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "completed"
    assert events[0].action.kind == "note"
    assert events[0].action.title == "Consider the options."
    assert events[0].ok is True


# ---------------------------------------------------------------------------
# #489 — server_tool_use + advisor_tool_result content blocks (regression)
# ---------------------------------------------------------------------------


def test_translate_server_tool_use_block() -> None:
    """server_tool_use shares the tool_use translation path: emits an
    action_started, populates state.pending_actions, and stamps
    state.last_tool_use_id. Regression for #489 — previously msgspec
    rejected the whole JSONL line and the event was silently dropped."""
    state = ClaudeStreamState()
    event = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "stu_01",
                    "name": "web_search",
                    "input": {"query": "untether telegram"},
                }
            ],
        },
    }

    events = translate_claude_event(
        _decode_event(event),
        title="claude",
        state=state,
        factory=state.factory,
    )

    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "started"
    assert events[0].action.id == "stu_01"
    assert "stu_01" in state.pending_actions
    assert state.last_tool_use_id == "stu_01"


def test_translate_exitplanmode_captures_plan_body() -> None:
    """#508 — translating a tool_use(name='ExitPlanMode', input.plan='...')
    captures the plan body onto state.last_exitplanmode_plan so the bridge
    can re-emit it in the final answer if the post-approval result is
    brief.  Regression for the live research-task short-final-message bug.
    """
    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine="claude", value="sess-508")
    plan_body = (
        "Findings:\n"
        "- File X has bug Y at line 42\n"
        "- File Z is unaffected\n"
        "- Recommend fix A\n"
    )
    event = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_epm_1",
                    "name": "ExitPlanMode",
                    "input": {"plan": plan_body},
                }
            ],
        },
    }

    translate_claude_event(
        _decode_event(event),
        title="claude",
        state=state,
        factory=state.factory,
    )

    assert state.last_exitplanmode_plan == plan_body


def test_translate_exitplanmode_ignores_empty_plan_body() -> None:
    """#508 — empty/whitespace-only plan bodies are NOT captured. Avoids
    overwriting a real prior value with an inadvertent retry/empty call."""
    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine="claude", value="sess-508")
    state.last_exitplanmode_plan = "earlier plan body"
    event = {
        "type": "assistant",
        "message": {
            "id": "msg_2",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_epm_2",
                    "name": "ExitPlanMode",
                    "input": {"plan": "   "},
                }
            ],
        },
    }

    translate_claude_event(
        _decode_event(event),
        title="claude",
        state=state,
        factory=state.factory,
    )

    assert state.last_exitplanmode_plan == "earlier plan body"


def test_translate_result_prepends_exitplanmode_plan_into_answer() -> None:
    """#510: the ExitPlanMode plan body re-emit happens HERE on the per-stream
    result path (claude.py), not in runner_bridge against the singleton
    runner.current_stream. Verifies the prepend uses state.last_exitplanmode_plan
    from the SAME state instance that received the result event.
    """
    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine="claude", value="sess-510")
    state.last_exitplanmode_plan = "- Finding 1\n- Finding 2\n- Recommend X"
    short_post_approval_result = "Plan approved — see file."

    event = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=2,
        session_id="sess-510",
        result=short_post_approval_result,
    )
    events = translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    completed = [evt for evt in events if isinstance(evt, CompletedEvent)]
    assert len(completed) == 1
    answer = completed[0].answer
    assert "📋 Plan (approved):" in answer
    assert "- Finding 1" in answer
    assert short_post_approval_result in answer
    # Plan body comes before the brief post-approval text
    assert answer.index("- Finding 1") < answer.index(short_post_approval_result)


def test_concurrent_states_do_not_leak_exitplanmode_plan_bodies() -> None:
    """#510 regression — the live bug. Two concurrent Claude sessions
    each had their own ClaudeStreamState. Previously the bridge read the
    plan body from ``runner.current_stream`` (a shared singleton on the
    runner), which was overwritten when either session re-entered
    run_impl. The fix routes the prepend through the per-stream
    translate path, so each state can ONLY ever read its own plan body.

    Models the production incident: chat A captured "PLAN — CHANNELO
    TUNNEL" on its state, chat B was completing a different task with
    its own short answer — chat B's CompletedEvent must NOT contain
    chat A's plan body.
    """
    state_a = ClaudeStreamState()
    state_a.factory._resume = ResumeToken(engine="claude", value="sess-A")
    state_a.last_exitplanmode_plan = "PLAN — CHANNELO TUNNEL secret content"

    state_b = ClaudeStreamState()
    state_b.factory._resume = ResumeToken(engine="claude", value="sess-B")
    # state_b has its own (smaller) plan body — different content
    state_b.last_exitplanmode_plan = "PLAN — legal-DB handover"

    # Session B completes with a brief post-approval result.
    event_b = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=2,
        session_id="sess-B",
        result="done",
    )
    events_b = translate_claude_event(
        event_b,
        title="claude",
        state=state_b,
        factory=state_b.factory,
    )
    completed_b = next(evt for evt in events_b if isinstance(evt, CompletedEvent))

    # Session B's answer must only contain its own plan body.
    assert "PLAN — legal-DB handover" in completed_b.answer
    assert "CHANNELO TUNNEL" not in completed_b.answer
    assert "secret content" not in completed_b.answer


def test_translate_result_error_does_not_prepend_plan(monkeypatch) -> None:
    """#510: only the OK path prepends. Errored result paths flow into
    _extract_error and must not also receive a plan-body prepend.
    """
    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine="claude", value="sess-510-err")
    state.last_exitplanmode_plan = "- Should not appear"

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=100,
        duration_api_ms=50,
        is_error=True,
        num_turns=1,
        session_id="sess-510-err",
    )
    events = translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert "📋 Plan (approved):" not in (completed.answer or "")
    assert "- Should not appear" not in (completed.answer or "")


def test_translate_result_skips_prepend_when_answer_substantive() -> None:
    """#515: when the post-approval text is already a substantive
    CLI-style summary (≥ ``_PREPEND_LENGTH_GATE`` chars), Layer E must
    NOT prepend the plan body. Without this gate the rc11/rc12 fix
    concatenated plan body + paraphrased summary on every well-behaved
    run, producing 25k-42k char Telegram finals on staging.
    """
    from untether.runners.claude import _PREPEND_LENGTH_GATE

    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine="claude", value="sess-515")
    state.last_exitplanmode_plan = "- Plan finding 1\n- Plan finding 2"

    # A real CLI-style summary, just above the gate. Claude paraphrases
    # rather than literal-copies, so the substring check would fail —
    # the length gate is what stops the double-ship.
    summary = (
        "Investigation complete. Here is what I found:\n\n"
        "- Module X had a regression introduced in commit abc123\n"
        "- The root cause was a missing null guard in the parser\n"
        "- Rolled back the change and added a regression test\n"
        "- Next step: backfill the affected rows on Monday morning\n\n"
        "Decisions made: kept the legacy code path for one more release cycle\n"
        "to give downstream consumers time to migrate; full removal scheduled\n"
        "for the next minor version once telemetry confirms zero active\n"
        "callers. Telegram message size budget respected (under 1500 chars).\n\n"
        "Next steps: open a follow-up issue to track the backfill timeline,\n"
        "send a heads-up in the team channel about the rollback, and re-run\n"
        "the daily-audit cron tomorrow morning to confirm the regression has\n"
        "cleared the verification window before closing this thread.\n"
    )
    assert len(summary) >= _PREPEND_LENGTH_GATE

    event = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=2,
        session_id="sess-515",
        result=summary,
    )
    events = translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.answer == summary
    assert "📋 Plan (approved):" not in completed.answer


def test_translate_result_caps_long_plan_body_when_prepending() -> None:
    """#515: when Layer E does fire (short post-approval answer), an
    over-long captured plan body must be truncated to
    ``_PREPEND_BODY_CAP`` chars + a truncation marker. Without this cap
    a 30k-char plan body still ships a 30k-char Telegram final even
    after the length gate is added.
    """
    from untether.runners.claude import _PREPEND_BODY_CAP

    state = ClaudeStreamState()
    state.factory._resume = ResumeToken(engine="claude", value="sess-515-cap")
    state.last_exitplanmode_plan = "x" * (_PREPEND_BODY_CAP + 2000)

    event = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=2,
        session_id="sess-515-cap",
        result="ok",
    )
    events = translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert "📋 Plan (approved):" in completed.answer
    assert "plan truncated" in completed.answer
    # Final answer should not contain the full 3500-char plan body.
    assert "x" * (_PREPEND_BODY_CAP + 100) not in completed.answer


def test_translate_advisor_tool_result_block() -> None:
    """advisor_tool_result shares the tool_result translation path: emits an
    action_completed and pops the matching entry from state.pending_actions.
    Regression for #489."""
    state = ClaudeStreamState()
    # Inject a pending action keyed on the tool_use_id (mirrors what would
    # have been registered by the prior server_tool_use / tool_use call).
    from untether.model import Action

    state.pending_actions["adv_01"] = Action(
        id="adv_01",
        kind="tool",
        title="advisor",
        detail={},
    )

    event = {
        "type": "user",
        "message": {
            "id": "msg_r",
            "content": [
                {
                    "type": "advisor_tool_result",
                    "tool_use_id": "adv_01",
                    "content": "Reviewer said: looks good.",
                    "is_error": False,
                }
            ],
        },
    }

    events = translate_claude_event(
        _decode_event(event),
        title="claude",
        state=state,
        factory=state.factory,
    )

    assert any(
        isinstance(e, ActionEvent)
        and e.phase == "completed"
        and e.action.id == "adv_01"
        for e in events
    )
    assert "adv_01" not in state.pending_actions


@pytest.mark.anyio
async def test_run_serializes_same_session() -> None:
    runner = ClaudeRunner(claude_cmd="claude")
    gate = anyio.Event()
    in_flight = 0
    max_in_flight = 0

    async def run_stub(*_args, **_kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await gate.wait()
            yield CompletedEvent(
                engine=ENGINE,
                resume=ResumeToken(engine=ENGINE, value="sid"),
                ok=True,
                answer="ok",
            )
        finally:
            in_flight -= 1

    runner.run_impl = run_stub  # type: ignore[assignment]

    async def drain(prompt: str, resume: ResumeToken | None) -> None:
        async for _event in runner.run(prompt, resume):
            pass

    token = ResumeToken(engine=ENGINE, value="sid")
    async with anyio.create_task_group() as tg:
        tg.start_soon(drain, "a", token)
        tg.start_soon(drain, "b", token)
        await anyio.lowlevel.checkpoint()
        gate.set()
    assert max_in_flight == 1


@pytest.mark.anyio
async def test_run_serializes_new_session_after_session_is_known(
    tmp_path, monkeypatch
) -> None:
    gate_path = tmp_path / "gate"
    resume_marker = tmp_path / "resume_started"
    session_id = "session_01"

    claude_path = tmp_path / "claude"
    claude_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "\n"
        "gate = os.environ['CLAUDE_TEST_GATE']\n"
        "resume_marker = os.environ['CLAUDE_TEST_RESUME_MARKER']\n"
        "session_id = os.environ['CLAUDE_TEST_SESSION_ID']\n"
        "\n"
        "init = {\n"
        "    'type': 'system',\n"
        "    'subtype': 'init',\n"
        "    'uuid': 'uuid',\n"
        "    'session_id': session_id,\n"
        "    'apiKeySource': 'env',\n"
        "    'cwd': '.',\n"
        "    'tools': [],\n"
        "    'mcp_servers': [],\n"
        "    'model': 'claude',\n"
        "    'permissionMode': 'default',\n"
        "    'slash_commands': [],\n"
        "    'output_style': 'default',\n"
        "}\n"
        "\n"
        "args = sys.argv[1:]\n"
        "if '--resume' in args or '-r' in args:\n"
        "    print(json.dumps(init), flush=True)\n"
        "    with open(resume_marker, 'w', encoding='utf-8') as f:\n"
        "        f.write('started')\n"
        "        f.flush()\n"
        "    sys.exit(0)\n"
        "\n"
        "print(json.dumps(init), flush=True)\n"
        "while not os.path.exists(gate):\n"
        "    time.sleep(0.001)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    claude_path.chmod(0o755)

    monkeypatch.setenv("CLAUDE_TEST_GATE", str(gate_path))
    monkeypatch.setenv("CLAUDE_TEST_RESUME_MARKER", str(resume_marker))
    monkeypatch.setenv("CLAUDE_TEST_SESSION_ID", session_id)

    runner = ClaudeRunner(claude_cmd=str(claude_path))

    session_started = anyio.Event()
    resume_value: str | None = None
    new_done = anyio.Event()

    async def run_new() -> None:
        nonlocal resume_value
        async for event in runner.run("hello", None):
            if isinstance(event, StartedEvent):
                resume_value = event.resume.value
                session_started.set()
        new_done.set()

    async def run_resume() -> None:
        assert resume_value is not None
        async for _event in runner.run(
            "resume", ResumeToken(engine=ENGINE, value=resume_value)
        ):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_new)
        await session_started.wait()

        tg.start_soon(run_resume)
        await anyio.sleep(0.01)

        assert not resume_marker.exists()

        gate_path.write_text("go", encoding="utf-8")
        await new_done.wait()

        with anyio.fail_after(2):
            while not resume_marker.exists():
                await anyio.sleep(0.001)


@pytest.mark.anyio
async def test_run_strips_anthropic_api_key_by_default(tmp_path, monkeypatch) -> None:
    claude_path = tmp_path / "claude"
    claude_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "\n"
        "session_id = 'session_01'\n"
        "status = 'set' if os.environ.get('ANTHROPIC_API_KEY') else 'unset'\n"
        "init = {\n"
        "    'type': 'system',\n"
        "    'subtype': 'init',\n"
        "    'uuid': 'uuid',\n"
        "    'session_id': session_id,\n"
        "    'apiKeySource': 'env',\n"
        "    'cwd': '.',\n"
        "    'tools': [],\n"
        "    'mcp_servers': [],\n"
        "    'model': 'claude',\n"
        "    'permissionMode': 'default',\n"
        "    'slash_commands': [],\n"
        "    'output_style': 'default',\n"
        "}\n"
        "print(json.dumps(init), flush=True)\n"
        "result = {\n"
        "    'type': 'result',\n"
        "    'subtype': 'success',\n"
        "    'uuid': 'uuid',\n"
        "    'session_id': session_id,\n"
        "    'duration_ms': 0,\n"
        "    'duration_api_ms': 0,\n"
        "    'is_error': False,\n"
        "    'num_turns': 1,\n"
        "    'result': f'api={status}',\n"
        "    'total_cost_usd': 0.0,\n"
        "    'usage': {'input_tokens': 0, 'output_tokens': 0},\n"
        "    'modelUsage': {},\n"
        "    'permission_denials': [],\n"
        "}\n"
        "print(json.dumps(result), flush=True)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    claude_path.chmod(0o755)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    runner = ClaudeRunner(claude_cmd=str(claude_path))
    answer: str | None = None
    async for event in runner.run("hello", None):
        if isinstance(event, CompletedEvent):
            answer = event.answer
    assert answer == "api=unset"

    runner_api = ClaudeRunner(claude_cmd=str(claude_path), use_api_billing=True)
    answer = None
    async for event in runner_api.run("hello", None):
        if isinstance(event, CompletedEvent):
            answer = event.answer
    assert answer == "api=set"


def test_env_sets_untether_session() -> None:
    """env() sets UNTETHER_SESSION=1 for Claude Code hook detection."""
    runner = ClaudeRunner(claude_cmd="claude")
    env = runner.env(state=None)
    assert env is not None
    assert env["UNTETHER_SESSION"] == "1"

    # Also set when using API billing
    runner_api = ClaudeRunner(claude_cmd="claude", use_api_billing=True)
    env_api = runner_api.env(state=None)
    assert env_api is not None
    assert env_api["UNTETHER_SESSION"] == "1"


def test_env_stream_idle_timeout_default_is_300s(monkeypatch) -> None:
    """#342: the default CLAUDE_STREAM_IDLE_TIMEOUT_MS must be 300000ms (5 min).

    60000ms (the value shipped in #322 / PR #323) tripped the upstream
    stream watchdog mid-reasoning on opus/max runs, aborting the run with
    "API Error: Stream idle timeout". 300000ms matches the undici idle-body
    timeout that motivated #322 and Untether's own
    stuck_after_tool_result_timeout default, so legitimate long-thinking
    windows no longer false-positive.
    """
    # Clear any shell-set value so we measure setdefault behaviour.
    monkeypatch.delenv("CLAUDE_STREAM_IDLE_TIMEOUT_MS", raising=False)
    runner = ClaudeRunner(claude_cmd="claude")
    env = runner.env(state=None)
    assert env is not None
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "300000"
    # Watchdog stays on; only the idle threshold changed.
    assert env["CLAUDE_ENABLE_STREAM_WATCHDOG"] == "1"


def test_env_stream_idle_timeout_user_override_wins(monkeypatch) -> None:
    """Shell-set CLAUDE_STREAM_IDLE_TIMEOUT_MS wins over the Untether default."""
    monkeypatch.setenv("CLAUDE_STREAM_IDLE_TIMEOUT_MS", "600000")
    runner = ClaudeRunner(claude_cmd="claude")
    env = runner.env(state=None)
    assert env is not None
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "600000"


def test_rate_limit_event_decodes_correctly() -> None:
    """rate_limit_event decodes to StreamRateLimitMessage.

    Translation behaviour is covered in detail by
    test_translate_rate_limit_event_* — this test only locks in the
    msgspec schema tag mapping. Prior to #349 this function also
    asserted that translation returned an empty list; that behaviour
    was a UX bug (silent API-wait) and has been replaced with a
    visible ⏳ action note.
    """
    event = _decode_event({"type": "rate_limit_event"})
    assert isinstance(event, claude_schema.StreamRateLimitMessage)


# ===========================================================================
# _extract_error enrichment
# ===========================================================================


def test_extract_error_includes_diagnostic_context() -> None:
    """_extract_error builds multi-line diagnostic with session, turns, cost."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=5000,
        duration_api_ms=3000,
        is_error=True,
        num_turns=2,
        session_id="abcdef1234567890",
        total_cost_usd=0.15,
    )
    result = _extract_error(event, resumed=True)
    assert result is not None
    assert "error_during_execution" in result
    assert "abcdef12" in result
    assert "resumed" in result
    assert "turns: 2" in result
    assert "$0.15" in result
    assert "3000ms" in result


def test_extract_error_new_session() -> None:
    """_extract_error shows 'new' for non-resumed sessions."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=1000,
        duration_api_ms=500,
        is_error=True,
        num_turns=0,
        session_id="sess123456789012",
    )
    result = _extract_error(event, resumed=False)
    assert result is not None
    assert "new" in result
    assert "turns: 0" in result


def test_extract_error_not_error() -> None:
    """_extract_error returns None for non-error results."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=500,
        is_error=False,
        num_turns=1,
        session_id="sess123456789012",
    )
    assert _extract_error(event) is None


def test_extract_error_with_result_text() -> None:
    """_extract_error uses result text as first line when available."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=1000,
        duration_api_ms=500,
        is_error=True,
        num_turns=0,
        session_id="sess123456789012",
        result="Context window limit reached",
    )
    result = _extract_error(event, resumed=False)
    assert result is not None
    assert result.startswith("Context window limit reached")


# ===========================================================================
# #438 — Stream idle timeout Type-A vs Type-B classification
# ===========================================================================


def test_extract_error_type_a_stream_idle_timeout() -> None:
    """Mid-generation stall: num_turns >= 1 and duration_api_ms > 0.
    Surface as Type A with hint to raise the timeout."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=635000,
        duration_api_ms=261086,
        is_error=True,
        num_turns=19,
        session_id="36693744aaaa0000",
        result="API Error: Stream idle timeout - partial response received",
    )
    result = _extract_error(event, resumed=False)
    assert result is not None
    assert "Type A" in result
    assert "Mid-generation" in result
    assert "claude_stream_idle_timeout_ms" in result
    # Type-B language must NOT appear.
    assert "Type B" not in result
    assert "no bytes" not in result.lower()


def test_extract_error_type_b_stream_idle_timeout_zero_bytes() -> None:
    """Cold-start zero-byte stall: num_turns <= 1 and duration_api_ms == 0.
    Surface as Type B and tell the user raising the timeout will NOT help."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=350000,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id="24960feabbbb0000",
        result="API Error: Stream idle timeout - partial response received",
    )
    result = _extract_error(event, resumed=True)
    assert result is not None
    assert "Type B" in result
    assert "Cold-start" in result
    assert "no bytes" in result
    assert "will NOT help" in result
    # Type-A language must NOT appear.
    assert "Type A" not in result


def test_extract_error_unrelated_failure_no_classification() -> None:
    """Non-stall errors must not gain a Type-A/B annotation."""
    from untether.runners.claude import _extract_error

    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=5000,
        duration_api_ms=3000,
        is_error=True,
        num_turns=2,
        session_id="abcdef1234567890",
        result="Tool execution failed with code 1",
    )
    result = _extract_error(event, resumed=False)
    assert result is not None
    assert "Type A" not in result
    assert "Type B" not in result
    assert "Tool execution failed" in result


# ===========================================================================
# #438 — claude_stream_idle_timeout_ms config knob
# ===========================================================================


def test_env_stream_idle_timeout_configured_value(monkeypatch, tmp_path) -> None:
    """[watchdog] claude_stream_idle_timeout_ms in untether.toml is honoured."""
    monkeypatch.delenv("CLAUDE_STREAM_IDLE_TIMEOUT_MS", raising=False)

    from untether import runners as untether_runners
    from untether.settings import (
        TelegramTransportSettings,
        UntetherSettings,
        WatchdogSettings,
    )

    settings = UntetherSettings(
        transport="telegram",
        transports={
            "telegram": TelegramTransportSettings(
                bot_token="test:token",
                chat_id=12345,
                allow_any_user=True,
            )
        },
        watchdog=WatchdogSettings(claude_stream_idle_timeout_ms=600_000),
    )

    monkeypatch.setattr(
        untether_runners.claude,
        "load_settings_if_exists",
        lambda: (settings, tmp_path / "untether.toml"),
    )

    runner = ClaudeRunner(claude_cmd="claude")
    env = runner.env(state=None)
    assert env is not None
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "600000"


def test_env_stream_idle_timeout_settings_load_failure_falls_back(
    monkeypatch,
) -> None:
    """If settings can't load, the hardcoded 300000 default still applies."""
    monkeypatch.delenv("CLAUDE_STREAM_IDLE_TIMEOUT_MS", raising=False)

    from untether import runners as untether_runners

    def _boom():
        raise RuntimeError("settings load failed")

    monkeypatch.setattr(
        untether_runners.claude,
        "load_settings_if_exists",
        _boom,
    )

    runner = ClaudeRunner(claude_cmd="claude")
    env = runner.env(state=None)
    assert env is not None
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "300000"


# ===========================================================================
# #361 — runtime env audit hook on system.init
# ===========================================================================


def test_env_audit_emits_warning_on_leaked_var(monkeypatch) -> None:
    """`_maybe_audit_env` warns once per leaked name when /proc shows it."""
    from structlog.testing import capture_logs

    from untether.runners.claude import _maybe_audit_env

    monkeypatch.setattr(
        claude_runner, "audit_proc_env", lambda pid, **kw: ["BWS_ACCESS_TOKEN"]
    )
    monkeypatch.setattr(claude_runner, "load_settings_if_exists", lambda: None)

    state = ClaudeStreamState()
    state.pid = 4242
    with capture_logs() as logs:
        _maybe_audit_env(state, "session-abc")

    leaked = [
        record
        for record in logs
        if record.get("event") == "claude.env_audit.leaked_var"
    ]
    assert len(leaked) == 1
    assert leaked[0]["name"] == "BWS_ACCESS_TOKEN"
    assert leaked[0]["pid"] == 4242
    assert leaked[0]["session_id"] == "session-abc"
    assert state.audited is True
    assert "BWS_ACCESS_TOKEN" in state.audited_leaks


def test_env_audit_dedups_per_session(monkeypatch) -> None:
    """Repeat calls don't re-warn for the same (session, name) pair."""
    from structlog.testing import capture_logs

    from untether.runners.claude import _maybe_audit_env

    monkeypatch.setattr(
        claude_runner, "audit_proc_env", lambda pid, **kw: ["BWS_ACCESS_TOKEN"]
    )
    monkeypatch.setattr(claude_runner, "load_settings_if_exists", lambda: None)

    state = ClaudeStreamState()
    state.pid = 4242
    with capture_logs() as logs:
        _maybe_audit_env(state, "session-abc")
        # Second call is a no-op because state.audited is True.
        _maybe_audit_env(state, "session-abc")

    leaked = [
        record
        for record in logs
        if record.get("event") == "claude.env_audit.leaked_var"
    ]
    assert len(leaked) == 1


def test_env_audit_skipped_when_pid_missing(monkeypatch) -> None:
    """No PID → silent no-op (audit_proc_env never called)."""
    from untether.runners.claude import _maybe_audit_env

    called = {"n": 0}

    def fake_audit(pid, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(claude_runner, "audit_proc_env", fake_audit)
    monkeypatch.setattr(claude_runner, "load_settings_if_exists", lambda: None)

    state = ClaudeStreamState()  # pid=None by default
    _maybe_audit_env(state, "session-abc")
    assert called["n"] == 0


def test_env_audit_disabled_via_settings(monkeypatch) -> None:
    """`security.env_audit = False` skips the audit even when PID is set."""
    from untether.runners.claude import _maybe_audit_env

    called = {"n": 0}

    def fake_audit(pid, **kw):
        called["n"] += 1
        return ["BWS_ACCESS_TOKEN"]

    class _Sec:
        env_audit = False

    class _Settings:
        security = _Sec()

    monkeypatch.setattr(claude_runner, "audit_proc_env", fake_audit)
    monkeypatch.setattr(
        claude_runner,
        "load_settings_if_exists",
        lambda: (_Settings(), Path("/tmp/none")),
    )

    state = ClaudeStreamState()
    state.pid = 4242
    _maybe_audit_env(state, "session-abc")
    assert called["n"] == 0
    # state.audited still flips to True so we don't keep retrying.
    assert state.audited is True


# ===========================================================================
# #361 — env -i wrap helper
# ===========================================================================


def test_wrap_with_env_i_prefixes_cmd_with_env_i_kvs() -> None:
    from untether.utils.subprocess import wrap_with_env_i

    cmd = ["claude", "-p", "hello"]
    env = {"PATH": "/usr/bin", "HOME": "/home/u"}
    wrapped = wrap_with_env_i(cmd, env)

    # First arg is path to env, second is "-i", then KEY=VAL pairs, then cmd.
    assert wrapped[0].endswith("env")
    assert wrapped[1] == "-i"
    assert "PATH=/usr/bin" in wrapped[2:4]
    assert "HOME=/home/u" in wrapped[2:4]
    assert wrapped[-3:] == ["claude", "-p", "hello"]


def test_wrap_with_env_i_passes_only_provided_env() -> None:
    """The env wrap only forwards keys present in the env dict — host vars
    stripped at the boundary even if upstream tries to read /etc/environment.
    """
    from untether.utils.subprocess import wrap_with_env_i

    env = {"PATH": "/usr/bin"}
    wrapped = wrap_with_env_i(["claude"], env)

    # Only one KEY=VAL between "-i" and "claude".
    kv_pairs = [a for a in wrapped[2:-1] if "=" in a]
    assert kv_pairs == ["PATH=/usr/bin"]


def test_redact_env_i_args_masks_values_between_i_and_program() -> None:
    """Spawn-log redaction hides KEY=VALUE secrets after env -i but keeps
    the actual program args visible (#361 follow-up — without this,
    `subprocess.spawn` would leak OPENAI_API_KEY etc. into journald).
    """
    from untether.utils.subprocess import redact_env_i_args

    cmd = [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin",
        "OPENAI_API_KEY=sk-secret",
        "/home/nathan/.local/bin/claude",
        "--output-format",
        "stream-json",
        "--effort",
        "xhigh",
    ]
    redacted = redact_env_i_args(cmd)
    assert redacted[0] == "/usr/bin/env"
    assert redacted[1] == "-i"
    assert "PATH=***" in redacted
    assert "OPENAI_API_KEY=***" in redacted
    # Program path + args are preserved verbatim
    assert "/home/nathan/.local/bin/claude" in redacted
    assert "--effort" in redacted
    assert "xhigh" in redacted
    # No raw secret value made it through
    assert all("sk-secret" not in arg for arg in redacted)


def test_redact_env_i_args_passthrough_when_not_env_wrapped() -> None:
    """When cmd doesn't start with `env -i`, no redaction happens."""
    from untether.utils.subprocess import redact_env_i_args

    cmd = ["claude", "--output-format", "stream-json", "--effort", "xhigh"]
    assert redact_env_i_args(cmd) == cmd


# ── #333 — post-result idle timeout & turn-complete UX signal ─────────────


def test_translate_result_arms_post_result_idle_timer() -> None:
    """A `result` event sets `state.result_received_at` for the watchdog."""
    state = ClaudeStreamState()
    assert state.result_received_at is None

    event = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=1,
        session_id="post-result-timer-session",
        result="done",
    )
    translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.result_received_at is not None
    assert state.result_received_at > 0


def test_translate_result_emits_turn_complete_meta() -> None:
    """Successful result emits supplementary StartedEvent with complete hint."""
    state = ClaudeStreamState()
    event = claude_schema.StreamResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=1,
        session_id="turn-complete-session",
        result="done",
    )
    events = translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    started = [evt for evt in events if isinstance(evt, StartedEvent)]
    completed = [evt for evt in events if isinstance(evt, CompletedEvent)]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].meta == {"complete": "✓ turn complete"}
    # CompletedEvent must remain the LAST event for the 3-event contract.
    assert events[-1] is completed[0]


def test_translate_result_skips_complete_meta_on_error() -> None:
    """Errored result does NOT add the turn-complete meta hint."""
    state = ClaudeStreamState()
    event = claude_schema.StreamResultMessage(
        subtype="error_during_execution",
        duration_ms=100,
        duration_api_ms=50,
        is_error=True,
        num_turns=1,
        session_id="errored-session",
    )
    events = translate_claude_event(
        event,
        title="claude",
        state=state,
        factory=state.factory,
    )
    started = [evt for evt in events if isinstance(evt, StartedEvent)]
    completed = [evt for evt in events if isinstance(evt, CompletedEvent)]
    assert len(started) == 0  # no supplementary started for failures
    assert len(completed) == 1
    assert completed[0].ok is False


@pytest.mark.anyio
async def test_post_result_idle_watchdog_fires_when_clean(monkeypatch) -> None:
    """Past the timeout with no pending approvals → stdin is closed."""
    import anyio

    from untether.runners.claude import (
        _PENDING_ASK_REQUESTS,
        _REQUEST_TO_SESSION,
        ClaudeRunner,
    )

    # Ensure registries are clean.
    _REQUEST_TO_SESSION.clear()
    _PENDING_ASK_REQUESTS.clear()

    runner = ClaudeRunner(claude_cmd="claude")
    state = ClaudeStreamState()
    # Seed the factory with a resume token so the watchdog can find the sid.
    state.factory.started(
        ResumeToken(engine="claude", value="watchdog-clean-session"),
    )
    # Arm the timer: pretend the result event landed 1000s ago.
    state.result_received_at = time.monotonic() - 1000.0

    closed = anyio.Event()

    class FakeStdin:
        async def aclose(self) -> None:
            closed.set()

    fake_stdin = FakeStdin()
    reader_done = anyio.Event()

    # Patch sleep so the watchdog ticks immediately.
    real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("untether.runners.claude.anyio.sleep", fast_sleep)

    class _StubLogger:
        def info(self, *a, **k) -> None:
            pass

        def warning(self, *a, **k) -> None:
            pass

        def debug(self, *a, **k) -> None:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            runner._post_result_idle_watchdog,
            state,
            fake_stdin,
            reader_done,
            _StubLogger(),
            60.0,
        )
        # Give the task one tick to detect the expired timer + close.
        with anyio.move_on_after(2.0):
            await closed.wait()
        tg.cancel_scope.cancel()

    assert closed.is_set(), "watchdog should have closed stdin"


@pytest.mark.anyio
async def test_post_result_idle_watchdog_defers_when_pending_approval(
    monkeypatch,
) -> None:
    """An in-flight approval suppresses the close, re-arming the timer."""
    import anyio

    from untether.runners.claude import (
        _PENDING_ASK_REQUESTS,
        _REQUEST_TO_SESSION,
        ClaudeRunner,
    )

    sid = "watchdog-deferred-session"
    _REQUEST_TO_SESSION.clear()
    _PENDING_ASK_REQUESTS.clear()
    _REQUEST_TO_SESSION["req_pending"] = sid
    try:
        runner = ClaudeRunner(claude_cmd="claude")
        state = ClaudeStreamState()
        state.factory.started(ResumeToken(engine="claude", value=sid))
        original_armed = time.monotonic() - 1000.0
        state.result_received_at = original_armed

        closed = anyio.Event()

        class FakeStdin:
            async def aclose(self) -> None:
                closed.set()

        real_sleep = anyio.sleep

        async def fast_sleep(s: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr("untether.runners.claude.anyio.sleep", fast_sleep)

        class _StubLogger:
            def info(self, *a, **k) -> None:
                pass

            def warning(self, *a, **k) -> None:
                pass

            def debug(self, *a, **k) -> None:
                pass

        reader_done = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                runner._post_result_idle_watchdog,
                state,
                FakeStdin(),
                reader_done,
                _StubLogger(),
                60.0,
            )
            # Let the watchdog tick a few times, then signal reader_done so
            # the loop exits without our needing to wait.
            for _ in range(5):
                await real_sleep(0)
            reader_done.set()
            tg.cancel_scope.cancel()

        assert not closed.is_set(), (
            "watchdog must not close stdin while approval pending"
        )
        # The timer was re-armed (pushed forward), so result_received_at
        # should now be more recent than the original arming.
        assert state.result_received_at is not None
        assert state.result_received_at > original_armed
    finally:
        _REQUEST_TO_SESSION.pop("req_pending", None)


# ───── #507 — dead ScheduleWakeup outside /loop shortcut ───────────────


@pytest.mark.anyio
async def test_dead_schedule_wakeup_shortens_post_result_timeout(
    monkeypatch,
) -> None:
    """When ScheduleWakeup armed during the run AND /loop is OFF for the
    chat, ``_post_result_idle_watchdog`` cuts its effective timeout to
    ``max_armed_delay + 60s`` so the session closes within delay+grace
    instead of waiting the default 600s. Validates the fix for #507.
    """
    import anyio

    from untether.runners.claude import (
        _PENDING_ASK_REQUESTS,
        _REQUEST_TO_SESSION,
        ClaudeRunner,
    )
    from untether.runners.run_options import EngineRunOptions, apply_run_options
    from untether.utils.paths import set_run_channel_id

    _REQUEST_TO_SESSION.clear()
    _PENDING_ASK_REQUESTS.clear()

    runner = ClaudeRunner(claude_cmd="claude")
    state = ClaudeStreamState()
    state.factory.started(
        ResumeToken(engine="claude", value="watchdog-dead-wakeup-session"),
    )
    # ScheduleWakeup armed with delaySeconds=75 → scalar high-water-mark
    # tracks 75.0. #544: the scalar replaced the per-tool_id
    # ``live_wakeups_arm_delay`` dict so the value survives
    # ``_clear_background_handle`` for the rest of the turn.
    state.live_wakeups["toolu_W"] = time.monotonic() + 75.0
    state.last_schedule_wakeup_arm_delay = 75.0
    # Pretend the result event landed 200s ago — past the dead-wakeup
    # effective_timeout (75 + 60 = 135s) but still well below the default
    # 600s timeout.
    state.result_received_at = time.monotonic() - 200.0

    closed = anyio.Event()

    class FakeStdin:
        async def aclose(self) -> None:
            closed.set()

    fake_stdin = FakeStdin()
    reader_done = anyio.Event()

    real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("untether.runners.claude.anyio.sleep", fast_sleep)

    captured_logs: list[dict] = []

    class _StubLogger:
        def info(self, event: str = "", **kwargs) -> None:
            captured_logs.append({"event": event, **kwargs})

        def warning(self, *a, **k) -> None:
            pass

        def debug(self, *a, **k) -> None:
            pass

    # /loop OFF for the chat (default). Set a chat_id so the shortcut
    # finds it.
    token = set_run_channel_id(12345)
    try:
        with apply_run_options(EngineRunOptions(loop_enabled=False)):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    runner._post_result_idle_watchdog,
                    state,
                    fake_stdin,
                    reader_done,
                    _StubLogger(),
                    600.0,  # default timeout — shortcut should cut to 135s
                )
                with anyio.move_on_after(2.0):
                    await closed.wait()
                tg.cancel_scope.cancel()
    finally:
        from untether.utils.paths import reset_run_channel_id

        reset_run_channel_id(token)

    assert closed.is_set(), "watchdog should have closed stdin"
    # Verify the closing log marked dead_wakeup=True with the shortened
    # effective_timeout.
    closing = next(
        (
            lg
            for lg in captured_logs
            if lg["event"] == "claude.post_result_idle.closing_stdin"
        ),
        None,
    )
    assert closing is not None
    assert closing["dead_wakeup"] is True
    assert closing["effective_timeout_s"] == 135.0


@pytest.mark.anyio
async def test_active_loop_preserves_default_post_result_timeout(
    monkeypatch,
) -> None:
    """When /loop is ON for the chat, the dead-wakeup shortcut must NOT
    apply — the wakeup is legitimate background work. The watchdog should
    use the full default timeout.
    """
    import anyio

    from untether.runners.claude import (
        _PENDING_ASK_REQUESTS,
        _REQUEST_TO_SESSION,
        ClaudeRunner,
    )
    from untether.runners.run_options import EngineRunOptions, apply_run_options
    from untether.utils.paths import set_run_channel_id

    _REQUEST_TO_SESSION.clear()
    _PENDING_ASK_REQUESTS.clear()

    runner = ClaudeRunner(claude_cmd="claude")
    state = ClaudeStreamState()
    state.factory.started(
        ResumeToken(engine="claude", value="watchdog-loop-on-session"),
    )
    state.live_wakeups["toolu_W"] = time.monotonic() + 75.0
    state.last_schedule_wakeup_arm_delay = 75.0
    # Pretend result landed 200s ago — past the dead-wakeup shortcut
    # threshold (135s), but well below the 600s default timeout. With
    # /loop ON the watchdog should NOT close stdin yet.
    state.result_received_at = time.monotonic() - 200.0

    closed = anyio.Event()

    class FakeStdin:
        async def aclose(self) -> None:
            closed.set()

    fake_stdin = FakeStdin()
    reader_done = anyio.Event()

    real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("untether.runners.claude.anyio.sleep", fast_sleep)

    class _StubLogger:
        def info(self, *a, **k) -> None:
            pass

        def warning(self, *a, **k) -> None:
            pass

        def debug(self, *a, **k) -> None:
            pass

    token = set_run_channel_id(12345)
    try:
        with apply_run_options(EngineRunOptions(loop_enabled=True)):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    runner._post_result_idle_watchdog,
                    state,
                    fake_stdin,
                    reader_done,
                    _StubLogger(),
                    600.0,
                )
                # Tick a few times, then signal reader_done.
                for _ in range(10):
                    await real_sleep(0)
                reader_done.set()
                tg.cancel_scope.cancel()
    finally:
        from untether.utils.paths import reset_run_channel_id

        reset_run_channel_id(token)

    assert not closed.is_set(), "with /loop ON, dead-wakeup shortcut must not fire"


@pytest.mark.anyio
async def test_dead_schedule_wakeup_shortens_post_result_after_tool_result_cleared(
    monkeypatch,
) -> None:
    """#544: full lifecycle test for the #507 redux fix.

    The original #507 unit tests directly seeded ``state.live_wakeups_arm_delay``
    and bypassed ``_clear_background_handle``, which is why the rc11 fix
    appeared green in CI but failed on channelo rc15 in production. This
    test exercises the real translate path — tool_use → tool_result → result
    — so the scalar high-water-mark MUST survive ``_clear_background_handle``
    for the dead-wakeup shortcut to engage.
    """
    import anyio

    from untether.runners.claude import (
        _PENDING_ASK_REQUESTS,
        _REQUEST_TO_SESSION,
        ClaudeRunner,
    )
    from untether.runners.run_options import EngineRunOptions, apply_run_options
    from untether.utils.paths import set_run_channel_id

    _REQUEST_TO_SESSION.clear()
    _PENDING_ASK_REQUESTS.clear()

    state = ClaudeStreamState()
    state.factory.started(ResumeToken(engine="claude", value="redux-session"))

    # 1. tool_use ScheduleWakeup(delaySeconds=120) → register arm-delay
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W", {"delaySeconds": 120})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_W" in state.live_wakeups
    assert state.last_schedule_wakeup_arm_delay == 120.0

    # 2. tool_result → _clear_background_handle pops live_wakeups but the
    # scalar high-water-mark MUST survive.
    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_W")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert "toolu_W" not in state.live_wakeups, "tool_result must clear dict"
    assert state.last_schedule_wakeup_arm_delay == 120.0, (
        "scalar must survive _clear_background_handle (#544 regression check)"
    )

    # 3. Pretend the result event landed 200s ago — past the dead-wakeup
    # effective_timeout (120 + 60 = 180s).
    state.result_received_at = time.monotonic() - 200.0

    # 4. Run the watchdog. With /loop OFF and scalar populated, it should
    # close stdin and stamp ``dead_wakeup=True effective_timeout_s=180.0``.
    closed = anyio.Event()

    class FakeStdin:
        async def aclose(self) -> None:
            closed.set()

    fake_stdin = FakeStdin()
    reader_done = anyio.Event()

    real_sleep = anyio.sleep

    async def fast_sleep(s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("untether.runners.claude.anyio.sleep", fast_sleep)

    captured_logs: list[dict] = []

    class _StubLogger:
        def info(self, event: str = "", **kwargs) -> None:
            captured_logs.append({"event": event, **kwargs})

        def warning(self, *a, **k) -> None:
            pass

        def debug(self, *a, **k) -> None:
            pass

    runner = ClaudeRunner(claude_cmd="claude")
    token = set_run_channel_id(54321)
    try:
        with apply_run_options(EngineRunOptions(loop_enabled=False)):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    runner._post_result_idle_watchdog,
                    state,
                    fake_stdin,
                    reader_done,
                    _StubLogger(),
                    600.0,
                )
                with anyio.move_on_after(2.0):
                    await closed.wait()
                tg.cancel_scope.cancel()
    finally:
        from untether.utils.paths import reset_run_channel_id

        reset_run_channel_id(token)

    assert closed.is_set(), "watchdog should have closed stdin"
    closing = next(
        (
            lg
            for lg in captured_logs
            if lg["event"] == "claude.post_result_idle.closing_stdin"
        ),
        None,
    )
    assert closing is not None
    assert closing["dead_wakeup"] is True
    assert closing["effective_timeout_s"] == 180.0


def test_multiple_schedule_wakeups_in_one_turn_use_max_delay() -> None:
    """Two ScheduleWakeup calls in a single turn — the scalar must hold the
    longest arm-delay so a 60s wakeup followed by a 240s wakeup still cuts
    the watchdog timeout to 240 + 60 = 300s, not 60 + 60 = 120s.
    """
    state = ClaudeStreamState()

    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W1", {"delaySeconds": 60})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.last_schedule_wakeup_arm_delay == 60.0

    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W2", {"delaySeconds": 240})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.last_schedule_wakeup_arm_delay == 240.0, "max wins"

    # A SHORTER arm after a longer one must NOT replace the high-water-mark.
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W3", {"delaySeconds": 120})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.last_schedule_wakeup_arm_delay == 240.0, (
        "shorter delays must not shrink the high-water-mark"
    )


def test_new_user_turn_resets_schedule_wakeup_arm_delay() -> None:
    """A fresh user prompt (StreamUserMessage with non-tool_result content)
    must reset the per-turn scalar so the next turn — if it does NOT call
    ScheduleWakeup — falls back to the default 600s post-result timeout.
    """
    state = ClaudeStreamState()

    # Turn 1: ScheduleWakeup armed
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W", {"delaySeconds": 90})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )
    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_W")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.last_schedule_wakeup_arm_delay == 90.0

    # Turn 2: a real user prompt arrives as a StreamUserMessage with a text
    # block (NOT a tool_result block). The reset path must fire.
    user_text_event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please continue with the next step."}
            ],
        },
    }
    translate_claude_event(
        _decode_event(user_text_event),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.last_schedule_wakeup_arm_delay is None, (
        "new user prompt must clear the per-turn arm-delay (#544)"
    )


def test_mixed_user_message_does_not_reset_arm_delay() -> None:
    """A user message that contains BOTH tool_results AND non-tool_result
    blocks (rare in practice but allowed by the protocol) must preserve
    the scalar — the tool turn is still in flight at that point, so the
    new-turn reset path is suppressed when any tool_result is present.
    """
    state = ClaudeStreamState()
    translate_claude_event(
        _decode_event(
            _make_tool_use_event("ScheduleWakeup", "toolu_W", {"delaySeconds": 90})
        ),
        title="claude",
        state=state,
        factory=state.factory,
    )

    mixed_event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_W",
                    "content": "ok",
                    "is_error": False,
                },
                {"type": "text", "text": "noise"},
            ],
        },
    }
    translate_claude_event(
        _decode_event(mixed_event),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert state.last_schedule_wakeup_arm_delay == 90.0, (
        "mixed user message must NOT clear the scalar (#544 edge case)"
    )


def test_meta_line_renders_turn_complete_marker() -> None:
    """format_meta_line includes the `complete` hint when set on meta."""
    from untether.markdown import format_meta_line

    line = format_meta_line({"model": "sonnet", "complete": "✓ turn complete"})
    assert line is not None
    assert "✓ turn complete" in line


def test_meta_line_omits_complete_when_absent() -> None:
    """Absence of the `complete` key keeps the legacy footer shape."""
    from untether.markdown import format_meta_line

    line = format_meta_line({"model": "sonnet"})
    assert line is not None
    assert "✓ turn complete" not in line


def test_is_session_alive_reads_session_stdin_registry() -> None:
    """is_session_alive (#289) returns True iff session_id is in _SESSION_STDIN."""
    from untether.runners.claude import _SESSION_STDIN, is_session_alive

    sid = "test-session-289-alive"
    try:
        assert is_session_alive(sid) is False
        _SESSION_STDIN[sid] = object()  # any sentinel is enough — we test membership
        assert is_session_alive(sid) is True
    finally:
        _SESSION_STDIN.pop(sid, None)


def test_is_session_alive_unknown_session_returns_false() -> None:
    """Sessions never registered are not alive."""
    from untether.runners.claude import is_session_alive

    assert is_session_alive("session-that-was-never-spawned") is False


# ───── #289 — /loop and ScheduleWakeup observation ─────────────────────


def _seed_state_for_loop_observation(
    state: ClaudeStreamState, *, session_id: str = "sess-289"
) -> None:
    """Helper: set state.factory._resume so ``_observe_loop_tool_use`` can
    read the session_id without a full system.init flow."""
    state.factory._resume = ResumeToken(engine="claude", value=session_id)
    state.first_user_message_text = "user typed /loop check the deploy"


@pytest.mark.anyio
class TestLoopObservation:
    """Cover the new ``_observe_loop_tool_use`` /
    ``_observe_loop_tool_result`` helpers and the ``_loop_enabled_for_chat``
    gate.  Mirrors ``test_loop_scheduler.py`` cleanup conventions.
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        from untether import loop_scheduler

        loop_scheduler.uninstall()
        yield
        loop_scheduler.uninstall()

    @pytest.fixture
    def _enable_loop(self):
        """Toggle Loop mode ON via the per-chat run-options contextvar so
        the master gate inside the observer doesn't short-circuit."""
        from untether.runners.run_options import (
            EngineRunOptions,
            apply_run_options,
        )

        with apply_run_options(EngineRunOptions(loop_enabled=True)):
            yield

    @pytest.fixture
    def _disable_loop(self):
        """Toggle Loop mode OFF explicitly."""
        from untether.runners.run_options import (
            EngineRunOptions,
            apply_run_options,
        )

        with apply_run_options(EngineRunOptions(loop_enabled=False)):
            yield

    @pytest.fixture
    def _set_chat(self):
        """Push a chat_id into the run-context contextvar."""
        from untether.utils.paths import (
            reset_run_channel_id,
            set_run_channel_id,
        )

        token = set_run_channel_id(7777)
        try:
            yield 7777
        finally:
            reset_run_channel_id(token)

    @pytest.fixture
    async def _installed_scheduler(self):
        """Install loop_scheduler so observers can call register_*."""
        from untether import loop_scheduler

        async def _noop(*args, **kwargs):
            return None

        class _Transport:
            async def send(self, **_):
                return None

            async def edit(self, **_):
                return None

            async def delete(self, _ref):
                return None

        async with anyio.create_task_group() as tg:
            loop_scheduler.install(tg, _noop, _Transport(), 1)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    @pytest.mark.usefixtures("_enable_loop", "_installed_scheduler")
    async def test_observer_skipped_when_chat_id_unset(self):
        """Without ``set_run_channel_id`` the observer must no-op."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_C1",
                    {"cron": "* * * * *", "prompt": "x", "recurring": True},
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        assert loop_scheduler.active_count() == 0

    @pytest.mark.usefixtures("_disable_loop", "_set_chat", "_installed_scheduler")
    async def test_observer_skipped_when_toggle_off(self):
        """Loop mode OFF → no registration."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_C2",
                    {"cron": "* * * * *", "prompt": "ping", "recurring": True},
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        assert loop_scheduler.active_count() == 0

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_cron_create_registers_when_enabled(self):
        """CronCreate with toggle ON registers a recurring entry."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state, session_id="sess-cron-on")
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_C3",
                    {
                        "cron": "*/5 * * * *",
                        "prompt": "check the deploy",
                        "recurring": True,
                    },
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        assert loop_scheduler.active_count() == 1
        pending = loop_scheduler.pending_for_chat(7777)
        assert len(pending) == 1
        assert pending[0].cron_expression == "*/5 * * * *"
        assert pending[0].prompt == "check the deploy"
        assert pending[0].recurring is True
        assert pending[0].resume_token == "sess-cron-on"

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_cron_create_uses_cron_field_not_cron_expression(self):
        """Probe 5: input field is ``cron`` — fallback aliases shouldn't
        override the canonical name."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_C4",
                    {
                        "cron": "0 * * * *",
                        "cron_expression": "* * * * *",  # legacy alias — should be ignored
                        "prompt": "y",
                        "recurring": True,
                    },
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        pending = loop_scheduler.pending_for_chat(7777)
        assert len(pending) == 1
        assert pending[0].cron_expression == "0 * * * *"

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_cron_create_skipped_when_prompt_missing(self):
        """Defensive: missing prompt field → no registration."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_C5",
                    {"cron": "* * * * *", "recurring": True},
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        assert loop_scheduler.active_count() == 0

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_schedule_wakeup_registers_when_above_threshold(self):
        """Long ScheduleWakeup → register Untether-side timer."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        # 3600s > default inline_threshold_seconds=300 — should register.
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "ScheduleWakeup",
                    "toolu_W1",
                    {
                        "delaySeconds": 3600,
                        "reason": "long-poll",
                        "prompt": "check progress",
                    },
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        pending = loop_scheduler.pending_for_chat(7777)
        assert len(pending) == 1
        assert pending[0].kind == "wakeup"
        assert pending[0].delay_seconds == 3600.0

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_schedule_wakeup_skipped_when_below_threshold(self):
        """Short waits stay rendered live by the rc8 countdown — no
        Untether-side timer."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "ScheduleWakeup",
                    "toolu_W2",
                    {"delaySeconds": 60, "prompt": "x"},
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        assert loop_scheduler.active_count() == 0
        # rc8 countdown (live_wakeups) still populated by
        # _register_background_handle, regardless of loop observation.
        assert "toolu_W2" in state.live_wakeups

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_cron_delete_cancels_matching_entry(self):
        """CronDelete with the upstream ID cancels the matching entry."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        # Register an entry, then bind upstream ID, then delete.
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_CD1",
                    {"cron": "* * * * *", "prompt": "x", "recurring": True},
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        # tool_result with upstream ID
        result = _make_tool_result_event("toolu_CD1")
        result["message"]["content"][0]["content"] = (
            "Scheduled recurring job abcdef12 (Every minute). Session-only ..."
        )
        translate_claude_event(
            _decode_event(result),
            title="claude",
            state=state,
            factory=state.factory,
        )
        # Now CronDelete that ID
        translate_claude_event(
            _decode_event(
                _make_tool_use_event("CronDelete", "toolu_CD2", {"id": "abcdef12"})
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        assert loop_scheduler.active_count() == 0

    @pytest.mark.usefixtures("_enable_loop", "_set_chat", "_installed_scheduler")
    async def test_tool_result_binds_upstream_cron_id(self):
        """``_observe_loop_tool_result`` parses the result text and binds
        the 8-char upstream ID via :func:`bind_upstream_id`."""
        from untether import loop_scheduler

        state = ClaudeStreamState()
        _seed_state_for_loop_observation(state)
        translate_claude_event(
            _decode_event(
                _make_tool_use_event(
                    "CronCreate",
                    "toolu_BU1",
                    {"cron": "* * * * *", "prompt": "x", "recurring": True},
                )
            ),
            title="claude",
            state=state,
            factory=state.factory,
        )
        result = _make_tool_result_event("toolu_BU1")
        result["message"]["content"][0]["content"] = (
            "Scheduled recurring job 12345678 (Every minute). Session-only ..."
        )
        translate_claude_event(
            _decode_event(result),
            title="claude",
            state=state,
            factory=state.factory,
        )
        # The entry now has upstream_cron_id bound — cancel_by_upstream_id
        # must succeed.
        assert loop_scheduler.cancel_by_upstream_id("12345678") is True

    @pytest.mark.usefixtures("_set_chat")
    async def test_loop_enabled_for_chat_run_options_overrides_global(self):
        """Per-chat run option True overrides global config False (the
        common case — user enables Loop mode in their chat)."""
        from untether.runners.claude import _loop_enabled_for_chat
        from untether.runners.run_options import (
            EngineRunOptions,
            apply_run_options,
        )

        with apply_run_options(EngineRunOptions(loop_enabled=True)):
            assert _loop_enabled_for_chat(7777) is True
        with apply_run_options(EngineRunOptions(loop_enabled=False)):
            assert _loop_enabled_for_chat(7777) is False
        # No run options at all → fall back to global ([loop] enabled,
        # default False).  Use a real options=None context to verify.
        from untether.runners.run_options import (
            reset_run_options,
            set_run_options,
        )

        token = set_run_options(None)
        try:
            assert _loop_enabled_for_chat(7777) is False
        finally:
            reset_run_options(token)


def test_first_user_message_text_captured_in_new_state() -> None:
    """new_state should snapshot the prompt for sentinel-fallback later."""
    runner = ClaudeRunner(
        claude_cmd="claude",
        model=None,
        permission_mode=None,
        allowed_tools=[],
        extra_args=[],
        dangerously_skip_permissions=False,
        use_api_billing=None,
        session_title=None,
    )
    state = runner.new_state("user typed /loop X", None)
    assert state.first_user_message_text == "user typed /loop X"
