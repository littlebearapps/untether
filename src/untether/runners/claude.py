"""
Updated ClaudeRunner with PTY support for control channel.

This replaces the existing claude.py with PTY-based stdin handling
to prevent deadlock when keeping stdin open for control responses.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import shutil
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import msgspec

from ..backends import EngineBackend, EngineConfig
from ..events import EventFactory
from ..logging import get_logger
from ..model import (
    Action,
    ActionKind,
    EngineId,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
    CompletedEvent,
)
from ..runner import (
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    JsonlStreamState,
    _rc_label,
    _session_label,
    _stderr_excerpt,
)
from .run_options import get_run_options
from ..schemas import claude as claude_schema
from .tool_actions import tool_input_path, tool_kind_and_title
from ..utils.paths import get_run_base_dir
from ..utils.streams import drain_stderr
from ..utils.subprocess import manage_subprocess

import subprocess as subprocess_module

logger = get_logger(__name__)

ENGINE: EngineId = "claude"
DEFAULT_ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write"]

_RESUME_RE = re.compile(
    r"(?im)^\s*`?claude\s+(?:--resume|-r)\s+(?P<token>[^`\s]+)`?\s*$"
)

# Phase 2: Global registry for active ClaudeRunner instances
# Keyed by session_id, stores (runner_instance, timestamp)
_ACTIVE_RUNNERS: dict[str, tuple[ClaudeRunner, float]] = {}

# Phase 2: Global registry mapping session_id -> process stdin
# Stored separately from _ACTIVE_RUNNERS to support concurrent sessions
# on the same runner instance (runner._proc_stdin would be overwritten).
_SESSION_STDIN: dict[str, Any] = {}

# Phase 2: Global registry mapping request_id -> session_id
# This allows callbacks to find the right runner instance
_REQUEST_TO_SESSION: dict[str, str] = {}

# Phase 2: Global registry mapping request_id -> original tool input
# Claude Code CLI requires updatedInput in can_use_tool responses
_REQUEST_TO_INPUT: dict[str, dict[str, Any]] = {}

# Recently handled request_ids (prevents duplicate callback warnings)
_HANDLED_REQUESTS: set[str] = set()

# Discuss cooldown: session_id -> (timestamp, deny_count)
# When user clicks "Pause & Outline Plan", this tracks when the denial was sent
# so rapid ExitPlanMode retries can be auto-denied with escalating messages.
_DISCUSS_COOLDOWN: dict[str, tuple[float, int]] = {}

# Discuss approval: session_ids where user approved the plan via post-outline buttons.
# When Claude next calls ExitPlanMode, it will be auto-approved.
_DISCUSS_APPROVED: set[str] = set()

# A1: Pending AskUserQuestion requests: request_id -> question text
# When Claude asks a question, the user can reply via Telegram text.
_PENDING_ASK_REQUESTS: dict[str, str] = {}
DISCUSS_COOLDOWN_BASE_SECONDS: float = 30.0
DISCUSS_COOLDOWN_MAX_SECONDS: float = 120.0

_DISCUSS_ESCALATION_MESSAGE = (
    "ExitPlanMode was temporarily held — Approve/Deny buttons have been shown to the user "
    "in Telegram. The user will click Approve when ready.\n\n"
    "If you haven't written a plan outline yet, write one NOW as your next assistant message "
    "(at least 15 lines of visible text). The user can ONLY see your assistant text messages.\n\n"
    "WAIT for the user to approve via the buttons. Do NOT call ExitPlanMode again until they respond."
)


@dataclass(slots=True)
class ClaudeStreamState:
    factory: EventFactory = field(default_factory=lambda: EventFactory(ENGINE))
    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_assistant_text: str | None = None
    note_seq: int = 0
    # Phase 2: Control request tracking
    pending_control_requests: dict[
        str, tuple[claude_schema.StreamControlRequest, float]
    ] = field(default_factory=dict)
    # Auto-approve queue: request IDs that should be approved without user interaction
    auto_approve_queue: list[str] = field(default_factory=list)
    # Auto-deny queue: (request_id, message) pairs for rate-limited denials
    auto_deny_queue: list[tuple[str, str]] = field(default_factory=list)
    # Whether the control channel initialization handshake has been sent
    control_init_sent: bool = False
    # Track last tool_use_id for mapping control requests to tool actions
    last_tool_use_id: str | None = None
    # Map tool_use_id -> control action_id for completing control actions on tool result
    control_action_for_tool: dict[str, str] = field(default_factory=dict)
    # Auto-approve ExitPlanMode when permission_mode is "auto"
    auto_approve_exit_plan_mode: bool = False
    # Whether this run is a resume (for error diagnostics)
    resumed: bool = False


def _normalize_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return str(content)


def _coerce_comma_list(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value if item is not None]
        joined = ",".join(part for part in parts if part)
        return joined or None
    text = str(value)
    return text or None


def _tool_kind_and_title(
    name: str, tool_input: dict[str, Any]
) -> tuple[ActionKind, str]:
    return tool_kind_and_title(name, tool_input, path_keys=("file_path", "path"))


def _tool_action(
    content: claude_schema.StreamToolUseBlock,
    *,
    parent_tool_use_id: str | None,
) -> Action:
    tool_id = content.id
    tool_name = str(content.name or "tool")
    tool_input = content.input

    kind, title = _tool_kind_and_title(tool_name, tool_input)

    detail: dict[str, Any] = {
        "name": tool_name,
        "input": tool_input,
    }
    if parent_tool_use_id:
        detail["parent_tool_use_id"] = parent_tool_use_id

    if kind == "file_change":
        path = tool_input_path(tool_input, path_keys=("file_path", "path"))
        if path:
            detail["changes"] = [{"path": path, "kind": "update"}]

    return Action(id=tool_id, kind=kind, title=title, detail=detail)


def _tool_result_event(
    content: claude_schema.StreamToolResultBlock,
    *,
    action: Action,
    factory: EventFactory,
) -> UntetherEvent:
    is_error = content.is_error is True
    raw_result = content.content
    normalized = _normalize_tool_result(raw_result)
    preview = normalized

    detail = action.detail | {
        "tool_use_id": content.tool_use_id,
        "result_preview": preview,
        "result_len": len(normalized),
        "is_error": is_error,
    }
    return factory.action_completed(
        action_id=action.id,
        kind=action.kind,
        title=action.title,
        ok=not is_error,
        detail=detail,
    )


def _format_diff_preview(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a compact diff preview for Edit/Write tool approval messages."""
    max_preview_lines = 8
    max_line_len = 60

    def _truncate(text: str, max_len: int) -> str:
        if len(text) > max_len:
            return text[: max_len - 1] + "…"
        return text

    if tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        if not old_string and not new_string:
            return ""
        lines: list[str] = []
        if file_path:
            from ..utils.paths import relativize_path

            lines.append(f"📝 {relativize_path(file_path)}")
        old_lines = old_string.splitlines()
        new_lines = new_string.splitlines()
        # Show removed/added lines
        half = max_preview_lines // 2
        lines.extend(f"- {_truncate(line, max_line_len)}" for line in old_lines[:half])
        if len(old_lines) > half:
            lines.append(f"  …({len(old_lines) - half} more removed)")
        lines.extend(f"+ {_truncate(line, max_line_len)}" for line in new_lines[:half])
        if len(new_lines) > half:
            lines.append(f"  …({len(new_lines) - half} more added)")
        return "\n".join(lines)

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        if not content:
            return ""
        lines = []
        if file_path:
            from ..utils.paths import relativize_path

            lines.append(f"📝 {relativize_path(file_path)}")
        content_lines = content.splitlines()
        line_count = len(content_lines)
        for line in content_lines[:max_preview_lines]:
            lines.append(f"+ {_truncate(line, max_line_len)}")
        if line_count > max_preview_lines:
            lines.append(f"  …({line_count - max_preview_lines} more lines)")
        return "\n".join(lines)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if command:
            return f"$ {_truncate(command, 200)}"
        return ""

    return ""


