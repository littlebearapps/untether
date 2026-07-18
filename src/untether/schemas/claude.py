"""Msgspec models and decoder for Claude Code stream-json output."""

from __future__ import annotations

from typing import Any, Literal

import msgspec


class StreamTextBlock(
    msgspec.Struct, tag="text", tag_field="type", forbid_unknown_fields=False
):
    text: str


class StreamThinkingBlock(
    msgspec.Struct, tag="thinking", tag_field="type", forbid_unknown_fields=False
):
    thinking: str
    signature: str


class StreamToolUseBlock(
    msgspec.Struct, tag="tool_use", tag_field="type", forbid_unknown_fields=False
):
    id: str
    name: str
    input: dict[str, Any]


class StreamToolResultBlock(
    msgspec.Struct, tag="tool_result", tag_field="type", forbid_unknown_fields=False
):
    tool_use_id: str
    # #501 — Claude Code may emit `content` as a single content block
    # object (e.g. {"type": "text", "text": "..."}) in addition to the
    # documented str / list[dict] / null shapes. _normalize_tool_result
    # already handles dict; the schema must accept it too or msgspec
    # raises ValidationError and the line is silently dropped.
    content: str | dict[str, Any] | list[dict[str, Any]] | None = None
    is_error: bool | None = None


# #489 — Anthropic server-side tools (web_search, code_execution, computer_use, …)
# emit `server_tool_use` content blocks. Structurally identical to `tool_use`.
class StreamServerToolUseBlock(
    msgspec.Struct,
    tag="server_tool_use",
    tag_field="type",
    forbid_unknown_fields=False,
):
    id: str
    name: str
    input: dict[str, Any]


# #489 — Result of the parent agent's `advisor()` meta-tool. Structurally identical
# to `tool_result`.
class StreamAdvisorToolResultBlock(
    msgspec.Struct,
    tag="advisor_tool_result",
    tag_field="type",
    forbid_unknown_fields=False,
):
    tool_use_id: str
    # #501 — see StreamToolResultBlock.content note.
    content: str | dict[str, Any] | list[dict[str, Any]] | None = None
    is_error: bool | None = None


# #597 — binary media echoed back through user-role messages (e.g. a `Read`
# on an image/PDF returns the content as an image/document block inside the
# tool_result envelope). ``source`` carries ``{"type": "base64"|"url",
# "media_type": ..., "data"|"url": ...}``; kept as a permissive dict — the
# payload is never rendered, the schema just needs to accept the line so the
# rest of the event isn't dropped (jsonl.msgspec.invalid x23 on nsd).
class StreamImageBlock(
    msgspec.Struct, tag="image", tag_field="type", forbid_unknown_fields=False
):
    source: dict[str, Any] | None = None


# #597 — see StreamImageBlock; PDFs and other documents use the same shape
# plus optional metadata fields (title, context, citations toggle).
class StreamDocumentBlock(
    msgspec.Struct, tag="document", tag_field="type", forbid_unknown_fields=False
):
    source: dict[str, Any] | None = None
    title: str | None = None


type StreamContentBlock = (
    StreamTextBlock
    | StreamThinkingBlock
    | StreamToolUseBlock
    | StreamToolResultBlock
    | StreamServerToolUseBlock
    | StreamAdvisorToolResultBlock
    | StreamImageBlock
    | StreamDocumentBlock
)


class StreamUserMessageBody(msgspec.Struct, forbid_unknown_fields=False):
    role: Literal["user"]
    content: str | list[StreamContentBlock]


class StreamAssistantMessageBody(msgspec.Struct, forbid_unknown_fields=False):
    role: Literal["assistant"]
    content: list[StreamContentBlock]
    model: str
    error: str | None = None


class StreamUserMessage(
    msgspec.Struct, tag="user", tag_field="type", forbid_unknown_fields=False
):
    message: StreamUserMessageBody
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    session_id: str | None = None


class StreamAssistantMessage(
    msgspec.Struct, tag="assistant", tag_field="type", forbid_unknown_fields=False
):
    message: StreamAssistantMessageBody
    parent_tool_use_id: str | None = None
    uuid: str | None = None
    session_id: str | None = None


class StreamSystemMessage(
    msgspec.Struct, tag="system", tag_field="type", forbid_unknown_fields=False
):
    subtype: str
    session_id: str | None = None
    uuid: str | None = None
    cwd: str | None = None
    tools: list[str] | None = None
    mcp_servers: list[Any] | None = None
    model: str | None = None
    permissionMode: str | None = None
    output_style: str | None = None
    apiKeySource: str | None = None


class StreamResultMessage(
    msgspec.Struct, tag="result", tag_field="type", forbid_unknown_fields=False
):
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    result: str | None = None
    structured_output: Any = None


class StreamEventMessage(
    msgspec.Struct, tag="stream_event", tag_field="type", forbid_unknown_fields=False
):
    uuid: str
    session_id: str
    event: dict[str, Any]
    parent_tool_use_id: str | None = None


class ControlInterruptRequest(
    msgspec.Struct, tag="interrupt", tag_field="subtype", forbid_unknown_fields=False
):
    pass


