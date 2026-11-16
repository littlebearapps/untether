"""Antigravity background rows end to end (#978, phase 10).

The real ``AntigravityRunner`` against ``tests/fake_clis/fake_agy.py``
replaying the agy 1.3.3 captures: through ``run()``, and through
``handle_message`` to show the ``⏳ background`` block reaches the progress
message with no bridge or panel change.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.telegram_fakes import FakeTransport
from untether.background_status import chat_live_background_count, live_shown
from untether.markdown import MarkdownPresenter
from untether.model import ActionEvent, CompletedEvent, StartedEvent
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture(autouse=True)
def _real_version_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest stubs the probe to "unknown"; here the fake agy answers it.
    monkeypatch.setattr(agy, "_probe_agy_version", agy._run_agy_version)


def _script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    hold_after: str,
    hold_s: float = 0.6,
) -> None:
    """*name*'s capture with no gaps except a short hold after the first line
    containing *hold_after* (where agy sits silent while the work runs)."""
    out: list[str] = []
    held = False
    for line in (FIXTURES / f"{name}.script").read_text().splitlines():
        if not line.startswith("out:"):
            continue
        out.append(line)
        if not held and hold_after in line:
            out.append(f"sleep:{hold_s}")
            held = True
    assert held, hold_after
    script = tmp_path / f"{name}.script"
    script.write_text("\n".join([*out, "rc:0"]) + "\n")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCRIPT", str(script))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_TIME_SCALE", "1")


@pytest.mark.anyio
async def test_subagent_tasks_through_run(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path, monkeypatch, "subagents_two", hold_after='"step_type":"subagent"')
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    seen_live: list[int] = []
    events: list[Any] = []
    async for evt in runner.run("start two subagents", None):
        events.append(evt)
        stream = runner.current_stream
        if isinstance(evt, ActionEvent) and stream is not None:
            seen_live.append(len(live_shown(stream.engine_state.tasks.values())))
    assert isinstance(events[0], StartedEvent)
    assert isinstance(events[-1], CompletedEvent) and events[-1].ok is True
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    # Both children were listed while the spawn row was open and after it
    # closed; none is live once the answer is out.
    assert seen_live == [2, 2]
    stream = runner.current_stream
    assert stream is not None
    tasks = stream.engine_state.tasks
    assert {tid: t.status for tid, t in tasks.items()} == {
        "sub:2:0": "completed",
        "sub:2:1": "completed",
    }
    assert [t.description for t in tasks.values()] == [
        "Alpha (research)",
        "Beta (research)",
    ]
    assert stream.liveness_stalls == 0


@pytest.mark.anyio
async def test_daemon_task_run_ends_clean_with_no_live_task(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path, monkeypatch, "daemon_task", hold_after='"state":"ACTIVE"')
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    with apply_run_options(EngineRunOptions(permission_mode="full")):
        events = [evt async for evt in runner.run("start a daemon", None)]
    done = events[-1]
    assert isinstance(done, CompletedEvent) and done.ok is True
    assert done.answer == "STARTED\n"
    actions = [e for e in events if isinstance(e, ActionEvent)]
    assert [(e.phase, e.ok) for e in actions if e.action.id == "step-2"] == [
        ("started", None),
        ("updated", None),  # ⏳ background: the answer overtook the command
        ("completed", True),
    ]
    stream = runner.current_stream
    assert stream is not None
    tasks = stream.engine_state.tasks
    assert {tid: t.status for tid, t in tasks.items()} == {"cmd:2": "ended"}
    assert live_shown(tasks.values()) == []


@pytest.mark.anyio
async def test_background_block_reaches_the_progress_message(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge's ``_bg_tasks`` reads the agy state's task map as it does
    Claude's: the block is in a progress edit, `/ping`'s count is back to 0
    at the end, and no post-result status message is opened."""
    from untether import runner_bridge
    from untether.settings import ProgressSettings

    _script(tmp_path, monkeypatch, "subagents_two", hold_after='"step_type":"subagent"')
    # No 2 s render throttle: the edit made while agy is silent must land.
    monkeypatch.setattr(
        runner_bridge,
        "_load_progress_settings",
        lambda: ProgressSettings(min_render_interval=0),
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    with anyio.fail_after(30):
        await handle_message(
            cfg,
            runner=AntigravityRunner(antigravity_cmd=str(FAKE_AGY)),
            incoming=IncomingMessage(
                channel_id=4242, message_id=10, text="start two subagents"
            ),
            resume_token=None,
            running_tasks={},
        )
    texts = [call["message"].text for call in transport.edit_calls]
    texts += [call["message"].text for call in transport.send_calls]
    blocks = [t for t in texts if "⏳ background (2)" in t]
    assert blocks, texts
    # MarkdownPresenter output: the description is escaped, never formatting.
    assert "🤖 Alpha \\(research\\) · " in blocks[0]
    assert "🤖 Beta \\(research\\) · " in blocks[0]
    final = transport.send_calls[-1]["message"].text
    assert "Both subagents have reported" in final
    assert "⏳ background" not in final
    assert chat_live_background_count(4242) == 0
    # Progress message + final answer only: no separate status message.
    assert not [t for t in texts if t.startswith("⏳ background")]


@pytest.mark.anyio
async def test_cancel_during_the_hold_says_where_the_answer_went(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1061: while agy holds its answer for a timer the ``⏳ background``
    block says the answer comes when it finishes; /cancel during the hold
    leaves a note that the conversation kept the answer. The engine's exit
    is in the journal (#1062)."""
    import structlog.testing

    from untether import runner_bridge
    from untether.settings import ProgressSettings

    _script(
        tmp_path,
        monkeypatch,
        "schedule_timer",
        hold_after='"tool_name":"schedule"',
        hold_s=30,
    )
    monkeypatch.setattr(
        runner_bridge,
        "_load_progress_settings",
        lambda: ProgressSettings(min_render_interval=0),
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    running_tasks: dict[Any, Any] = {}

    async def run() -> None:
        await handle_message(
            cfg,
            runner=AntigravityRunner(antigravity_cmd=str(FAKE_AGY)),
            incoming=IncomingMessage(channel_id=4243, message_id=10, text="timer"),
            resume_token=None,
            running_tasks=running_tasks,
        )

    def _texts() -> list[str]:
        return [c["message"].text for c in transport.edit_calls] + [
            c["message"].text for c in transport.send_calls
        ]

    with structlog.testing.capture_logs() as logs, anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            while not any("⏳ background (1)" in t for t in _texts()):
                await anyio.sleep(0.05)
            task = running_tasks[next(iter(running_tasks))]
            task.cancel_requested.set()
    held = [t for t in _texts() if "⏳ background (1)" in t]
    assert "agy sends its answer when this work finishes \\(up to 30 min\\)" in held[-1]
    last = transport.edit_calls[-1]["message"].text
    assert "cancelled" in last
    assert "Cancelled while agy was holding its answer for background work" in last
    assert "repeat your last answer" in last
    assert any(e["event"] == "handle.cancelled_during_hold" for e in logs)
    exits = [e for e in logs if e["event"] == "subprocess.exit"]
    assert exits and exits[-1]["reason"] == "teardown"
