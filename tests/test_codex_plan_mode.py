"""Codex plan mode on the app-server transport (#965, phase 965-1).

``plan`` / ``plan-auto`` run each turn with the experimental
``turn/start.collaborationMode {mode:"plan"}`` (so the connection opts in to
``experimentalApi``), under the Ask before writes tier labelled "plan first"
(D3). The mode is saved on the thread (P7 on codex-cli 0.171.0), so the first
non-plan run on a thread Untether saw in plan mode opens an experimental
connection too and sends ``collaborationMode {mode:"default"}`` (D1). When
that marker was lost, a stable resume whose echo says ``plan`` adds a one-time
warning row and marks the thread for the next run.

Frames: ``tests/fixtures/codex_app_server/p7-plan-frames-0.171.0.jsonl`` holds
three sanitised P7 frames (the plan turn's ``thread/settings/updated``, its
``plan`` item, and a stable ``thread/resume`` result echoing ``plan``). Both
the settings frame and the echo carry Codex's full plan template in
``developer_instructions``, which must never reach a log.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests import test_codex_app_server_runner as _rt
from tests.test_codex_app_server_runner import (
    THREAD,
    Fake,
    _app_state,
    _bare_runner,
    _completed,
    _run,
    _runner,
    _translate,
)
from tests.test_command_engine_gates import FakeCommandContext, FakeTransportRuntime
from untether.live_sessions import live_backend
from untether.model import ActionEvent, ResumeToken, StartedEvent
from untether.runner import prespawn_blocked_reason
from untether.runner_bridge import _apply_preamble
from untether.runners import codex_app_server as gate
from untether.runners.codex import (
    CODEX_MODE_NEEDS_APP_SERVER_BLOCK,
    codex_mode_needs_app_server_event,
)
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas.codex_app_server import CODEX_APP_SERVER_FIELDS
from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
from untether.telegram.commands.planmode import PlanModeCommand

project = _rt.project
gate_calls = _rt.gate_calls
_fresh_cache = _rt._fresh_cache

_FRAMES = [
    json.loads(line)
    for line in (
        Path(__file__).parent
        / "fixtures"
        / "codex_app_server"
        / "p7-plan-frames-0.171.0.jsonl"
    )
    .read_text()
    .splitlines()
]
_TEMPLATE_MARK = "Plan Mode (Conversational)"
_STUCK = "This Codex thread was still in Plan mode"


def _frame(method: str | None) -> dict[str, Any]:
    for frame in _FRAMES:
        if frame.get("method") == method:
            return copy.deepcopy(frame)
    raise KeyError(method)


def _settings_frame() -> dict[str, Any]:
    frame = _frame("thread/settings/updated")
    frame["params"]["threadId"] = THREAD
    return frame


def _plan_item_frame() -> dict[str, Any]:
    frame = _frame("item/completed")
    frame["params"]["threadId"] = THREAD
    frame["params"]["turnId"] = "turn-1"
    return frame


def _resume_echo() -> dict[str, Any]:
    return _frame(None)["result"]["collaborationMode"]


_PLAN_TEXT = _frame("item/completed")["params"]["item"]["text"]
_RESUME = ResumeToken(engine="codex", value=THREAD)


@pytest.fixture(autouse=True)
def _fresh_plan_threads():
    gate._PLAN_THREADS.clear()
    gate._RUNS_BY_THREAD.clear()
    yield
    gate._PLAN_THREADS.clear()
    gate._RUNS_BY_THREAD.clear()


def _fake(tmp_path: Path, name: str, **scenario: Any) -> Fake:
    (tmp_path / name).mkdir()
    return Fake(tmp_path / name, **scenario)


def _init(fake: Fake, n: int = -1) -> dict[str, Any]:
    return fake.requests("initialize")[n]


def _turn(fake: Fake, n: int = -1) -> dict[str, Any]:
    return fake.requests("turn/start")[n]


def _events(logs: list[dict], name: str) -> list[dict]:
    return [e for e in logs if e["event"] == name]


# --- the connection and the turn ---------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["plan", "plan-auto"])
async def test_plan_mode_sends_experimental_and_collaboration_mode(
    tmp_path: Path, project: Path, gate_calls: list[str], mode: str
) -> None:
    fake = Fake(tmp_path, messages=[], notifications=[_plan_item_frame()])
    with capture_logs() as logs:
        events = await _run(
            _runner(fake),
            options=EngineRunOptions(permission_mode=mode, reasoning="high"),
        )
    assert _completed(events).ok
    caps = _init(fake)["capabilities"]
    assert caps["experimentalApi"] is True
    # Δ D40 / F19: experimental-only noise stays off.
    assert "turn/moderationMetadata" in caps["optOutNotificationMethods"]
    assert "thread/readState/changed" in caps["optOutNotificationMethods"]
    # D3: the Ask before writes triple, labelled "plan first".
    start = fake.requests("thread/start")[0]
    assert start["approvalPolicy"] == "on-request"
    assert start["sandbox"] == "read-only"
    started = next(e for e in events if isinstance(e, StartedEvent))
    assert started.meta["permissionMode"].startswith("plan first")
    assert _turn(fake)["collaborationMode"] == {
        "mode": "plan",
        "settings": {
            "model": "gpt-fake",
            "reasoning_effort": "high",
            "developer_instructions": None,
        },
    }
    assert THREAD in gate._PLAN_THREADS
    init_logs = _events(logs, "codex.app_server.initialized")
    assert [e["experimental"] for e in init_logs] == [True]


@pytest.mark.anyio
async def test_non_plan_run_stays_stable(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    with capture_logs() as logs:
        events = await _run(
            _runner(fake), options=EngineRunOptions(permission_mode="ask-writes")
        )
    assert _completed(events).ok
    caps = _init(fake)["capabilities"]
    assert caps["experimentalApi"] is False
    assert caps["optOutNotificationMethods"] == list(gate.OPT_OUT_NOTIFICATIONS)
    assert "collaborationMode" not in _turn(fake)
    assert THREAD not in gate._PLAN_THREADS
    assert [
        e["experimental"] for e in _events(logs, "codex.app_server.initialized")
    ] == [False]


@pytest.mark.anyio
async def test_default_reset_after_plan_turn(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, messages=[], notifications=[_plan_item_frame()])
    runner = _runner(fake)
    await _run(runner, options=EngineRunOptions(permission_mode="plan"))
    assert THREAD in gate._PLAN_THREADS
    fake2 = _fake(tmp_path, "second")
    with capture_logs() as logs:
        events = await _run(
            _runner(fake2), resume=_RESUME, options=EngineRunOptions(reasoning="low")
        )
    assert _completed(events).ok
    assert _init(fake2)["capabilities"]["experimentalApi"] is True
    assert _turn(fake2)["collaborationMode"] == {
        "mode": "default",
        "settings": {
            "model": "gpt-fake",
            "reasoning_effort": "low",
            "developer_instructions": None,
        },
    }
    assert _events(logs, "codex.app_server.plan_mode_reset")
    # Reset once: the next non-plan run is an ordinary stable one.
    assert THREAD not in gate._PLAN_THREADS
    fake3 = _fake(tmp_path, "third")
    await _run(_runner(fake3), resume=_RESUME)
    assert _init(fake3)["capabilities"]["experimentalApi"] is False
    assert "collaborationMode" not in _turn(fake3)


@pytest.mark.anyio
async def test_stable_resume_echo_plan_adds_row_and_marks_thread(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, collab_echo=_resume_echo())
    with capture_logs() as logs:
        events = await _run(_runner(fake), resume=_RESUME)
    assert _completed(events).ok
    assert _init(fake)["capabilities"]["experimentalApi"] is False
    assert "collaborationMode" not in _turn(fake)
    stuck = [
        e for e in events if isinstance(e, ActionEvent) and _STUCK in e.action.title
    ]
    assert len(stuck) == 1 and stuck[0].action.kind == "warning"
    assert "send your message again" in stuck[0].action.title
    assert _events(logs, "codex.app_server.plan_mode_stuck")
    assert THREAD in gate._PLAN_THREADS
    # The next run resets it.
    fake2 = _fake(tmp_path, "second")
    await _run(_runner(fake2), resume=_RESUME)
    assert _init(fake2)["capabilities"]["experimentalApi"] is True
    assert _turn(fake2)["collaborationMode"]["mode"] == "default"


@pytest.mark.anyio
async def test_plan_chat_with_plan_echo_has_no_stuck_row(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, collab_echo=_resume_echo())
    events = await _run(
        _runner(fake), resume=_RESUME, options=EngineRunOptions(permission_mode="plan")
    )
    assert not [
        e for e in events if isinstance(e, ActionEvent) and _STUCK in e.action.title
    ]


@pytest.mark.anyio
async def test_plan_item_is_final_answer(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, messages=[], notifications=[_plan_item_frame()])
    events = await _run(_runner(fake), options=EngineRunOptions(permission_mode="plan"))
    done = _completed(events)
    assert done.ok and done.answer == _PLAN_TEXT
    # The plan item itself stays silent (no progress row).
    assert not [
        e for e in events if isinstance(e, ActionEvent) and e.action.kind == "tool"
    ]
    # Commentary alone doesn't beat the plan; a final answer does.
    fake2 = _fake(
        tmp_path,
        "b",
        messages=[["looking around", "commentary"]],
        notifications=[_plan_item_frame()],
    )
    done2 = _completed(
        await _run(_runner(fake2), options=EngineRunOptions(permission_mode="plan"))
    )
    assert done2.answer == _PLAN_TEXT
    fake3 = _fake(
        tmp_path,
        "c",
        messages=[["the answer", "final_answer"]],
        notifications=[_plan_item_frame()],
    )
    done3 = _completed(
        await _run(_runner(fake3), options=EngineRunOptions(permission_mode="plan"))
    )
    assert done3.answer == "the answer"


@pytest.mark.anyio
@pytest.mark.parametrize("effort", [None, "low"])
async def test_effort_sent_in_settings(
    tmp_path: Path, project: Path, gate_calls: list[str], effort: str | None
) -> None:
    fake = Fake(tmp_path)
    await _run(
        _runner(fake),
        options=EngineRunOptions(permission_mode="plan", reasoning=effort),
    )
    turn = _turn(fake)
    # collaborationMode takes precedence over turn/start.effort (SCH).
    assert turn["collaborationMode"]["settings"]["reasoning_effort"] == effort
    assert turn.get("effort") == effort


@pytest.mark.anyio
async def test_settings_echo_never_logged(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    assert _TEMPLATE_MARK in json.dumps(_settings_frame())
    assert _TEMPLATE_MARK in json.dumps(_resume_echo())
    fake = Fake(
        tmp_path,
        collab_echo=_resume_echo(),
        notifications=[_settings_frame()],
    )
    with capture_logs() as logs:
        events = await _run(
            _runner(fake),
            resume=_RESUME,
            options=EngineRunOptions(permission_mode="plan"),
        )
    assert _completed(events).ok
    assert _TEMPLATE_MARK not in repr(logs)
    assert "developer_instructions" not in repr(logs)
    assert [e["mode"] for e in _events(logs, "codex.app_server.thread_settings")] == [
        "plan"
    ]


def test_turn_plan_updated_row() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    params = {
        "threadId": THREAD,
        "turnId": "turn-1",
        "explanation": "Two steps",
        "plan": [
            {"step": "Read the files", "status": "completed"},
            {"step": "Write the script", "status": "inProgress"},
            {"step": "Test it", "status": "pending"},
        ],
    }
    rows = [
        e
        for e in _translate(runner, state, "turn/plan/updated", params)
        if isinstance(e, ActionEvent)
    ]
    assert len(rows) == 1
    title = rows[0].action.title
    assert title.startswith("\N{CLIPBOARD} Plan")
    assert "✓ Read the files" in title
    assert "▸ Write the script" in title
    assert "· Test it" in title
    assert rows[0].action.detail["explanation"] == "Two steps"
    # Updated in place: the same row id.
    params["plan"][1]["status"] = "completed"
    again = [
        e
        for e in _translate(runner, state, "turn/plan/updated", params)
        if isinstance(e, ActionEvent)
    ]
    assert again[0].action.id == rows[0].action.id
    assert "✓ Write the script" in again[0].action.title
    # A sub-agent thread's checklist is not this run's.
    assert not _translate(
        runner, state, "turn/plan/updated", {**params, "threadId": "subagent"}
    )


# --- preamble, exec, live sessions ---------------------------------------------


@pytest.mark.parametrize("mode", ["plan", "plan-auto"])
def test_preamble_skipped_for_codex_plan(mode: str) -> None:
    with apply_run_options(EngineRunOptions(permission_mode=mode)):
        with capture_logs() as logs:
            assert _apply_preamble("plan it", "codex", transport="app-server") == (
                "plan it"
            )
        skipped = _events(logs, "preamble.skipped")
        assert [e["reason"] for e in skipped] == ["codex_plan_mode"]
        # Exec (refused before spawn anyway) and Claude are unchanged.
        assert _apply_preamble("plan it", "codex", transport="exec") != "plan it"
        assert _apply_preamble("plan it", "claude") != "plan it"
    with apply_run_options(EngineRunOptions(permission_mode="ask")):
        assert _apply_preamble("x", "codex", transport="app-server") != "x"


@pytest.mark.parametrize("mode", ["plan", "plan-auto"])
def test_exec_refuses_plan_modes(mode: str) -> None:
    done = codex_mode_needs_app_server_event(mode, _RESUME)
    assert done is not None and not done.ok
    assert prespawn_blocked_reason(done.usage) == CODEX_MODE_NEEDS_APP_SERVER_BLOCK
    assert "Plan first" in done.error
    assert 'transport = "app-server"' in done.error
    assert done.resume == _RESUME


def test_live_session_mode_switch_respawns() -> None:
    """A stable live process can't take a turn on a thread stuck in Plan
    mode: the follow-up closes it so the message resumes on a process that
    can reset the mode. A plan-mode (experimental) process keeps it."""
    backend = live_backend("codex")
    state = gate.AppServerRunState(
        factory=gate.EventFactory("codex"), thread_id=THREAD, live_mode=True
    )
    gate._RUNS_BY_THREAD[THREAD] = state
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate, "APP_SERVER_ACTIVE", True)
            assert backend.pre_inject_close_reason(THREAD) is None
            gate._PLAN_THREADS.add(THREAD)
            assert backend.pre_inject_close_reason(THREAD) == "options_changed"
            state.experimental = True
            assert backend.pre_inject_close_reason(THREAD) is None
    finally:
        gate._RUNS_BY_THREAD.pop(THREAD, None)


def test_field_tiers_pinned_in_the_fixture() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "codex_app_server"
            / "schema-subset-0.171.0.json"
        ).read_text()
    )["subset"]
    for (key, name), tier in CODEX_APP_SERVER_FIELDS.items():
        assert fixture[key]["names"][name] == tier, (key, name)
    assert fixture["server_notification turn/plan/updated"]["tier"] == "S"
    assert fixture["server_notification thread/settings/updated"]["tier"] == "S"


# --- /planmode and /config --------------------------------------------------------


class _Runner:
    def __init__(self, transport: str) -> None:
        self.transport = transport


async def _planmode(tmp_path: Path, args: str, transport: str = "app-server") -> str:
    ctx = FakeCommandContext(
        args_text=args,
        config_path=tmp_path / "untether.toml",
        runtime=FakeTransportRuntime(default_engine="codex", runner=_Runner(transport)),
    )
    result = await PlanModeCommand().handle(ctx)  # type: ignore[arg-type]
    assert result is not None
    return result.text


async def _stored(tmp_path: Path) -> str | None:
    prefs = ChatPrefsStore(resolve_prefs_path(tmp_path / "untether.toml"))
    override = await prefs.get_engine_override(100, "codex")
    return override.permission_mode if override else None


@pytest.mark.anyio
async def test_planmode_command_codex(tmp_path: Path) -> None:
    text = await _planmode(tmp_path, "on")
    assert "Plan first" in text and await _stored(tmp_path) == "plan"
    assert "Plan first" in await _planmode(tmp_path, "show")
    await _planmode(tmp_path, "plan-auto")
    assert await _stored(tmp_path) == "plan-auto"
    await _planmode(tmp_path, "off")
    assert await _stored(tmp_path) is None
    # Bare toggle: on, then off.
    await _planmode(tmp_path, "")
    assert await _stored(tmp_path) == "plan"
    await _planmode(tmp_path, "")
    assert await _stored(tmp_path) is None
    assert "plan mode" in (await _planmode(tmp_path, "auto")).lower()
    assert await _stored(tmp_path) is None
    # Exec: refused, nothing stored.
    exec_text = await _planmode(tmp_path, "on", transport="exec")
    assert "Plan mode needs the app-server transport" in exec_text
    assert await _stored(tmp_path) is None


@pytest.mark.anyio
async def test_config_offers_plan_first_on_app_server(tmp_path: Path) -> None:
    from tests.test_config_command import (
        _app_server,
        _buttons_data,
        _last_edit_msg,
        _make_ctx,
    )
    from untether.telegram.commands.config import ConfigCommand

    state_path = tmp_path / "prefs.json"
    ctx = _app_server(
        _make_ctx(
            args_text="pm",
            text="config:pm",
            config_path=state_path,
            default_engine="codex",
        )
    )
    await ConfigCommand().handle(ctx)
    msg = _last_edit_msg(ctx)
    assert "config:pm:plan" in _buttons_data(msg)
    assert "Plan first" in msg.text
    tap = _app_server(
        _make_ctx(
            args_text="pm:plan",
            text="config:pm:plan",
            config_path=state_path,
            default_engine="codex",
            callback_query_id="cb-p",
        )
    )
    await ConfigCommand().handle(tap)
    prefs = ChatPrefsStore(resolve_prefs_path(state_path))
    override = await prefs.get_engine_override(123, "codex")
    assert override is not None and override.permission_mode == "plan"
    assert ConfigCommand.pop_late_toast("cb-p") == "Approval policy: plan first"
    # Exec's page has no Plan first.
    exec_ctx = _make_ctx(
        args_text="pm", text="config:pm", config_path=state_path, default_engine="codex"
    )
    await ConfigCommand().handle(exec_ctx)
    assert "config:pm:plan" not in _buttons_data(_last_edit_msg(exec_ctx))