class ControlCanUseToolRequest(
    msgspec.Struct, tag="can_use_tool", tag_field="subtype", forbid_unknown_fields=False
):
    tool_name: str
    input: dict[str, Any]
    permission_suggestions: list[Any] | None = None
    blocked_path: str | None = None


class ControlInitializeRequest(
    msgspec.Struct, tag="initialize", tag_field="subtype", forbid_unknown_fields=False
):
    hooks: dict[str, Any] | None = None


class ControlSetPermissionModeRequest(
    msgspec.Struct,
    tag="set_permission_mode",
    tag_field="subtype",
    forbid_unknown_fields=False,
):
    mode: str


class ControlHookCallbackRequest(
    msgspec.Struct,
    tag="hook_callback",
    tag_field="subtype",
    forbid_unknown_fields=False,
):
    callback_id: str
    input: Any
    tool_use_id: str | None = None


class ControlMcpMessageRequest(
    msgspec.Struct, tag="mcp_message", tag_field="subtype", forbid_unknown_fields=False
):
    server_name: str
    message: Any


class ControlRewindFilesRequest(
    msgspec.Struct, tag="rewind_files", tag_field="subtype", forbid_unknown_fields=False
):
    user_message_id: str


type ControlRequest = (
    ControlInterruptRequest
    | ControlCanUseToolRequest
    | ControlInitializeRequest
    | ControlSetPermissionModeRequest
    | ControlHookCallbackRequest
    | ControlMcpMessageRequest
    | ControlRewindFilesRequest
)


class StreamControlRequest(
    msgspec.Struct, tag="control_request", tag_field="type", forbid_unknown_fields=False
):
    request_id: str
    request: ControlRequest


class ControlSuccessResponse(
    msgspec.Struct, tag="success", tag_field="subtype", forbid_unknown_fields=False
):
    request_id: str
    response: dict[str, Any] | None = None


class ControlErrorResponse(
    msgspec.Struct, tag="error", tag_field="subtype", forbid_unknown_fields=False
):
    request_id: str
    error: str


type ControlResponse = ControlSuccessResponse | ControlErrorResponse


class StreamControlResponse(
    msgspec.Struct,
    tag="control_response",
    tag_field="type",
    forbid_unknown_fields=False,
):
    response: ControlResponse


class StreamControlCancelRequest(
    msgspec.Struct,
    tag="control_cancel_request",
    tag_field="type",
    forbid_unknown_fields=False,
):
    request_id: str | None = None


class RateLimitInfo(msgspec.Struct, forbid_unknown_fields=False):
    requests_limit: int | None = None
    requests_remaining: int | None = None
    requests_reset: str | None = None
    tokens_limit: int | None = None
    tokens_remaining: int | None = None
    tokens_reset: str | None = None
    retry_after_ms: int | None = None


class StreamRateLimitMessage(
    msgspec.Struct,
    tag="rate_limit_event",
    tag_field="type",
    forbid_unknown_fields=False,
):
    rate_limit_info: RateLimitInfo | None = None


# #637 — Claude Code emits a top-level `tool_progress` heartbeat while a
# long-running tool is in flight, e.g.
#   {"type":"tool_progress","tool_use_id":"toolu_…-heartbeat-0",
#    "tool_name":"Bash","parent_tool_use_id":"toolu_…",
#    "elapsed_time_seconds":30,"heartbeat":true,"session_id":…,"uuid":…}
# Verified on CLI 2.1.214 by running a >30s Bash command. Every field is
# optional so a shape change upstream can't reintroduce the drop; the line
# just needs to decode so the rest of the stream isn't discarded
# (jsonl.msgspec.invalid x2 on nsd). Same family as #489 / #597, but the
# first *top-level* addition since `rate_limit_event`.
#
# No runner change is required: `translate_claude_event`'s fallback logs
# `claude.event.unrecognised` at DEBUG and returns [], so the heartbeat is
# accepted and ignored. Wire it into progress rendering separately if the
# elapsed-time detail ever becomes useful (#481 already renders elapsed
# time from Untether's own clock).
class StreamToolProgressMessage(
    msgspec.Struct,
    tag="tool_progress",
    tag_field="type",
    forbid_unknown_fields=False,
):
    tool_use_id: str | None = None
    tool_name: str | None = None
    parent_tool_use_id: str | None = None
    elapsed_time_seconds: float | None = None
    heartbeat: bool | None = None
    session_id: str | None = None
    uuid: str | None = None


type StreamJsonMessage = (
    StreamUserMessage
    | StreamAssistantMessage
    | StreamSystemMessage
    | StreamResultMessage
    | StreamEventMessage
    | StreamControlRequest
    | StreamControlResponse
    | StreamControlCancelRequest
    | StreamRateLimitMessage
    | StreamToolProgressMessage
)


STREAM_JSON_SCHEMA = msgspec.json.schema(StreamJsonMessage)

_DECODER = msgspec.json.Decoder(StreamJsonMessage)


def decode_stream_json_line(line: str | bytes) -> StreamJsonMessage:
    return _DECODER.decode(line)
