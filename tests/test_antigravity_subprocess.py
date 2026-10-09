"""Antigravity (agy) runner end to end through ``run()`` with a fake ``agy``
(#558). Real subprocess, real stdin/stdout/stderr pipes, real msgspec decode
and translate; the fake replays agy 1.3.x captures (``tests/fake_clis/
fake_agy.py``). No network, no Google quota.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runner import PRESPAWN_BLOCKED_KEY, JsonlSubprocessRunner
from untether.runners import antigravity as agy
from untether.runners.antigravity import CONVERSATION_GONE_TEXT, AntigravityRunner
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
ENGINE = "antigravity"
OK_ID = "90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(path))
    monkeypatch.setenv("UNTETHER_SECRET_IS_FINE", "x")  # UNTETHER_ prefix passes
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-leak")
    return path


@pytest.fixture(autouse=True)
def _real_version_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest stubs the probe to "unknown"; here the fake agy answers it.
    monkeypatch.setattr(agy, "_probe_agy_version", agy._run_agy_version)


def _scenario(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", name)


async def _run(
    prompt: str = "Reply with exactly: OK",
    resume: ResumeToken | None = None,
    runner: AntigravityRunner | None = None,
) -> list[Any]:
    assert os.access(FAKE_AGY, os.X_OK), f"chmod +x {FAKE_AGY}"
    runner = runner or AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    return [evt async for evt in runner.run(prompt, resume)]


def _assert_contract(events: list[Any]) -> CompletedEvent:
    assert isinstance(events[0], StartedEvent)
    assert isinstance(events[-1], CompletedEvent)
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    assert sum(isinstance(e, StartedEvent) for e in events) == 1
    assert all(isinstance(e, ActionEvent) for e in events[1:-1])
    return events[-1]


@pytest.mark.anyio
async def test_run_ok_end_to_end(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "ok")
    events = await _run()
    done = _assert_contract(events)
    assert done.ok is True and done.answer == "OK\n"
    assert events[0].resume == ResumeToken(engine=ENGINE, value=OK_ID)
    rec = json.loads(record.read_text())
    assert rec["argv"][:4] == [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    assert "Reply with exactly" not in " ".join(rec["argv"])
    assert json.loads(rec["stdin"]) == {
        "event": "user",
        "message": {"content": "Reply with exactly: OK"},
    }
    assert rec["stdin"].count("\n") == 1
    assert "UNRELATED_SECRET" not in rec["env_keys"]
    assert "NO_COLOR" in rec["env_keys"]


@pytest.mark.anyio
async def test_run_resume_end_to_end(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "resume_after_interrupt")
    resume = ResumeToken(engine=ENGINE, value="b66a64cf-dd95-4344-9df6-25d35539e10f")
    events = await _run("Reply with exactly: RESUMED", resume)
    done = _assert_contract(events)
    assert events[0].resume == resume
    assert done.ok is True and done.answer == "RESUMED\n"
    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("--conversation") + 1] == resume.value


@pytest.mark.anyio
async def test_run_continue_end_to_end(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "continue_workspace")
    resume = ResumeToken(engine=ENGINE, value="", is_continue=True)
    events = await _run("quote it", resume)
    done = _assert_contract(events)
    assert events[0].resume.value == "5bcf649b-0dfc-44b4-b691-2f3314724abf"
    assert done.resume == events[0].resume
    argv = json.loads(record.read_text())["argv"]
    assert "--continue" in argv and "--conversation" not in argv


@pytest.mark.anyio
async def test_run_tools_denied_end_to_end(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "tools_denied")
    events = await _run("read notes and run a command")
    done = _assert_contract(events)
    assert done.ok is True
    completed = {
        e.action.title: e
        for e in events
        if isinstance(e, ActionEvent) and e.phase == "completed"
    }
    view = [e for t, e in completed.items() if "notes.txt" in t]
    cmd = [e for t, e in completed.items() if "probe-shell" in t]
    assert view and view[0].ok is True
    # Closed, not left running — as a ⚠️ warning row (phase 02).
    assert cmd and cmd[0].ok is True
    assert cmd[0].action.title.startswith("⚠️ Blocked: shell command (RunCommand)")


@pytest.mark.anyio
async def test_run_default_mode_shell_denial_end_to_end(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 02 exit gate: no override → no bypass flag; the shell denial
    becomes a ⚠️ row and the final explains how to allow it."""
    _scenario(monkeypatch, "tools_denied")
    with structlog.testing.capture_logs() as logs:
        events = await _run("run echo probe-shell")
    done = _assert_contract(events)
    argv = json.loads(record.read_text())["argv"]
    assert "--dangerously-skip-permissions" not in argv
    assert done.ok is True
    assert "blocked from using a shell command" in done.answer
    assert events[0].meta["permissionMode"] == "workspace"
    rows = [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.action.title.startswith("⚠️ Blocked")
    ]
    assert len(rows) == 1
    (log,) = [e for e in logs if e["event"] == "antigravity.denied_actions"]
    assert log["actions"] == ["command"] and log["permission_mode"] == "workspace"


