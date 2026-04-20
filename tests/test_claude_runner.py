import json
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


def test_tool_result_queues_mcp_status_when_notify_enabled() -> None:
    """With notify_catalog_refresh on, each tool_result batch queues one request."""
    state = ClaudeStreamState()
    state.notify_catalog_refresh = True
    state.factory._resume = ResumeToken(engine=ENGINE, value="sess-5")

    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_1")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    assert len(state.pending_catalog_refresh_ids) == 1
    assert state.pending_catalog_refresh_ids[0].startswith("ut_catalog_refresh_sess-5_")

    translate_claude_event(
        _decode_event(_make_tool_result_event("toolu_2")),
        title="claude",
        state=state,
        factory=state.factory,
    )
    # Second batch queues a second distinct ID
    assert len(state.pending_catalog_refresh_ids) == 2
    assert state.pending_catalog_refresh_ids[0] != state.pending_catalog_refresh_ids[1]


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
