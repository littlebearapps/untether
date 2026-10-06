from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..model import ActionPhase, EngineId, ResumeToken, UntetherEvent
from ..runner import (
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
    _stderr_excerpt,
)
from ..schemas import codex as codex_schema
from ..utils.paths import relativize_command
from .extra_args_guard import (
    SECURITY_DOC_REF,
    BlockedArg,
    BlockedCategory,
    BlockedExtraArgsError,
    dedupe_hits,
    format_blocked,
    iter_option_tokens,
    normalise_value,
    option_value,
)
from .run_options import get_run_options

logger = get_logger(__name__)

ENGINE: EngineId = "codex"

__all__ = [
    "CODEX_SAFE_PERMISSION_MODE",
    "ENGINE",
    "CodexRunner",
    "find_blocked_codex_args",
    "find_exec_only_flag",
    "translate_codex_event",
]

_RESUME_RE = re.compile(r"(?im)^\s*`?codex\s+resume\s+(?P<token>[^`\s]+)`?\s*$")
_RECONNECTING_RE = re.compile(
    r"^Reconnecting\.{3}\s*(?P<attempt>\d+)/(?P<max>\d+)\s*$",
    re.IGNORECASE,
)
# Flags Untether manages, rejected in ``extra_args`` (#407). NB the name is
# historical: ``--ask-for-approval`` is top-level only — ``codex exec`` never
# reads it and forces approval=never itself (#830), so Untether no longer
# passes it at all. #209's `find_blocked_codex_args` rejects every spelling
# (`-a`, `-aVALUE`, `--ask-for-approval=…`).
_EXEC_ONLY_FLAGS = {
    "--ask-for-approval",
    "--skip-git-repo-check",
    "--json",
    "--output-schema",
    "--output-last-message",
    "--color",
    "-o",
}
_EXEC_ONLY_PREFIXES = (
    "--output-schema=",
    "--output-last-message=",
    "--color=",
)


# #830: Codex ``safe`` approval policy. `codex exec` hard-codes
# approval=never and ignores the root ``-a`` (and codex-cli 0.149.0 removed
# ``untrusted`` outright), so the only lever exec enforces is the sandbox.
CODEX_SAFE_PERMISSION_MODE = "safe"
# Exec-level (after ``exec``, before ``resume``) so it outranks any root-level
# --sandbox / --yolo / --approve-for-me in extra_args (upstream
# shared_options.rs inherit_exec_root_options) and config.toml sandbox_mode.
_CODEX_SAFE_SANDBOX = "read-only"
# Values that mean "full auto" (no sandbox flag). `/config` maps its Full auto
# button to "auto" (stored as None); engine_overrides documents it too.
_CODEX_FULL_AUTO_MODES = frozenset({"auto"})
_UNKNOWN_PM_WARNED: set[str] = set()


def _warn_unknown_permission_mode(mode: str) -> None:
    """Once per distinct value per process: an unknown Codex mode runs full auto."""
    if mode in _UNKNOWN_PM_WARNED:
        return
    _UNKNOWN_PM_WARNED.add(mode)
    logger.warning(
        "codex.permission_mode.unknown",
        mode=mode,
        note="unknown Codex permission_mode runs as full auto; valid: safe",
    )


# clap's argv-rejection lines (#830): a removed/renamed flag or value exits
# rc=2 before any JSONL, so this is the crisp signature for the next drift.
_CLAP_ARGV_ERROR_PREFIXES = (
    "error: invalid value ",
    "error: unexpected argument ",
    "error: a value is required for ",
)


def _clap_argv_error_line(stderr_lines: list[str] | None) -> str | None:
    for line in stderr_lines or ():
        stripped = line.strip()
        if stripped.startswith(_CLAP_ARGV_ERROR_PREFIXES):
            return stripped[:200]
    return None


def find_exec_only_flag(extra_args: list[str]) -> str | None:
    for arg in extra_args:
        if arg in _EXEC_ONLY_FLAGS:
            return arg
        for prefix in _EXEC_ONLY_PREFIXES:
            if arg.startswith(prefix):
                return arg
    return None


