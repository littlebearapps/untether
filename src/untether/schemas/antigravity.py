"""Msgspec models and decoder for agy --output-format stream-json output."""

from __future__ import annotations

from typing import Any

import msgspec


class _Event(msgspec.Struct, tag_field="event", forbid_unknown_fields=False):
    pass


class InitPayload(msgspec.Struct, forbid_unknown_fields=False):
    cwd: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None
    model: str | None = None


class Init(_Event, tag="init"):
    conversation_id: str | None = None
    init: InitPayload | None = None


class ToolInfo(msgspec.Struct, forbid_unknown_fields=False):
    name: str | None = None
    parameters: dict[str, Any] | None = None
    output: Any | None = None


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


class ResultPayload(msgspec.Struct, forbid_unknown_fields=False):
    conversation_id: str | None = None
    status: str | None = None
    response: str | None = None
    duration_seconds: float | None = None
    num_turns: int | None = None
    usage: ResultUsage | None = None
    command: CommandPayload | None = None
    error: str | None = None


class AntigravityResult(_Event, tag="result"):
    result: ResultPayload | None = None


class Error(_Event, tag="error"):
    message: str | None = None
    error: str | None = None


type AntigravityEvent = Init | StepUpdate | AntigravityResult | CommandResult | Error

_DECODER = msgspec.json.Decoder(AntigravityEvent)


def decode_event(line: str | bytes) -> AntigravityEvent:
    return _DECODER.decode(line)