def _extract_error(
    event: claude_schema.StreamResultMessage,
    *,
    resumed: bool = False,
) -> str | None:
    if not event.is_error:
        return None
    # First line: error summary
    if isinstance(event.result, str) and event.result:
        first = event.result
    elif event.subtype:
        first = f"claude run failed ({event.subtype})"
    else:
        first = "claude run failed"

    # Second line: diagnostic context
    parts: list[str] = []
    sid = event.session_id[:8] if event.session_id else None
    if sid:
        parts.append(f"session: {sid}")
    parts.append("resumed" if resumed else "new")
    parts.append(f"turns: {event.num_turns}")
    cost = event.total_cost_usd
    if cost is not None:
        parts.append(f"cost: ${cost:.2f}")
    if event.duration_api_ms:
        parts.append(f"api: {event.duration_api_ms}ms")

    return f"{first}\n{' · '.join(parts)}"


def _usage_payload(event: claude_schema.StreamResultMessage) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in (
        "total_cost_usd",
        "duration_ms",
        "duration_api_ms",
        "num_turns",
    ):
        value = getattr(event, key, None)
        if value is not None:
            usage[key] = value
    if event.usage is not None:
        usage["usage"] = event.usage
    return usage


def translate_claude_event(
    event: claude_schema.StreamJsonMessage,
    *,
    title: str,
    state: ClaudeStreamState,
    factory: EventFactory,
) -> list[UntetherEvent]:
    # DEBUG: Log all incoming events to track flow
    import structlog

    debug_logger = structlog.get_logger()
    debug_logger.info(
        "translate_claude_event.received",
        event_type=type(event).__name__,
        event_dict=event.__dict__ if hasattr(event, "__dict__") else str(event)[:200],
    )

    match event:
        case claude_schema.StreamSystemMessage(subtype=subtype):
            if subtype != "init":
                return []
            session_id = event.session_id
            if not session_id:
                return []
            meta: dict[str, Any] = {}
            for key in (
                "cwd",
                "model",
                "tools",
                "permissionMode",
                "output_style",
                "apiKeySource",
                "mcp_servers",
            ):
                value = getattr(event, key, None)
                if value is not None:
                    meta[key] = value
            model = event.model
            token = ResumeToken(engine=ENGINE, value=session_id)
            event_title = str(model) if isinstance(model, str) and model else title
            return [factory.started(token, title=event_title, meta=meta or None)]
        case claude_schema.StreamAssistantMessage(
            message=message, parent_tool_use_id=parent_tool_use_id
        ):
            out: list[UntetherEvent] = []
            for content in message.content:
                match content:
                    case claude_schema.StreamToolUseBlock():
                        action = _tool_action(
                            content,
                            parent_tool_use_id=parent_tool_use_id,
                        )
                        state.pending_actions[action.id] = action
                        state.last_tool_use_id = content.id
                        out.append(
                            factory.action_started(
                                action_id=action.id,
                                kind=action.kind,
                                title=action.title,
                                detail=action.detail,
                            )
                        )
                    case claude_schema.StreamThinkingBlock(
                        thinking=thinking, signature=signature
                    ):
                        if not thinking:
                            continue
                        state.note_seq += 1
                        action_id = f"claude.thinking.{state.note_seq}"
                        detail: dict[str, Any] = {}
                        if parent_tool_use_id:
                            detail["parent_tool_use_id"] = parent_tool_use_id
                        if signature:
                            detail["signature"] = signature
                        out.append(
                            factory.action_completed(
                                action_id=action_id,
                                kind="note",
                                title=thinking,
                                ok=True,
                                detail=detail,
                            )
                        )
                    case claude_schema.StreamTextBlock(text=text):
                        if text:
                            state.last_assistant_text = text
                    case _:
                        continue
            return out
        case claude_schema.StreamUserMessage(message=message):
            if not isinstance(message.content, list):
                return []
            out: list[UntetherEvent] = []
            for content in message.content:
                if not isinstance(content, claude_schema.StreamToolResultBlock):
                    continue
                tool_use_id = content.tool_use_id
                action = state.pending_actions.pop(tool_use_id, None)
                if action is None:
                    action = Action(
                        id=tool_use_id,
                        kind="tool",
                        title="tool result",
                        detail={},
                    )
                out.append(
                    _tool_result_event(
                        content,
                        action=action,
                        factory=factory,
                    )
                )
                # Complete any associated control action (e.g. permission approval)
                control_action_id = state.control_action_for_tool.pop(tool_use_id, None)
                if control_action_id:
                    out.append(
                        factory.action_completed(
                            action_id=control_action_id,
                            kind="warning",
                            title="Permission resolved",
                            ok=True,
                        )
                    )
            return out
        case claude_schema.StreamResultMessage():
            ok = not event.is_error
            result_text = event.result or ""
            if ok and not result_text and state.last_assistant_text:
                result_text = state.last_assistant_text

            resume = ResumeToken(engine=ENGINE, value=event.session_id)
            error = None if ok else _extract_error(event, resumed=state.resumed)
            usage = _usage_payload(event)

            return [
                factory.completed(
                    ok=ok,
                    answer=result_text,
                    resume=resume,
                    error=error,
                    usage=usage or None,
                )
            ]
        case claude_schema.StreamControlRequest(request_id=request_id, request=request):
            # Auto-approve non-user-facing control requests
            _AUTO_APPROVE_TYPES = (
                claude_schema.ControlInitializeRequest,
                claude_schema.ControlHookCallbackRequest,
                claude_schema.ControlMcpMessageRequest,
                claude_schema.ControlRewindFilesRequest,
                claude_schema.ControlInterruptRequest,
            )
            if isinstance(request, _AUTO_APPROVE_TYPES):
                request_type = (
                    type(request).__name__.replace("Control", "").replace("Request", "")
                )
                logger.debug(
                    "control_request.auto_approve",
                    request_id=request_id,
                    request_type=request_type,
                )
                state.auto_approve_queue.append(request_id)
                return []

            # Auto-approve tool requests that don't need user interaction
            _TOOLS_REQUIRING_APPROVAL = {"ExitPlanMode", "AskUserQuestion"}
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "unknown")
                if tool_name not in _TOOLS_REQUIRING_APPROVAL:
                    logger.debug(
                        "control_request.auto_approve_tool",
                        request_id=request_id,
                        tool_name=tool_name,
                    )
                    state.auto_approve_queue.append(request_id)
                    return []

            # Auto-approve ExitPlanMode in "auto" permission mode
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and state.auto_approve_exit_plan_mode:
                    logger.debug(
                        "control_request.auto_approve_exit_plan_mode",
                        request_id=request_id,
                    )
                    state.auto_approve_queue.append(request_id)
                    return []

            # Auto-approve ExitPlanMode after user approved via post-outline buttons
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and factory.resume:
                    session_id = factory.resume.value
                    if session_id in _DISCUSS_APPROVED:
                        _DISCUSS_APPROVED.discard(session_id)
                        clear_discuss_cooldown(session_id)
                        logger.info(
                            "control_request.discuss_approved",
                            request_id=request_id,
                            session_id=session_id,
                        )
                        state.auto_approve_queue.append(request_id)
                        return []

            # Rate-limit ExitPlanMode after a discuss denial
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode" and factory.resume:
                    escalation_msg = check_discuss_cooldown(factory.resume.value)
                    if escalation_msg is not None:
                        session_id = factory.resume.value
                        logger.info(
                            "control_request.discuss_cooldown_deny",
                            request_id=request_id,
                            session_id=session_id,
                        )
                        _REQUEST_TO_INPUT.pop(request_id, None)
                        state.auto_deny_queue.append((request_id, escalation_msg))

                        # Show Approve/Deny buttons so user can approve the plan
                        # without typing — synthetic control request for the UI.
                        # Prefix "da:" = discuss-approve (short to fit 64-byte
                        # callback_data limit: "claude_control:approve:da:UUID"
                        # = 26 + 36 = 62 chars).
                        state.note_seq += 1
                        synth_action_id = f"claude.discuss_approve.{state.note_seq}"
                        synth_request_id = f"da:{session_id}"
                        _REQUEST_TO_SESSION[synth_request_id] = session_id
                        return [
                            state.factory.action_started(
                                action_id=synth_action_id,
                                kind="warning",
                                title="Plan outlined — approve to proceed",
                                detail={
                                    "request_id": synth_request_id,
                                    "request_type": "DiscussApproval",
                                    "inline_keyboard": {
                                        "buttons": [
                                            [
                                                {
                                                    "text": "Approve Plan",
                                                    "callback_data": f"claude_control:approve:{synth_request_id}",
                                                },
                                                {
                                                    "text": "Deny",
                                                    "callback_data": f"claude_control:deny:{synth_request_id}",
                                                },
                                            ],
                                        ]
                                    },
                                },
                            ),
                        ]

            # Phase 2: Interactive control request with inline keyboard
            request_type = (
                type(request).__name__.replace("Control", "").replace("Request", "")
            )

            # Extract details based on request type
            details = ""
            diff_preview = ""
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "unknown")
                tool_input = getattr(request, "input", {})
                details = f"tool: {tool_name}"
                # Include key input parameters if available
                if tool_input:
                    key_params = []
                    for key in ["file_path", "path", "command", "pattern"]:
                        if key in tool_input:
                            value = str(tool_input[key])
                            if len(value) > 50:
                                value = value[:47] + "..."
                            key_params.append(f"{key}={value}")
                    if key_params:
                        details += f" ({', '.join(key_params)})"
                # CC4: Diff preview for Edit/Write tools
                diff_preview = _format_diff_preview(tool_name, tool_input)
            elif isinstance(request, claude_schema.ControlSetPermissionModeRequest):
                mode = getattr(request, "mode", "unknown")
                details = f"mode: {mode}"
            elif isinstance(request, claude_schema.ControlHookCallbackRequest):
                callback_id = getattr(request, "callback_id", "unknown")
                details = f"callback: {callback_id}"

            warning_text = f"Permission Request [{request_type}]"
            if details:
                warning_text += f" - {details}"
            if diff_preview:
                warning_text += f"\n{diff_preview}"

            # Store in pending requests with timestamp
            state.pending_control_requests[request_id] = (event, time.time())

            # Phase 2: Register request_id -> session_id mapping for callback routing
            if factory.resume:
                session_id = factory.resume.value
                _REQUEST_TO_SESSION[request_id] = session_id
                # Store original tool input for updatedInput in response
                if isinstance(request, claude_schema.ControlCanUseToolRequest):
                    _REQUEST_TO_INPUT[request_id] = getattr(request, "input", {})
                logger.debug(
                    "control_request.registered",
                    request_id=request_id,
                    session_id=session_id,
                )

            # Clean up expired requests (older than timeout)
            current_time = time.time()
            expired = [
                rid
                for rid, (_, timestamp) in state.pending_control_requests.items()
                if current_time - timestamp > 300.0  # 5 minutes
            ]
            for rid in expired:
                del state.pending_control_requests[rid]
                _REQUEST_TO_INPUT.pop(rid, None)
                logger.warning("control_request.expired", request_id=rid)

            # Check max pending limit
            if len(state.pending_control_requests) > 100:
                logger.warning(
                    "control_request.max_pending",
                    count=len(state.pending_control_requests),
                )

            state.note_seq += 1
            action_id = f"claude.control.{state.note_seq}"

            # Map the preceding tool_use_id to this control action for cleanup
            if state.last_tool_use_id:
                state.control_action_for_tool[state.last_tool_use_id] = action_id

            # Include inline keyboard data in detail
            button_rows: list[list[dict[str, str]]] = [
                [
                    {
                        "text": "Approve",
                        "callback_data": f"claude_control:approve:{request_id}",
                    },
                    {
                        "text": "Deny",
                        "callback_data": f"claude_control:deny:{request_id}",
                    },
                ],
            ]
            # ExitPlanMode gets an extra "Outline Plan" button
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "ExitPlanMode":
                    button_rows.append(
                        [
                            {
                                "text": "Pause & Outline Plan",
                                "callback_data": f"claude_control:discuss:{request_id}",
                            },
                        ]
                    )

            # A1: AskUserQuestion — extract the question for display
            ask_question: str | None = None
            if isinstance(request, claude_schema.ControlCanUseToolRequest):
                tool_name = getattr(request, "tool_name", "")
                if tool_name == "AskUserQuestion":
                    ask_question = ""
                    if tool_input:
                        # Direct "question" key
                        ask_question = tool_input.get("question", "")
                        # Nested "questions" array format
                        if not ask_question:
                            questions = tool_input.get("questions", [])
                            if questions and isinstance(questions, list):
                                ask_question = (
                                    questions[0].get("question", "")
                                    if isinstance(questions[0], dict)
                                    else ""
                                )
                    if ask_question:
                        warning_text = f"❓ {ask_question}"
                    # Register this request for reply handling
                    _PENDING_ASK_REQUESTS[request_id] = ask_question or ""

            detail: dict[str, Any] = {
                "request_id": request_id,
                "request_type": request_type,
                "inline_keyboard": {
                    "buttons": button_rows,
                },
            }
            if ask_question:
                detail["ask_question"] = ask_question

            return [
                factory.action_started(
                    action_id=action_id,
                    kind="warning",  # Use warning kind for visibility
                    title=warning_text,
                    detail=detail,
                )
            ]
        case _:
            return []