@pytest.mark.anyio
async def test_run_full_mode_passes_bypass_flag(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.runners.run_options import EngineRunOptions, apply_run_options

    _scenario(monkeypatch, "hook_deny_bypass")
    with apply_run_options(EngineRunOptions(permission_mode="full")):
        events = await _run("run the hooks probe")
    done = _assert_contract(events)
    assert done.ok is True
    argv = json.loads(record.read_text())["argv"]
    assert argv[-1] == "--dangerously-skip-permissions"
    assert events[0].meta["permissionMode"] == "full access"


@pytest.mark.anyio
async def test_run_stream_ends_without_result(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "synthetic_no_result")
    events = await _run()
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert "without a result event" in (done.error or "")
    assert done.resume == ResumeToken(engine=ENGINE, value=OK_ID)


@pytest.mark.anyio
async def test_run_nonzero_rc_result_wins(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "invalid_model")
    events = await _run()
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert (done.error or "").startswith("invalid model selection")

    script = tmp_path / "no_result.script"
    script.write_text(
        "err:error: something broke in /home/someone/secret/place\nrc:1\n"
    )
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(script))
    events = await _run()
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert (done.error or "").startswith("antigravity failed (rc=1).")
    assert "something broke" in (done.error or "")
    assert "/home/someone" not in (done.error or "")


@pytest.mark.anyio
async def test_run_agy_error_rc3_without_result(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "synthetic_agy_error_rc3")
    events = await _run()
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert "rc=3" in (done.error or "")
    assert done.resume == ResumeToken(engine=ENGINE, value=OK_ID)


@pytest.mark.anyio
async def test_run_result_then_late_stderr_uses_result_only(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "synthetic_result_then_agy_error")
    events = await _run()
    done = _assert_contract(events)
    assert done.ok is True and done.answer == "OK\n"
    assert done.error is None


@pytest.mark.anyio
async def test_run_unknown_conversation_end_to_end(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "unknown_conversation")
    resume = ResumeToken(engine=ENGINE, value="00000000-1111-2222-3333-444444444444")
    events = await _run("Reply with exactly: OK", resume)
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.error == CONVERSATION_GONE_TEXT
    assert done.resume == resume


@pytest.mark.anyio
async def test_saw_result_latched_for_event_envelope(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "ok")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    await _run(runner=runner)
    stream = runner.current_stream
    assert stream is not None
    assert stream.saw_result is True
    assert stream.last_event_type == "result"
    assert stream.engine_state is not None  # 08 §2 opt-in
    assert stream.engine_state.agy_pid == runner.last_pid


@pytest.mark.anyio
async def test_run_logs_version_and_session_started(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "ok")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_VERSION", "1.3.2")
    agy._VERSION_CACHE.clear()
    with structlog.testing.capture_logs() as logs:
        await _run()
    names = [e["event"] for e in logs]
    assert "antigravity.version.probe" in names
    (started,) = [e for e in logs if e["event"] == "antigravity.session.started"]
    assert started["agy_version"] == "1.3.2"
    (start,) = [e for e in logs if e["event"] == "runner.start"]
    assert "--disable-slash-commands" in start["args"]
    assert not any("Reply with exactly" in a for a in start["args"])


@pytest.mark.anyio
async def test_run_version_guard_refuses_old_agy(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_VERSION", "1.2.14")
    agy._VERSION_CACHE.clear()
    events = await _run()
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "unsupported_version"}
    assert not record.exists()  # never spawned for a turn


@pytest.mark.anyio
async def test_run_refused_without_project(
    record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scenario(monkeypatch, "ok")
    with structlog.testing.capture_logs() as logs:
        events = await _run()
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert "needs a project" in (done.error or "")
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "no_project"}
    names = [e["event"] for e in logs]
    assert "antigravity.no_project_dir" in names
    assert "runner.start" not in names
    assert not record.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["home", "root", "service_cwd"])
async def test_run_refused_in_home_root_or_service_cwd(
    where: str, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = {"home": Path.home(), "root": Path("/"), "service_cwd": Path.cwd()}[where]
    token = set_run_base_dir(base)
    try:
        events = await _run()
    finally:
        reset_run_base_dir(token)
    (done,) = events
    assert isinstance(done, CompletedEvent)
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "no_project"}
    assert not record.exists()


@pytest.mark.anyio
async def test_run_impl_guard_first_with_no_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = CompletedEvent(
        engine=ENGINE, ok=False, answer="", resume=None, error="blocked"
    )
    monkeypatch.setattr(
        JsonlSubprocessRunner, "_check_prespawn_ram_guard", lambda self, r: sentinel
    )
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    assert [e async for e in runner.run_impl("hi", None)] == [sentinel]


@pytest.mark.anyio
async def test_run_sigterm_maps_to_interrupted(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external SIGTERM mid-run (stall kill, service stop) → agy's
    ``interrupted`` result → a resumable error, never ``ok=True``."""
    import signal

    import anyio

    script = tmp_path / "hang.script"
    lines = (
        Path(__file__).parent / "fixtures" / "antigravity" / "ok.script"
    ).read_text()
    head = [
        ln
        for ln in lines.splitlines()
        if ln.startswith("out:") and '"event":"result"' not in ln
    ]
    script.write_text("\n".join([*head, "sleep:30", "rc:0"]) + "\n")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(script))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_TIME_SCALE", "1")
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    events: list[Any] = []
    with anyio.fail_after(20):
        async for evt in runner.run("hi", None):
            events.append(evt)
            if isinstance(evt, StartedEvent):
                os.kill(runner.last_pid or 0, signal.SIGTERM)
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert "interrupted" in (done.error or "")
    assert done.resume == ResumeToken(engine=ENGINE, value=OK_ID)
