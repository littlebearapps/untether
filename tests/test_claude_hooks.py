"""#812: hook lifecycle frames (``--include-hook-events``) — pairing, the
async-hook hold predicate, and rewake attribution.

Frame shapes are the CLI 2.1.284 ones from
``docs/findings/2026-09-29-claude-rc14-cli-surface.md`` §A2/§A3.
"""

from __future__ import annotations

import time
from typing import Any

import msgspec
import pytest
from structlog.testing import capture_logs

from untether.events import EventFactory
from untether.model import TurnEvent
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    ClaudeStreamState,
    has_live_background_work,
    has_pending_async_hooks,
    release_settled_async_hooks,
    translate_claude_event,
)
from untether.schemas import claude as claude_schema

SID = "sess-hooks"


def _state(*, live: bool = True) -> tuple[ClaudeStreamState, EventFactory]:
    state = ClaudeStreamState()
    factory = state.factory  # its resume is set by the first ``init``
    state.live_mode = live
    return state, factory


def _decode(obj: dict[str, Any]) -> claude_schema.StreamJsonMessage:
    obj.setdefault("session_id", SID)
    return claude_schema.decode_stream_json_line(msgspec.json.encode(obj))


def _feed(
    state: ClaudeStreamState, factory: EventFactory, *objs: dict[str, Any]
) -> list[Any]:
    out: list[Any] = []
    for obj in objs:
        out.extend(
            translate_claude_event(
                _decode(obj), title="claude", state=state, factory=factory
            )
        )
    return out


def _started(hook_id: str, event: str, name: str | None = None) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "hook_started",
        "hook_id": hook_id,
        "hook_name": name or event,
        "hook_event": event,
        "uuid": f"u-{hook_id}",
    }


def _response(
    hook_id: str,
    event: str,
    *,
    outcome: str = "success",
    exit_code: int | None = 0,
    stderr: str = "",
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "type": "system",
        "subtype": "hook_response",
        "hook_id": hook_id,
        "hook_name": event,
        "hook_event": event,
        "output": stderr,
        "stdout": "",
        "stderr": stderr,
        "outcome": outcome,
        "uuid": f"u-{hook_id}-r",
    }
    if exit_code is not None:
        obj["exit_code"] = exit_code
    return obj


def _progress(hook_id: str, event: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "hook_progress",
        "hook_id": hook_id,
        "hook_name": event,
        "hook_event": event,
        "stdout": "x" * 10_000,
        "stderr": "",
        "output": "x" * 10_000,
    }


def _init() -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "model": "claude-fake"}


def _text(msg: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": f"m-{msg}",
            "role": "assistant",
            "model": "claude-fake",
            "content": [{"type": "text", "text": msg}],
        },
    }


def _result(answer: str, *, origin: Any = None) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 10,
        "duration_api_ms": 9,
        "num_turns": 1,
        "result": answer,
        "total_cost_usd": 0.01,
    }
    if origin is not None:
        obj["origin"] = origin
    return obj


def _first_turn(state: ClaudeStreamState, factory: EventFactory, *hooks: str) -> None:
    """Turn 1 ends with the given Stop-style hooks still pending."""
    _feed(state, factory, _init(), _text("done"))
    _feed(state, factory, *(_started(h, "Stop") for h in hooks))
    _feed(state, factory, _result("done"))
    assert state.completed_turns == 1
    assert not state.turn_open


def test_hook_started_response_pairing() -> None:
    state, factory = _state()
    _feed(state, factory, _init(), _started("h1", "PreToolUse"))
    assert set(state.pending_hooks) == {"h1"}
    assert state.pending_hooks["h1"].event == "PreToolUse"
    assert state.pending_hooks["h1"].turn == 1
    _feed(state, factory, _progress("h1", "PreToolUse"))
    assert set(state.pending_hooks) == {"h1"}  # progress changes nothing
    _feed(state, factory, _response("h1", "PreToolUse"))
    assert state.pending_hooks == {}
    assert state.hooks_started == 1
    # A response for a hook never seen (or already paired) is harmless.
    _feed(state, factory, _response("h1", "PreToolUse"), _response("zz", "Stop"))
    assert state.pending_hooks == {}