@dataclass(slots=True)
class ClaudeRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE

    claude_cmd: str = "claude"
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    dangerously_skip_permissions: bool = False
    use_api_billing: bool = False
    session_title: str = "claude"
    logger = logger

    # Phase 2: Control channel support
    supports_control_channel: bool = True
    _pty_master_fd: int | None = None  # legacy PTY approach (non-permission mode)
    _proc_stdin: Any | None = None  # PIPE stdin for control channel (permission mode)
    _control_timeout_seconds: float = 300.0  # 5 minutes
    _max_pending_control_requests: int = 100

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`claude --resume {token.value}`"

    def _effective_permission_mode(self) -> str | None:
        """Resolve effective permission mode from per-chat override or engine config."""
        run_options = get_run_options()
        return (
            run_options.permission_mode if run_options else None
        ) or self.permission_mode

    async def write_control_response(
        self, request_id: str, approved: bool, *, deny_message: str | None = None
    ) -> None:
        """Write a control response to the Claude Code process via PIPE or PTY.

        Uses _SESSION_STDIN to find the correct stdin for the session,
        supporting concurrent sessions on the same runner instance.
        """
        if approved:
            inner: dict[str, Any] = {"behavior": "allow"}
            # Claude Code CLI requires updatedInput for can_use_tool responses
            if request_id in _REQUEST_TO_INPUT:
                inner["updatedInput"] = _REQUEST_TO_INPUT.pop(request_id)
        else:
            inner = {"behavior": "deny", "message": deny_message or "User denied"}
            # Clean up stored input on denial too
            _REQUEST_TO_INPUT.pop(request_id, None)
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": inner,
            },
        }

        jsonl_line = json.dumps(response) + "\n"

        # Look up the session-specific stdin from _SESSION_STDIN
        session_id = _REQUEST_TO_SESSION.get(request_id)
        session_stdin = _SESSION_STDIN.get(session_id) if session_id else None

        # Prefer session-specific stdin, fall back to instance stdin, then PTY
        stdin_to_use = session_stdin or self._proc_stdin
        if stdin_to_use is not None:
            try:
                await stdin_to_use.send(jsonl_line.encode())
                logger.info(
                    "control_response.sent",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    channel="pipe",
                )
            except (OSError, anyio.ClosedResourceError) as e:
                logger.error(
                    "control_response.failed",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pipe",
                )
        elif self._pty_master_fd is not None:
            try:
                os.write(self._pty_master_fd, jsonl_line.encode())
                logger.info(
                    "control_response.sent",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    channel="pty",
                )
            except OSError as e:
                logger.error(
                    "control_response.failed",
                    request_id=request_id,
                    approved=approved,
                    session_id=session_id,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pty",
                )
        else:
            logger.warning(
                "control_response.no_channel",
                request_id=request_id,
                approved=approved,
                session_id=session_id,
            )

    def _build_args(self, prompt: str, resume: ResumeToken | None) -> list[str]:
        run_options = get_run_options()
        effective_mode = self._effective_permission_mode()

        # When using permission mode with control channel, don't use -p mode.
        # The SDK-style streaming protocol requires bidirectional stdin/stdout
        # without -p. The prompt is sent as a JSON user message on stdin.
        if effective_mode is not None:
            args: list[str] = [
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
            ]
        else:
            args = [
                "-p",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
            ]

        if resume is not None:
            args.extend(["--resume", resume.value])
        model = self.model
        if run_options is not None and run_options.model:
            model = run_options.model
        if model is not None:
            args.extend(["--model", str(model)])
        allowed_tools = _coerce_comma_list(self.allowed_tools)
        if allowed_tools is not None:
            args.extend(["--allowedTools", allowed_tools])
        if self.dangerously_skip_permissions is True:
            args.append("--dangerously-skip-permissions")

        if effective_mode is not None:
            cli_mode = "plan" if effective_mode == "auto" else effective_mode
            args.extend(["--permission-mode", cli_mode])
            args.extend(["--permission-prompt-tool", "stdio"])
            # Prompt sent via stdin as JSON, not as CLI arg
        else:
            args.append("--")
            args.append(prompt)

        return args

    def command(self) -> str:
        return self.claude_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        return self._build_args(prompt, resume)

    def stdin_payload(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> bytes | None:
        effective_mode = self._effective_permission_mode()
        if effective_mode is not None:
            # SDK-style control channel: send init handshake + user message.
            # The CLI reads both from stdin (no -p mode).
            init_request = {
                "type": "control_request",
                "request_id": f"init_{id(self)}",
                "request": {"subtype": "initialize", "hooks": None},
            }
            user_message = {
                "type": "user",
                "session_id": resume.value if resume else "",
                "message": {
                    "role": "user",
                    "content": prompt,
                },
                "parent_tool_use_id": None,
            }
            payload = json.dumps(init_request) + "\n" + json.dumps(user_message) + "\n"
            return payload.encode()
        return None

    def env(self, *, state: Any) -> dict[str, str] | None:
        if self.use_api_billing is not True:
            env = dict(os.environ)
            env.pop("ANTHROPIC_API_KEY", None)
            return env
        return None

    def new_state(self, prompt: str, resume: ResumeToken | None) -> ClaudeStreamState:
        state = ClaudeStreamState()
        state.auto_approve_exit_plan_mode = self._effective_permission_mode() == "auto"
        state.resumed = resume is not None
        return state

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: ClaudeStreamState,
    ) -> None:
        # Phase 2: Register this runner for control responses
        if resume is not None and self.supports_control_channel:
            _ACTIVE_RUNNERS[resume.value] = (self, time.time())
            logger.debug(
                "claude_runner.registered",
                session_id=resume.value,
            )

    def decode_jsonl(
        self,
        *,
        line: bytes,
    ) -> claude_schema.StreamJsonMessage:
        return claude_schema.decode_stream_json_line(line)

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: ClaudeStreamState,
    ) -> list[UntetherEvent]:
        if isinstance(error, msgspec.DecodeError):
            self.get_logger().warning(
                "jsonl.msgspec.invalid",
                tag=self.tag(),
                error=str(error),
                error_type=error.__class__.__name__,
            )
            return []
        return super().decode_error_events(
            raw=raw,
            line=line,
            error=error,
            state=state,
        )

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: ClaudeStreamState,
    ) -> list[UntetherEvent]:
        return []

    async def _iter_jsonl_events(
        self,
        *,
        stdout: Any,
        stream: JsonlStreamState,
        state: ClaudeStreamState,
        resume: ResumeToken | None,
        logger: Any,
        pid: int,
        session_stdin: Any = None,
    ) -> anyio.abc.AsyncIterator[UntetherEvent]:
        """Override to drain auto-approve queue after every line, not just after yielded events.

        The base class only drains auto-approves in run_impl after `yield evt`.
        If a line produces no events (e.g. auto-approved control requests), the drain
        never runs, causing a deadlock when Claude Code blocks waiting for the response.

        session_stdin is passed from run_impl to avoid using self._proc_stdin
        which may be overwritten by a concurrent session on the same runner.
        """
        registered_session_id: str | None = None
        async for raw_line in self.iter_json_lines(stdout):
            for evt in self._handle_jsonl_line(
                raw_line=raw_line,
                stream=stream,
                state=state,
                resume=resume,
                logger=logger,
                pid=pid,
            ):
                # Register _SESSION_STDIN here (not in translate) because we
                # have the correct captured stdin.  translate() would use the
                # stale self._proc_stdin which may have been overwritten by a
                # concurrent session on the same runner.
                if (
                    not registered_session_id
                    and isinstance(evt, StartedEvent)
                    and evt.resume
                ):
                    registered_session_id = evt.resume.value
                    _SESSION_STDIN[registered_session_id] = session_stdin
                    logger.debug(
                        "session_stdin.registered",
                        session_id=registered_session_id,
                        pid=pid,
                    )
                yield evt
            # Drain auto-approve and auto-deny queues after EVERY line, even if no events
            # were yielded.  This prevents deadlock when auto-handled requests produce no events.
            await self._drain_auto_approve(state, stdin=session_stdin)
            await self._drain_auto_deny(state, stdin=session_stdin)
            # After CompletedEvent, stop reading stdout immediately.
            # Claude Code's MCP server child processes may inherit the stdout pipe FD,
            # keeping it open even after Claude Code exits. Without this break,
            # we'd block forever waiting for EOF that never comes.
            if stream.did_emit_completed:
                break

    async def _drain_auto_approve(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """Drain the auto-approve queue, writing responses to the control channel."""
        if not state.auto_approve_queue:
            return

        # Use provided stdin (session-specific) or fall back to instance
        pipe = stdin or self._proc_stdin
        for req_id in state.auto_approve_queue:
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": req_id,
                    "response": {"behavior": "allow"},
                },
            }
            payload = (json.dumps(response) + "\n").encode()
            try:
                if pipe is not None:
                    await pipe.send(payload)
                    logger.info(
                        "control_response.auto_approved",
                        request_id=req_id,
                        channel="pipe",
                    )
                elif self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, payload)
                    logger.info(
                        "control_response.auto_approved",
                        request_id=req_id,
                        channel="pty",
                    )
                else:
                    logger.error(
                        "control_response.auto_approve_failed", request_id=req_id
                    )
            except (OSError, anyio.ClosedResourceError) as e:
                logger.error(
                    "control_response.auto_approve_failed",
                    request_id=req_id,
                    error=str(e),
                )
        state.auto_approve_queue.clear()

    async def _drain_auto_deny(
        self, state: ClaudeStreamState, *, stdin: Any = None
    ) -> None:
        """Drain the auto-deny queue, writing deny responses to the control channel."""
        if not state.auto_deny_queue:
            return

        pipe = stdin or self._proc_stdin
        for req_id, message in state.auto_deny_queue:
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": req_id,
                    "response": {"behavior": "deny", "message": message},
                },
            }
            payload = (json.dumps(response) + "\n").encode()
            try:
                if pipe is not None:
                    await pipe.send(payload)
                    logger.info(
                        "control_response.auto_denied",
                        request_id=req_id,
                        channel="pipe",
                    )
                elif self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, payload)
                    logger.info(
                        "control_response.auto_denied", request_id=req_id, channel="pty"
                    )
                else:
                    logger.error("control_response.auto_deny_failed", request_id=req_id)
            except (OSError, anyio.ClosedResourceError) as e:
                logger.error(
                    "control_response.auto_deny_failed",
                    request_id=req_id,
                    error=str(e),
                )
        state.auto_deny_queue.clear()

    def translate(
        self,
        data: claude_schema.StreamJsonMessage,
        *,
        state: ClaudeStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        events = translate_claude_event(
            data,
            title=self.session_title,
            state=state,
            factory=state.factory,
        )

        # Phase 2: Register runner when we get a session_id
        # NOTE: _SESSION_STDIN is registered in _iter_jsonl_events (not here)
        # because self._proc_stdin may be stale if another session has started
        # concurrently on the same runner instance.
        if self.supports_control_channel:
            for evt in events:
                if isinstance(evt, StartedEvent) and evt.resume:
                    session_id = evt.resume.value
                    _ACTIVE_RUNNERS[session_id] = (self, time.time())
                    logger.debug(
                        "claude_runner.registered",
                        session_id=session_id,
                    )

        # Auto-approve queue is drained asynchronously in run_impl
        # after events are yielded (see _drain_auto_approve)

        return events

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: ClaudeStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        # Phase 2: Cleanup runner registration on error
        session_id = (
            found_session.value if found_session else (resume.value if resume else None)
        )
        if session_id:
            _ACTIVE_RUNNERS.pop(session_id, None)
            _SESSION_STDIN.pop(session_id, None)
            clear_discuss_cooldown(session_id)
            _DISCUSS_APPROVED.discard(session_id)
            logger.debug("claude_runner.unregistered", session_id=session_id)

        parts = [f"claude failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        resume_for_completed = found_session or resume
        return [
            self.note_event(message, state=state, ok=False),
            state.factory.completed_error(
                error=message,
                resume=resume_for_completed,
            ),
        ]

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: ClaudeStreamState,
    ) -> list[UntetherEvent]:
        # Phase 2: Cleanup runner registration
        session_id = (
            found_session.value if found_session else (resume.value if resume else None)
        )
        if session_id:
            _ACTIVE_RUNNERS.pop(session_id, None)
            _SESSION_STDIN.pop(session_id, None)
            clear_discuss_cooldown(session_id)
            _DISCUSS_APPROVED.discard(session_id)
            logger.debug("claude_runner.unregistered", session_id=session_id)

        if not found_session:
            parts = ["claude finished but no session_id was captured"]
            session = _session_label(None, resume)
            if session:
                parts.append(f"session: {session}")
            message = "\n".join(parts)
            resume_for_completed = resume
            return [
                state.factory.completed_error(
                    error=message,
                    resume=resume_for_completed,
                )
            ]

        parts = ["claude finished without a result event"]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        message = "\n".join(parts)
        return [
            state.factory.completed_error(
                error=message,
                answer=state.last_assistant_text or "",
                resume=found_session,
            )
        ]

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> anyio.abc.AsyncIterator[UntetherEvent]:
        """
        Override run_impl to support two modes:

        1. Permission mode (SDK-style): No -p flag. Stdin stays open for
           bidirectional control protocol. Init handshake + user message
           sent on stdin; control_request/response flow over stdin/stdout.

        2. Legacy mode: -p flag with PTY stdin. Prompt passed as CLI arg.
           Stdin used only for initial payload, then kept open via PTY.
        """
        state = self.new_state(prompt, resume)
        self.start_run(prompt, resume, state=state)

        tag = self.tag()
        run_logger = self.get_logger()
        cmd = [self.command(), *self.build_args(prompt, resume, state=state)]
        payload = self.stdin_payload(prompt, resume, state=state)
        env = self.env(state=state)
        run_logger.info(
            "runner.start",
            engine=self.engine,
            resume=resume.value if resume else None,
            prompt=prompt,
            prompt_len=len(prompt),
        )

        cwd = get_run_base_dir()
        effective_mode = self._effective_permission_mode()
        use_control_channel = effective_mode is not None

        # PTY setup only for legacy (non-permission) mode
        pty_master_fd: int | None = None
        pty_slave_fd: int | None = None
        this_proc_stdin: Any = None

        try:
            if use_control_channel:
                # SDK-style: use PIPE stdin, keep it open for control responses
                stdin_arg = subprocess_module.PIPE
            elif self.supports_control_channel and os.name == "posix":
                # Legacy: use PTY for stdin
                pty_master_fd, pty_slave_fd = pty.openpty()
                with contextlib.suppress(OSError):
                    tty.setraw(pty_master_fd)
                self._pty_master_fd = pty_master_fd
                stdin_arg = pty_slave_fd
            else:
                stdin_arg = subprocess_module.PIPE

            async with manage_subprocess(
                cmd,
                stdin=stdin_arg,
                stdout=subprocess_module.PIPE,
                stderr=subprocess_module.PIPE,
                env=env,
                cwd=cwd,
            ) as proc:
                # Close slave fd in parent after subprocess starts (PTY mode)
                if pty_slave_fd is not None:
                    os.close(pty_slave_fd)
                    pty_slave_fd = None

                if proc.stdout is None or proc.stderr is None:
                    raise RuntimeError(self.pipes_error_message())

                run_logger.info(
                    "subprocess.spawn",
                    cmd=cmd[0] if cmd else None,
                    args=cmd[1:],
                    pid=proc.pid,
                    use_control_channel=use_control_channel,
                )

                if use_control_channel and proc.stdin is not None:
                    # SDK-style: send payload but keep stdin open
                    if payload is not None:
                        await proc.stdin.send(payload)
                        run_logger.info(
                            "subprocess.stdin.payload_sent",
                            pid=proc.pid,
                            payload_len=len(payload),
                        )
                    # Store stdin for writing control responses later.
                    # Keep a local copy too - self._proc_stdin may be
                    # overwritten by a concurrent session on the same runner.
                    self._proc_stdin = proc.stdin
                    this_proc_stdin = proc.stdin
                elif payload is not None and self._pty_master_fd is not None:
                    # Legacy PTY: write to master
                    os.write(self._pty_master_fd, payload)
                    run_logger.info(
                        "subprocess.pty.payload_sent",
                        pid=proc.pid,
                        payload_len=len(payload),
                    )
                elif payload is not None and proc.stdin is not None:
                    # Legacy PIPE fallback: send and close
                    await proc.stdin.send(payload)
                    await proc.stdin.aclose()

                rc: int | None = None
                stream = JsonlStreamState(expected_session=resume)
                stderr_lines: list[str] = []

                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        drain_stderr,
                        proc.stderr,
                        run_logger,
                        tag,
                        stderr_lines,
                    )
                    async for evt in self._iter_jsonl_events(
                        stdout=proc.stdout,
                        stream=stream,
                        state=state,
                        resume=resume,
                        logger=run_logger,
                        pid=proc.pid,
                        session_stdin=this_proc_stdin if use_control_channel else None,
                    ):
                        yield evt

                    # Close stdin after all events to let CLI exit.
                    # Use this_proc_stdin (local) not self._proc_stdin (may
                    # have been overwritten by a concurrent session).
                    if use_control_channel and this_proc_stdin is not None:
                        with contextlib.suppress(Exception):
                            await this_proc_stdin.aclose()

                    rc = await proc.wait()

                run_logger.info("subprocess.exit", pid=proc.pid, rc=rc)
                if stream.did_emit_completed:
                    return
                found_session = stream.found_session
                if rc is not None and rc != 0:
                    events = self.process_error_events(
                        rc,
                        resume=resume,
                        found_session=found_session,
                        state=state,
                        stderr_lines=stderr_lines or None,
                    )
                    for evt in events:
                        if isinstance(evt, CompletedEvent):
                            self._log_completed_event(
                                logger=run_logger,
                                pid=proc.pid,
                                event=evt,
                                source="process_error",
                            )
                        yield evt
                    return

                events = self.stream_end_events(
                    resume=resume,
                    found_session=found_session,
                    state=state,
                )
                for evt in events:
                    if isinstance(evt, CompletedEvent):
                        self._log_completed_event(
                            logger=run_logger,
                            pid=proc.pid,
                            event=evt,
                            source="stream_end",
                        )
                    yield evt

        finally:
            # Cleanup - close the local stdin if it wasn't already closed
            if this_proc_stdin is not None:
                with contextlib.suppress(Exception):
                    await this_proc_stdin.aclose()
            if pty_slave_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(pty_slave_fd)
            if pty_master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(pty_master_fd)
            self._pty_master_fd = None


