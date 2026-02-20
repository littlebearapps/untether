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
from ..model import Action, ActionKind, EngineId, ResumeToken, StartedEvent, TakopiEvent, CompletedEvent
from ..runner import JsonlSubprocessRunner, ResumeTokenMixin, Runner, JsonlStreamState
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

# Phase 2: Global registry mapping request_id -> session_id
# This allows callbacks to find the right runner instance
_REQUEST_TO_SESSION: dict[str, str] = {}


@dataclass(slots=True)
class ClaudeStreamState:
    factory: EventFactory = field(default_factory=lambda: EventFactory(ENGINE))
    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_assistant_text: str | None = None
    note_seq: int = 0
    # Phase 2: Control request tracking
    pending_control_requests: dict[str, tuple[claude_schema.StreamControlRequest, float]] = field(default_factory=dict)
    # Auto-approve queue: request IDs that should be approved without user interaction
    auto_approve_queue: list[str] = field(default_factory=list)
    # Whether the control channel initialization handshake has been sent
    control_init_sent: bool = False


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
) -> TakopiEvent:
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


def _extract_error(event: claude_schema.StreamResultMessage) -> str | None:
    if event.is_error:
        if isinstance(event.result, str) and event.result:
            return event.result
        subtype = event.subtype
        if subtype:
            return f"claude run failed ({subtype})"
        return "claude run failed"
    return None


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
) -> list[TakopiEvent]:
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
            out: list[TakopiEvent] = []
            for content in message.content:
                match content:
                    case claude_schema.StreamToolUseBlock():
                        action = _tool_action(
                            content,
                            parent_tool_use_id=parent_tool_use_id,
                        )
                        state.pending_actions[action.id] = action
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
            out: list[TakopiEvent] = []
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
            return out
        case claude_schema.StreamResultMessage():
            ok = not event.is_error
            result_text = event.result or ""
            if ok and not result_text and state.last_assistant_text:
                result_text = state.last_assistant_text

            resume = ResumeToken(engine=ENGINE, value=event.session_id)
            error = None if ok else _extract_error(event)
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
                request_type = type(request).__name__.replace("Control", "").replace("Request", "")
                logger.debug(
                    "control_request.auto_approve",
                    request_id=request_id,
                    request_type=request_type,
                )
                state.auto_approve_queue.append(request_id)
                return []

            # Phase 2: Interactive control request with inline keyboard
            request_type = type(request).__name__.replace("Control", "").replace("Request", "")

            # Extract details based on request type
            details = ""
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
            elif isinstance(request, claude_schema.ControlSetPermissionModeRequest):
                mode = getattr(request, "mode", "unknown")
                details = f"mode: {mode}"
            elif isinstance(request, claude_schema.ControlHookCallbackRequest):
                callback_id = getattr(request, "callback_id", "unknown")
                details = f"callback: {callback_id}"

            warning_text = f"Permission Request [{request_type}]"
            if details:
                warning_text += f" - {details}"

            # Store in pending requests with timestamp
            state.pending_control_requests[request_id] = (event, time.time())

            # Phase 2: Register request_id -> session_id mapping for callback routing
            if factory.resume:
                session_id = factory.resume.value
                _REQUEST_TO_SESSION[request_id] = session_id
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
                logger.warning("control_request.expired", request_id=rid)

            # Check max pending limit
            if len(state.pending_control_requests) > 100:
                logger.warning(
                    "control_request.max_pending",
                    count=len(state.pending_control_requests),
                )

            state.note_seq += 1
            action_id = f"claude.control.{state.note_seq}"

            # Include inline keyboard data in detail
            detail: dict[str, Any] = {
                "request_id": request_id,
                "request_type": request_type,
                "inline_keyboard": {
                    "buttons": [
                        [
                            {
                                "text": "Approve",
                                "callback_data": f"claude_control:approve:{request_id}",
                            },
                            {
                                "text": "Deny",
                                "callback_data": f"claude_control:deny:{request_id}",
                            },
                        ]
                    ]
                },
            }

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
            (run_options.permission_mode if run_options else None)
            or self.permission_mode
        )

    async def write_control_response(
        self, request_id: str, approved: bool
    ) -> None:
        """Write a control response to the Claude Code process via PIPE or PTY."""
        if approved:
            inner = {"behavior": "allow"}
        else:
            inner = {"behavior": "deny", "message": "User denied"}
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": inner,
            },
        }

        jsonl_line = json.dumps(response) + "\n"

        # Prefer PIPE stdin (permission mode), fall back to PTY master
        if self._proc_stdin is not None:
            try:
                await self._proc_stdin.send(jsonl_line.encode())
                logger.info(
                    "control_response.sent",
                    request_id=request_id,
                    approved=approved,
                    channel="pipe",
                )
            except Exception as e:
                logger.error(
                    "control_response.failed",
                    request_id=request_id,
                    approved=approved,
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
                    channel="pty",
                )
            except OSError as e:
                logger.error(
                    "control_response.failed",
                    request_id=request_id,
                    approved=approved,
                    error=str(e),
                    error_type=e.__class__.__name__,
                    channel="pty",
                )
        else:
            logger.warning(
                "control_response.no_channel",
                request_id=request_id,
                approved=approved,
            )

    def _build_args(self, prompt: str, resume: ResumeToken | None) -> list[str]:
        run_options = get_run_options()
        effective_mode = self._effective_permission_mode()

        # When using permission mode with control channel, don't use -p mode.
        # The SDK-style streaming protocol requires bidirectional stdin/stdout
        # without -p. The prompt is sent as a JSON user message on stdin.
        if effective_mode is not None:
            args: list[str] = [
                "--output-format", "stream-json",
                "--input-format", "stream-json",
                "--verbose",
            ]
        else:
            args = [
                "-p",
                "--output-format", "stream-json",
                "--input-format", "stream-json",
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
            args.extend(["--permission-mode", effective_mode])
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
        return ClaudeStreamState()

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
    ) -> list[TakopiEvent]:
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
    ) -> list[TakopiEvent]:
        return []

    async def _drain_auto_approve(self, state: ClaudeStreamState) -> None:
        """Drain the auto-approve queue, writing responses to the control channel."""
        if not state.auto_approve_queue:
            return

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
                if self._proc_stdin is not None:
                    await self._proc_stdin.send(payload)
                    logger.info("control_response.auto_approved", request_id=req_id, channel="pipe")
                elif self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, payload)
                    logger.info("control_response.auto_approved", request_id=req_id, channel="pty")
                else:
                    logger.error("control_response.auto_approve_failed", request_id=req_id)
            except Exception as e:
                logger.error(
                    "control_response.auto_approve_failed",
                    request_id=req_id,
                    error=str(e),
                )
        state.auto_approve_queue.clear()

    def translate(
        self,
        data: claude_schema.StreamJsonMessage,
        *,
        state: ClaudeStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[TakopiEvent]:
        events = translate_claude_event(
            data,
            title=self.session_title,
            state=state,
            factory=state.factory,
        )

        # Phase 2: Register runner when we get a session_id
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
    ) -> list[TakopiEvent]:
        # Phase 2: Cleanup runner registration on error
        session_id = found_session.value if found_session else (resume.value if resume else None)
        if session_id and session_id in _ACTIVE_RUNNERS:
            del _ACTIVE_RUNNERS[session_id]
            logger.debug("claude_runner.unregistered", session_id=session_id)

        message = f"claude failed (rc={rc})."
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
    ) -> list[TakopiEvent]:
        # Phase 2: Cleanup runner registration
        session_id = found_session.value if found_session else (resume.value if resume else None)
        if session_id and session_id in _ACTIVE_RUNNERS:
            del _ACTIVE_RUNNERS[session_id]
            logger.debug("claude_runner.unregistered", session_id=session_id)

        if not found_session:
            message = "claude finished but no session_id was captured"
            resume_for_completed = resume
            return [
                state.factory.completed_error(
                    error=message,
                    resume=resume_for_completed,
                )
            ]

        message = "claude finished without a result event"
        return [
            state.factory.completed_error(
                error=message,
                answer=state.last_assistant_text or "",
                resume=found_session,
            )
        ]

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> anyio.abc.AsyncIterator[TakopiEvent]:
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
                    # Store stdin for writing control responses later
                    self._proc_stdin = proc.stdin
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

                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        drain_stderr,
                        proc.stderr,
                        run_logger,
                        tag,
                    )
                    async for evt in self._iter_jsonl_events(
                        stdout=proc.stdout,
                        stream=stream,
                        state=state,
                        resume=resume,
                        logger=run_logger,
                        pid=proc.pid,
                    ):
                        yield evt
                        # Drain auto-approve queue after yielding events
                        await self._drain_auto_approve(state)

                    # Close stdin after all events to let CLI exit
                    if use_control_channel and self._proc_stdin is not None:
                        with contextlib.suppress(Exception):
                            await self._proc_stdin.aclose()
                        self._proc_stdin = None

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
            # Cleanup
            self._proc_stdin = None
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
async def send_claude_control_response(request_id: str, approved: bool) -> bool:
    """Send a control response to an active Claude Code session.

    Args:
        request_id: The control request ID
        approved: Whether to approve (True) or deny (False) the request

    Returns:
        True if the response was sent successfully, False if the request is not found
    """
    # Look up session_id from request_id
    if request_id not in _REQUEST_TO_SESSION:
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
        # Clean up stale mapping
        del _REQUEST_TO_SESSION[request_id]
        return False

    runner, _ = _ACTIVE_RUNNERS[session_id]
    await runner.write_control_response(request_id, approved)

    # Clean up the mapping after use
    del _REQUEST_TO_SESSION[request_id]

    return True


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