def test_hook_started_while_idle_belongs_to_the_next_turn() -> None:
    state, factory = _state()
    _first_turn(state, factory)
    _feed(state, factory, _started("h-ups", "UserPromptSubmit"))
    assert state.pending_hooks["h-ups"].turn == 2


def test_sessionstart_and_setup_never_hold() -> None:
    state, factory = _state()
    _feed(
        state,
        factory,
        _started("h-ss", "SessionStart", name="SessionStart:startup"),
        _started("h-setup", "Setup"),
        # No hook_event: the name's prefix identifies it.
        {
            **_started("h-ss2", "SessionStart", name="SessionStart:resume"),
            "hook_event": None,
        },
    )
    assert state.hooks_started == 3
    assert state.pending_hooks == {}
    _feed(state, factory, _init(), _result("done"))
    assert has_pending_async_hooks(state) is False
    # An exit-2 SessionStart response never arms a rewake hint (P2: no turn).
    _feed(
        state,
        factory,
        _response("h-ss", "SessionStart", outcome="error", exit_code=2),
    )
    assert state.hook_rewake_hint is None


def test_pending_hook_holds_and_is_separate_from_background_work() -> None:
    state, factory = _state()
    _first_turn(state, factory, "h-stop")
    assert has_pending_async_hooks(state) is True
    # D-1: hooks are not background tasks (footers, #777 panel, #346 gate).
    assert has_live_background_work(state) is False
    assert claude_mod.background_task_summary(state) is None
    _feed(state, factory, _response("h-stop", "Stop"))
    assert has_pending_async_hooks(state) is False


def _age(state: ClaudeStreamState, **ages: float) -> float:
    """Backdate hooks' ``started_at``; returns the shared ``now``."""
    now = time.monotonic()
    for hook_id, age in ages.items():
        hook = state.pending_hooks.get(hook_id) or state.expired_hooks[hook_id]
        hook.started_at = now - age
    return now


def test_release_with_no_hook_process_defers_and_still_pairs() -> None:
    """Plain ``async`` hooks: the CLI withholds their response while idle.
    With no hook process left (the caller's decision) they are deferred —
    no hold, not reported as cut short at a close — and still pair when
    the response finally lands."""
    state, factory = _state()
    _first_turn(state, factory, "h-plain", "h-young")
    now = _age(state, **{"h-plain": 5.0, "h-young": 0.2})
    moved = release_settled_async_hooks(state, now=now)
    assert [h.hook_id for h in moved] == ["h-plain"]
    assert set(state.deferred_hooks) == {"h-plain"}
    # Too young to judge: its process may not be visible yet.
    assert has_pending_async_hooks(state) is True
    assert [h.hook_id for h in claude_mod._hooks_outstanding(state)] == ["h-young"]
    _feed(state, factory, _response("h-young", "Stop"))
    assert has_pending_async_hooks(state) is False
    # The withheld response lands at the next turn / teardown.
    _feed(state, factory, _response("h-plain", "Stop"))
    assert state.deferred_hooks == {}
    assert release_settled_async_hooks(state) == []


def test_release_also_retires_expired_hooks_silently() -> None:
    """No hook process left: an expired hook is finished too — it leaves
    the close candidates without being reported as released."""
    state, factory = _state()
    state.async_hook_max_hold_s = 5.0
    _first_turn(state, factory, "h-a", "h-b")
    _age(state, **{"h-a": 6.0, "h-b": 6.5})
    with capture_logs():
        assert has_pending_async_hooks(state) is False
    assert set(state.expired_hooks) == {"h-a", "h-b"}
    assert release_settled_async_hooks(state) == []
    assert state.expired_hooks == {}
    assert set(state.deferred_hooks) == {"h-a", "h-b"}
    assert claude_mod._hooks_outstanding(state) == []