def build_runner(config: EngineConfig, _config_path: Path) -> Runner:
    claude_cmd = shutil.which("claude") or "claude"

    model = config.get("model")
    if "allowed_tools" in config:
        allowed_tools = config.get("allowed_tools")
    else:
        allowed_tools = DEFAULT_ALLOWED_TOOLS
    dangerously_skip_permissions = config.get("dangerously_skip_permissions") is True
    use_api_billing = config.get("use_api_billing") is True
    permission_mode = config.get("permission_mode")
    title = str(model) if model is not None else "claude"

    return ClaudeRunner(
        claude_cmd=claude_cmd,
        model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        dangerously_skip_permissions=dangerously_skip_permissions,
        use_api_billing=use_api_billing,
        session_title=title,
    )


BACKEND = EngineBackend(
    id="claude",
    build_runner=build_runner,
    install_cmd="npm install -g @anthropic-ai/claude-code",
)


# Phase 2: Public API for sending control responses
async def send_claude_control_response(
    request_id: str, approved: bool, *, deny_message: str | None = None
) -> bool:
    """Send a control response to an active Claude Code session.

    Args:
        request_id: The control request ID
        approved: Whether to approve (True) or deny (False) the request
        deny_message: Custom denial message (used when approved=False)

    Returns:
        True if the response was sent successfully, False if the request is not found
    """
    # Look up session_id from request_id
    if request_id not in _REQUEST_TO_SESSION:
        # Duplicate callback (Telegram long-polling can deliver the same update twice)
        if request_id in _HANDLED_REQUESTS:
            logger.debug("control_response.duplicate", request_id=request_id)
            return True
        logger.warning(
            "control_response.request_not_found",
            request_id=request_id,
        )
        return False

    session_id = _REQUEST_TO_SESSION[request_id]

    if session_id not in _ACTIVE_RUNNERS:
        logger.warning(
            "control_response.no_active_session",
            session_id=session_id,
            request_id=request_id,
        )
        # Clean up stale mappings
        del _REQUEST_TO_SESSION[request_id]
        _REQUEST_TO_INPUT.pop(request_id, None)
        return False

    runner, _ = _ACTIVE_RUNNERS[session_id]
    await runner.write_control_response(request_id, approved, deny_message=deny_message)

    # Clean up the mapping after use
    del _REQUEST_TO_SESSION[request_id]
    _HANDLED_REQUESTS.add(request_id)

    # Cap the set size to prevent unbounded growth
    if len(_HANDLED_REQUESTS) > 100:
        _HANDLED_REQUESTS.clear()

    return True


