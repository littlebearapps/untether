"""Untether domain model types (events, actions, resume tokens)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type EngineId = str

type ActionKind = Literal[
    "command",
    "tool",
    "file_change",
    "web_search",
    "subagent",
    "note",
    "turn",
    "warning",
    # #819: a value for the status line (``detail["context_pct"]``), not a
    # step — ProgressTracker stores it apart from the actions.
    "telemetry",
]

type UntetherEventType = Literal[
    "started",
    "action",
    "completed",
    "turn",
]

type TurnPhase = Literal["started", "completed"]
type TurnReason = Literal[
    "task_finished",
    "scheduled_wakeup",
    "monitor_event",
    "followup",
    # #812: an asyncRewake hook exited 2 and woke the idle session.
    "hook_rewake",
    "unknown",
]

# #333: footer marker (``meta["complete"]``) for a finished turn — the session
# is idle and waiting for the next prompt. The Claude runner adds it on the
# run's first result; the bridge adds it on each live follow-up turn (#798).
TURN_COMPLETE_MARKER = "\N{CHECK MARK} turn complete"

type ActionPhase = Literal["started", "updated", "completed"]
type ActionLevel = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class ResumeToken:
    engine: EngineId
    value: str
    is_continue: bool = False


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    kind: ActionKind
    title: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StartedEvent:
    type: Literal["started"] = field(default="started", init=False)
    engine: EngineId
    resume: ResumeToken
    title: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActionEvent:
    type: Literal["action"] = field(default="action", init=False)
    engine: EngineId
    action: Action
    phase: ActionPhase
    ok: bool | None = None
    message: str | None = None
    level: ActionLevel | None = None


@dataclass(frozen=True, slots=True)
class CompletedEvent:
    type: Literal["completed"] = field(default="completed", init=False)
    engine: EngineId
    ok: bool
    answer: str
    resume: ResumeToken | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """Boundary of a follow-up turn after a run's ``CompletedEvent`` (#776).

    Only runners that keep their process live after the first result emit
    these (Claude in control-channel mode). A run is still exactly one
    ``Started → Action* → Completed``; each later turn in the same process
    is a ``TurnEvent(started) → Action* → TurnEvent(completed)`` segment.
    ``usage`` on the completed boundary is the raw result payload (session-
    cumulative cost — consumers derive deltas).
    """

    type: Literal["turn"] = field(default="turn", init=False)
    engine: EngineId
    phase: TurnPhase
    turn: int
    reason: TurnReason = "unknown"
    resume: ResumeToken | None = None
    ok: bool | None = None
    answer: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    command_uuid: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    # #815 (``started`` only): how long before this event the turn really
    # began — the CLI announces a follow-up (``command_lifecycle``) before
    # the frame that opens it. Lets the bridge time the turn from its start.
    started_ago_s: float | None = None


type UntetherEvent = StartedEvent | ActionEvent | CompletedEvent | TurnEvent
