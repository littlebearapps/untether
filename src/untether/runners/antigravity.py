"""Antigravity CLI runner.

This runner integrates with the Antigravity CLI (agy) (https://antigravity.google).

Antigravity CLI outputs JSON events in a streaming format with types:
- init: Session initialisation with conversation_id and model
- step_update: Tool execution and message streaming with state (ACTIVE/DONE)
- result: Final result with status, response, duration, and usage
- error: Error event with message

Session IDs are UUID strings.
"""

from __future__ import annotations

import os
import re
import shutil
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
from ..schemas import antigravity as antigravity_schema
from .run_options import get_run_options
from .tool_actions import tool_input_path, tool_kind_and_title

logger = get_logger(__name__)

ENGINE: EngineId = "antigravity"

_RESUME_RE = re.compile(
    r"(?im)^\s*`?(?:agy|antigravity)\s+--conversation\s+(?P<token>[A-Za-z0-9_-]+)`?\s*$"
)

_TOOL_NAME_MAP: dict[str, str] = {
    "run_command": "bash",
    "view_file": "read",
    "write_to_file": "write",
    "replace_file_content": "edit",
    "multi_replace_file_content": "edit",
    "sed_file": "edit",
    "list_dir": "ls",
    "find_by_name": "glob",
    "grep_search": "grep",
    "search_web": "websearch",
    "read_url_content": "webfetch",
    "invoke_subagent": "agent",
    "ask_question": "askuserquestion",
}


@dataclass(slots=True)
class AntigravityStreamState:
    """State tracked during Antigravity JSONL streaming."""

    pending_actions: dict[str, Action] = field(default_factory=dict)
    last_text: str | None = None
    session_id: str | None = None
    emitted_started: bool = False
    model: str | None = None
    saw_result: bool = False
    command_usage: dict[str, Any] | None = None


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


