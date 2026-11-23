"""Codex ``app-server`` protocol: the parts Untether depends on (#961, #962).

The contract dicts are what the drift tests and the pre-spawn gate check
against (``codex app-server generate-json-schema [--experimental]``). Tier
``S`` = stable (in the plain bundle), ``E`` = only behind ``experimentalApi``.
The msgspec structs below them are what the transport decodes.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec

type Tier = Literal["S", "E"]
type MessageKind = Literal[
    "client_request", "client_notification", "server_notification", "server_request"
]

# Last CLI the committed schema fixture was derived from.
PROBED_APP_SERVER_VERSION = "0.171.0"
# Oldest CLI the app-server transport was tested with; named in the gate's
# refusal only. The gate itself detects support from the schema, never by
# comparing versions.
MIN_TESTED_APP_SERVER_VERSION = "0.169.0"

# Methods, notifications and server requests the app-server transport uses,
# with the tier it relies on (#962 plan §3). Later phases add theirs.
CODEX_APP_SERVER_USED: dict[tuple[MessageKind, str], Tier] = {
    ("client_request", "initialize"): "S",
    ("client_request", "thread/start"): "S",
    ("client_request", "thread/resume"): "S",
    ("client_request", "thread/list"): "S",
    ("client_request", "turn/start"): "S",
    ("client_request", "turn/interrupt"): "S",
    ("client_notification", "initialized"): "S",
    # Zero-token reads (runners/codex_reads.py: #966, #846, #423). config/read
    # is also the runner's trust and model check (#1369).
    ("client_request", "account/read"): "S",
    ("client_request", "account/rateLimits/read"): "S",
    ("client_request", "model/list"): "S",
    ("client_request", "config/read"): "S",
    ("client_request", "configRequirements/read"): "S",
    ("client_request", "modelProvider/capabilities/read"): "S",
    ("server_notification", "account/rateLimits/updated"): "S",
    ("server_notification", "model/rerouted"): "S",
    ("server_notification", "thread/started"): "S",
    ("server_notification", "turn/started"): "S",
    ("server_notification", "turn/completed"): "S",
    ("server_notification", "item/started"): "S",
    ("server_notification", "item/completed"): "S",
    ("server_notification", "thread/tokenUsage/updated"): "S",
    ("server_notification", "error"): "S",
    ("server_notification", "warning"): "S",
    ("server_notification", "configWarning"): "S",
    ("server_notification", "deprecationNotice"): "S",
    # #963: approval cards, withdrawal, diff refresh, auto-review rows. The
    # two review notifications are stable methods with an [UNSTABLE] payload
    # (decoded as plain dicts).
    ("server_notification", "serverRequest/resolved"): "S",
    ("server_notification", "item/fileChange/patchUpdated"): "S",
    ("server_notification", "item/autoApprovalReview/started"): "S",
    ("server_notification", "item/autoApprovalReview/completed"): "S",
    ("server_notification", "guardianWarning"): "S",
    # Answered from a Telegram card (#963), or declined when unattended.
    ("server_request", "item/commandExecution/requestApproval"): "S",
    ("server_request", "item/fileChange/requestApproval"): "S",
    ("server_request", "item/permissions/requestApproval"): "S",
    ("server_request", "mcpServer/elicitation/request"): "S",
}

# Payload types Untether reads, by schema definition name. A non-empty tuple
# names the ``type`` variants of a tagged union (``ThreadItem.agentMessage``).
CODEX_APP_SERVER_TYPES: dict[str, tuple[str, ...]] = {
    "ThreadItem": (
        "agentMessage",
        "reasoning",
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "collabAgentToolCall",
        "webSearch",
        "imageView",
        "contextCompaction",
    ),
    "Turn": (),
    "TurnError": (),
    "ThreadTokenUsage": (),
    "TokenUsageBreakdown": (),
    # #963 cards: the diff, the network prompt, the permission request.
    "FileUpdateChange": (),
    "PatchChangeKind": ("add", "delete", "update"),
    "NetworkApprovalContext": (),
    "NetworkPolicyAmendment": (),
    "AdditionalNetworkPermissions": (),
    "AdditionalFileSystemPermissions": (),
}

# Values Untether sends (permission tiers DEC-5/#421, approval answers #963,
# plan mode). An object decision counts by its only key
# (``acceptWithExecpolicyAmendment``).
CODEX_APP_SERVER_SENDS: dict[str, frozenset[str]] = {
    "AskForApproval": frozenset({"never", "on-request"}),
    "SandboxMode": frozenset({"read-only", "workspace-write"}),
    "ApprovalsReviewer": frozenset({"user", "auto_review"}),
    "CommandExecutionApprovalDecision": frozenset(
        {
            "accept",
            "acceptForSession",
            "acceptWithExecpolicyAmendment",
            "applyNetworkPolicyAmendment",
            "decline",
            "cancel",
        }
    ),
    "FileChangeApprovalDecision": frozenset(
        {"accept", "acceptForSession", "decline", "cancel"}
    ),
    "PermissionGrantScope": frozenset({"turn", "session"}),
    "McpServerElicitationAction": frozenset({"decline"}),
    "ModeKind": frozenset({"plan", "default"}),
}


# --- msgspec structs (#962) --------------------------------------------------
# Lenient by design (plan §0.5): unknown fields are ignored, enum-like values
# stay ``str``, and shapes Codex has reshaped between releases stay untyped
# (``moderation`` changed in 0.170.0; ``codexErrorInfo`` is a string-or-object
# union). Field names are snake_case here, camelCase on the wire.


class _Wire(msgspec.Struct, rename="camel", kw_only=True):
    pass


class TokenUsageBreakdown(_Wire):
    total_tokens: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


class ThreadTokenUsage(_Wire):
    total: TokenUsageBreakdown
    last: TokenUsageBreakdown
    model_context_window: int | None = None


class TurnError(_Wire):
    message: str = ""
    codex_error_info: Any = None
    additional_details: str | None = None


class Turn(_Wire):
    id: str
    status: str
    error: TurnError | None = None
    # Raw items: decode each with ``decode_thread_item``.
    items: list[dict[str, Any]] = msgspec.field(default_factory=list)


class Thread(_Wire):
    id: str
    cwd: str | None = None
    preview: str | None = None
    model: str | None = None
    # SessionSource: a string or ``{"custom": …}`` / ``{"subAgent": …}``.
    source: Any = None
    updated_at: int | None = None


# ThreadItem variants Untether reads (CODEX_APP_SERVER_TYPES["ThreadItem"]).
class AgentMessageItem(_Wire):
    id: str
    text: str = ""
    phase: str | None = None
    moderation: dict[str, Any] | None = None
    type: str = "agentMessage"


class ReasoningItem(_Wire):
    id: str
    summary: list[Any] = msgspec.field(default_factory=list)
    content: list[Any] = msgspec.field(default_factory=list)
    type: str = "reasoning"


class CommandExecutionItem(_Wire):
    id: str
    command: str = ""
    cwd: str | None = None
    status: str = ""
    exit_code: int | None = None
    aggregated_output: str | None = None
    duration_ms: int | None = None
    type: str = "commandExecution"


class FileChangeItem(_Wire):
    id: str
    # Each change's ``kind`` is an object here, not exec's string.
    changes: list[Any] = msgspec.field(default_factory=list)
    status: str = ""
    type: str = "fileChange"


class McpToolCallItem(_Wire):
    id: str
    server: str = ""
    tool: str = ""
    status: str = ""
    arguments: Any = None
    result: Any = None
    error: Any = None
    duration_ms: int | None = None
    type: str = "mcpToolCall"


class CollabAgentToolCallItem(_Wire):
    id: str
    tool: str = ""
    status: str = ""
    prompt: str | None = None
    model: str | None = None
    receiver_thread_ids: list[str] = msgspec.field(default_factory=list)
    type: str = "collabAgentToolCall"


class WebSearchItem(_Wire):
    id: str
    query: str = ""
    action: Any = None
    results: Any = None
    type: str = "webSearch"


class ImageViewItem(_Wire):
    id: str
    path: str = ""
    type: str = "imageView"


class ContextCompactionItem(_Wire):
    id: str
    type: str = "contextCompaction"


class GenericItem(_Wire):
    """An item type Untether doesn't model, or one that no longer decodes."""

    type: str
    id: str | None = None
    raw: dict[str, Any] = msgspec.field(default_factory=dict)


