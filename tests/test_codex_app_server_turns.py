"""Codex app-server turn state machine and ownership (#964, phase 964-3a).

The runner-side groundwork for live sessions (plan §16 items 2-5, the 3a
half of the 964-3 split): a process that stays up after its first
``turn/completed`` (``keep_live``; 964-3b decides when, with the live
backend, idle close and death notice) runs later turns as
``TurnEvent(started) → ActionEvent* → TurnEvent(completed)`` segments.

- Item 2: a follow-up ``turn/start`` is attributed to its message by a
  request-id / turn-id state machine (one start at a time, the waiter
  registered before the write, an early ``turn/started``, a lost response).
- Item 3: ``/cancel`` interrupts the *current* turn by its id, in a second
  turn, during an approval and in the window before a started turn is known.
- Item 4: the thread stays owned until the process has exited; removal checks
  identity.
- Item 5: cards belong to the turn they arrived in; turn-local state resets.

Also the ``sleep`` item (``clock.sleep``, seen in 964-0): a row whose
deadline the bridge's stall monitor reads as an expected wait.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests import test_codex_app_server_runner as _rt
from tests.test_codex_app_server_runner import (
    THREAD,
    Fake,
    _app_state,
    _bare_runner,
    _translate,
)
from untether.interactive.registry import Decision
from untether.model import ActionEvent, CompletedEvent, ResumeToken, TurnEvent
from untether.runners import codex_app_server as gate
from untether.runners import codex_approvals as approvals
from untether.runners.codex_rpc import CodexRpcPeer, Response
from untether.runners.run_options import apply_run_options

project = _rt.project
gate_calls = _rt.gate_calls
_fresh_cache = _rt._fresh_cache

RESUME = ResumeToken(engine="codex", value=THREAD)


def _live_runner(fake: Fake, **attrs: Any) -> gate.CodexAppServerRunner:
    runner = _rt._runner(fake, **attrs)
    runner.keep_live = True
    return runner


class LiveRun:
    """Drive a live run: events land in ``events`` from a consumer task, so
    a follow-up or a cancel can be sent while the reader keeps reading."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner
        self.events: list[Any] = []
        self.scope = anyio.CancelScope()
        self.done = anyio.Event()

    async def consume(self) -> None:
        try:
            with self.scope:
                agen = self.runner.run("hello", RESUME)
                try:
                    async for evt in agen:
                        self.events.append(evt)
                finally:
                    with anyio.CancelScope(shield=True):
                        await agen.aclose()
        finally:
            self.done.set()

    async def until(self, pred: Any, timeout: float = 10) -> None:
        with anyio.fail_after(timeout):
            while not any(pred(e) for e in self.events):
                await anyio.sleep(0.01)

    @property
    def state(self) -> Any:
        return gate._RUNS_BY_THREAD[THREAD]

    async def close(self) -> None:
        """End the process with the live session's close (964-3b)."""
        if not self.done.is_set():
            await gate.close_live_session(THREAD, "new")
        with anyio.fail_after(10):
            await self.done.wait()


def _completed_run(e: Any) -> bool:
    return isinstance(e, CompletedEvent)


def _turns(events: list[Any]) -> list[tuple]:
    return [
        (e.phase, e.turn, e.reason, e.command_uuid, e.ok, e.answer)
        for e in events
        if isinstance(e, TurnEvent)
    ]


def _turn_done(n: int):
    return lambda e: isinstance(e, TurnEvent) and e.phase == "completed" and e.turn == n


def _turn_started(n: int):
    return lambda e: isinstance(e, TurnEvent) and e.phase == "started" and e.turn == n


async def _live(fake: Fake, body: Any, **attrs: Any) -> LiveRun:
    run = LiveRun(_live_runner(fake, **attrs))
    with apply_run_options(None), anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume)
            await run.until(_completed_run)
            await body(run)
            await run.close()
    return run


# --- item 2: follow-up turns and their attribution ---------------------------


@pytest.mark.anyio
async def test_first_turn_completes_run_and_process_stays(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)

    async def body(run: LiveRun) -> None:
        await anyio.sleep(0.2)
        assert not run.done.is_set()  # still reading
        state = run.state
        assert state.completed_turns == 1 and state.turn_open is False
        assert state.live_mode is True

    run = await _live(fake, body)
    assert sum(isinstance(e, CompletedEvent) for e in run.events) == 1
    assert gate._RUNS_BY_THREAD == {}