def test_no_per_hook_binding_left() -> None:
    """271bb96 bound live hook shells to hooks by spawn time; hooks started
    in the same ms made it release a still-running asyncRewake hook. The
    hold is all-or-nothing again — nothing on a hook records a process."""
    assert not hasattr(claude_mod, "release_finished_async_hooks")
    assert not hasattr(claude_mod, "_HOOK_BIND_WINDOW_S")
    assert "pid" not in claude_mod.PendingHook.__slots__
    assert "unmatched_since" not in claude_mod.PendingHook.__slots__


def test_deferred_hook_exit_2_still_arms_the_rewake_hint() -> None:
    state, factory = _state()
    _first_turn(state, factory, "h-stop")
    now = _age(state, **{"h-stop": 5.0})
    release_settled_async_hooks(state, now=now)
    assert "h-stop" in state.deferred_hooks
    _feed(state, factory, _response("h-stop", "Stop", outcome="error", exit_code=2))
    assert state.hook_rewake_hint is not None
    assert state.deferred_hooks == {}


def test_kill_switch_off_never_holds() -> None:
    state, factory = _state()
    state.hold_for_async_hooks = False
    _first_turn(state, factory, "h-stop")
    assert state.pending_hooks  # still tracked (fallback grace / notice)
    assert has_pending_async_hooks(state) is False


def test_hold_expires_once_for_the_whole_hold() -> None:
    """All or nothing: an older unpaired hook past the bound doesn't expire
    on its own — any of them could be the running one — so the hold lasts
    until the NEWEST is past it, then one WARN covers every unpaired hook,
    labelled by the live hook processes, not the candidates."""
    state, factory = _state()
    state.async_hook_max_hold_s = 5.0
    _feed(state, factory, _started("h-ups", "UserPromptSubmit"))
    _first_turn(state, factory, "h-stop", "h-young")
    _age(state, **{"h-ups": 9.0, "h-stop": 6.0, "h-young": 1.0})
    state.live_hook_processes = 1
    with capture_logs() as logs:
        assert has_pending_async_hooks(state) is True  # h-young still in bound
    assert [e for e in logs if e["event"] == "claude.hook.hold_expired"] == []
    assert set(state.pending_hooks) == {"h-ups", "h-stop", "h-young"}
    _age(state, **{"h-young": 5.0})
    with capture_logs() as logs:
        assert has_pending_async_hooks(state) is False
        assert has_pending_async_hooks(state) is False  # nothing left to log
    expired = [e for e in logs if e["event"] == "claude.hook.hold_expired"]
    assert len(expired) == 1
    assert expired[0]["log_level"] == "warning"
    assert expired[0]["live_hook_processes"] == 1
    assert expired[0]["pending_hooks"] == 3
    assert expired[0]["hook_events"] == ["Stop", "UserPromptSubmit"]
    assert sorted(expired[0]["hook_ids"]) == ["h-stop", "h-ups", "h-young"]
    assert expired[0]["held_s"] >= 9.0
    assert expired[0]["max_hold_s"] == 5.0
    assert state.pending_hooks == {}
    assert set(state.expired_hooks) == {"h-ups", "h-stop", "h-young"}
    # A late response for an expired hook still clears it.
    _feed(state, factory, _response("h-stop", "Stop"))
    assert "h-stop" not in state.expired_hooks


def test_hold_expiry_unreadable_process_table_logs_none() -> None:
    state, factory = _state()
    state.async_hook_max_hold_s = 5.0
    _first_turn(state, factory, "h-stop")
    _age(state, **{"h-stop": 6.0})
    with capture_logs() as logs:
        assert has_pending_async_hooks(state) is False
    expired = [e for e in logs if e["event"] == "claude.hook.hold_expired"]
    assert len(expired) == 1 and expired[0]["live_hook_processes"] is None


def test_hold_bound_zero_disables_the_hold() -> None:
    state, factory = _state()
    state.async_hook_max_hold_s = 0.0
    _first_turn(state, factory, "h-stop")
    assert has_pending_async_hooks(state) is False


