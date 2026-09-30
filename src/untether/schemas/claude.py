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
    # #814: API message id — one API response may span several stream
    # frames, so refusal counting dedupes on it.
    id: str | None = None
    # #814: ``"refusal"`` when Anthropic's safeguards stopped the response;
    # ``stop_details`` then carries ``{type:"refusal", category,
    # explanation}`` (category ∈ cyber / bio / frontier_llm /
    # reasoning_extraction / general_harms, or null). Passed through
    # verbatim by the CLI's headless emitter.
    stop_reason: str | None = None
    stop_details: dict[str, Any] | None = None
    # #819: the API usage of this response. ``input_tokens`` +
    # ``cache_creation_input_tokens`` + ``cache_read_input_tokens`` of the
    # latest main-thread frame is the usage part of the CLI's ``/context``
    # total. Any: the runner reads ints only, so a shape change can never
    # drop the line.
    usage: Any = None


class StreamUserMessage(
    msgspec.Struct, tag="user", tag_field="type", forbid_unknown_fields=False
):
    message: StreamUserMessageBody
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    session_id: str | None = None
    # #819: ``isCompactSummary`` marks the compaction summary the CLI writes
    # after ``compact_boundary``; ``isReplay`` a replayed local-command echo
    # (``/compact``'s "Compacted" stdout). Wire presence unconfirmed on
    # 2.1.285, so Any + optional.
    isReplay: Any = None
    isCompactSummary: Any = None


class StreamAssistantMessage(
    msgspec.Struct, tag="assistant", tag_field="type", forbid_unknown_fields=False
):
    message: StreamAssistantMessageBody
    parent_tool_use_id: str | None = None
    uuid: str | None = None
    session_id: str | None = None


class ApiRetryNoResponse(msgspec.Struct, forbid_unknown_fields=False):
    """#792: ``system/api_retry.no_response`` — the failed attempt waited
    ``waited_ms`` for response headers; the retry will wait up to
    ``retry_wait_ms`` for them."""

    waited_ms: int | None = None
    retry_wait_ms: int | None = None


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
    # Background-task lifecycle subtypes (#776), verified on CLI 2.1.283:
    # task_started / task_progress / task_updated / task_notification carry
    # ``task_id``; background_tasks_changed carries a ``tasks`` snapshot.
    # All optional so upstream shape drift degrades to "field missing"
    # rather than a dropped line.
    task_id: str | None = None
    tool_use_id: str | None = None
    description: str | None = None
    task_type: str | None = None
    is_backgrounded: bool | None = None
    owned_by_subagent: bool | None = None
    subagent_type: str | None = None
    spawn_depth: int | None = None
    prompt: str | None = None
    status: str | None = None
    patch: dict[str, Any] | None = None
    summary: str | None = None
    output_file: str | None = None
    usage: dict[str, Any] | None = None
    last_tool_name: str | None = None
    tasks: list[dict[str, Any]] | None = None
    # #792 ``api_retry`` (CLI 2.1.283, SDKAPIRetryMessage): an API call
    # failed with a retryable error and the CLI is backing off, e.g.
    #   {"type":"system","subtype":"api_retry","attempt":2,"max_retries":10,
    #    "retry_delay_ms":8000,"error_status":529,"error":"overloaded",…}
    # ``error_status`` is null for connection errors with no HTTP response;
    # ``error`` is an upstream category string today (overloaded /
    # rate_limit / server_error / …) — typed Any so a richer shape can't
    # drop the line. ``no_response`` appears only when no headers arrived
    # within CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS. No other system subtype
    # uses these keys with a conflicting type (checked on 2.1.283).
    attempt: int | None = None
    max_retries: int | None = None
    retry_delay_ms: int | None = None
    error_status: int | None = None
    error: Any = None
    no_response: ApiRetryNoResponse | None = None
    # #814 ``informational`` (SDKInformationalMessage): a text banner —
    # ``content`` + ``level`` (info / notice / suggestion / warning),
    # optional ``tool_use_id`` / ``prevent_continuation``. The safeguard
    # notice is ``"<Model>'s safeguards stopped the response above ·
    # continuing once with that noted"`` at level notice.
    # ``model_refusal_fallback`` / ``model_refusal_no_fallback`` /
    # ``model_fallback`` (undocumented, CLI 2.1.285) carry
    # ``original_model`` / ``fallback_model`` / ``api_refusal_*`` plus
    # ``trigger`` / ``direction`` / ``scope`` enums. All typed Any: ~40
    # system subtypes share this struct and their shapes are unverified,
    # so a type clash must never drop the whole line as
    # ``jsonl.msgspec.invalid`` — the runner normalises on read.
    content: Any = None
    level: Any = None
    prevent_continuation: Any = None
    original_model: Any = None
    fallback_model: Any = None
    api_refusal_category: Any = None
    api_refusal_explanation: Any = None
    trigger: Any = None
    direction: Any = None
    scope: Any = None
    # #812 ``--include-hook-events`` (CLI 2.1.284): ``hook_started`` /
    # ``hook_progress`` / ``hook_response`` carry ``hook_id`` (pairs started
    # with response), ``hook_name`` (e.g. ``Stop`` / ``SessionStart:startup``)
    # and ``hook_event``; ``hook_response`` adds ``outcome``
    # (success / error / cancelled) and an optional ``exit_code`` (2 =
    # blocking error, the asyncRewake wake signal). ``stdout`` / ``stderr``
    # / ``output`` are deliberately NOT declared, so msgspec skips them and
    # hook output is never held in memory. All typed Any (hook_id included —
    # the runner normalises on read) for the same drift reason as above.
    hook_id: Any = None
    hook_name: Any = None
    hook_event: Any = None
    outcome: Any = None
    exit_code: Any = None
    # #819 compaction (CLI 2.1.285): ``status`` frames carry
    # ``status: "compacting"`` (re-sent every 30 s) and then ``status: null``
    # with ``compact_result`` (success / failed) and an optional
    # ``compact_error``; ``compact_boundary`` carries ``compact_metadata``
    # ``{trigger: manual|auto, pre_tokens, post_tokens?,
    # cumulative_dropped_tokens?, duration_ms?, …}`` and an optional
    # ``logical_parent_uuid``. All Any for the drift reason above.
    compact_metadata: Any = None
    compact_result: Any = None
    compact_error: Any = None
    logical_parent_uuid: Any = None


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
    # #806: why the turn ended. ``aborted_streaming`` / ``aborted_tools``
    # mean the turn was interrupted (see CLAUDE_ABORTED_TERMINAL_REASONS);
    # the subtype is then usually ``success``, sometimes
    # ``error_during_execution`` — classify on this field, never on subtype.
    terminal_reason: str | None = None
    # What started the turn (user / task notification / …); read by #812.
    # Any, not dict: a non-object origin must not drop the result line —
    # readers check ``isinstance(origin, dict)`` first.
    origin: Any = None
    stop_reason: Any = None
    # #819: per-model usage for the session, keyed by the model id the CLI
    # used; each entry carries ``contextWindow`` (the context-% denominator)
    # and ``maxOutputTokens``. Field name as on the wire. Any: readers check
    # ``isinstance(..., dict)`` and int fields.
    modelUsage: Any = None


