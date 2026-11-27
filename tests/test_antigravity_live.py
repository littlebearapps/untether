"""Antigravity warm process (#977, phase 977-2): one ``agy`` per chat with
stdin left open behind ``[antigravity] live_sessions`` (default off).

agy answers each ``{"event":"user"}`` stdin line with one ``result`` and
waits (probe P1, agy 1.5.17), so a warm run's first result is the run's only
``CompletedEvent`` and every later turn is a ``TurnEvent(started) →
ActionEvent* → TurnEvent(completed)`` segment. stdin EOF ends the process
with rc 0 (P6). End to end through ``run()`` with the fake ``agy``
(``UNTETHER_FAKE_AGY_WARM=1``: one script segment per stdin line).

977-3: the live-session backend (``antigravity/live.py`` behind
``live_sessions._AgyLive``) — follow-ups only between turns and only after
the turn's config re-scan, idle close, the time limit at a turn boundary,
``/compact`` and ``/continue`` closing an idle process, per-turn "send it
again" for steers that never ran, and the bridge's live probes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
import structlog.testing

from tests.test_antigravity_subprocess import (
    _real_version_probe,  # noqa: F401  (autouse)
    project,  # noqa: F401  (fixture)
)
from untether import live_sessions
from untether.antigravity import gate_server
from untether.model import (
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    TurnEvent,
)
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner, AntigravityStreamState
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as schema
from untether.session_costs import RUN_TOKENS_KEY
from untether.utils import antigravity_scan as scan

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
CID = "4e1f0c2a-7b3d-4c59-9a86-2d5f3e7b9c10"


@pytest.fixture
def warm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Warm fake agy and a record of what agy received (the real agy live
    backend is registered, 977-3)."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_WARM", "1")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "warm_two_turns")
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    return record


@pytest.fixture
def gemini_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gemini-config"
    home.mkdir()
    monkeypatch.setattr(scan, "user_config_dir", lambda: home)
    return home


def _runner(**kwargs: Any) -> AntigravityRunner:
    assert os.access(FAKE_AGY, os.X_OK), f"chmod +x {FAKE_AGY}"
    kwargs.setdefault("live_sessions", True)
    return AntigravityRunner(antigravity_cmd=str(FAKE_AGY), **kwargs)


class WarmRun:
    """Consume a run in a task so the test can write follow-ups meanwhile."""

    def __init__(self, runner: AntigravityRunner) -> None:
        self.runner = runner
        self.events: list[Any] = []
        self.done = anyio.Event()
        self.scope = anyio.CancelScope()

    async def consume(self, prompt: str) -> None:
        try:
            with self.scope:
                async for evt in self.runner.run(prompt, None):
                    self.events.append(evt)
        finally:
            self.done.set()

    async def until(self, pred: Any, timeout: float = 10) -> None:
        with anyio.fail_after(timeout):
            while not any(pred(e) for e in self.events):
                if self.done.is_set():
                    raise AssertionError(f"run ended first: {self.events}")
                await anyio.sleep(0.01)

    @property
    def state(self) -> AntigravityStreamState:
        stream = self.runner.current_stream
        assert stream is not None
        return stream.engine_state


def _is_completed(e: Any) -> bool:
    return isinstance(e, CompletedEvent)


def _turn(phase: str, n: int):
    return lambda e: isinstance(e, TurnEvent) and e.phase == phase and e.turn == n


def _turns(events: list[Any]) -> list[tuple]:
    return [
        (e.phase, e.turn, e.reason, e.command_uuid, e.ok, e.answer)
        for e in events
        if isinstance(e, TurnEvent)
    ]


async def _drive(runner: AntigravityRunner, body: Any, prompt: str = "ONE") -> WarmRun:
    run = WarmRun(runner)
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run.consume, prompt)
            await run.until(_is_completed)
            await body(run)
            with anyio.fail_after(10):
                await run.done.wait()
    return run


# ── the exit gate ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_two_lines_one_process(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    runner = _runner()

    async def body(run: WarmRun) -> None:
        state = run.state
        assert state.warm is True and state.warm_turns == 1
        assert not run.done.is_set()  # stdin is still open: agy waits
        out = await runner.send_followup(state, "Reply TWO", command_uuid="f1")
        assert out == "written"
        await run.until(_turn("completed", 2))
        await runner.close_warm(state, "test")

    with structlog.testing.capture_logs() as logs:
        run = await _drive(runner, body)

    spawns = [e for e in logs if e["event"] == "subprocess.spawn"]
    assert len(spawns) == 1
    events = run.events
    assert isinstance(events[0], StartedEvent)
    assert events[0].resume == ResumeToken(engine="antigravity", value=CID)
    done = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(done) == 1 and done[0].ok is True and done[0].answer == "ONE\n"
    first_turn = next(i for i, e in enumerate(events) if isinstance(e, TurnEvent))
    assert events.index(done[0]) < first_turn
    assert _turns(events) == [
        ("started", 2, "followup", "f1", None, None),
        ("completed", 2, "followup", "f1", True, "TWO\n"),
    ]
    rec = json.loads(warm.read_text())
    lines = rec["stdin"].splitlines()
    assert [json.loads(line)["message"]["content"] for line in lines] == [
        "ONE",
        "Reply TWO",
    ]
    # #977 / D16: prompts only ever reach agy on stdin, never on argv.
    assert "Reply TWO" not in " ".join(rec["argv"])
    assert "ONE" not in rec["argv"]
    (exit_log,) = [e for e in logs if e["event"] == "subprocess.exit"]
    assert exit_log["rc"] == 0  # stdin EOF (P6)
    (eligible,) = [e for e in logs if e["event"] == "antigravity.live_session.eligible"]
    assert eligible["warm"] is True and eligible["reason"] == "ok"


@pytest.mark.anyio
async def test_first_result_is_completed_then_turn_events(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    runner = _runner()

    async def body(run: WarmRun) -> None:
        await runner.send_followup(run.state, "TWO", command_uuid=None)
        await run.until(_turn("completed", 2))
        await runner.close_warm(run.state, "test")

    events = (await _drive(runner, body)).events
    kinds = [type(e).__name__ for e in events]
    completed = kinds.index("CompletedEvent")
    assert kinds.count("CompletedEvent") == 1 and kinds.count("StartedEvent") == 1
    assert all(k in {"StartedEvent", "ActionEvent"} for k in kinds[:completed])
    after = events[completed + 1 :]
    assert isinstance(after[0], TurnEvent) and after[0].phase == "started"
    assert isinstance(after[-1], TurnEvent) and after[-1].phase == "completed"
    assert all(isinstance(e, ActionEvent) for e in after[1:-1])


@pytest.mark.anyio
async def test_switch_off_stays_one_shot(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    """Default off: stdin closes after the prompt; the fake's warm mode then
    ends at EOF after the first segment, as today's agy does."""
    runner = _runner(live_sessions=False)
    with structlog.testing.capture_logs() as logs:
        events = [e async for e in runner.run("ONE", None)]
    assert [type(e) for e in events if not isinstance(e, ActionEvent)] == [
        StartedEvent,
        CompletedEvent,
    ]
    assert not any(isinstance(e, TurnEvent) for e in events)
    assert not any(e["event"].startswith("antigravity.live_session") for e in logs)
    assert len(json.loads(warm.read_text())["stdin"].splitlines()) == 1