def test_rewake_hint_ttl_and_clear_on_turn_open() -> None:
    state, factory = _state()
    _first_turn(state, factory, "h-stop")
    with capture_logs() as logs:
        events = _feed(
            state,
            factory,
            _response("h-stop", "Stop", outcome="error", exit_code=2, stderr="x"),
        )
    assert events == []
    assert state.hook_rewake_hint is not None
    assert state.hook_rewake_hint[:2] == ("Stop", "Stop")
    assert any(e["event"] == "claude.hook.rewake_signal" for e in logs)
    with capture_logs() as logs:
        events = _feed(state, factory, _started("h-ups", "UserPromptSubmit"), _init())
    opened = [e for e in events if isinstance(e, TurnEvent)]
    assert len(opened) == 1
    assert opened[0].reason == "hook_rewake"
    assert opened[0].detail == {"hook": "Stop", "hook_event": "Stop"}
    assert state.hook_rewake_hint is None  # spent on open
    assert any(
        e["event"] == "claude.turn.hook_rewake" and e["attributed"] == "open"
        for e in logs
    )
    events = _feed(
        state,
        factory,
        _text("finding"),
        _result("finding", origin={"kind": "task-notification"}),
    )
    done = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert done[0].reason == "hook_rewake"


def test_stale_hint_is_cleared_and_retro_attributed_at_result() -> None:
    state, factory = _state()
    _first_turn(state, factory, "h-stop")
    _feed(state, factory, _response("h-stop", "Stop", outcome="error", exit_code=2))
    assert state.hook_rewake_hint is not None
    name, event, _ts = state.hook_rewake_hint
    state.hook_rewake_hint = (name, event, time.monotonic() - 60.0)  # past TTL
    events = _feed(state, factory, _init())
    assert [e.reason for e in events if isinstance(e, TurnEvent)] == ["unknown"]
    assert state.hook_rewake_hint is None
    with capture_logs() as logs:
        events = _feed(
            state,
            factory,
            _text("finding"),
            _result("finding", origin={"kind": "task-notification"}),
        )
    done = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert done[0].reason == "hook_rewake"
    assert done[0].detail["hook_event"] == "Stop"
    assert done[0].detail["retro_attributed"] is True
    assert any(
        e["event"] == "claude.turn.hook_rewake" and e["attributed"] == "result"
        for e in logs
    )


def test_retro_needs_task_notification_origin() -> None:
    state, factory = _state()
    _first_turn(state, factory, "h-stop")
    _feed(state, factory, _response("h-stop", "Stop", outcome="error", exit_code=2))
    name, event, _ts = state.hook_rewake_hint  # type: ignore[misc]
    state.hook_rewake_hint = (name, event, time.monotonic() - 60.0)
    _feed(state, factory, _init(), _text("x"))
    # A non-dict origin must not break decoding or attribute the turn.
    events = _feed(state, factory, _result("x", origin="task-notification"))
    done = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert done[0].reason == "unknown"
    assert state.turn_hook_hint is None


def test_hint_mid_turn_from_earlier_turn_hook_retro_attributes() -> None:
    """The rewake's exit-2 response lands just after the turn opened."""
    state, factory = _state()
    _first_turn(state, factory, "h-stop")
    _feed(state, factory, _init())  # opens turn 2 as unknown
    # The open turn's own sync Stop hook exiting 2 is NOT a rewake signal...
    _feed(
        state,
        factory,
        _started("h-own", "Stop"),
        _response("h-own", "Stop", outcome="error", exit_code=2),
    )
    assert state.turn_hook_hint is None
    # ...but turn 1's async hook is.
    _feed(state, factory, _response("h-stop", "Stop", outcome="error", exit_code=2))
    assert state.turn_hook_hint is not None
    events = _feed(state, factory, _result("x", origin={"kind": "task-notification"}))
    done = [e for e in events if isinstance(e, TurnEvent) and e.phase == "completed"]
    assert done[0].reason == "hook_rewake"


def test_cancelled_response_logs_info() -> None:
    state, factory = _state()
    _first_turn(state, factory, "h-async")
    with capture_logs() as logs:
        _feed(
            state,
            factory,
            _response("h-async", "PostToolUse", outcome="cancelled", exit_code=1),
        )
    cancelled = [e for e in logs if e["event"] == "claude.hook.cancelled"]
    assert cancelled and cancelled[0]["log_level"] == "info"
    assert state.hook_rewake_hint is None