def _cooldown_seconds(count: int) -> float:
    """Progressive cooldown: 30s, 60s, 90s, 120s (capped)."""
    return min(DISCUSS_COOLDOWN_BASE_SECONDS * count, DISCUSS_COOLDOWN_MAX_SECONDS)


def set_discuss_cooldown(session_id: str) -> None:
    """Record that a discuss denial was sent for this session.

    Called by claude_control when the user clicks 'Pause & Outline Plan'.
    Subsequent ExitPlanMode requests within the cooldown window will
    be auto-denied with an escalating message. The cooldown window
    grows with each click: 30s, 60s, 90s, 120s (capped).
    """
    existing = _DISCUSS_COOLDOWN.get(session_id)
    count = (existing[1] + 1) if existing else 1
    _DISCUSS_COOLDOWN[session_id] = (time.time(), count)
    cooldown = _cooldown_seconds(count)
    logger.info(
        "discuss_cooldown.set",
        session_id=session_id,
        deny_count=count,
        cooldown_seconds=cooldown,
    )


def check_discuss_cooldown(session_id: str) -> str | None:
    """Check if an ExitPlanMode request should be auto-denied due to discuss cooldown.

    Returns an escalation deny message (with cooldown duration) if within
    cooldown, or None if clear. Uses progressive timing based on deny count.
    """
    entry = _DISCUSS_COOLDOWN.get(session_id)
    if entry is None:
        return None
    ts, count = entry
    cooldown = _cooldown_seconds(count)
    if time.time() - ts > cooldown:
        # Cooldown expired — keep the count so next click escalates further
        # Only clear the timestamp so the next ExitPlanMode gets through
        # but set_discuss_cooldown will use count+1 for the next window
        _DISCUSS_COOLDOWN[session_id] = (0.0, count)
        return None
    return _DISCUSS_ESCALATION_MESSAGE