# --- #209: `[codex] extra_args` deny-list -----------------------------------
# `find_exec_only_flag` above is kept for back-compat (tests pin its raw-token
# return); `build_runner` uses `find_blocked_codex_args`, a superset.
# extra_args sit at the ROOT (before `exec`), where clap inherits the
# bypass/workspace flags into exec (upstream shared_options.rs
# inherit_exec_root_options). Codex findings Q1; probes in plan 01 §3.3.
_CODEX_MANAGED_HINT = "is managed by Untether and cannot be overridden"
_CODEX_BYPASS_HINT = (
    "bypasses Codex's sandbox/approval/hook-trust checks and is not accepted in"
    f" `extra_args`; see {SECURITY_DOC_REF}"
)
_CODEX_WORKSPACE_HINT = (
    "is managed by Untether: Untether sets the working directory from the"
    " project (`/ctx`, worktrees)"
)
_CODEX_BLOCKED: dict[str, tuple[BlockedCategory, str]] = {
    # managed (existing #407 set, every spelling)
    "--skip-git-repo-check": ("managed", _CODEX_MANAGED_HINT),
    "--json": ("managed", _CODEX_MANAGED_HINT),
    "--output-schema": ("managed", _CODEX_MANAGED_HINT),
    "--output-last-message": ("managed", _CODEX_MANAGED_HINT),
    "--color": ("managed", _CODEX_MANAGED_HINT),
    # managed (new): top-level only and ignored by `codex exec` (#830)
    "--ask-for-approval": (
        "managed",
        "is managed by Untether: `codex exec` ignores it — use /config →"
        " Approval policy",
    ),
    # exec-only: before `exec`, codex rejects them (rc=2 on every run)
    "--ignore-rules": (
        "managed",
        "is managed by Untether: it is exec-only, and before `exec` codex rejects it",
    ),
    "--ignore-user-config": (
        "managed",
        "is managed by Untether: it is exec-only, and before `exec` codex rejects it",
    ),
    # bypass
    "--dangerously-bypass-approvals-and-sandbox": ("bypass", _CODEX_BYPASS_HINT),
    "--yolo": ("bypass", _CODEX_BYPASS_HINT),
    "--approve-for-me": ("bypass", _CODEX_BYPASS_HINT),
    "--not-so-yolo": ("bypass", _CODEX_BYPASS_HINT),
    "--dangerously-bypass-hook-trust": ("bypass", _CODEX_BYPASS_HINT),
    # workspace
    "--cd": ("workspace", _CODEX_WORKSPACE_HINT),
    "--worktree": ("workspace", _CODEX_WORKSPACE_HINT),
    "--": (
        "separator",
        "is not accepted: a bare `--` would turn `exec …` into prompt text",
    ),
}
_CODEX_DANGER_SANDBOX = "danger-full-access"
# D5: case-insensitive substring over the whole `-c key=value` — catches
# sandbox_mode, permission profiles (`:danger-full-access`,
# `:danger-no-sandbox`), inline tables, `bypass_hook_trust` and the
# `dangerously_allow_*` keys without parsing TOML keys.
_CODEX_CONFIG_DANGER_SUBSTRINGS: tuple[str, ...] = (
    "danger-full-access",
    ":danger",
    "bypass",
    "dangerously",
)
# clap short options per `codex --help` / `codex exec --help` (0.157.1).
_CODEX_SHORT_ALIASES: dict[str, str] = {
    "c": "--config",
    "i": "--image",
    "m": "--model",
    "p": "--profile",
    "s": "--sandbox",
    "C": "--cd",
    "a": "--ask-for-approval",
    "o": "--output-last-message",
    "h": "--help",
    "V": "--version",
}
_CODEX_SHORT_VALUE_FLAGS: frozenset[str] = frozenset(
    {"c", "i", "m", "p", "s", "C", "a", "o"}
)


