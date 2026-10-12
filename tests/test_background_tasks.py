"""The engine-neutral background-task contract (#978).

The status panel duck-types ``engine_state.tasks``; ``BackgroundTaskView``
names what it reads, ``EngineBackgroundTask`` is the plain implementation for
engines without Claude's native task events.
"""

from __future__ import annotations

import time

from untether.background_status import (
    is_live,
    live_shown,
    render_background_block,
)
from untether.background_tasks import BackgroundTaskView, EngineBackgroundTask
from untether.markdown import HARD_BREAK
from untether.runners.claude import ClaudeTask


def test_claude_task_satisfies_background_task_view() -> None:
    """Claude's native task entry is unchanged and still fits the contract."""
    assert isinstance(ClaudeTask(task_id="t1"), BackgroundTaskView)


def test_engine_background_task_satisfies_view() -> None:
    task = EngineBackgroundTask(task_id="cmd:2", task_type="local_bash")
    assert isinstance(task, BackgroundTaskView)
    assert task.status == "running" and is_live(task)
    assert task.is_backgrounded is True
    # Never holds a live session open, never a subagent's own task.
    assert task.holds_session is False
    assert task.owned_by_subagent is False
    assert task.prior_active_s == 0.0


def test_background_from_is_read_lazily() -> None:
    """A task registered before it is known to be background work becomes
    one when its deadline passes — with no event and no timer, because the
    panel reads ``is_backgrounded`` each time it renders."""
    now = time.monotonic()
    task = EngineBackgroundTask(task_id="cmd:2", background_from=now + 60.0)
    assert task.is_backgrounded is False
    assert live_shown([task]) == []
    task.background_from = now - 0.1
    assert task.is_backgrounded is True
    assert live_shown([task]) == [task]


def test_panel_renders_engine_tasks() -> None:
    now = time.monotonic()
    tasks = [
        EngineBackgroundTask(
            task_id="sub:2:0",
            task_type="local_agent",
            description="Alpha (research)",
            started_at=now - 7.0,
        ),
        EngineBackgroundTask(
            task_id="cmd:4",
            task_type="local_bash",
            description="sleep 90; echo done",
            started_at=now - 12.0,
        ),
        EngineBackgroundTask(
            task_id="cmd:5",
            task_type="local_bash",
            description="finished",
            status="completed",
            started_at=now - 20.0,
            ended_at=now - 1.0,
        ),
    ]
    block = render_background_block(tasks, now=now)
    assert block is not None
    assert block.split(HARD_BREAK) == [
        "⏳ background (2)",
        "🐚 sleep 90; echo done · 12s",
        "🤖 Alpha \\(research\\) · 7s",
    ]