def clear_discuss_cooldown(session_id: str) -> None:
    """Clear the discuss cooldown for a session (e.g. on approve/deny)."""
    _DISCUSS_COOLDOWN.pop(session_id, None)


def get_pending_ask_request() -> tuple[str, str] | None:
    """Return the oldest pending AskUserQuestion (request_id, question) or None."""
    if not _PENDING_ASK_REQUESTS:
        return None
    request_id = next(iter(_PENDING_ASK_REQUESTS))
    return request_id, _PENDING_ASK_REQUESTS[request_id]


async def answer_ask_question(request_id: str, answer: str) -> bool:
    """Answer a pending AskUserQuestion by denying with the user's response.

    The deny message contains the user's answer so Claude reads it and
    continues with that information.
    """
    _PENDING_ASK_REQUESTS.pop(request_id, None)
    deny_message = (
        f"The user answered your question via Telegram:\n\n"
        f'"{answer}"\n\n'
        f"Use this answer and continue. Do not call AskUserQuestion again "
        f"for this same question."
    )
    return await send_claude_control_response(
        request_id, approved=False, deny_message=deny_message
    )


def get_active_claude_sessions() -> list[str]:
    """Get list of active Claude Code session IDs."""
    return list(_ACTIVE_RUNNERS.keys())


def cleanup_expired_sessions(max_age_seconds: float = 3600.0) -> int:
    """Clean up stale session registrations.

    Args:
        max_age_seconds: Maximum age of a session before cleanup (default: 1 hour)

    Returns:
        Number of sessions cleaned up
    """
    current_time = time.time()
    expired = [
        session_id
        for session_id, (_, timestamp) in _ACTIVE_RUNNERS.items()
        if current_time - timestamp > max_age_seconds
    ]
    for session_id in expired:
        del _ACTIVE_RUNNERS[session_id]
        logger.info("claude_runner.expired_cleanup", session_id=session_id)
    return len(expired)