def find_blocked_codex_args(extra_args: list[str]) -> list[BlockedArg]:
    """Every blocked flag in *extra_args* (#209), deduped, in order.

    Root `-s/--sandbox read-only|workspace-write` stays allowed: it is the
    documented way to pick a full-auto sandbox, and safe mode's exec-level
    `--sandbox read-only` outranks it (#830 R4). Only `danger-full-access`
    is refused (D3).
    """
    hits: list[BlockedArg] = []
    for tok in iter_option_tokens(
        extra_args,
        short_aliases=_CODEX_SHORT_ALIASES,
        short_value_flags=_CODEX_SHORT_VALUE_FLAGS,
    ):
        rule = _CODEX_BLOCKED.get(tok.flag)
        if rule is not None:
            category, hint = rule
            hits.append(BlockedArg(flag=tok.flag, category=category, hint=hint))
            continue
        if tok.flag == "--sandbox":
            value = option_value(extra_args, tok)
            if value is not None and normalise_value(value) == _CODEX_DANGER_SANDBOX:
                hits.append(
                    BlockedArg(
                        flag="--sandbox",
                        category="bypass",
                        hint=f"with `{_CODEX_DANGER_SANDBOX}` {_CODEX_BYPASS_HINT}",
                    )
                )
        elif tok.flag == "--config":
            value = option_value(extra_args, tok)
            lowered = value.lower() if value is not None else ""
            matched = next(
                (sub for sub in _CODEX_CONFIG_DANGER_SUBSTRINGS if sub in lowered),
                None,
            )
            if matched is not None:
                hits.append(
                    BlockedArg(
                        flag="--config",
                        category="bypass",
                        hint=(
                            f"with a value mentioning `{matched}` {_CODEX_BYPASS_HINT}"
                        ),
                    )
                )
    return dedupe_hits(hits)


def _parse_reconnect_message(message: str) -> tuple[int, int] | None:
    match = _RECONNECTING_RE.match(message)
    if not match:
        return None
    try:
        attempt = int(match.group("attempt"))
        max_attempts = int(match.group("max"))
    except (TypeError, ValueError):
        return None
    return (attempt, max_attempts)


def _short_tool_name(server: str | None, tool: str | None) -> str:
    name = ".".join(part for part in (server, tool) if part)
    return name or "tool"


def _summarize_tool_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, codex_schema.McpToolCallItemResult):
        summary: dict[str, Any] = {}
        content = result.content
        if isinstance(content, list):
            summary["content_blocks"] = len(content)
        elif content is not None:
            summary["content_blocks"] = 1
        summary["has_structured"] = result.structured_content is not None
        return summary or None

    if isinstance(result, dict):
        summary = {}
        content = result.get("content")
        if isinstance(content, list):
            summary["content_blocks"] = len(content)
        elif content is not None:
            summary["content_blocks"] = 1

        structured_key: str | None = None
        if "structured_content" in result:
            structured_key = "structured_content"
        elif "structured" in result:
            structured_key = "structured"

        if structured_key is not None:
            summary["has_structured"] = result.get(structured_key) is not None
        return summary or None

    return None


