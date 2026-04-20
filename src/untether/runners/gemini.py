"""Gemini CLI runner.

This runner integrates with the Gemini CLI (https://github.com/google-gemini/gemini-cli).

Gemini CLI outputs JSON events in a streaming format with types:
- init: Session initialisation with session_id and model
- message: Text content (role=user or role=assistant, optionally delta=true)
- tool_use: Tool invocation with tool_name, tool_id, and parameters
- tool_result: Tool completion with tool_id, status, and output
- result: Final result with status and stats
- error: Error with message

Session IDs are short alphanumeric strings (e.g., abc123def).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import msgspec

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..logging import get_logger
from ..model import (
    Action,
    ActionEvent,
    ActionKind,
    CompletedEvent,
    EngineId,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)
from ..runner import (
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
    _stderr_excerpt,
)
from ..schemas import gemini as gemini_schema
from .run_options import get_run_options
from .tool_actions import tool_input_path, tool_kind_and_title

logger = get_logger(__name__)

ENGINE: EngineId = "gemini"

_RESUME_RE = re.compile(
    r"(?im)^\s*`?gemini\s+--resume\s+(?P<token>[A-Za-z0-9_-]+)`?\s*$"
)

_TOOL_NAME_MAP: dict[str, str] = {
    "read_file": "read",
    "edit_file": "edit",
    "write_file": "write",
    "web_search": "websearch",
    "web_fetch": "webfetch",
    "list_dir": "ls",
    "find_files": "glob",
    "search_files": "grep",
}


@dataclass(slots=True)
class GeminiStreamState:
    """State tracked during Gemini JSONL streaming."""

    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_text: str | None = None
    note_seq: int = 0
    session_id: str | None = None
    emitted_started: bool = False
    model: str | None = None
    saw_result: bool = False


def _action_event(
    *,
    phase: Literal["started", "updated", "completed"],
    action: Action,
    ok: bool | None = None,
    message: str | None = None,
    level: Literal["debug", "info", "warning", "error"] | None = None,
) -> ActionEvent:
    return ActionEvent(
        engine=ENGINE,
        action=action,
        phase=phase,
        ok=ok,
        message=message,
        level=level,
    )


def _gemini_tool_kind_and_title(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[ActionKind, str]:
    """Normalise Gemini snake_case tool names then delegate to shared helper."""
    normalised = _TOOL_NAME_MAP.get(tool_name, tool_name.lower())
    return tool_kind_and_title(
        normalised,
        tool_input,
        path_keys=("file_path", "path", "filePath"),
        task_kind="tool",
    )


def _build_usage(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build a usage dict from Gemini result stats."""
    if stats is None:
        return None
    usage: dict[str, Any] = {}
    input_tokens = stats.get("input_tokens")
    output_tokens = stats.get("output_tokens")
    if isinstance(input_tokens, int) or isinstance(output_tokens, int):
        token_usage: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        cached = stats.get("cached")
        if isinstance(cached, int):
            token_usage["cache_read_tokens"] = cached
        usage["usage"] = token_usage
    duration_ms = stats.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        usage["duration_ms"] = duration_ms
    total_cost_usd = stats.get("total_cost_usd")
    if isinstance(total_cost_usd, (int, float)):
        usage["total_cost_usd"] = float(total_cost_usd)
    return usage or None