# #806: result ``terminal_reason`` values that mean the turn was cancelled
# (an interrupt), not that it failed.
CLAUDE_ABORTED_TERMINAL_REASONS: frozenset[str] = frozenset(
    {"aborted_streaming", "aborted_tools"}
)


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
    # e.g. "invalid_mode" / "bypass_not_launched" on a refused
    # set_permission_mode (CLI 2.1.285; #383).
    error_code: str | None = None


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


# #790: `rate_limit_event` is a quota-status *snapshot*, not a throttle
# notice. Real payload (CLI 2.1.283):
#   {"type":"rate_limit_event","rate_limit_info":{"status":"allowed",
#    "resetsAt":1790578200,"rateLimitType":"five_hour",
#    "overageStatus":"rejected","overageDisabledReason":"out_of_credits",
#    "isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.09,
#    "resetsAt":1790578200},"seven_day":{…}}},"uuid":…,"session_id":…}
# The upstream zod enums are mirrored below and pinned by
# tests/test_claude_cli_schema_drift.py. The struct fields stay plain `str`
# (not Literal) so a new upstream value degrades to "unknown status" in the
# runner instead of a dropped line.
CLAUDE_RATE_LIMIT_STATUSES: tuple[str, ...] = ("allowed", "allowed_warning", "rejected")
CLAUDE_RATE_LIMIT_TYPES: tuple[str, ...] = (
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_overage_included",
    "overage",
)
CLAUDE_OVERAGE_STATUSES: tuple[str, ...] = CLAUDE_RATE_LIMIT_STATUSES


class RateLimitWindow(msgspec.Struct, forbid_unknown_fields=False):
    utilization: float | None = None
    resets_at: float | None = msgspec.field(default=None, name="resetsAt")


class RateLimitUnifiedWindows(msgspec.Struct, forbid_unknown_fields=False):
    five_hour: RateLimitWindow | None = None
    seven_day: RateLimitWindow | None = None
    seven_day_overage_included: RateLimitWindow | None = None


class RateLimitInfo(msgspec.Struct, forbid_unknown_fields=False):
    # --- real snapshot shape (#790, CLI 2.1.283) ---
    status: str | None = None
    # epoch seconds
    resets_at: float | None = msgspec.field(default=None, name="resetsAt")
    rate_limit_type: str | None = msgspec.field(default=None, name="rateLimitType")
    utilization: float | None = None
    unified_windows: RateLimitUnifiedWindows | None = msgspec.field(
        default=None, name="unifiedWindows"
    )
    overage_status: str | None = msgspec.field(default=None, name="overageStatus")
    overage_resets_at: float | None = msgspec.field(
        default=None, name="overageResetsAt"
    )
    overage_disabled_reason: str | None = msgspec.field(
        default=None, name="overageDisabledReason"
    )
    is_using_overage: bool | None = msgspec.field(default=None, name="isUsingOverage")
    error_code: str | None = msgspec.field(default=None, name="errorCode")
    # --- legacy shape (#349/#518); never observed from a real CLI, kept so
    # an older/alternative emitter still gets the precise countdown ---
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
    uuid: str | None = None
    session_id: str | None = None


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


# #776: one line per stdin input command, e.g.
#   {"type":"command_lifecycle","command_uuid":"<uuid>","state":"queued"}
# ``state`` is queued / started / completed. ``command_uuid`` echoes the
# ``uuid`` Untether puts on an injected user line, which makes follow-up →
# turn attribution exact; a ScheduleWakeup firing appears as ``started``
# with an unknown uuid. Verified on CLI 2.1.283.
class StreamCommandLifecycleMessage(
    msgspec.Struct,
    tag="command_lifecycle",
    tag_field="type",
    forbid_unknown_fields=False,
):
    command_uuid: str | None = None
    state: str | None = None
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
    | StreamCommandLifecycleMessage
)


STREAM_JSON_SCHEMA = msgspec.json.schema(StreamJsonMessage)

_DECODER = msgspec.json.Decoder(StreamJsonMessage)


def decode_stream_json_line(line: str | bytes) -> StreamJsonMessage:
    return _DECODER.decode(line)