def _antigravity_tool_kind_and_title(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[ActionKind, str]:
    """Normalise Antigravity tool names then delegate to shared helper."""
    normalised = _TOOL_NAME_MAP.get(tool_name, tool_name.lower())
    input_copy = dict(tool_input)
    if (
        normalised in {"bash", "shell"}
        and "CommandLine" in input_copy
        and "command" not in input_copy
    ):
        input_copy["command"] = input_copy["CommandLine"]
    if (
        normalised in {"glob", "grep"}
        and "Pattern" in input_copy
        and "pattern" not in input_copy
    ):
        input_copy["pattern"] = input_copy["Pattern"]
    if normalised == "grep" and "Query" in input_copy and "pattern" not in input_copy:
        input_copy["pattern"] = input_copy["Query"]
    if (
        normalised == "websearch"
        and "query" not in input_copy
        and "Query" in input_copy
    ):
        input_copy["query"] = input_copy["Query"]
    if normalised == "webfetch" and "url" not in input_copy and "Url" in input_copy:
        input_copy["url"] = input_copy["Url"]
    return tool_kind_and_title(
        normalised,
        input_copy,
        path_keys=(
            "TargetFile",
            "AbsolutePath",
            "DirectoryPath",
            "file_path",
            "path",
            "filePath",
        ),
        task_kind="subagent",
    )


def _build_usage(
    stats: antigravity_schema.ResultUsage | dict[str, Any] | None,
    duration_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Build a usage dict from Antigravity result stats."""
    if stats is None:
        return None
    if isinstance(stats, antigravity_schema.ResultUsage):
        input_tokens = stats.input_tokens
        output_tokens = stats.output_tokens
        cached = stats.cache_read_tokens
        thinking = stats.thinking_tokens
    elif isinstance(stats, dict):
        input_tokens = stats.get("input_tokens")
        output_tokens = stats.get("output_tokens")
        cached = stats.get("cache_read_tokens") or stats.get("cached")
        thinking = stats.get("thinking_tokens")
    else:
        return None

    usage: dict[str, Any] = {}
    if isinstance(input_tokens, int) or isinstance(output_tokens, int):
        token_usage: dict[str, Any] = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
        }
        if isinstance(cached, int):
            token_usage["cache_read_tokens"] = cached
        if isinstance(thinking, int):
            token_usage["thinking_tokens"] = thinking
        usage["usage"] = token_usage

    if isinstance(duration_seconds, (int, float)):
        usage["duration_ms"] = int(duration_seconds * 1000)

    return usage or None


def translate_antigravity_event(
    event: antigravity_schema.AntigravityEvent,
    *,
    title: str,
    state: AntigravityStreamState,
    meta: dict[str, Any] | None,
) -> list[UntetherEvent]:
    """Translate an Antigravity JSON event into Untether events."""
    out: list[UntetherEvent] = []

    if isinstance(event, antigravity_schema.Init):
        session_id = event.conversation_id
        model = event.init.model if event.init else None
        if isinstance(session_id, str) and session_id:
            state.session_id = session_id
        if isinstance(model, str) and model:
            state.model = model
        if not state.emitted_started:
            state.emitted_started = True
            logger.info(
                "antigravity.session.started",
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

    if isinstance(event, antigravity_schema.StepUpdate):
        su = event.step_update
        if su is None:
            return out
        if su.conversation_id and not state.session_id:
            state.session_id = su.conversation_id

        if su.step_type == "tool":
            tool_id = str(
                su.step_index if su.step_index is not None else su.tool_name or "tool"
            )
            tool_name = (
                su.tool_name
                or (su.tool_info.name if su.tool_info and su.tool_info.name else None)
                or "tool"
            )
            parameters = (
                su.tool_info.parameters
                if (su.tool_info and isinstance(su.tool_info.parameters, dict))
                else {}
            )
            output = su.tool_info.output if su.tool_info else None
            kind, action_title = _antigravity_tool_kind_and_title(tool_name, parameters)
            detail: dict[str, Any] = {
                "tool_name": tool_name,
                "input": parameters,
                "tool_id": tool_id,
            }
            if kind == "file_change":
                path = tool_input_path(
                    parameters,
                    path_keys=("TargetFile", "file_path", "path", "filePath"),
                )
                if path:
                    detail["changes"] = [{"path": path, "kind": "update"}]

            if su.state == "ACTIVE":
                action = Action(
                    id=tool_id, kind=kind, title=action_title, detail=detail
                )
                state.pending_actions[tool_id] = action
                out.append(_action_event(phase="started", action=action))
            elif su.state == "DONE":
                pending = state.pending_actions.pop(tool_id, None)
                action_to_use = (
                    pending
                    if pending is not None
                    else Action(
                        id=tool_id, kind=kind, title=action_title, detail=detail
                    )
                )
                final_detail = dict(action_to_use.detail)
                if output is not None:
                    final_detail["output_preview"] = (
                        str(output)[:500] if len(str(output)) > 500 else str(output)
                    )
                completed_action = Action(
                    id=action_to_use.id,
                    kind=action_to_use.kind,
                    title=action_to_use.title,
                    detail=final_detail,
                )
                out.append(
                    _action_event(phase="completed", action=completed_action, ok=True)
                )
            return out

        if su.step_type == "agent_response":
            delta = su.text_delta
            if delta:
                if state.last_text is None:
                    state.last_text = delta
                else:
                    state.last_text += delta
            return out

        return out

    if isinstance(event, antigravity_schema.AntigravityResult):
        res = event.result
        if res is None:
            return out
        status = res.status
        state.saw_result = True
        if res.conversation_id:
            state.session_id = res.conversation_id
        resume = None
        if state.session_id:
            resume = ResumeToken(engine=ENGINE, value=state.session_id)
        usage = _build_usage(res.usage, res.duration_seconds)
        answer = res.response if res.response is not None else (state.last_text or "")
        logger.info(
            "antigravity.completed",
            session_id=state.session_id,
            status=status,
            answer_len=len(answer),
        )
        if not state.emitted_started:
            state.emitted_started = True
            started_resume = resume or ResumeToken(
                engine=ENGINE, value=state.session_id or ""
            )
            out.append(
                StartedEvent(
                    engine=ENGINE,
                    resume=started_resume,
                    title=title,
                    meta=meta or None,
                )
            )
        if res.command and res.command.data:
            state.command_usage = res.command.data
        if status == "SUCCESS":
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
            error = res.error or f"antigravity result status: {status}"
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

    if isinstance(event, antigravity_schema.CommandResult):
        if event.command and event.command.data:
            state.command_usage = event.command.data
        if not state.emitted_started:
            state.emitted_started = True
            started_resume = ResumeToken(engine=ENGINE, value=state.session_id or "")
            meta_dict = dict(meta) if meta else {}
            if state.model:
                meta_dict["model"] = state.model
            out.append(
                StartedEvent(
                    engine=ENGINE,
                    resume=started_resume,
                    title=title,
                    meta=meta_dict or None,
                )
            )
        return out

    if isinstance(event, antigravity_schema.Error):
        msg = event.error or event.message
        error_message = str(msg) if msg else "antigravity error"
        resume = None
        if state.session_id:
            resume = ResumeToken(engine=ENGINE, value=state.session_id)
        logger.error(
            "antigravity.error",
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
        "antigravity.event.unrecognised",
        event_type=type(event).__name__,
    )
    return out


def default_antigravity_cmd() -> str:
    """Resolve default binary path for Antigravity CLI (`agy`).

    Checks ``~/.local/bin/agy`` first, falling back to ``agy`` on PATH,
    or returning ``~/.local/bin/agy`` (expanded) as the default location.
    """
    local_bin = Path.home() / ".local" / "bin" / "agy"
    if local_bin.exists():
        return str(local_bin)
    which_cmd = shutil.which("agy")
    if which_cmd:
        return which_cmd
    return str(local_bin)


@dataclass(slots=True)
class AntigravityRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    """Runner for Antigravity CLI (agy)."""

    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE
    antigravity_cmd: str = field(default_factory=default_antigravity_cmd)
    model: str | None = None
    session_title: str = "antigravity"
    dangerously_skip_permissions: bool = True
    logger = logger

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`agy --conversation {token.value}`"

    def command(self) -> str:
        return self.antigravity_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        run_options = get_run_options()
        args: list[str] = ["--output-format", "stream-json"]
        if resume is not None:
            if resume.is_continue:
                args.append("--continue")
            else:
                args.extend(["--conversation", resume.value])
        model = self.model
        if run_options is not None and run_options.model:
            model = run_options.model
        if model:
            args.extend(["--model", str(model)])
        if run_options is not None and run_options.reasoning:
            args.extend(["--effort", str(run_options.reasoning)])
        permission_mode = (
            run_options.permission_mode if run_options is not None else None
        )
        if permission_mode:
            if permission_mode == "plan":
                args.extend(["--mode", "plan"])
            elif permission_mode in {"accept-edits", "acceptEdits"}:
                args.extend(["--mode", "accept-edits"])
        if self.dangerously_skip_permissions and permission_mode in {
            None,
            "default",
            "auto",
            "bypassPermissions",
        }:
            args.append("--dangerously-skip-permissions")
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

    def new_state(
        self, prompt: str, resume: ResumeToken | None
    ) -> AntigravityStreamState:
        session_id = (
            resume.value if resume is not None and not resume.is_continue else None
        )
        return AntigravityStreamState(session_id=session_id)

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: AntigravityStreamState,
    ) -> None:
        pass

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: AntigravityStreamState,
    ) -> list[UntetherEvent]:
        message = "invalid JSON from antigravity; ignoring line"
        return [self.note_event(message, state=state, detail={"line": raw})]

    def translate(
        self,
        data: antigravity_schema.AntigravityEvent,
        *,
        state: AntigravityStreamState,
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
            if pm in {"auto", "bypassPermissions", "always-proceed"}:
                if meta is None:
                    meta = {}
                meta["permissionMode"] = "full access"
            elif pm in {"accept-edits", "acceptEdits"}:
                if meta is None:
                    meta = {}
                meta["permissionMode"] = "edit files"
            elif pm == "plan":
                if meta is None:
                    meta = {}
                meta["permissionMode"] = "plan"
        if run_options is not None and run_options.reasoning:
            if meta is None:
                meta = {}
            meta["effort"] = run_options.reasoning

        return translate_antigravity_event(
            data,
            title=self.session_title,
            state=state,
            meta=meta,
        )

    def decode_jsonl(self, *, line: bytes) -> antigravity_schema.AntigravityEvent:
        try:
            return antigravity_schema.decode_event(line)
        except msgspec.DecodeError:
            text = line.decode("utf-8", errors="replace")
            brace = text.find("{")
            if brace > 0:
                return antigravity_schema.decode_event(text[brace:].encode("utf-8"))
            raise

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: AntigravityStreamState,
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
        state: AntigravityStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        parts = [f"antigravity failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        logger.error("antigravity.process.failed", rc=rc, session_id=state.session_id)
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
        state: AntigravityStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        effective_session = found_session or resume
        if state.saw_result:
            return [
                CompletedEvent(
                    engine=ENGINE,
                    ok=True,
                    answer=state.last_text or "",
                    resume=effective_session,
                    usage=None,
                )
            ]

        if not effective_session:
            parts = ["antigravity finished but no session_id was captured"]
            session = _session_label(None, resume)
            if session:
                parts.append(f"session: {session}")
            message = "\n".join(parts)
            logger.warning("antigravity.stream.no_session")
            return [
                CompletedEvent(
                    engine=ENGINE,
                    ok=False,
                    answer=state.last_text or "",
                    resume=resume,
                    error=message,
                )
            ]

        parts = ["antigravity finished without a result event"]
        session = _session_label(effective_session, resume)
        if session:
            parts.append(f"session: {session}")
        message = "\n".join(parts)
        return [
            CompletedEvent(
                engine=ENGINE,
                ok=False,
                answer=state.last_text or "",
                resume=effective_session,
                error=message,
            )
        ]


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    """Build an AntigravityRunner from configuration."""
    model = config.get("model")
    if model is not None and not isinstance(model, str):
        logger.warning(
            "antigravity.config.invalid",
            error="model must be a string",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `antigravity.model` in {config_path}; expected a string."
        )

    dangerously_skip_permissions = config.get("dangerously_skip_permissions", True)
    if not isinstance(dangerously_skip_permissions, bool):
        logger.warning(
            "antigravity.config.invalid",
            error="dangerously_skip_permissions must be a boolean",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `antigravity.dangerously_skip_permissions` in {config_path}; expected a boolean."
        )

    raw_cmd = config.get("cmd") or config.get("antigravity_cmd")
    if raw_cmd is not None:
        if not isinstance(raw_cmd, str):
            logger.warning(
                "antigravity.config.invalid",
                error="cmd must be a string",
                config_path=str(config_path),
            )
            raise ConfigError(
                f"Invalid `antigravity.cmd` in {config_path}; expected a string."
            )
        antigravity_cmd = os.path.expanduser(raw_cmd)
    else:
        antigravity_cmd = default_antigravity_cmd()

    title = str(model) if model is not None else "antigravity"

    return AntigravityRunner(
        antigravity_cmd=antigravity_cmd,
        model=model,
        session_title=title,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )


BACKEND = EngineBackend(
    id="antigravity",
    build_runner=build_runner,
    cli_cmd=default_antigravity_cmd(),
    install_cmd="curl -fsSL https://antigravity.google/install.sh | bash",
)