type ThreadItem = (
    AgentMessageItem
    | ReasoningItem
    | CommandExecutionItem
    | FileChangeItem
    | McpToolCallItem
    | CollabAgentToolCallItem
    | WebSearchItem
    | ImageViewItem
    | ContextCompactionItem
    | GenericItem
)

THREAD_ITEM_TYPES: dict[str, type[_Wire]] = {
    "agentMessage": AgentMessageItem,
    "reasoning": ReasoningItem,
    "commandExecution": CommandExecutionItem,
    "fileChange": FileChangeItem,
    "mcpToolCall": McpToolCallItem,
    "collabAgentToolCall": CollabAgentToolCallItem,
    "webSearch": WebSearchItem,
    "imageView": ImageViewItem,
    "contextCompaction": ContextCompactionItem,
}


def decode_thread_item(raw: Any) -> ThreadItem:
    """A raw ``ThreadItem`` dict → its struct, or ``GenericItem`` for an
    unknown type or a shape that no longer fits. Never raises."""
    raw = raw if isinstance(raw, dict) else {}
    item_type = raw.get("type")
    item_id = raw.get("id")
    cls = THREAD_ITEM_TYPES.get(item_type) if isinstance(item_type, str) else None
    if cls is not None:
        try:
            return msgspec.convert(raw, cls)  # type: ignore[return-value]
        except msgspec.ValidationError:
            pass
    return GenericItem(
        type=item_type if isinstance(item_type, str) else "",
        id=item_id if isinstance(item_id, str) else None,
        raw=raw,
    )