def translate_gemini_event(
    event: gemini_schema.GeminiEvent,
    *,
    title: str,
    state: GeminiStreamState,
    meta: dict[str, Any] | None,
) -> list[UntetherEvent]:
    """Translate a Gemini JSON event into Untether events."""
    out: list[UntetherEvent] = []

    if isinstance(event, gemini_schema.Init):
        session_id = event.session_id
        model = event.model
        if isinstance(session_id, str) and session_id:
            state.session_id = session_id
        if isinstance(model, str) and model:
            state.model = model
        if not state.emitted_started:
            state.emitted_started = True
            logger.info(
                "gemini.session.started",
                session_id=state.session_id,
                model=state.model,
                title=title,
            )
            resume = ResumeToken(engine=ENGINE, value=state.session_id or "")
            meta = dict(meta) if meta else {}
            if state.model:
                meta["model"] = state.model
            out.append(
                StartedEvent(
                    engine=ENGINE,
                    resume=resume,
                    title=title,
                    meta=meta or None,
                )
            )
        return out

    if isinstance(event, gemini_schema.ToolUse):
        tool_id = event.tool_id
        tool_name = event.tool_name
        parameters = event.parameters
        if not isinstance(tool_id, str) or not tool_id:
            return out
        tool_input = parameters if isinstance(parameters, dict) else {}
        name = str(tool_name or "tool")
        kind, action_title = _gemini_tool_kind_and_title(name, tool_input)
        detail: dict[str, Any] = {
            "tool_name": name,
            "input": tool_input,
            "tool_id": tool_id,
        }
        if kind == "file_change":
            path = tool_input_path(
                tool_input, path_keys=("file_path", "path", "filePath")
            )
            if path:
                detail["changes"] = [{"path": path, "kind": "update"}]
        action = Action(id=tool_id, kind=kind, title=action_title, detail=detail)
        state.pending_actions[action.id] = action
        out.append(_action_event(phase="started", action=action))
        return out

    if isinstance(event, gemini_schema.ToolResult):
        tool_id = event.tool_id
        status = event.status
        output = event.output
        if not isinstance(tool_id, str) or not tool_id:
            return out
        pending = state.pending_actions.pop(tool_id, None)
        if pending is None:
            return out
        is_ok = status == "success"
        detail = dict(pending.detail)
        if output is not None:
            detail["output_preview"] = (
                str(output)[:500] if len(str(output)) > 500 else str(output)
            )
        out.append(
            _action_event(
                phase="completed",
                action=Action(
                    id=pending.id,
                    kind=pending.kind,
                    title=pending.title,
                    detail=detail,
                ),
                ok=is_ok,
            )
        )
        return out

    if isinstance(event, gemini_schema.Message):
        role = event.role
        content = event.content
        if role == "assistant" and isinstance(content, str) and content:
            if state.last_text is None:
                state.last_text = content
            else:
                state.last_text += content
        return out

    if isinstance(event, gemini_schema.GeminiResult):
        status = event.status
        stats = event.stats
        state.saw_result = True
        resume = None
        if state.session_id:
            resume = ResumeToken(engine=ENGINE, value=state.session_id)
        usage = _build_usage(stats)
        answer = state.last_text or ""
        logger.info(
            "gemini.completed",
            session_id=state.session_id,
            status=status,
            answer_len=len(answer),
        )
        if status == "success":
            out.append(
                CompletedEvent(
                    engine=ENGINE,
                    ok=True,
                    answer=answer,
                    resume=resume,
                    usage=usage,
                )
            )
        else:
            error = f"gemini result status: {status}"
            out.append(
                CompletedEvent(
                    engine=ENGINE,
                    ok=False,
                    answer=answer,
                    resume=resume,
                    usage=usage,
                    error=error,
                )
            )
        return out

    if isinstance(event, gemini_schema.Error):
        msg = event.message
        error_message = str(msg) if msg else "gemini error"
        resume = None
        if state.session_id:
            resume = ResumeToken(engine=ENGINE, value=state.session_id)
        logger.error(
            "gemini.error",
            session_id=state.session_id,
            error=error_message,
        )
        out.append(
            CompletedEvent(
                engine=ENGINE,
                ok=False,
                answer=state.last_text or "",
                resume=resume,
                error=error_message,
            )
        )
        return out

    logger.debug(
        "gemini.event.unrecognised",
        event_type=type(event).__name__,
    )
    return out