def _normalize_change_list(changes: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for change in changes:
        path: str | None = None
        kind: str | None = None
        if isinstance(change, codex_schema.FileUpdateChange):
            path = change.path
            kind = change.kind
        elif isinstance(change, dict):
            path = change.get("path")
            kind = change.get("kind")
        if not isinstance(path, str) or not path:
            continue
        entry = {"path": path}
        if isinstance(kind, str) and kind:
            entry["kind"] = kind
        normalized.append(entry)
    return normalized


def _format_change_summary(changes: list[Any]) -> str:
    paths: list[str] = []
    for change in changes:
        if isinstance(change, codex_schema.FileUpdateChange):
            if change.path:
                paths.append(change.path)
            continue
        if isinstance(change, dict):
            path = change.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    if not paths:
        total = len(changes)
        if total <= 0:
            return "files"
        return f"{total} files"
    return ", ".join(str(path) for path in paths)


_WEB_SEARCH_MAX_QUERIES = 3


def _str_field(obj: object, key: str) -> str:
    """``obj[key]`` when ``obj`` is a dict and the value is a non-empty str."""
    if not isinstance(obj, dict):
        return ""
    value = obj.get(key)
    return value if isinstance(value, str) else ""


def _web_search_title(query: str | None, action: object) -> tuple[str, str]:
    """Return ``(title, action_type)`` for a Codex ``web_search`` item (#419).

    Never raises: ``action`` is whatever JSON arrived (the schema leaves it
    untyped). A non-dict action is treated as absent; non-str
    ``type``/``query``/``url``/``pattern`` values are ignored; ``queries``
    contributes only its str elements.
    """
    query = query if isinstance(query, str) else ""
    action_type = _str_field(action, "type")
    if action_type == "search":
        title = _str_field(action, "query") or query
        if not title and isinstance(action, dict):
            raw = action.get("queries")
            queries = (
                [q for q in raw if isinstance(q, str) and q]
                if isinstance(raw, list)
                else []
            )
            if queries:
                title = " · ".join(queries[:_WEB_SEARCH_MAX_QUERIES])
                extra = len(queries) - _WEB_SEARCH_MAX_QUERIES
                if extra > 0:
                    title += f" (+{extra} more)"
        return (title or "web search", "search" if title else "other")
    if action_type == "open_page":
        return (_str_field(action, "url") or query or "page", "open_page")
    if action_type == "find_in_page":
        pattern = _str_field(action, "pattern")
        url = _str_field(action, "url")
        if pattern and url:
            title = f'"{pattern}" in {url}'
        elif pattern:
            title = f'"{pattern}"'
        else:
            title = url or query or "page"
        return (title, "find_in_page")
    # "other", absent or an unknown future type.
    if query:
        return (query, "search")
    return ("web search", "other")


@dataclass(frozen=True, slots=True)
class _TodoSummary:
    done: int
    total: int
    next_text: str | None


def _summarize_todo_list(items: Any) -> _TodoSummary:
    if not isinstance(items, list):
        return _TodoSummary(done=0, total=0, next_text=None)

    done = 0
    total = 0
    next_text: str | None = None

    for raw_item in items:
        if isinstance(raw_item, codex_schema.TodoItem):
            total += 1
            if raw_item.completed:
                done += 1
                continue
            if next_text is None:
                next_text = raw_item.text
            continue
        if not isinstance(raw_item, dict):
            continue
        total += 1
        completed = raw_item.get("completed") is True
        if completed:
            done += 1
            continue
        if next_text is None:
            text = raw_item.get("text")
            next_text = str(text) if text is not None else None

    return _TodoSummary(done=done, total=total, next_text=next_text)


def _todo_title(summary: _TodoSummary) -> str:
    if summary.total <= 0:
        return "todo"
    if summary.next_text:
        return f"todo {summary.done}/{summary.total}: {summary.next_text}"
    return f"todo {summary.done}/{summary.total}: done"


@dataclass(frozen=True, slots=True)
class _AgentMessageSummary:
    text: str
    phase: str | None


def _select_final_answer(agent_messages: list[_AgentMessageSummary]) -> str | None:
    for message in reversed(agent_messages):
        if message.phase == "final_answer":
            return message.text
    for message in reversed(agent_messages):
        if message.phase in {None, ""}:
            return message.text
    return None


def _translate_item_event(
    phase: ActionPhase, item: codex_schema.ThreadItem, *, factory: EventFactory
) -> list[UntetherEvent]:
    match item:
        case codex_schema.AgentMessageItem(
            id=action_id,
            text=text,
            phase="commentary",
        ):
            detail = {"phase": "commentary"}
            if phase in {"started", "updated"}:
                return [
                    factory.action(
                        phase=phase,
                        action_id=action_id,
                        kind="note",
                        title=text,
                        detail=detail,
                    )
                ]
            if phase == "completed":
                return [
                    factory.action_completed(
                        action_id=action_id,
                        kind="note",
                        title=text,
                        detail=detail,
                        ok=True,
                    )
                ]
            return []
        case codex_schema.AgentMessageItem():
            return []
        case codex_schema.ErrorItem(id=action_id, message=message):
            if phase != "completed":
                return []
            # #987: a non-fatal warning (e.g. an ignored config key), not a
            # failed step — ok=True with a leading ⚠️ renders the ⚠️ as the
            # row's status instead of ✗ (#868).
            return [
                factory.action_completed(
                    action_id=action_id,
                    kind="warning",
                    title=f"\N{WARNING SIGN}\N{VARIATION SELECTOR-16} {message}",
                    detail={"message": message},
                    ok=True,
                    message=message,
                    level="warning",
                ),
            ]
        case codex_schema.CommandExecutionItem(
            id=action_id,
            command=command,
            exit_code=exit_code,
            status=status,
        ):
            title = relativize_command(command)
            if phase in {"started", "updated"}:
                return [
                    factory.action(
                        phase=phase,
                        action_id=action_id,
                        kind="command",
                        title=title,
                    )
                ]
            if phase == "completed":
                ok = status == "completed"
                if isinstance(exit_code, int):
                    ok = ok and exit_code == 0
                detail = {"exit_code": exit_code, "status": status}
                return [
                    factory.action_completed(
                        action_id=action_id,
                        kind="command",
                        title=title,
                        detail=detail,
                        ok=ok,
                    ),
                ]
        case codex_schema.McpToolCallItem(
            id=action_id,
            server=server,
            tool=tool,
            arguments=arguments,
            status=status,
            result=result,
            error=error,
        ):
            title = _short_tool_name(server, tool)
            detail: dict[str, Any] = {
                "server": server,
                "tool": tool,
                "status": status,
                "arguments": arguments,
            }

            if phase in {"started", "updated"}:
                return [
                    factory.action(
                        phase=phase,
                        action_id=action_id,
                        kind="tool",
                        title=title,
                        detail=detail,
                    )
                ]
            if phase == "completed":
                ok = status == "completed" and error is None
                if error is not None:
                    detail["error_message"] = str(error.message)
                result_summary = _summarize_tool_result(result)
                if result_summary is not None:
                    detail["result_summary"] = result_summary
                return [
                    factory.action_completed(
                        action_id=action_id,
                        kind="tool",
                        title=title,
                        detail=detail,
                        ok=ok,
                    ),
                ]
        case codex_schema.WebSearchItem(
            id=action_id, query=query, action=ws_action, results=results
        ):
            title, action_type = _web_search_title(query, ws_action)
            detail = {"query": query or "", "action_type": action_type}
            url = _str_field(ws_action, "url")
            if url:
                detail["url"] = url
            if isinstance(results, list):
                # Only the count — raw results (page content) are never copied.
                detail["result_count"] = len(results)
            if phase in {"started", "updated"}:
                return [
                    factory.action(
                        phase=phase,
                        action_id=action_id,
                        kind="web_search",
                        title=title,
                        detail=detail,
                    )
                ]
            if phase == "completed":
                return [
                    factory.action_completed(
                        action_id=action_id,
                        kind="web_search",
                        title=title,
                        detail=detail,
                        ok=True,
                    )
                ]
        case codex_schema.FileChangeItem(id=action_id, changes=changes, status=status):
            if phase != "completed":
                return []
            title = _format_change_summary(changes)
            normalized_changes = _normalize_change_list(changes)
            detail = {
                "changes": normalized_changes,
                "status": status,
                "error": None,
            }
            ok = status == "completed"
            return [
                factory.action_completed(
                    action_id=action_id,
                    kind="file_change",
                    title=title,
                    detail=detail,
                    ok=ok,
                )
            ]
        case codex_schema.TodoListItem(id=action_id, items=items):
            summary = _summarize_todo_list(items)
            title = _todo_title(summary)
            detail = {"done": summary.done, "total": summary.total}
            if phase in {"started", "updated"}:
                return [
                    factory.action(
                        phase=phase,
                        action_id=action_id,
                        kind="note",
                        title=title,
                        detail=detail,
                    )
                ]
            if phase == "completed":
                return [
                    factory.action_completed(
                        action_id=action_id,
                        kind="note",
                        title=title,
                        detail=detail,
                        ok=True,
                    )
                ]
        case codex_schema.ReasoningItem(id=action_id, text=text):
            if phase in {"started", "updated"}:
                return [
                    factory.action(
                        phase=phase,
                        action_id=action_id,
                        kind="note",
                        title=text,
                    )
                ]
            if phase == "completed":
                return [
                    factory.action_completed(
                        action_id=action_id,
                        kind="note",
                        title=text,
                        ok=True,
                    )
                ]
    return []


def translate_codex_event(
    event: codex_schema.ThreadEvent,
    *,
    title: str,
    factory: EventFactory,
    meta: dict[str, Any] | None = None,
) -> list[UntetherEvent]:
    match event:
        case codex_schema.ThreadStarted(thread_id=thread_id):
            logger.info("codex.session.started", session_id=thread_id)
            token = ResumeToken(engine=ENGINE, value=thread_id)
            return [factory.started(token, title=title, meta=meta)]
        case codex_schema.ItemStarted(item=item):
            return _translate_item_event("started", item, factory=factory)
        case codex_schema.ItemUpdated(item=item):
            return _translate_item_event("updated", item, factory=factory)
        case codex_schema.ItemCompleted(item=item):
            return _translate_item_event("completed", item, factory=factory)
        case _:
            logger.debug(
                "codex.event.unrecognised",
                event_type=type(event).__name__,
            )
            return []


@dataclass(slots=True)
class CodexRunState:
    factory: EventFactory
    note_seq: int = 0
    final_answer: str | None = None
    turn_agent_messages: list[_AgentMessageSummary] = field(default_factory=list)
    turn_index: int = 0
    # #987: Codex emits each config warning twice (two item ids, one message).
    seen_warnings: set[str] = field(default_factory=set)
    # The argv build_args produced for this run (the prompt goes via stdin),
    # kept for the #830 `codex.argv.rejected` diagnostic.
    argv: list[str] | None = None


class CodexRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: EngineId = ENGINE
    resume_re = _RESUME_RE
    model: str | None = None
    logger = logger

    def __init__(
        self,
        *,
        codex_cmd: str,
        extra_args: list[str],
        title: str = "Codex",
    ) -> None:
        self.codex_cmd = codex_cmd
        self.extra_args = extra_args
        self.session_title = title

    def command(self) -> str:
        return self.codex_cmd

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        run_options = get_run_options()
        args = [*self.extra_args]
        if run_options is not None:
            if run_options.model:
                args.extend(["--model", str(run_options.model)])
            if run_options.reasoning:
                args.extend(
                    [
                        "-c",
                        f"model_reasoning_effort={run_options.reasoning}",
                    ]
                )
        # No --ask-for-approval: `codex exec` never reads the root -a and forces
        # approval=never itself; `untrusted` is rejected from 0.149.0 (#830).
        args.extend(
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--color=never",
            ]
        )
        mode = run_options.permission_mode if run_options is not None else None
        if mode == CODEX_SAFE_PERMISSION_MODE:
            # Must sit before `resume`: `codex exec resume` has no --sandbox.
            args.extend(["--sandbox", _CODEX_SAFE_SANDBOX])
        elif mode is not None and mode not in _CODEX_FULL_AUTO_MODES:
            _warn_unknown_permission_mode(mode)
        if resume:
            if resume.is_continue:
                args.extend(["resume", "--last", "-"])
            else:
                args.extend(["resume", resume.value, "-"])
        else:
            args.append("-")
        if isinstance(state, CodexRunState):
            state.argv = list(args)
        return args

    def new_state(self, prompt: str, resume: ResumeToken | None) -> CodexRunState:
        return CodexRunState(factory=EventFactory(ENGINE))

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: CodexRunState,
    ) -> None:
        pass

    def decode_jsonl(self, *, line: bytes) -> codex_schema.ThreadEvent:
        return codex_schema.decode_event(line)

    def decode_error_events(
        self,
        *,
        raw: str,
        line: str,
        error: Exception,
        state: CodexRunState,
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

    def pipes_error_message(self) -> str:
        return "codex exec failed to open subprocess pipes"

    def translate(
        self,
        data: codex_schema.ThreadEvent,
        *,
        state: CodexRunState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        factory = state.factory
        match data:
            case codex_schema.StreamError(message=message):
                reconnect = _parse_reconnect_message(message)
                if reconnect is not None:
                    attempt, max_attempts = reconnect
                    phase: ActionPhase = "started" if attempt <= 1 else "updated"
                    return [
                        factory.action(
                            phase=phase,
                            action_id="codex.reconnect",
                            kind="note",
                            title=message,
                            detail={"attempt": attempt, "max": max_attempts},
                            level="info",
                        )
                    ]
                return [self.note_event(message, state=state, ok=False)]
            case codex_schema.TurnFailed(error=error):
                resume_for_completed = found_session or resume
                return [
                    factory.completed_error(
                        error=error.message,
                        answer=state.final_answer or "",
                        resume=resume_for_completed,
                    )
                ]
            case codex_schema.TurnStarted():
                action_id = f"turn_{state.turn_index}"
                state.turn_index += 1
                state.final_answer = None
                state.turn_agent_messages.clear()
                return [
                    factory.action_started(
                        action_id=action_id,
                        kind="turn",
                        title="turn started",
                    )
                ]
            case codex_schema.TurnCompleted(usage=usage):
                resume_for_completed = found_session or resume
                return [
                    factory.completed_ok(
                        answer=state.final_answer or "",
                        resume=resume_for_completed,
                        usage=msgspec.to_builtins(usage),
                    )
                ]
            case codex_schema.ItemCompleted(
                item=codex_schema.AgentMessageItem(text=text, phase=message_phase)
            ):
                state.turn_agent_messages.append(
                    _AgentMessageSummary(text=text, phase=message_phase)
                )
                selected = _select_final_answer(state.turn_agent_messages)
                if selected is not None:
                    state.final_answer = selected
                if len(state.turn_agent_messages) > 1:
                    logger.debug("codex.multiple_agent_messages")
            case codex_schema.ItemCompleted(
                item=codex_schema.ErrorItem(message=message)
            ):
                if message in state.seen_warnings:
                    return []
                state.seen_warnings.add(message)
            case _:
                pass

        # Build meta from runner config + run options.
        # Always include a model name — use override, runner config, or CLI default.
        meta: dict[str, Any] | None = None
        model = self.model
        run_options = get_run_options()
        if run_options is not None and run_options.model:
            model = run_options.model
        if model is None:
            model = "codex-mini-latest"
        meta = {"model": str(model)}
        if run_options is not None and run_options.reasoning:
            if meta is None:
                meta = {}
            meta["effort"] = run_options.reasoning
        if (
            run_options is not None
            and run_options.permission_mode == CODEX_SAFE_PERMISSION_MODE
        ):
            if meta is None:
                meta = {}
            meta["permissionMode"] = CODEX_SAFE_PERMISSION_MODE

        return translate_codex_event(
            data,
            title=self.session_title,
            factory=factory,
            meta=meta,
        )

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: CodexRunState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        parts = [f"codex exec failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        argv_error = _clap_argv_error_line(stderr_lines) if rc == 2 else None
        if argv_error is not None:
            logger.error(
                "codex.argv.rejected",
                rc=rc,
                first_error_line=argv_error,
                args=state.argv,
            )
        logger.error(
            "codex.process.failed",
            rc=rc,
            session_id=found_session.value if found_session else None,
        )
        resume_for_completed = found_session or resume
        return [
            self.note_event(
                message,
                state=state,
                ok=False,
            ),
            state.factory.completed_error(
                error=message,
                answer=state.final_answer or "",
                resume=resume_for_completed,
            ),
        ]

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: CodexRunState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        if not found_session:
            logger.warning("codex.stream.no_session")
            parts = ["codex exec finished but no session_id/thread_id was captured"]
            session = _session_label(None, resume)
            if session:
                parts.append(f"session: {session}")
            message = "\n".join(parts)
            resume_for_completed = resume
            return [
                state.factory.completed_error(
                    error=message,
                    answer=state.final_answer or "",
                    resume=resume_for_completed,
                )
            ]
        logger.info("codex.session.completed", resume=found_session.value)
        return [
            state.factory.completed_ok(
                answer=state.final_answer or "",
                resume=found_session,
            )
        ]


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    codex_cmd = "codex"

    extra_args_value = config.get("extra_args")
    if extra_args_value is None:
        extra_args = ["-c", "notify=[]"]
    elif isinstance(extra_args_value, list) and all(
        isinstance(item, str) for item in extra_args_value
    ):
        extra_args = list(extra_args_value)
    else:
        logger.warning(
            "codex.config.invalid",
            error="extra_args must be a list of strings",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `codex.extra_args` in {config_path}; expected a list of strings."
        )

    blocked = find_blocked_codex_args(extra_args)
    if blocked:
        # Flag names only — never the values (a `-c` value can hold a secret).
        logger.warning(
            "codex.config.invalid",
            error="blocked extra_args flag",
            flags=[hit.flag for hit in blocked],
            categories=[hit.category for hit in blocked],
            config_path=str(config_path),
        )
        raise BlockedExtraArgsError(format_blocked("codex", config_path, blocked))

    title = "Codex"
    profile_value = config.get("profile")
    if profile_value:
        if not isinstance(profile_value, str):
            logger.warning(
                "codex.config.invalid",
                error="profile must be a string",
                config_path=str(config_path),
            )
            raise ConfigError(
                f"Invalid `codex.profile` in {config_path}; expected a string."
            )
        extra_args.extend(["--profile", profile_value])
        title = profile_value

    return CodexRunner(codex_cmd=codex_cmd, extra_args=extra_args, title=title)


BACKEND = EngineBackend(
    id="codex",
    build_runner=build_runner,
    install_cmd="npm install -g @openai/codex",
)