# Client request results.
class InitializeResult(_Wire):
    user_agent: str = ""
    codex_home: str | None = None
    platform_family: str | None = None
    platform_os: str | None = None


class ThreadOpenResult(_Wire):
    """``thread/start`` and ``thread/resume`` (same stable result fields)."""

    thread: Thread
    model: str | None = None
    cwd: str | None = None
    approval_policy: Any = None
    # A ``SandboxPolicy`` object (``{"type": "readOnly", …}``), not the
    # kebab-case ``SandboxMode`` string Untether sends.
    sandbox: Any = None
    approvals_reviewer: str | None = None
    reasoning_effort: str | None = None


class ThreadListResult(_Wire):
    data: list[Thread] = msgspec.field(default_factory=list)
    next_cursor: str | None = None


class TurnStartResult(_Wire):
    turn: Turn


RESULT_TYPES: dict[str, type[_Wire]] = {
    "initialize": InitializeResult,
    "thread/start": ThreadOpenResult,
    "thread/resume": ThreadOpenResult,
    "thread/list": ThreadListResult,
    "turn/start": TurnStartResult,
}


# Server notification params.
class ThreadStartedParams(_Wire):
    thread: Thread


class TurnParams(_Wire):
    """``turn/started`` and ``turn/completed``."""

    thread_id: str
    turn: Turn


class ItemParams(_Wire):
    """``item/started`` / ``item/completed``; decode ``item`` with
    ``decode_thread_item``."""

    item: dict[str, Any]
    thread_id: str = ""
    turn_id: str = ""


class TokenUsageUpdatedParams(_Wire):
    thread_id: str
    turn_id: str
    token_usage: ThreadTokenUsage


class ErrorParams(_Wire):
    error: TurnError
    will_retry: bool = False
    thread_id: str = ""
    turn_id: str = ""


class WarningParams(_Wire):
    message: str
    thread_id: str | None = None


class ConfigWarningParams(_Wire):
    summary: str
    details: str | None = None
    path: str | None = None


class DeprecationNoticeParams(_Wire):
    summary: str
    details: str | None = None


class ServerRequestResolvedParams(_Wire):
    thread_id: str
    # RequestId: a number or a string on the wire.
    request_id: int | str


class PatchUpdatedParams(_Wire):
    item_id: str
    changes: list[Any] = msgspec.field(default_factory=list)
    thread_id: str = ""
    turn_id: str = ""