@pytest.mark.anyio
async def test_cancel_mid_turn_ends_process_keeps_conversation(
    project: Path,  # noqa: F811
    warm: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P7: agy has no turn-only interrupt, so /cancel during a later turn
    ends the process (as today); the conversation id is unchanged."""
    lines = (FIXTURES / "warm_two_turns.script").read_text().splitlines()
    at = lines.index("turn:") + 1
    script = tmp_path / "hold.script"
    script.write_text("\n".join([*lines[: at + 2], "wait_go:", *lines[at + 2 :]]))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(script))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_GO", str(tmp_path / "never"))
    runner = _runner()
    pids: list[int] = []

    async def body(run: WarmRun) -> None:
        pids.append(run.state.agy_pid)
        await runner.send_followup(run.state, "TWO", command_uuid="f2")
        await run.until(_turn("started", 2))
        run.scope.cancel()

    run = await _drive(runner, body)
    assert sum(isinstance(e, CompletedEvent) for e in run.events) == 1
    assert run.events[0].resume.value == CID
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


@pytest.mark.anyio
async def test_followup_after_close_is_not_written(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    runner = _runner()
    outcomes: list[str] = []

    async def body(run: WarmRun) -> None:
        state = run.state
        await runner.close_warm(state, "test")
        outcomes.append(await runner.send_followup(state, "late"))

    await _drive(runner, body)
    assert outcomes == ["closed"]
    assert len(json.loads(warm.read_text())["stdin"].splitlines()) == 1


@pytest.mark.anyio
async def test_compact_never_goes_to_a_warm_process(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    """§5.4: a warm process runs with --disable-slash-commands; /compact is
    977-3's to route one-shot after closing the warm process."""
    runner = _runner()
    outcomes: list[str] = []

    async def body(run: WarmRun) -> None:
        outcomes.append(await runner.send_followup(run.state, "/compact"))
        await runner.close_warm(run.state, "test")

    await _drive(runner, body)
    assert outcomes == ["refused"]


# ── config change and trust (#1032) ──────────────────────────────────────────


@pytest.mark.anyio
async def test_config_change_closes_after_turn(
    project: Path,  # noqa: F811
    warm: Path,
    gemini_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: agy loaded its hooks/plugins/agents at spawn. A planted-config
    change seen after a turn shows its ⚠️ row and closes the process, so the
    next message respawns (and runs every pre-spawn check) on the new set."""
    lines = (FIXTURES / "warm_two_turns.script").read_text().splitlines()
    result_at = next(i for i, x in enumerate(lines) if '"event":"result"' in x)
    script = tmp_path / "planted.script"
    script.write_text(
        "\n".join([*lines[:result_at], "write:.agents/hooks.json", *lines[result_at:]])
    )
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(script))
    runner = _runner()
    outcomes: list[str] = []

    async def body(run: WarmRun) -> None:
        with anyio.fail_after(10):
            await run.done.wait()  # the runner closed it, not the test
        outcomes.append(await runner.send_followup(run.state, "TWO"))

    with structlog.testing.capture_logs() as logs:
        run = await _drive(runner, body)
    assert outcomes == ["closed"]
    (closing,) = [e for e in logs if e["event"] == "antigravity.live_session.closing"]
    assert closing["reason"] == "config_changed"
    rows = [
        e
        for e in run.events
        if isinstance(e, ActionEvent)
        and e.action.kind == "warning"
        and e.action.title.startswith("⚠️ Antigravity changed .agents/hooks.json")
    ]
    assert rows
    assert not any(isinstance(e, TurnEvent) for e in run.events)


@pytest.mark.parametrize(
    ("options", "prompt", "reason"),
    [
        (EngineRunOptions(permission_mode="ask"), "hi", "mode_ask"),
        (EngineRunOptions(permission_mode="plan"), "hi", "mode_plan"),
        (
            EngineRunOptions(permission_mode="workspace", unattended_trigger="cron:x"),
            "hi",
            "unattended",
        ),
        (None, "/compact", "compact"),
    ],
)
def test_ineligible_modes_stay_one_shot(
    monkeypatch: pytest.MonkeyPatch,
    options: EngineRunOptions | None,
    prompt: str,
    reason: str,
) -> None:
    monkeypatch.setitem(live_sessions._BACKENDS, "antigravity", object())
    runner = _runner()
    with apply_run_options(options):
        decision = agy.decide_antigravity_mode(runner._effective_mode())
        assert runner._warm_eligibility(prompt, decision, None) == (False, reason)


def test_eligibility_switch_backend_and_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = agy.decide_antigravity_mode("workspace")
    full = agy.decide_antigravity_mode("full")
    assert _runner(live_sessions=False)._warm_eligibility("hi", workspace, None) == (
        False,
        "switch_off",
    )
    # Until 977-3 registers agy's live backend nothing would route a
    # follow-up to a warm process (the next message would fork the
    # conversation in a second agy), so the switch alone isn't enough.
    monkeypatch.delitem(live_sessions._BACKENDS, "antigravity", raising=False)
    assert _runner()._warm_eligibility("hi", workspace, None) == (False, "no_backend")
    monkeypatch.setitem(live_sessions._BACKENDS, "antigravity", object())
    assert _runner()._warm_eligibility("hi", workspace, None) == (True, "ok")
    assert _runner()._warm_eligibility("hi", full, None) == (True, "ok")


def test_trusted_config_run_is_never_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1032: "Trust current config" is per run and the post-run re-scan
    revokes it; a process that outlived the run would keep a revoked trust.
    Trust is only ever used by Ask me (never warm, D4); a precheck carrying a
    trust record is refused too, whatever the mode."""
    monkeypatch.setitem(live_sessions._BACKENDS, "antigravity", object())
    pre = agy._RunPrecheck()
    pre.trusted = (4242, "/proj", "abcd1234")
    workspace = agy.decide_antigravity_mode("workspace")
    assert _runner()._warm_eligibility("hi", workspace, pre) == (
        False,
        "trusted_config",
    )


def test_build_runner_reads_live_sessions(tmp_path: Path) -> None:
    from untether.config import ConfigError

    path = tmp_path / "untether.toml"
    assert agy.build_runner({}, path).live_sessions is False
    assert agy.build_runner({"live_sessions": True}, path).live_sessions is True
    with pytest.raises(ConfigError):
        agy.build_runner({"live_sessions": "yes"}, path)


# ── translate: turn segmentation and per-turn state (no subprocess) ─────────


def _replay_warm() -> tuple[list[Any], AntigravityStreamState]:
    runner = AntigravityRunner(antigravity_cmd="agy")
    state = runner.new_state("ONE", None)
    state.warm = True
    runner.start_run("ONE", None, state=state)
    events: list[Any] = []
    for line in (FIXTURES / "warm_two_turns.jsonl").read_text().splitlines():
        events.extend(
            runner.translate(
                schema.decode_event(line), state=state, resume=None, found_session=None
            )
        )
    return events, state


def test_per_turn_answer_and_usage_reset() -> None:
    events, state = _replay_warm()
    (done,) = [e for e in events if isinstance(e, CompletedEvent)]
    (_, later) = [e for e in events if isinstance(e, TurnEvent)]
    assert done.answer == "ONE\n" and later.answer == "TWO\n"
    # result.usage is the conversation total; the run's own figure is per turn.
    assert done.usage[RUN_TOKENS_KEY] == {
        "input_tokens": 10966,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert later.usage[RUN_TOKENS_KEY] == {
        "input_tokens": 2130,
        "output_tokens": 1,
        "cache_read_tokens": 8919,
        "reasoning_tokens": 0,
    }
    assert state.warm_turns == 2 and state.warm_turn_open is False


def test_step_index_keeps_counting() -> None:
    """P1: agy keeps counting ``step_index`` across turns; nothing resets it
    and the later turn's ``user_input`` (step 2) opens the turn."""
    events, state = _replay_warm()
    turn_started = next(e for e in events if isinstance(e, TurnEvent))
    assert turn_started.phase == "started" and turn_started.turn == 2
    assert state.text_by_step == {3: "TWO\n"}


def test_bridge_probes_read_the_warm_state() -> None:
    """977-3: the bridge duck-types ``live_mode`` / ``completed_turns`` /
    ``turn_open`` on ``stream.engine_state`` (/cancel's idle close, /new,
    drain, the progress monitor, early final delivery). A warm state answers
    them read-only; Claude-only probes stay absent; a one-shot state reads
    as not live, as before."""
    _, state = _replay_warm()
    assert state.live_mode is True and state.completed_turns == 2
    assert state.turn_open is True  # its re-scan hasn't run (no run_impl here)
    state.warm_checking = False
    assert state.turn_open is False
    state.warm_uuids.append("u")  # a written line agy hasn't started
    assert state.turn_open is True
    state.warm_expired = 1  # ... that timed out: a tombstone
    assert state.turn_open is False
    for name in ("live_wakeups", "result_received_at", "live_monitors"):
        assert not hasattr(state, name), name
    cold = AntigravityRunner(antigravity_cmd="agy").new_state("x", None)
    assert cold.live_mode is False and cold.completed_turns == 0


# ── gate: the steer window follows the turn ─────────────────────────────────


async def _gate(tmp_path: Path) -> gate_server.GateRun:
    from untether.events import EventFactory

    gate = gate_server.GateRun(
        config_path=tmp_path / "u.toml", factory=EventFactory("antigravity")
    )
    gate.set_conversation(CID)
    return gate


@pytest.mark.anyio
async def test_steer_window_reopens_per_turn(tmp_path: Path) -> None:
    gate = await _gate(tmp_path)
    assert gate.queue_steer("one", "u1")
    gate.note_result()
    assert not gate.queue_steer("between turns", "u2")
    with structlog.testing.capture_logs() as logs:
        gate.note_turn_start()
    # Turn 1's steer never ran and must not leak into turn 2.
    assert not gate.steer_queue and not gate.injected
    (not_run,) = [e for e in logs if e["event"] == "antigravity.steer.not_run"]
    assert not_run["command_uuid"] == "u1"
    assert gate.queue_steer("turn two", "u3")


@pytest.mark.anyio
async def test_steer_window_stays_shut_on_a_dead_or_closed_gate(tmp_path: Path) -> None:
    gate = await _gate(tmp_path)
    gate.note_result()
    gate.mark_dead()
    gate.note_turn_start()
    assert not gate.queue_steer("x", "u")
    gate2 = await _gate(tmp_path)
    await gate2.aclose()
    gate2.note_turn_start()
    assert not gate2.queue_steer("x", "u")


# ── 977-3: the live session (``antigravity/live.py``) ───────────────────────

from types import SimpleNamespace  # noqa: E402

from untether.antigravity import live as agy_live  # noqa: E402
from untether.live_followup import inject_live_followup  # noqa: E402
from untether.scheduler import ThreadJob  # noqa: E402

BACKEND = live_sessions.live_backend("antigravity")
TOKEN = ResumeToken(engine="antigravity", value=CID)


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifecycle clocks for tests: a 50 ms tick, a 0.3 s idle grace."""
    monkeypatch.setattr(agy_live, "POLL_S", 0.05)
    monkeypatch.setattr(agy_live, "INJECT_POLL_S", 0.02)
    monkeypatch.setattr(agy_live, "_live_settings", lambda: (0.3, 14400.0))


def switch_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``[antigravity] live_sessions = true`` in the config the backend reads."""
    config = tmp_path / "untether.toml"
    config.write_text(
        '[transports.telegram]\nbot_token = "tok"\nchat_id = 123\n'
        "allow_any_user = true\n\n[antigravity]\nlive_sessions = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(config))


class Listener:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.calls.append((kind, payload))


def _stdin_lines(record: Path) -> list[str]:
    lines = json.loads(record.read_text())["stdin"].splitlines()
    return [json.loads(line)["message"]["content"] for line in lines]


def _ev(logs: list[dict], name: str) -> list[dict]:
    return [e for e in logs if e["event"] == name]


@pytest.mark.anyio
async def test_idle_session_reads_through_the_backend(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    runner = _runner()

    async def body(run: WarmRun) -> None:
        assert BACKEND is not None
        entry = BACKEND.get(CID)
        assert entry is not None and entry.closing is False
        assert BACKEND.is_accepting(CID) is True
        assert BACKEND.idle_count() == 1
        assert BACKEND.linger_info(CID) == (True, 0)
        assert BACKEND.turn_pending(CID) is False
        assert BACKEND.spawn_cwd(CID) == project
        assert BACKEND.spawn_options(CID) is None  # no run options applied
        assert BACKEND.pre_inject_close_reason(CID) is None
        assert await BACKEND.interrupt_pending_turn(CID, any_turn=True) is False
        # Between turns there is nothing to steer: "no running turn".
        assert await BACKEND.steer(CID, "x", command_uuid="s") is None
        assert await BACKEND.wait_for_handoff(CID, 0.05) == "timed_out"
        assert await BACKEND.close(CID, "test") is True
        assert await BACKEND.close(CID, "test") is False  # idempotent
        assert BACKEND.is_accepting(CID) is False

    await _drive(runner, body)
    assert BACKEND.get(CID) is None  # gone once the process exited
    assert await BACKEND.wait_for_handoff(CID, 1.0) == "free"


@pytest.mark.anyio
async def test_followup_waits_for_the_turns_config_recheck(
    project: Path,  # noqa: F811
    warm: Path,
    fast: None,
) -> None:
    """977-2 finding: a line written mid-turn is queued by agy itself, and
    EOF from a ``config_changed`` close might still let it run on the old
    config. So a follow-up is written only between turns, and "between
    turns" starts once the turn's re-scan has passed."""
    runner = _runner()
    outcome: list[bool] = []

    async def body(run: WarmRun) -> None:
        run.state.warm_checking = True  # the re-scan hasn't passed yet

        async def inject() -> None:
            outcome.append(
                await agy_live.inject_when_idle(CID, "TWO", command_uuid="f")
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(inject)
            await anyio.sleep(0.6)  # past the idle grace: busy, so no close
            assert outcome == [] and _stdin_lines(warm) == ["ONE"]
            assert BACKEND.turn_pending(CID) is True
            run.state.warm_checking = False
        await run.until(_turn("completed", 2))
        await BACKEND.close(CID, "test")

    await _drive(runner, body)
    assert outcome == [True]
    assert _stdin_lines(warm) == ["ONE", "TWO"]


@pytest.mark.anyio
async def test_config_change_while_idle_closes_instead_of_injecting(
    project: Path,  # noqa: F811
    warm: Path,
    gemini_home: Path,
) -> None:
    """R3 / #1032: config changed between turns (after the turn's own
    re-scan): the follow-up is never written; the process closes so the
    message resumes through a fresh spawn and its pre-spawn checks."""
    runner = _runner()
    out: list[bool] = []

    async def body(run: WarmRun) -> None:
        (project / ".agents").mkdir()
        (project / ".agents" / "hooks.json").write_text("{}")
        out.append(await agy_live.inject_when_idle(CID, "TWO", command_uuid="f"))

    with structlog.testing.capture_logs() as logs:
        await _drive(runner, body)
    assert out == [False]
    assert _stdin_lines(warm) == ["ONE"]
    (changed,) = _ev(logs, "antigravity.live_session.config_changed")
    assert changed["reason"] == "changed"
    (closed,) = _ev(logs, "antigravity.live_session.closed")
    assert closed["reason"] == "config_changed"


@pytest.mark.anyio
async def test_compact_closes_idle_warm_session_then_one_shot(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    """§5.4: agy's ``/compact`` can't run in a process started with
    ``--disable-slash-commands``: the idle warm process closes and the
    message resumes one-shot (eligibility ``compact``)."""
    runner = _runner()
    out: list[bool] = []

    async def body(run: WarmRun) -> None:
        out.append(await agy_live.inject_when_idle(CID, "/compact", command_uuid="c"))

    with structlog.testing.capture_logs() as logs:
        await _drive(runner, body)
    assert out == [False] and _stdin_lines(warm) == ["ONE"]
    (closed,) = _ev(logs, "antigravity.live_session.closed")
    assert closed["reason"] == "compact"


@pytest.mark.anyio
async def test_continue_closes_idle_warm_session_first(
    project: Path,  # noqa: F811
    warm: Path,
    tmp_path: Path,
) -> None:
    """#1275 / R1: a ``/continue`` in the warm process's directory closes it
    first, so the resolved resume never runs beside it (a fork)."""
    runner = _runner()
    closed: list[int] = []

    async def body(run: WarmRun) -> None:
        closed.append(await BACKEND.close_idle_for_continue(tmp_path / "other"))
        closed.append(await BACKEND.close_idle_for_continue(project))

    await _drive(runner, body)
    assert closed == [0, 1]


@pytest.mark.anyio
async def test_options_changed_closes_instead_of_injecting(
    project: Path,  # noqa: F811
    warm: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#996: agy takes model, mode and agent only at spawn; a follow-up under
    other options closes the idle process (the message resumes)."""
    switch_on(monkeypatch, tmp_path)
    runner = _runner()
    job = ThreadJob(chat_id=1, user_msg_id=2, text="TWO", resume_token=TOKEN)

    async def options_for(_job: ThreadJob) -> Any:
        return EngineRunOptions(model="other")

    out: list[bool] = []

    async def body(run: WarmRun) -> None:
        out.append(await inject_live_followup(job, options_for=options_for))

    with structlog.testing.capture_logs() as logs:
        await _drive(runner, body)
    assert out == [False] and _stdin_lines(warm) == ["ONE"]
    assert len(_ev(logs, "antigravity.live_session.options_changed")) == 1
    (closed,) = _ev(logs, "antigravity.live_session.closed")
    assert closed["reason"] == "options_changed"


@pytest.mark.anyio
async def test_abs_cap_waits_for_the_turn_boundary(
    project: Path,  # noqa: F811
    warm: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[watchdog] live_session_max_s``: past it mid-turn the close waits
    for the turn to end (#887 grace), then closes with ``abs_cap``."""
    lines = (FIXTURES / "warm_two_turns.script").read_text().splitlines()
    at = lines.index("turn:") + 1
    script = tmp_path / "hold.script"
    script.write_text("\n".join([*lines[: at + 2], "wait_go:", *lines[at + 2 :]]))
    go = tmp_path / "go"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(script))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_GO", str(go))
    monkeypatch.setattr(agy_live, "POLL_S", 0.05)
    monkeypatch.setattr(agy_live, "_live_settings", lambda: (60.0, 0.5))
    runner = _runner()
    listener = Listener()

    async def body(run: WarmRun) -> None:
        assert BACKEND.add_listener(CID, listener)
        await runner.send_followup(run.state, "TWO", command_uuid="f")
        await run.until(_turn("started", 2))
        await anyio.sleep(0.8)  # past the cap, mid-turn
        assert not run.state.warm_closing
        go.write_text("go")

    with structlog.testing.capture_logs() as logs:
        run = await _drive(runner, body)
    assert len(_ev(logs, "antigravity.live_session.abs_cap_deferred")) == 1
    (closed,) = _ev(logs, "antigravity.live_session.closed")
    assert closed["reason"] == "abs_cap"
    assert _turns(run.events)[-1][:2] == ("completed", 2)
    # Closed between turns: no "closing" notice (nothing was cut short).
    assert [k for k, _ in listener.calls] == ["closed"]


@pytest.mark.anyio
async def test_idle_process_that_dies_gives_the_died_notice(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    """#1001: agy never exits on its own with stdin open, so an idle exit
    nobody asked for is announced (named Antigravity by the bridge)."""
    import signal as _signal

    runner = _runner()
    listener = Listener()

    async def body(run: WarmRun) -> None:
        assert BACKEND.add_listener(CID, listener)
        os.kill(run.state.agy_pid, _signal.SIGKILL)

    with structlog.testing.capture_logs() as logs:
        await _drive(runner, body)
    assert [k for k, _ in listener.calls] == ["died"]
    assert listener.calls[0][1]["signal_name"] == "SIGKILL"
    assert len(_ev(logs, "antigravity.live_session.died_unexpectedly")) == 1


@pytest.mark.anyio
async def test_steers_not_run_are_reported_at_each_turn_end(tmp_path: Path) -> None:
    """977-2 only logged them at the next turn's start; now each warm turn's
    end hands their uuids to the listener (the bridge's "send it again"),
    and only then does the session count as idle."""
    gate = await _gate(tmp_path)
    assert gate.queue_steer("late", "u1")
    gate.note_result()
    state = SimpleNamespace(gate=gate, warm_checking=True, session_id=CID)
    entry = agy_live.register(None, state, options=None, cwd=None)
    listener = Listener()
    entry.listeners.append(listener)
    try:
        with structlog.testing.capture_logs() as logs:
            await agy_live.turn_ended(state)
    finally:
        agy_live._ENTRIES.remove(entry)
    assert listener.calls == [("not_run", {"command_uuids": ["u1"]})]
    assert state.warm_checking is False
    assert not gate.steer_queue
    assert len(_ev(logs, "antigravity.steer.not_run")) == 1


def test_followup_whose_turn_never_starts_expires_to_a_tombstone() -> None:
    _, state = _replay_warm()
    state.warm_checking = False
    state.warm_uuids.append("late")
    state.warm_written_at = 100.0
    assert agy_live.is_idle(state, now=101.0) is False
    with structlog.testing.capture_logs() as logs:
        assert agy_live.is_idle(state, now=100.0 + 121) is True
    assert state.warm_expired == 1 and state.turn_open is False
    assert len(_ev(logs, "antigravity.live_session.followup_turn_timeout")) == 1
    # A late turn still takes its own uuid (the tombstone), not the next one.
    su = schema.StepUpdatePayload(
        conversation_id=CID, step_index=9, state="DONE", step_type="user_input"
    )
    AntigravityRunner._warm_turn_start(su, state)
    assert state.warm_turn_uuid == "late" and state.warm_expired == 0


@pytest.mark.anyio
async def test_close_steer_window_shuts_the_gate_for_good(tmp_path: Path) -> None:
    gate = await _gate(tmp_path)
    try:
        assert await BACKEND.close_steer_window(CID, "cancel") is True
        assert not gate.queue_steer("x", "u")
        gate.note_result()
        gate.note_turn_start()  # a later turn doesn't reopen it
        assert not gate.queue_steer("y", "u2")
    finally:
        await gate.aclose()
    assert await BACKEND.close_steer_window(CID, "cancel") is False


@pytest.mark.anyio
async def test_dead_gate_closes_instead_of_taking_a_followup(
    project: Path,  # noqa: F811
    warm: Path,
) -> None:
    runner = _runner()
    reasons: list[str | None] = []

    async def body(run: WarmRun) -> None:
        run.state.gate = SimpleNamespace(dead=True, closed=False)
        reasons.append(BACKEND.pre_inject_close_reason(CID))
        run.state.gate = None
        await BACKEND.close(CID, "test")

    await _drive(runner, body)
    assert reasons == ["gate_not_ready"]
