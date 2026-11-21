"""Msgspec models and decoder for pi --mode json output."""

from __future__ import annotations

from typing import Any

import msgspec


class _Event(msgspec.Struct, tag_field="type", forbid_unknown_fields=False):
    pass


class SessionHeader(_Event, tag="session"):
    id: str | None = None
    version: int | None = None
    timestamp: str | None = None
    cwd: str | None = None
    parentSession: str | None = None


class AgentStart(_Event, tag="agent_start"):
    pass


class AgentEnd(_Event, tag="agent_end"):
    messages: list[dict[str, Any]]


# #1255: Pi >= 0.80.5 — no more automatic work (retry, compaction, queue).
class AgentSettled(_Event, tag="agent_settled"):
    aborted: bool | None = None


class MessageEnd(_Event, tag="message_end"):
    message: dict[str, Any]


class MessageStart(_Event, tag="message_start"):
    message: dict[str, Any] | None = None


class MessageUpdate(_Event, tag="message_update"):
    message: dict[str, Any] | None = None
    assistantMessageEvent: dict[str, Any] | None = None


class TurnStart(_Event, tag="turn_start"):
    pass


class TurnEnd(_Event, tag="turn_end"):
    message: dict[str, Any] | None = None
    toolResults: list[dict[str, Any]] | None = None


class ToolExecutionStart(_Event, tag="tool_execution_start"):
    toolCallId: str
    toolName: str | None = None
    args: dict[str, Any] = msgspec.field(default_factory=dict)


class ToolExecutionUpdate(_Event, tag="tool_execution_update"):
    toolCallId: str | None = None
    toolName: str | None = None
    args: dict[str, Any] = msgspec.field(default_factory=dict)
    partialResult: Any = None


class ToolExecutionEnd(_Event, tag="tool_execution_end"):
    toolCallId: str
    toolName: str | None = None
    result: Any = None
    isError: bool = False


class CompactionStart(_Event, tag="compaction_start"):
    reason: str | None = None


class CompactionEnd(_Event, tag="compaction_end"):
    reason: str | None = None
    result: dict[str, Any] | None = None
    aborted: bool | None = None
    willRetry: bool | None = None
    errorMessage: str | None = None


# #1239: Pi < 0.66.1 names; subclasses so one match arm covers both.
class AutoCompactionStart(CompactionStart, tag="auto_compaction_start"):
    pass


class AutoCompactionEnd(CompactionEnd, tag="auto_compaction_end"):
    pass


class AutoRetryStart(_Event, tag="auto_retry_start"):
    attempt: int | None = None
    maxAttempts: int | None = None
    delayMs: int | None = None
    errorMessage: str | None = None


class AutoRetryEnd(_Event, tag="auto_retry_end"):
    success: bool | None = None
    attempt: int | None = None
    finalError: str | None = None


type PiEvent = (
    SessionHeader
    | AgentStart
    | AgentEnd
    | AgentSettled
    | MessageStart
    | MessageUpdate
    | MessageEnd
    | TurnStart
    | TurnEnd
    | ToolExecutionStart
    | ToolExecutionUpdate
    | ToolExecutionEnd
    | CompactionStart
    | CompactionEnd
    | AutoCompactionStart
    | AutoCompactionEnd
    | AutoRetryStart
    | AutoRetryEnd
)

_DECODER = msgspec.json.Decoder(PiEvent)


def decode_event(line: str | bytes) -> PiEvent:
    return _DECODER.decode(line)
