"""Msgspec models and decoder for gemini --output-format stream-json output."""

from __future__ import annotations

from typing import Any

import msgspec


class _Event(msgspec.Struct, tag_field="type", forbid_unknown_fields=False):
    pass


class Init(_Event, tag="init"):
    session_id: str | None = None
    model: str | None = None
    timestamp: str | None = None


class Message(_Event, tag="message"):
    role: str | None = None
    content: str | None = None
    delta: bool | None = None
    timestamp: str | None = None


class ToolUse(_Event, tag="tool_use"):
    tool_name: str | None = None
    tool_id: str | None = None
    parameters: dict[str, Any] | None = None
    timestamp: str | None = None


class ToolResult(_Event, tag="tool_result"):
    tool_id: str | None = None
    status: str | None = None
    output: str | None = None
    timestamp: str | None = None


class GeminiResult(_Event, tag="result"):
    status: str | None = None
    stats: dict[str, Any] | None = None
    timestamp: str | None = None


class Error(_Event, tag="error"):
    message: str | None = None
    timestamp: str | None = None


type GeminiEvent = Init | Message | ToolUse | ToolResult | GeminiResult | Error

_DECODER = msgspec.json.Decoder(GeminiEvent)


def decode_event(line: str | bytes) -> GeminiEvent:
    return _DECODER.decode(line)
