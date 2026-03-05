"""Amp CLI runner (Sourcegraph).

This runner integrates with the Amp CLI (https://ampcode.com).

Amp uses a Claude Code-compatible stream-json protocol with types:
- system(subtype="init"): Session initialisation with session_id and tools
- assistant: Assistant messages with content blocks (text, tool_use)
- user: User messages with content blocks (tool_result)
- result: Final result with subtype, is_error, and result text

Session IDs use the format: T-<uuid> (e.g., T-2775dc92-90ed-4f85-8b73-8f9766029e83).
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
from .run_options import get_run_options
from ..schemas import amp as amp_schema
from .tool_actions import tool_input_path, tool_kind_and_title

logger = get_logger(__name__)

ENGINE: EngineId = "amp"

_RESUME_RE = re.compile(
    r"(?im)^\s*`?amp\s+threads\s+continue\s+(?P<token>T-[A-Za-z0-9-]+)`?\s*$"
)


@dataclass(slots=True)
class AmpStreamState:
    """State tracked during Amp JSONL streaming."""

    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_text: str | None = None
    note_seq: int = 0
    session_id: str | None = None
    emitted_started: bool = False
    saw_result: bool = False
    accumulated_usage: dict[str, int] = field(default_factory=dict)


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


def _tool_kind_and_title(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[ActionKind, str]:
    return tool_kind_and_title(
        tool_name,
        tool_input,
        path_keys=("file_path", "path"),
        task_kind="subagent",
    )


def _extract_content_blocks(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract content blocks from an Amp message."""
    if message is None:
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return content


def _accumulate_usage(state: AmpStreamState, message: dict[str, Any] | None) -> None:
    """Accumulate token usage from an assistant message."""
    if message is None:
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens"):
        val = usage.get(key)
        if isinstance(val, int):
            state.accumulated_usage[key] = state.accumulated_usage.get(key, 0) + val


def _build_usage(state: AmpStreamState) -> dict[str, Any] | None:
    """Build a usage dict from accumulated token data."""
    if not state.accumulated_usage:
        return None
    return {
        "usage": {
            "input_tokens": state.accumulated_usage.get("input_tokens", 0),
            "output_tokens": state.accumulated_usage.get("output_tokens", 0),
        }
    }


