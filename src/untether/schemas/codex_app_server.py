"""Codex ``app-server`` protocol: the parts Untether depends on (#961).

The JSON-RPC structs arrive with the transport (#962). Until then this module
is the contract the drift tests and the pre-spawn gate check against
(``codex app-server generate-json-schema [--experimental]``). Tier ``S`` =
stable (in the plain bundle), ``E`` = only behind ``experimentalApi``.
"""

from __future__ import annotations

from typing import Literal

type Tier = Literal["S", "E"]
type MessageKind = Literal[
    "client_request", "client_notification", "server_notification", "server_request"
]

# Last CLI the committed schema fixture was derived from.
PROBED_APP_SERVER_VERSION = "0.169.0"
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
    # Never expected with approvalPolicy=never, but always answered (decline).
    ("server_request", "item/commandExecution/requestApproval"): "S",
    ("server_request", "item/fileChange/requestApproval"): "S",
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
}

# Enum values Untether sends (permission modes DEC-5, declines, plan mode).
CODEX_APP_SERVER_SENDS: dict[str, frozenset[str]] = {
    "AskForApproval": frozenset({"never", "on-request"}),
    "SandboxMode": frozenset({"read-only", "workspace-write"}),
    "ApprovalsReviewer": frozenset({"user", "auto_review"}),
    "CommandExecutionApprovalDecision": frozenset({"decline"}),
    "FileChangeApprovalDecision": frozenset({"decline"}),
    "McpServerElicitationAction": frozenset({"decline"}),
    "ModeKind": frozenset({"plan", "default"}),
}
