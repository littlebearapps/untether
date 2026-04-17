"""Msgspec models and decoder for amp --stream-json output.

Amp uses the same stream-json protocol as Claude Code.
Event shapes: system(init), user, assistant, result.
"""

from __future__ import annotations

from typing import Any

import msgspec


class _Event(msgspec.Struct, tag_field="type", forbid_unknown_fields=False):
    pass


class SystemInit(_Event, tag="system"):
    subtype: str | None = None
    session_id: str | None = None
    cwd: str | None = None
    tools: list[str] = msgspec.field(default_factory=list)
    mcp_servers: list[Any] = msgspec.field(default_factory=list)


class UserMessage(_Event, tag="user"):
    session_id: str | None = None
    message: dict[str, Any] | None = None
    parent_tool_use_id: str | None = None


class AssistantMessage(_Event, tag="assistant"):
    session_id: str | None = None
    message: dict[str, Any] | None = None
    parent_tool_use_id: str | None = None


class AmpResult(_Event, tag="result"):
    subtype: str | None = None
    is_error: bool = False
    result: str | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    session_id: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None


type AmpEvent = SystemInit | UserMessage | AssistantMessage | AmpResult

_DECODER = msgspec.json.Decoder(AmpEvent)


def decode_event(line: str | bytes) -> AmpEvent:
    return _DECODER.decode(line)