def translate_amp_event(
    event: amp_schema.AmpEvent,
    *,
    title: str,
    state: AmpStreamState,
    meta: dict[str, Any] | None,
) -> list[UntetherEvent]:
    """Translate an Amp JSON event into Untether events."""
    out: list[UntetherEvent] = []

    if isinstance(event, amp_schema.SystemInit):
        subtype = event.subtype
        session_id = event.session_id
        if subtype != "init":
            return out
        if isinstance(session_id, str) and session_id:
            state.session_id = session_id
        if not state.emitted_started:
            state.emitted_started = True
            logger.info(
                "amp.session.started",
                session_id=state.session_id,
                title=title,
            )
            resume = ResumeToken(engine=ENGINE, value=state.session_id or "")
            out.append(
                StartedEvent(
                    engine=ENGINE,
                    resume=resume,
                    title=title,
                    meta=meta,
                )
            )
        return out

    if isinstance(event, amp_schema.AssistantMessage):
        message = event.message
        blocks = _extract_content_blocks(message)
        _accumulate_usage(state, message)
        results: list[UntetherEvent] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    if state.last_text is None:
                        state.last_text = text
                    else:
                        state.last_text += text
            elif block_type == "tool_use":
                tool_id = block.get("id")
                tool_name = block.get("name") or "tool"
                tool_input = block.get("input")
                if not isinstance(tool_id, str) or not tool_id:
                    continue
                if not isinstance(tool_input, dict):
                    tool_input = {}
                kind, action_title = _tool_kind_and_title(str(tool_name), tool_input)
                detail: dict[str, Any] = {
                    "tool_name": str(tool_name),
                    "input": tool_input,
                    "tool_id": tool_id,
                }
                if kind == "file_change":
                    path = tool_input_path(tool_input, path_keys=("file_path", "path"))
                    if path:
                        detail["changes"] = [{"path": path, "kind": "update"}]
                action = Action(
                    id=tool_id, kind=kind, title=action_title, detail=detail
                )
                state.pending_actions[action.id] = action
                results.append(_action_event(phase="started", action=action))
        return results

    if isinstance(event, amp_schema.UserMessage):
        message = event.message
        blocks = _extract_content_blocks(message)
        results = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str) or not tool_use_id:
                    continue
                pending = state.pending_actions.pop(tool_use_id, None)
                if pending is None:
                    continue
                is_error = block.get("is_error", False)
                content = block.get("content")
                # Extract text from content blocks
                output = ""
                if isinstance(content, list):
                    texts = [
                        str(c.get("text", ""))
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    output = "\n".join(texts)
                elif isinstance(content, str):
                    output = content
                detail = dict(pending.detail)
                if output:
                    detail["output_preview"] = (
                        output[:500] if len(output) > 500 else output
                    )
                results.append(
                    _action_event(
                        phase="completed",
                        action=Action(
                            id=pending.id,
                            kind=pending.kind,
                            title=pending.title,
                            detail=detail,
                        ),
                        ok=not is_error,
                    )
                )
        return results

    if isinstance(event, amp_schema.AmpResult):
        state.saw_result = True
        is_error = event.is_error
        error_text = event.error
        resume = None
        if state.session_id:
            resume = ResumeToken(engine=ENGINE, value=state.session_id)
        usage = _build_usage(state)
        answer = state.last_text or ""
        error = error_text if is_error else None
        logger.info(
            "amp.completed",
            session_id=state.session_id,
            is_error=is_error,
            answer_len=len(answer),
        )
        out.append(
            CompletedEvent(
                engine=ENGINE,
                ok=not is_error,
                answer=answer,
                resume=resume,
                usage=usage,
                error=error,
            )
        )
        return out

    return out


@dataclass(slots=True)
class AmpRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    """Runner for Amp CLI."""

    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE
    amp_cmd: str = "amp"
    model: str | None = None
    mode: str | None = None
    dangerously_allow_all: bool = True
    session_title: str = "amp"
    logger = logger

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`amp threads continue {token.value}`"

    def command(self) -> str:
        return self.amp_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        args: list[str] = []
        if resume is not None:
            args.extend(["threads", "continue", resume.value])
        if self.dangerously_allow_all:
            args.append("--dangerously-allow-all")
        run_options = get_run_options()
        mode = self.mode
        if run_options is not None and run_options.model:
            model = run_options.model
        else:
            model = self.model
        if mode:
            args.extend(["--mode", mode])
        if model:
            args.extend(["--model", str(model)])
        args.extend(["-x", "--stream-json"])
        args.append(prompt)
        return args

    def stdin_payload(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> bytes | None:
        return None

    def new_state(self, prompt: str, resume: ResumeToken | None) -> AmpStreamState:
        return AmpStreamState()

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: AmpStreamState,
    ) -> None:
        pass

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: AmpStreamState,
    ) -> list[UntetherEvent]:
        message = "invalid JSON from amp; ignoring line"
        return [self.note_event(message, state=state, detail={"line": raw})]

    def translate(
        self,
        data: amp_schema.AmpEvent,
        *,
        state: AmpStreamState,
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
        return translate_amp_event(
            data,
            title=self.session_title,
            state=state,
            meta=meta,
        )

    def decode_jsonl(self, *, line: bytes) -> amp_schema.AmpEvent:
        return amp_schema.decode_event(line)

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: AmpStreamState,
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
        state: AmpStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        parts = [f"amp failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        logger.error("amp.process.failed", rc=rc, session_id=state.session_id)
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
        state: AmpStreamState,
    ) -> list[UntetherEvent]:
        if not found_session:
            parts = ["amp finished but no session_id was captured"]
            session = _session_label(None, resume)
            if session:
                parts.append(f"session: {session}")
            message = "\n".join(parts)
            logger.warning("amp.stream.no_session")
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
                    usage=_build_usage(state),
                )
            ]

        parts = ["amp finished without a result event"]
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
    """Build an AmpRunner from configuration."""
    model = config.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigError(f"Invalid `amp.model` in {config_path}; expected a string.")

    mode = config.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise ConfigError(f"Invalid `amp.mode` in {config_path}; expected a string.")

    dangerously_allow_all = config.get("dangerously_allow_all")
    if dangerously_allow_all is None:
        dangerously_allow_all = True
    elif not isinstance(dangerously_allow_all, bool):
        raise ConfigError(
            f"Invalid `amp.dangerously_allow_all` in {config_path}; expected a boolean."
        )

    title = str(model) if model is not None else "amp"

    return AmpRunner(
        model=model,
        mode=mode,
        dangerously_allow_all=dangerously_allow_all,
        session_title=title,
    )


BACKEND = EngineBackend(
    id="amp",
    build_runner=build_runner,
    install_cmd="npm install -g @sourcegraph/amp",
)