class GuardianWarningParams(_Wire):
    message: str
    thread_id: str = ""


# Server request params (#963). Lenient: nested shapes stay untyped, and
# ``availableDecisions`` (experimental field, sent on stable connections
# anyway, findings H2) is optional.
class CommandApprovalParams(_Wire):
    thread_id: str
    item_id: str
    turn_id: str = ""
    kind: str = "command"
    command: str | None = None
    cwd: str | None = None
    reason: str | None = None
    command_actions: list[Any] | None = None
    approval_id: str | None = None
    network_approval_context: dict[str, Any] | None = None
    proposed_execpolicy_amendment: list[str] | None = None
    proposed_network_policy_amendments: list[Any] | None = None
    available_decisions: list[Any] | None = None


class FileChangeApprovalParams(_Wire):
    thread_id: str
    item_id: str
    turn_id: str = ""
    reason: str | None = None
    grant_root: str | None = None


class PermissionsApprovalParams(_Wire):
    thread_id: str
    item_id: str
    turn_id: str = ""
    cwd: str | None = None
    reason: str | None = None
    permissions: dict[str, Any] = msgspec.field(default_factory=dict)


# Notifications whose payload the schema marks [UNSTABLE] (auto-review, Δ D47):
# read as plain dicts, a missing key gives a generic row, never an error.
DICT_NOTIFICATIONS = frozenset(
    {"item/autoApprovalReview/started", "item/autoApprovalReview/completed"}
)
# The one experimental-tier field read on a stable connection (register row:
# the server sends it unasked; a stable fallback mirrors Codex's default set).
EXPERIMENTAL_FIELDS_READ = frozenset({"availableDecisions"})

REQUEST_PARAM_TYPES: dict[str, type[_Wire]] = {
    "item/commandExecution/requestApproval": CommandApprovalParams,
    "item/fileChange/requestApproval": FileChangeApprovalParams,
    "item/permissions/requestApproval": PermissionsApprovalParams,
}


def decode_request(method: str, params: Any) -> Any | None:
    """Approval request params → their struct; None for another method or a
    shape that no longer fits (the caller then declines it). Never raises."""
    cls = REQUEST_PARAM_TYPES.get(method)
    if cls is None:
        return None
    try:
        return msgspec.convert(params, cls)
    except msgspec.ValidationError:
        return None
class RateLimitsUpdatedParams(_Wire):
    """Sparse: merge non-null fields into the last read (#966)."""

    rate_limits: dict[str, Any] | None = None


class ModelReroutedParams(_Wire):
    thread_id: str = ""
    turn_id: str = ""
    from_model: str = ""
    to_model: str = ""
    reason: Any = None


NOTIFICATION_TYPES: dict[str, type[_Wire]] = {
    "thread/started": ThreadStartedParams,
    "turn/started": TurnParams,
    "turn/completed": TurnParams,
    "item/started": ItemParams,
    "item/completed": ItemParams,
    "thread/tokenUsage/updated": TokenUsageUpdatedParams,
    "error": ErrorParams,
    "warning": WarningParams,
    "configWarning": ConfigWarningParams,
    "deprecationNotice": DeprecationNoticeParams,
    "serverRequest/resolved": ServerRequestResolvedParams,
    "item/fileChange/patchUpdated": PatchUpdatedParams,
    "guardianWarning": GuardianWarningParams,
    "account/rateLimits/updated": RateLimitsUpdatedParams,
    "model/rerouted": ModelReroutedParams,
}


def decode_result(method: str, result: Any) -> Any:
    """A request's ``result`` → its struct (the raw value for a method
    without one). Raises ``msgspec.ValidationError`` on a missing field."""
    cls = RESULT_TYPES.get(method)
    return result if cls is None else msgspec.convert(result, cls)


def decode_notification(method: str, params: Any) -> Any | None:
    """Notification params → their struct, or ``None`` for a method
    Untether doesn't read. Raises ``msgspec.ValidationError`` on a shape
    that no longer fits."""
    cls = NOTIFICATION_TYPES.get(method)
    return None if cls is None else msgspec.convert(params, cls)
