"""The engine-neutral background-task contract (#978).

The ``⏳ background`` block and `/ping`'s count are pulled from
``engine_state.tasks`` (a dict) and each value is duck-typed — see
``background_status`` and ``runner_bridge._bg_tasks``. ``BackgroundTaskView``
names exactly what those readers use; Claude's ``ClaudeTask`` (built from its
native ``system/task_*`` events) satisfies it as it is, and
``EngineBackgroundTask`` is the plain implementation for an engine whose
stream reports background work some other way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BackgroundTaskView(Protocol):
    """What the status panel and the bridge read from one task."""

    @property
    def task_id(self) -> str: ...
    @property
    def task_type(self) -> str | None: ...
    @property
    def description(self) -> str | None: ...
    @property
    def is_backgrounded(self) -> bool: ...
    @property
    def owned_by_subagent(self) -> bool: ...
    @property
    def status(self) -> str: ...
    @property
    def started_at(self) -> float: ...
    @property
    def ended_at(self) -> float | None: ...
    @property
    def last_usage(self) -> dict[str, Any] | None: ...
    @property
    def last_step(self) -> str | None: ...
    @property
    def last_tool_name(self) -> str | None: ...
    @property
    def holds_session(self) -> bool: ...
    @property
    def origin_turn(self) -> int | None: ...
    @property
    def tool_use_id(self) -> str | None: ...
    @property
    def owner_tool_use_id(self) -> str | None: ...
    @property
    def prior_active_s(self) -> float: ...


@dataclass(slots=True)
class EngineBackgroundTask:
    """One background task of a non-Claude engine. Times are
    ``time.monotonic()``, as the panel's clock is."""

    task_id: str
    # ``local_agent`` gets the panel's agent row; anything else a shell row.
    task_type: str | None = None
    description: str | None = None
    status: str = "running"
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    # When the task starts counting as background work: ``None`` = from the
    # start. A later time lets a runner register a step it can't yet tell
    # apart from a foreground one; the panel is pull-based, so the row
    # appears when that time passes, with no event and no timer.
    background_from: float | None = None
    # The engine's own id for the work (agy: the subagent's conversation id).
    ref: str | None = None
    owned_by_subagent: bool = False
    last_usage: dict[str, Any] | None = None
    last_step: str | None = None
    last_tool_name: str | None = None
    # Claude-only notions the shared readers still look for.
    holds_session: bool = False
    origin_turn: int | None = None
    tool_use_id: str | None = None
    owner_tool_use_id: str | None = None
    prior_active_s: float = 0.0

    @property
    def is_backgrounded(self) -> bool:
        return self.background_from is None or time.monotonic() >= self.background_from
