"""Msgspec models and decoder for ``agy --output-format stream-json`` (#558).

Built from contributor PR #766 (Manuel Naranjo) and extended for agy 1.3.x:
hook-denied tool errors (``ToolError``), ``result.denied_actions`` for
soft-denied steps, ``subagent_info`` and the ``--json-schema`` result fields.

Every struct tolerates unknown fields, and ``step_type`` / ``state`` /
``status`` stay plain ``str`` — agy adds values between releases (1.3.1
added ``finish`` and ``subagent``). ``ResultPayload.error`` is ``Any`` so a
structured error can never drop the whole ``result`` line.
"""

from __future__ import annotations

from typing import Any

import msgspec


class _Event(msgspec.Struct, tag_field="event", forbid_unknown_fields=False):
    pass


class InitPayload(msgspec.Struct, forbid_unknown_fields=False):
    cwd: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None
    # Absent on 1.3.1; reported (base id) from 1.3.2.
    model: str | None = None


class Init(_Event, tag="init"):
    conversation_id: str | None = None
    init: InitPayload | None = None


class ToolError(msgspec.Struct, forbid_unknown_fields=False):
    type: str | None = None
    message: str | None = None


class ToolInfo(msgspec.Struct, forbid_unknown_fields=False):
    name: str | None = None
    parameters: dict[str, Any] | None = None
    output: Any | None = None
    error: ToolError | None = None


class StepUsage(msgspec.Struct, forbid_unknown_fields=False):
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None


class StepUpdatePayload(msgspec.Struct, forbid_unknown_fields=False):
    conversation_id: str | None = None
    step_index: int | None = None
    state: str | None = None
    step_type: str | None = None
    tool_name: str | None = None
    tool_info: ToolInfo | None = None
    text_delta: str | None = None
    duration_seconds: float | None = None
    usage: StepUsage | None = None
    subagent_info: dict[str, Any] | None = None


class StepUpdate(_Event, tag="step_update"):
    step_update: StepUpdatePayload | None = None


class ResultUsage(msgspec.Struct, forbid_unknown_fields=False):
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None


class CommandPayload(msgspec.Struct, forbid_unknown_fields=False):
    name: str | None = None
    data: dict[str, Any] | None = None


class CommandResult(_Event, tag="command_result"):
    command: CommandPayload | None = None


class DeniedAction(msgspec.Struct, forbid_unknown_fields=False):
    action: str | None = None
    display_name: str | None = None


class ResultPayload(msgspec.Struct, forbid_unknown_fields=False):
    conversation_id: str | None = None
    status: str | None = None
    response: str | None = None
    # Session-cumulative across ``--conversation`` resumes: log only.
    duration_seconds: float | None = None
    num_turns: int | None = None
    usage: ResultUsage | None = None
    command: CommandPayload | None = None
    error: Any = None
    denied_actions: list[DeniedAction] | None = None
    structured_output: Any | None = None
    json_schema: Any | None = None


class AntigravityResult(_Event, tag="result"):
    result: ResultPayload | None = None


class Error(_Event, tag="error"):
    """Decode-only: no agy release has been seen emitting it."""

    message: str | None = None
    error: str | None = None


type AntigravityEvent = Init | StepUpdate | AntigravityResult | CommandResult | Error

_DECODER = msgspec.json.Decoder(AntigravityEvent)


def decode_event(line: str | bytes) -> AntigravityEvent:
    return _DECODER.decode(line)