def test_hook_frames_emit_no_untether_events() -> None:
    # Both before the first result (base path) and inside a live session.
    state, factory = _state()
    frames = [
        _started("h1", "PreToolUse"),
        _progress("h1", "PreToolUse"),
        _response("h1", "PreToolUse"),
        _started("h2", "Stop"),
        {"type": "system", "subtype": "hook_future_thing", "hook_id": "h9"},
    ]
    assert _feed(state, factory, *frames) == []
    _feed(state, factory, _init(), _result("done"))
    assert _feed(state, factory, *frames) == []
    # Non-live (legacy -p) mode too.
    state2, factory2 = _state(live=False)
    assert _feed(state2, factory2, *frames) == []


def test_hook_output_fields_are_not_decoded() -> None:
    event = _decode(_progress("h1", "PreToolUse"))
    assert isinstance(event, claude_schema.StreamSystemMessage)
    assert not hasattr(event, "stdout")
    assert not hasattr(event, "output")
    assert event.hook_id == "h1"


def test_hook_fields_tolerate_shape_drift() -> None:
    state, factory = _state()
    weird = {
        "type": "system",
        "subtype": "hook_response",
        "hook_id": 7,
        "hook_name": ["x"],
        "hook_event": {"a": 1},
        "outcome": 3,
        "exit_code": "2",
    }
    assert _feed(state, factory, weird) == []
    assert state.hook_rewake_hint is None


def test_pending_dict_bounded() -> None:
    state, factory = _state()
    cap = claude_mod._PENDING_HOOKS_MAX
    with capture_logs() as logs:
        _feed(
            state, factory, *(_started(f"h{i}", "PreToolUse") for i in range(cap + 5))
        )
    assert len(state.pending_hooks) == cap
    assert "h0" not in state.pending_hooks  # oldest evicted first
    assert f"h{cap + 4}" in state.pending_hooks
    evicted = [e for e in logs if e["event"] == "claude.hook.pending_evicted"]
    assert len(evicted) == 5
    assert all(e["log_level"] == "debug" for e in evicted)


def test_scan_hook_children_labels_hook_processes(monkeypatch) -> None:
    from untether.utils import proc_diag

    tree = {10: [11], 20: []}
    labels = {11: "stop.sh", 20: None, 10: None}
    monkeypatch.setattr(proc_diag, "find_descendants", lambda pid: tree.get(pid, []))
    monkeypatch.setattr(proc_diag, "hook_script_label", lambda pid: labels.get(pid))
    assert claude_mod._scan_hook_children([10, 20]) == ["stop.sh"]

    def boom(pid: int) -> list[int]:
        raise RuntimeError("proc gone")

    monkeypatch.setattr(proc_diag, "find_descendants", boom)
    assert claude_mod._scan_hook_children([10]) == []  # never raises


def test_session_summary_hook_fields() -> None:
    from types import SimpleNamespace

    from untether.runner_bridge import _hook_summary_fields

    state = ClaudeStreamState()
    state.hooks_started = 7
    assert _hook_summary_fields(SimpleNamespace(engine_state=state)) == {
        "hooks_started": 7
    }
    # Other engines (no hook tracking) add nothing.
    assert _hook_summary_fields(SimpleNamespace(engine_state=object())) == {}
    assert _hook_summary_fields(None) == {}


def _cli_scan(children: dict[int, tuple[list[str], int, float]]) -> Any:
    """CLI pid/pgid 4242; children: pid -> (argv, pgid, started_by)."""
    from untether.utils.proc_diag import CliChild, CliScan

    return CliScan(
        cli_pid=4242,
        cli_pgid=4242,
        children={
            pid: CliChild(argv=argv, pgid=pgid, start_id=pid * 7, started_by=start)
            for pid, (argv, pgid, start) in children.items()
        },
    )