@pytest.mark.anyio
async def test_followup_turn_start_is_bracketed_with_command_uuid(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        outcomes.append(await gate.start_followup(run.state, "next", command_uuid="f1"))
        await run.until(_turn_done(2))

    run = await _live(fake, body)
    assert outcomes == ["started"]
    # Nothing but the turn: the thread keeps what thread/resume set (§5.4).
    assert fake.requests("turn/start")[1] == {
        "threadId": THREAD,
        "input": [{"type": "text", "text": "next"}],
        "clientUserMessageId": "f1",
    }
    assert _turns(run.events) == [
        ("started", 2, "followup", "f1", None, None),
        ("completed", 2, "followup", "f1", True, "reply 2"),
    ]
    assert sum(isinstance(e, CompletedEvent) for e in run.events) == 1


@pytest.mark.anyio
async def test_concurrent_starts_write_only_one(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, followup={"hold": True})
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        async def one(uuid: str) -> None:
            outcomes.append(
                await gate.start_followup(run.state, uuid, command_uuid=uuid)
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(one, "a")
            tg.start_soon(one, "b")
        # a turn is open now: a third start is refused too
        outcomes.append(await gate.start_followup(run.state, "c", command_uuid="c"))

    await _live(fake, body)
    assert sorted(outcomes) == ["busy", "busy", "started"]
    assert len(fake.requests("turn/start")) == 2  # the first turn + one


@pytest.mark.anyio
async def test_early_turn_started_is_attributed(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """P9 (0.171.0) saw turn/started before the turn/start response."""
    fake = Fake(tmp_path, followup={"early_started": True})
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        outcomes.append(await gate.start_followup(run.state, "x", command_uuid="f1"))
        await run.until(_turn_done(2))

    run = await _live(fake, body)
    assert outcomes == ["started"]
    assert _turns(run.events)[0] == ("started", 2, "followup", "f1", None, None)


@pytest.mark.anyio
async def test_answer_after_the_turn_completed_still_settles_the_start(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """The follow-up's turn starts and finishes before Codex's answer to its
    turn/start: the late answer is still that start's (never the run's error)
    and the next follow-up isn't refused as busy."""
    fake = Fake(tmp_path, followup={"late_response": True})
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        outcomes.append(await gate.start_followup(run.state, "x", command_uuid="f1"))
        await run.until(_turn_done(2))
        assert run.state.pending_start is None
        outcomes.append(await gate.start_followup(run.state, "y", command_uuid="f2"))
        await run.until(_turn_done(3))

    run = await _live(fake, body)
    assert outcomes == ["started", "started"]
    assert [t[3] for t in _turns(run.events)] == ["f1", "f1", "f2", "f2"]


@pytest.mark.anyio
async def test_lost_start_response_with_a_started_turn_is_started(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "FOLLOWUP_RESPONSE_S", 0.3)
    fake = Fake(tmp_path, followup={"lost_response": True})
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        outcomes.append(await gate.start_followup(run.state, "x", command_uuid="f1"))
        await run.until(_turn_done(2))

    run = await _live(fake, body)
    assert outcomes == ["started"]
    assert _turns(run.events)[0][3] == "f1"


@pytest.mark.anyio
async def test_lost_start_response_and_no_turn_is_unconfirmed(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sent but never answered: reported unconfirmed (the caller treats it as
    written, so it is never sent twice; §16 item 1's rule)."""
    monkeypatch.setattr(gate, "FOLLOWUP_RESPONSE_S", 0.3)
    fake = Fake(tmp_path, followup={"start_delay": 2})
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        with capture_logs() as logs:
            outcomes.append(
                await gate.start_followup(run.state, "x", command_uuid="f1")
            )
        assert [e["event"] for e in logs if "followup" in e["event"]] == [
            "codex.app_server.followup_unconfirmed"
        ]
        # It does start later, and is still attributed.
        await run.until(_turn_done(2), timeout=10)

    run = await _live(fake, body)
    assert outcomes == ["unconfirmed"]
    assert _turns(run.events)[0] == ("started", 2, "followup", "f1", None, None)


@pytest.mark.anyio
async def test_refused_start_is_refused_and_never_ends_the_run(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, followup={"error": "thread is busy"})
    outcomes: list[str] = []

    async def body(run: LiveRun) -> None:
        outcomes.append(await gate.start_followup(run.state, "x", command_uuid="f1"))
        assert run.state.pending_start is None
        assert not run.done.is_set()

    run = await _live(fake, body)
    assert outcomes == ["refused"]
    assert _turns(run.events) == []


def test_response_naming_an_open_turn_of_another_owner_is_absorbed() -> None:
    """§16 item 2: Codex steered the follow-up into a turn that was already
    running (``start_or_steer_turn``): no turn of its own, one absorbed row."""
    runner = _bare_runner()
    state = _app_state(runner)
    state.live_mode = True
    state.completed_turns = 1
    state.turn_open = True
    state.turn_id = "turn-s"  # a server-started turn
    wait = gate._StartWait(command_uuid="f1", text="hello there")
    wait.rid = 9
    state.pending_start = wait
    state.followup_rids.add(9)
    state.inbound = Response(9, "turn/start", result={"turn": {"id": "turn-s"}})
    events = runner.translate(b"", state=state, resume=None, found_session=None)
    assert wait.absorbed and wait.done.is_set()
    assert state.pending_start is None
    rows = [e for e in events if isinstance(e, ActionEvent)]
    assert [r.action.detail for r in rows] == [
        {"absorbed_command_uuid": "f1", "steer": True}
    ] * 2


def test_server_started_turn_is_unknown_reason() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    state.live_mode = True
    state.completed_turns = 1
    turn = {"id": "turn-g", "status": "inProgress", "items": []}
    [evt] = _translate(
        runner, state, "turn/started", {"threadId": THREAD, "turn": turn}
    )
    assert isinstance(evt, TurnEvent)
    assert (evt.phase, evt.turn, evt.reason, evt.command_uuid) == (
        "started",
        2,
        "unknown",
        None,
    )
    assert state.turn_open is True


# --- item 3: /cancel targets the current turn --------------------------------


def _interrupts(fake: Fake) -> list[dict]:
    return fake.requests("turn/interrupt")


@pytest.mark.anyio
async def test_cancel_during_second_turn_interrupts_that_turn(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, followup={"hold": True})

    async def body(run: LiveRun) -> None:
        await gate.start_followup(run.state, "x", command_uuid="f1")
        await run.until(_turn_started(2))
        with capture_logs() as logs:
            run.scope.cancel()
            await run.done.wait()
        [log] = [e for e in logs if e["event"] == "codex.app_server.interrupt"]
        assert log["ok"] is True and log["turn_id"] == "turn-2"

    await _live(fake, body)
    assert _interrupts(fake) == [{"threadId": THREAD, "turnId": "turn-2"}]


@pytest.mark.anyio
async def test_cancel_during_an_approval_in_the_second_turn(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    request = {
        "id": 70,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": THREAD, "itemId": "c9", "command": "rm -rf build"},
    }
    fake = Fake(tmp_path, followup={"approval": request, "hold": True})

    async def body(run: LiveRun) -> None:
        await gate.start_followup(run.state, "x", command_uuid="f1")
        await run.until(
            lambda e: isinstance(e, ActionEvent)
            and (e.action.detail or {}).get("request_type") == request["method"]
        )
        assert run.state.cards.pending
        run.scope.cancel()
        await run.done.wait()

    await _live(fake, body)
    assert _interrupts(fake) == [{"threadId": THREAD, "turnId": "turn-2"}]


@pytest.mark.anyio
async def test_cancel_in_the_pending_start_window(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """The follow-up is written but Codex hasn't said which turn it is yet:
    the cancel waits (bounded) for that turn and interrupts it."""
    fake = Fake(tmp_path, followup={"start_delay": 0.5, "hold": True})

    async def body(run: LiveRun) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: gate.start_followup(run.state, "x", command_uuid="f1")
            )
            with anyio.fail_after(5):
                while run.state.pending_start is None:
                    await anyio.sleep(0.01)
            run.scope.cancel()
            await run.done.wait()

    await _live(fake, body)
    assert _interrupts(fake) == [{"threadId": THREAD, "turnId": "turn-2"}]


@pytest.mark.anyio
async def test_cancel_while_idle_interrupts_nothing(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path)

    async def body(run: LiveRun) -> None:
        run.scope.cancel()
        await run.done.wait()

    await _live(fake, body)
    assert _interrupts(fake) == []


@pytest.mark.anyio
async def test_a_late_completion_of_an_old_turn_never_ends_the_current_one(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, followup={"late_old_completion": True})

    async def body(run: LiveRun) -> None:
        await gate.start_followup(run.state, "x", command_uuid="f1")
        await run.until(_turn_done(2))

    with capture_logs() as logs:
        run = await _live(fake, body)
    assert _turns(run.events) == [
        ("started", 2, "followup", "f1", None, None),
        ("completed", 2, "followup", "f1", True, "reply 2"),
    ]
    assert [
        e["turn_id"]
        for e in logs
        if e["event"] == "codex.app_server.stale_turn_completed"
    ] == ["turn-1"]


# --- item 4: ownership until the process is gone ------------------------------


@pytest.mark.anyio
async def test_thread_stays_owned_until_the_process_exits(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    """A per-run process that lingers after stdin closes: no steer target
    (closing), but the thread is still owned until the process is gone."""
    fake = Fake(tmp_path, linger=True)
    runner = _rt._runner(fake, exit_grace_s=1.0)
    events: list[Any] = []
    seen: list[tuple[bool, bool]] = []

    async def consume() -> None:
        async for evt in runner.run("hello", RESUME):
            events.append(evt)  # noqa: PERF401 — read while the run goes on

    with apply_run_options(None), anyio.fail_after(20):
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            while not any(isinstance(e, CompletedEvent) for e in events):
                await anyio.sleep(0.01)
            await anyio.sleep(0.3)  # in the exit grace
            state = gate._RUNS_BY_THREAD.get(THREAD)
            seen.append((state is not None, gate.STEER_BACKEND.get(THREAD) is not None))
    assert seen == [(True, False)]
    assert gate._RUNS_BY_THREAD == {}


def test_release_checks_identity() -> None:
    runner = _bare_runner()
    old, new = _app_state(runner), _app_state(runner)
    gate._RUNS_BY_THREAD[THREAD] = new  # a newer run took the thread over
    gate._release_thread(old)
    assert gate._RUNS_BY_THREAD[THREAD] is new
    gate._release_thread(new)
    assert gate._RUNS_BY_THREAD == {}


@pytest.mark.anyio
async def test_teardown_during_a_cancel_releases_after_exit(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(tmp_path, followup={"hold": True})
    procs: list[Any] = []

    async def body(run: LiveRun) -> None:
        await gate.start_followup(run.state, "x", command_uuid="f1")
        await run.until(_turn_started(2))
        procs.append(run.state.proc)
        run.scope.cancel()
        await run.done.wait()

    await _live(fake, body)
    assert procs[0].returncode is not None
    assert gate._RUNS_BY_THREAD == {}


# --- item 5: cards belong to their turn; turn-local state ---------------------


def _request(rpc_id: int) -> dict:
    return {
        "id": rpc_id,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": THREAD, "itemId": f"c{rpc_id}", "command": "ls"},
    }


@pytest.mark.anyio
async def test_card_belongs_to_its_turn_and_a_stale_tap_writes_nothing(
    tmp_path: Path, project: Path, gate_calls: list[str]
) -> None:
    fake = Fake(
        tmp_path,
        approvals=[{"request": _request(5), "withdraw": True}],
        followup={"approval": _request(6), "hold": True, "late_old_completion": True},
    )
    taps: list[bool] = []

    async def body(run: LiveRun) -> None:
        state = run.state
        await gate.start_followup(state, "x", command_uuid="f1")
        with anyio.fail_after(5):
            while not state.cards.pending:
                await anyio.sleep(0.01)
        await anyio.sleep(0.3)  # turn-1's late completion has been read
        # turn-2's card survives turn-1's late completion
        [(rid, pending)] = list(state.cards.pending.items())
        assert pending.turn_id == "turn-2"
        # a tap on turn-1's (closed) card writes nothing
        old_ids = {
            (e.action.detail or {}).get("request_id")
            for e in run.events
            if isinstance(e, ActionEvent) and (e.action.detail or {}).get("request_id")
        } - {rid}
        taps.extend(
            [
                await approvals.RESPONDER.respond(
                    old,
                    state.cards.session_key,
                    Decision(approved=True, action="approve"),
                )
                for old in old_ids
            ]
        )
        run.scope.cancel()
        await run.done.wait()

    await _live(fake, body)
    assert taps == [False]


def test_turn_local_state_resets_on_a_new_turn() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    state.live_mode = True
    state.completed_turns = 1
    state.card_stop = True
    state.ctx_emitted = (5, 1000)
    state.compactions = 1  # an earlier turn's /compact: never this turn's
    state.file_changes["x"] = []
    turn = {"id": "turn-2", "status": "inProgress", "items": []}
    _translate(runner, state, "turn/started", {"threadId": THREAD, "turn": turn})
    assert state.card_stop is False
    assert state.ctx_emitted is None
    assert state.compactions == 0
    _translate(
        runner,
        state,
        "turn/completed",
        {"threadId": THREAD, "turn": {**turn, "status": "completed"}},
    )
    assert state.file_changes == {}


# --- the ``sleep`` item (clock.sleep) -----------------------------------------


def test_sleep_item_row_and_wakeup_deadline() -> None:
    runner = _bare_runner()
    state = _app_state(runner)
    item = {"type": "sleep", "id": "call_1", "durationMs": 90_000}
    params = {"threadId": THREAD, "turnId": "turn-1", "item": item}
    before = time.monotonic()
    [row] = _translate(runner, state, "item/started", params)
    assert (row.phase, row.action.id, row.action.kind) == ("started", "call_1", "tool")
    assert row.action.title == "\N{HOURGLASS WITH FLOWING SAND} sleeping 1m 30s"
    deadline = state.live_wakeups["call_1"]
    assert before + 89 < deadline < time.monotonic() + 91
    [done] = _translate(runner, state, "item/completed", params)
    assert done.phase == "completed" and done.ok
    assert state.live_wakeups == {}


# --- Codex adversarial review of 964-3a (2026-11-26) --------------------------


class _Sink:
    """A peer transport: records each line; ``gate`` holds the write."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self.gate: anyio.Event | None = None

    async def send(self, data: bytes) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.lines.append(json.loads(data))


def _live_state(sink: _Sink) -> tuple[Any, Any]:
    runner = _bare_runner()
    state = _app_state(runner)
    state.live_mode = True
    state.completed_turns = 1
    state.peer = CodexRpcPeer(sink.send)
    return runner, state


def _turn(turn_id: str, status: str = "inProgress") -> dict:
    return {"threadId": THREAD, "turn": {"id": turn_id, "status": status, "items": []}}


def _answer(runner: Any, state: Any, rid: int, turn_id: str) -> list[Any]:
    state.inbound = Response(rid, "turn/start", result={"turn": {"id": turn_id}})
    try:
        return runner.translate(b"", state=state, resume=None, found_session=None)
    finally:
        state.inbound = None


async def _wait_for(pred: Any) -> None:
    with anyio.fail_after(2):
        while not pred():
            await anyio.sleep(0.001)


@pytest.mark.anyio
async def test_late_answer_of_a_dropped_start_never_moves_the_current_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review H1: the follow-up's turn started, but Codex's answer came after
    the waiter timed out, and a later turn is running when it lands. It must
    not become the current turn id (``/cancel`` would interrupt the old turn
    and the real turn's completion would be rejected as stale)."""
    monkeypatch.setattr(gate, "FOLLOWUP_RESPONSE_S", 0.05)
    sink = _Sink()
    runner, state = _live_state(sink)
    outcome: list[str] = []

    async def start() -> None:
        outcome.append(await gate.start_followup(state, "x", command_uuid="f1"))

    async with anyio.create_task_group() as tg:
        tg.start_soon(start)
        await _wait_for(lambda: sink.lines)
        _translate(runner, state, "turn/started", _turn("turn-2"))
    assert outcome == ["started"] and state.pending_start is None
    rid = sink.lines[0]["id"]
    _translate(runner, state, "turn/completed", _turn("turn-2", "completed"))
    _translate(runner, state, "turn/started", _turn("turn-3"))  # Codex's own
    assert _answer(runner, state, rid, "turn-2") == []
    assert state.turn_id == "turn-3"
    [done] = _translate(runner, state, "turn/completed", _turn("turn-3", "completed"))
    assert isinstance(done, TurnEvent) and (done.phase, done.turn) == ("completed", 3)


@pytest.mark.anyio
async def test_turn_started_while_the_start_is_still_unwritten_is_not_the_followups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review H2, the real race: the follow-up is registered but its write is
    held (the RPC write lock), and Codex starts a turn of its own. That turn
    is ``unknown``; Codex then steers the follow-up into it (its answer names
    that turn), which is an absorption: one row, no turn of its own."""
    sink = _Sink()
    sink.gate = anyio.Event()
    runner, state = _live_state(sink)
    outcome: list[str] = []

    async def start() -> None:
        outcome.append(await gate.start_followup(state, "hi", command_uuid="f1"))

    async with anyio.create_task_group() as tg:
        tg.start_soon(start)
        await _wait_for(lambda: state.pending_start is not None)
        [opened] = _translate(runner, state, "turn/started", _turn("turn-a"))
        assert (opened.reason, opened.command_uuid) == ("unknown", None)
        sink.gate.set()  # the write goes out now
        await _wait_for(lambda: sink.lines)
        rows = _answer(runner, state, sink.lines[0]["id"], "turn-a")
    assert outcome == ["absorbed"]
    assert [r.action.detail["absorbed_command_uuid"] for r in rows] == ["f1", "f1"]
    [done] = _translate(runner, state, "turn/completed", _turn("turn-a", "completed"))
    assert (done.reason, done.command_uuid) == ("unknown", None)


@pytest.mark.anyio
async def test_an_answer_naming_another_turn_re_labels_the_attribution() -> None:
    """Review H2, reconcile: a turn attributed to the written follow-up was
    not its turn (the answer names another). The open one is no longer the
    follow-up's, and the named turn is, when it starts."""
    sink = _Sink()
    runner, state = _live_state(sink)
    outcome: list[str] = []

    async def start() -> None:
        outcome.append(await gate.start_followup(state, "hi", command_uuid="f1"))

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(start)
            await _wait_for(lambda: sink.lines)
            [first] = _translate(runner, state, "turn/started", _turn("turn-a"))
            assert first.command_uuid == "f1"  # written: tentatively its turn
            _answer(runner, state, sink.lines[0]["id"], "turn-b")
        [done_a] = _translate(
            runner, state, "turn/completed", _turn("turn-a", "completed")
        )
        [started_b] = _translate(runner, state, "turn/started", _turn("turn-b"))
    assert outcome == ["started"]
    assert (done_a.reason, done_a.command_uuid) == ("unknown", None)
    assert (started_b.reason, started_b.command_uuid) == ("followup", "f1")
    assert state.pending_start is None
    assert [
        e["answered"]
        for e in logs
        if e["event"] == "codex.app_server.followup_turn_mismatch"
    ] == ["turn-b"]


@pytest.mark.anyio
async def test_a_process_ignoring_sigterm_is_killed_before_the_thread_is_released(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review H3 (per-run too): stdin closed, the exit grace ran out, SIGTERM
    ignored. The thread is released only once the process is gone (SIGKILL,
    reaped), never while it can still serve the thread."""
    fake = Fake(tmp_path, linger=True, ignore_term=True)
    runner = _rt._runner(fake, exit_grace_s=0.3)
    runner._KILL_ESCALATION_S = 0.3
    alive_at_release: list[bool] = []
    release = gate._release_thread

    def spy(state: Any) -> None:
        alive_at_release.append(state.proc.returncode is None)
        release(state)

    monkeypatch.setattr(gate, "_release_thread", spy)
    with capture_logs() as logs:
        events = await _rt._run(runner, resume=RESUME)
    assert _rt._completed(events).ok
    assert alive_at_release == [False]
    assert gate._RUNS_BY_THREAD == {}
    assert [
        e["event"] for e in logs if e["event"].startswith("codex.app_server.exit")
    ] == [
        "codex.app_server.exit_timeout",
        "codex.app_server.exit_killed",
    ]


@pytest.mark.anyio
async def test_an_exit_that_cannot_be_confirmed_keeps_the_thread_owned(
    tmp_path: Path,
    project: Path,
    gate_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review H3, fail closed: if the process can't be confirmed gone, the
    thread stays owned (closing, so no steer) and a warning says so."""

    async def not_gone(self: Any, proc: Any) -> bool:
        await proc.stdin.aclose()  # it does exit; we just couldn't confirm it
        return False

    monkeypatch.setattr(gate.CodexAppServerRunner, "_close_process", not_gone)
    fake = Fake(tmp_path)
    try:
        with capture_logs() as logs:
            await _rt._run(_rt._runner(fake), resume=RESUME)
        state = gate._RUNS_BY_THREAD[THREAD]
        assert state.closing is True and gate.STEER_BACKEND.get(THREAD) is None
        assert [
            e["thread_id"] for e in logs if e["event"] == "codex.app_server.thread_kept"
        ] == [THREAD]
    finally:
        gate._RUNS_BY_THREAD.clear()  # never leaks into later tests