@dataclass(slots=True)
class GeminiRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    """Runner for Gemini CLI."""

    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE
    gemini_cmd: str = "gemini"
    model: str | None = None
    session_title: str = "gemini"
    logger = logger

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`gemini --resume {token.value}`"

    def command(self) -> str:
        return self.gemini_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        run_options = get_run_options()
        args: list[str] = []
        if resume is not None:
            if resume.is_continue:
                args.extend(["--resume", "latest"])
            else:
                args.extend(["--resume", resume.value])
        args.extend(["--output-format", "stream-json"])
        model = self.model
        if run_options is not None and run_options.model:
            model = run_options.model
        if model:
            args.extend(["--model", str(model)])
        if run_options is not None and run_options.permission_mode:
            args.extend(["--approval-mode", run_options.permission_mode])
        else:
            args.extend(["--approval-mode", "yolo"])
        args.append(f"--prompt={self.sanitize_prompt(prompt)}")
        return args

    def stdin_payload(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> bytes | None:
        return None

    def new_state(self, prompt: str, resume: ResumeToken | None) -> GeminiStreamState:
        return GeminiStreamState()

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: GeminiStreamState,
    ) -> None:
        pass

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: GeminiStreamState,
    ) -> list[UntetherEvent]:
        message = "invalid JSON from gemini; ignoring line"
        return [self.note_event(message, state=state, detail={"line": raw})]

    def translate(
        self,
        data: gemini_schema.GeminiEvent,
        *,
        state: GeminiStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        meta: dict[str, Any] | None = None
        model = self.model
        run_options = get_run_options()
        if run_options is not None and run_options.model:
            model = run_options.model
        if model is not None:
            meta = {"model": str(model)}
        if run_options is not None and run_options.permission_mode:
            pm = run_options.permission_mode
            if pm == "yolo":
                if meta is None:
                    meta = {}
                meta["permissionMode"] = "full access"
            elif pm == "auto_edit":
                if meta is None:
                    meta = {}
                meta["permissionMode"] = "edit files"
            # Default (None/read-only) — omit from footer
        return translate_gemini_event(
            data,
            title=self.session_title,
            state=state,
            meta=meta,
        )

    def decode_jsonl(self, *, line: bytes) -> gemini_schema.GeminiEvent:
        return gemini_schema.decode_event(line)

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: GeminiStreamState,
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

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: GeminiStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        parts = [f"gemini failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        logger.error("gemini.process.failed", rc=rc, session_id=state.session_id)
        resume_for_completed = found_session or resume
        return [
            self.note_event(message, state=state, ok=False),
            CompletedEvent(
                engine=ENGINE,
                ok=False,
                answer=state.last_text or "",
                resume=resume_for_completed,
                error=message,
            ),
        ]

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: GeminiStreamState,
    ) -> list[UntetherEvent]:
        if not found_session:
            parts = ["gemini finished but no session_id was captured"]
            session = _session_label(None, resume)
            if session:
                parts.append(f"session: {session}")
            message = "\n".join(parts)
            logger.warning("gemini.stream.no_session")
            return [
                CompletedEvent(
                    engine=ENGINE,
                    ok=False,
                    answer=state.last_text or "",
                    resume=resume,
                    error=message,
                )
            ]

        if state.saw_result:
            return [
                CompletedEvent(
                    engine=ENGINE,
                    ok=True,
                    answer=state.last_text or "",
                    resume=found_session,
                    usage=None,
                )
            ]

        parts = ["gemini finished without a result event"]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        message = "\n".join(parts)
        return [
            CompletedEvent(
                engine=ENGINE,
                ok=False,
                answer=state.last_text or "",
                resume=found_session,
                error=message,
            )
        ]


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    """Build a GeminiRunner from configuration."""
    model = config.get("model")
    if model is not None and not isinstance(model, str):
        logger.warning(
            "gemini.config.invalid",
            error="model must be a string",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `gemini.model` in {config_path}; expected a string."
        )

    title = str(model) if model is not None else "gemini"

    return GeminiRunner(
        model=model,
        session_title=title,
    )


BACKEND = EngineBackend(
    id="gemini",
    build_runner=build_runner,
    install_cmd="npm install -g @google/gemini-cli",
)