@pytest.mark.anyio
async def test_capture_cli_baseline_once_and_unreadable_leaves_none(
    monkeypatch: Any,
) -> None:
    """#812: the baseline (MCP servers up by system/init) is recorded once
    per process, as (pid, start) of the CLI's non-detached children; an
    unreadable table leaves None, so nothing is exempt and the hold errs
    long (bounded)."""
    scans: list[int | None] = []
    tables: list[Any] = [
        None,
        _cli_scan(
            {
                10: (["node", "srv.js"], 4242, 1.0),
                11: (["npm", "exec", "x-mcp"], 4242, 1.0),
                # A hook alive at init (UserPromptSubmit): detached.
                12: (["sleep", "30"], 12, 2.0),
            }
        ),
        _cli_scan({99: (["sleep", "1"], 99, 3.0)}),
    ]

    async def fake_children(pid: int | None) -> Any:
        scans.append(pid)
        return tables[len(scans) - 1]

    monkeypatch.setattr(claude_mod, "_cli_children", fake_children)
    state, _ = _state()
    await claude_mod.capture_cli_baseline(state, 4242)
    assert state.cli_baseline_children is None
    await claude_mod.capture_cli_baseline(state, 4242)
    assert state.cli_baseline_children == frozenset({(10, 70), (11, 77)})
    await claude_mod.capture_cli_baseline(state, 4242)  # already captured
    assert state.cli_baseline_children == frozenset({(10, 70), (11, 77)})
    assert scans == [4242, 4242]
    # The baseline feeds the evidence filter; the hook alive at init counts.
    scans.clear()
    tables[:] = [
        _cli_scan(
            {
                10: (["node", "srv.js"], 4242, 1.0),
                12: (["sleep", "30"], 12, 2.0),
                13: (["sleep", "120"], 13, 5.0),
            }
        )
    ]
    assert sorted(await claude_mod._hook_processes(state, 4242) or []) == [12, 13]


@pytest.mark.anyio
async def test_hook_processes_ignore_children_older_than_the_oldest_hook(
    monkeypatch: Any,
) -> None:
    """#812: ``since`` is the oldest unpaired (pending or expired) hook's
    ``hook_started``; a detached child that started well before it can't be
    any unpaired hook's process."""
    from untether.utils.proc_diag import hook_clock

    now = hook_clock()
    table = _cli_scan(
        {
            20: (["sleep", "600"], 20, now - 120),  # long before any hook
            21: (["sleep", "600"], 21, now - 1),  # the hook
        }
    )

    async def fake_children(pid: int | None) -> Any:
        return table

    monkeypatch.setattr(claude_mod, "_cli_children", fake_children)
    state, _ = _state()
    # No unpaired hook: no timing exemption.
    assert sorted(await claude_mod._hook_processes(state, 4242) or []) == [20, 21]
    state.pending_hooks["h1"] = claude_mod.PendingHook(
        hook_id="h1",
        name="Stop",
        event="Stop",
        started_at=time.monotonic(),
        turn=1,
        started_clock=now - 2,
    )
    assert await claude_mod._hook_processes(state, 4242) == [21]
    # An older expired hook widens the window back.
    state.expired_hooks["h0"] = claude_mod.PendingHook(
        hook_id="h0",
        name="Stop",
        event="Stop",
        started_at=time.monotonic(),
        turn=1,
        started_clock=now - 200,
    )
    assert sorted(await claude_mod._hook_processes(state, 4242) or []) == [20, 21]
    # A hook with no recorded clock start: no timing exemption at all.
    state.expired_hooks.clear()
    state.pending_hooks["h2"] = claude_mod.PendingHook(
        hook_id="h2", name="Stop", event="Stop", started_at=time.monotonic(), turn=1
    )
    assert sorted(await claude_mod._hook_processes(state, 4242) or []) == [20, 21]


def test_hook_started_records_the_shared_clock() -> None:
    """#812: a pending hook's start is also taken on ``hook_clock()`` (the
    clock child start times use; it counts through system sleep)."""
    from untether.utils.proc_diag import hook_clock

    state, factory = _state()
    before = hook_clock()
    _feed(state, factory, _started("h9", "Stop"))
    hook = state.pending_hooks["h9"]
    assert hook.started_clock is not None
    assert before <= hook.started_clock <= hook_clock()
