from __future__ import annotations

import contextlib
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..context import WEBHOOK_TRIGGER_PREFIX
from ..events import EventFactory
from ..logging import get_logger
from ..model import ActionPhase, EngineId, ResumeToken, UntetherEvent
from ..runner import (
    _URL_RE,
    PRESPAWN_BLOCKED_KEY,
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
    _stderr_excerpt,
)
from ..schemas import codex as codex_schema
from ..utils import engine_argv
from ..utils.env_policy import (
    NETWORK_ENV_NAMES,
    filtered_env,
    load_env_extras,
    log_dropped_once,
    log_user_extensions_once,
)
from ..utils.paths import get_run_base_dir, relativize_command, relativize_path
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
from .run_options import (
    CODEX_APP_SERVER_ONLY_MODES,
    CODEX_PERMISSION_MODES_APP_SERVER,
    EngineRunOptions,
    get_run_options,
)

logger = get_logger(__name__)

ENGINE: EngineId = "codex"

# #1241: Codex-only env names on top of the global allowlist (codex-cli
# 0.170.0). ``CODEX_*`` covers CODEX_HOME, CODEX_SQLITE_HOME, CODEX_API_KEY,
# CODEX_ACCESS_TOKEN and CODEX_CA_CERTIFICATE; the ChatGPT login is found via
# HOME (~/.codex), and the keyring credential store needs D-Bus. The #962
# app-server child reuses these two tuples.
_CODEX_ENV_EXTRAS: tuple[str, ...] = (
    "RUST_LOG",
    "DBUS_SESSION_BUS_ADDRESS",
    "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_IDENTITY_TOKEN_FILE",
    "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
    *NETWORK_ENV_NAMES,
)
_CODEX_ENV_PREFIXES: tuple[str, ...] = ("CODEX_",)

__all__ = [
    "CODEX_SAFE_PERMISSION_MODE",
    "ENGINE",
    "CodexRunner",
    "find_blocked_codex_args",
    "find_exec_only_flag",
    "translate_codex_event",
]

_RESUME_RE = re.compile(r"(?im)^\s*`?codex\s+resume\s+(?P<token>[^`\s]+)`?\s*$")
# #1065: a Codex thread id (``thread.started.thread_id``, a UUID). `codex exec
# resume` (0.162.1) also takes a thread *name*, but an unknown name silently
# starts a new thread instead of failing, and Untether's session tracking
# keys on the UUID the run reports — so a name never resumed under Untether
# (it failed the "emitted session id … but expected …" check) and is refused
# before spawning instead. Recorded in docs/reference/runners/codex/.
_CODEX_THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# #1066: an absolute path inside a Codex warning's text (``user
# (/home/<user>/.codex/config.toml): `project` is ignored.``). Not preceded
# by a word character, ``~`` or ``.`` (so ``~/x``, ``a/b`` and ``./x`` are
# left alone); stops at the punctuation that wraps a path in prose.
_WARNING_PATH_RE = re.compile(r"(?<![\w~.])/[^\s'\"`<>()\[\]{},;:]+")
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
# #1142: (trigger, mode) pairs whose webhook run was moved to ``safe``.
_WEBHOOK_UNKNOWN_PM_WARNED: set[tuple[str, str]] = set()
_WEBHOOK_UNKNOWN_PM_WARNED_MAX = 256


def _is_unknown_permission_mode(mode: str | None) -> bool:
    # The app-server tiers (#421) are known on exec too: refused before
    # spawn there, never run as something else.
    return mode is not None and mode not in CODEX_PERMISSION_MODES_APP_SERVER


CODEX_MODE_NEEDS_APP_SERVER_BLOCK = "codex_mode_needs_app_server"
# #421: tier labels (DEC-5 wording), for the footer and the exec refusal.
CODEX_MODE_LABELS: dict[str, str] = {
    "auto": "Full auto",
    "safe": "Safe",
    "ask": "Ask me",
    "ask-writes": "Ask before writes",
    "review": "Auto-review",
}


def codex_mode_needs_app_server_event(
    mode: str | None, resume: ResumeToken | None
) -> UntetherEvent | None:
    """#421: an app-server-only tier on the exec transport is refused before
    spawn, naming the fix; never a silent downgrade. Keeps the session."""
    if mode not in CODEX_APP_SERVER_ONLY_MODES:
        return None
    logger.warning("codex.permission_mode.needs_app_server", mode=mode)
    return EventFactory(ENGINE).completed_error(
        error=(
            f"\N{OCTAGONAL SIGN} Codex's {CODEX_MODE_LABELS[mode]} mode needs the"
            ' app-server transport. Set `transport = "app-server"` under'
            " `[engines.codex]`, or pick Full auto or Safe in /config →"
            " Approval policy."
        ),
        resume=resume,
        usage={PRESPAWN_BLOCKED_KEY: CODEX_MODE_NEEDS_APP_SERVER_BLOCK},
    )


def _webhook_safe_trigger(options: EngineRunOptions | None) -> str | None:
    """#1142: the webhook trigger when an unknown mode must run ``safe``.

    Unknown modes fail open (full auto) for attended runs and crons; a
    webhook's prompt is built from its payload, so it fails closed instead.
    """
    if options is None or not _is_unknown_permission_mode(options.permission_mode):
        return None
    trigger = options.unattended_trigger
    if trigger is None or not trigger.startswith(WEBHOOK_TRIGGER_PREFIX):
        return None
    return trigger


def _warn_webhook_unknown_mode(trigger: str, mode: str) -> None:
    key = (trigger, mode)
    if key in _WEBHOOK_UNKNOWN_PM_WARNED:
        return
    if len(_WEBHOOK_UNKNOWN_PM_WARNED) >= _WEBHOOK_UNKNOWN_PM_WARNED_MAX:
        _WEBHOOK_UNKNOWN_PM_WARNED.clear()
    _WEBHOOK_UNKNOWN_PM_WARNED.add(key)
    logger.warning(
        "codex.webhook_unknown_mode_safe",
        trigger=trigger,
        mode=mode,
        note="unknown Codex permission_mode on a webhook run runs as safe "
        "(--sandbox read-only), not full auto",
    )


def _warn_unknown_permission_mode(mode: str) -> None:
    """Once per distinct value per process: an unknown Codex mode runs full auto."""
    if mode in _UNKNOWN_PM_WARNED:
        return
    _UNKNOWN_PM_WARNED.add(mode)
    logger.warning(
        "codex.permission_mode.unknown",
        mode=mode,
        note="unknown Codex permission_mode runs as full auto; valid: auto, safe",
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
_CODEX_REMOTE_HINT = (
    "runs Codex on another app server; Untether can't apply its permission modes there"
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
    # 0.169.0, exec-only: persists project trust for future chats (#1040)
    "--trust": (
        "workspace",
        "saves project trust for this and future Codex chats and is not accepted"
        ' in `extra_args`; set `[projects."<path>"] trust_level` in'
        " `~/.codex/config.toml` yourself",
    ),
    # 0.171.0 (#1394): exec accepts them too; the run would use another app
    # server's config, sandbox, approvals and credentials, outside Untether.
    "--remote": ("workspace", _CODEX_REMOTE_HINT),
    "--remote-auth-token-env": ("workspace", _CODEX_REMOTE_HINT),
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


# #962: `codex app-server --help` (0.170.0) options a user may set; the rest
# are transport/auth plumbing Untether owns, or exec-only.
_APP_SERVER_ALLOWED_FLAGS = frozenset(
    {"--config", "--enable", "--disable", "--strict-config"}
)


def find_non_app_server_args(extra_args: list[str]) -> list[BlockedArg]:
    """Flags `transport = "app-server"` refuses: everything but the allow-list."""
    return dedupe_hits(
        [
            BlockedArg(
                flag=tok.flag,
                category="managed",
                hint='is not accepted with `transport = "app-server"`; only'
                " `-c`, `--enable`, `--disable` and `--strict-config` are",
            )
            for tok in iter_option_tokens(
                extra_args,
                short_aliases=_CODEX_SHORT_ALIASES,
                short_value_flags=_CODEX_SHORT_VALUE_FLAGS,
            )
            if tok.flag not in _APP_SERVER_ALLOWED_FLAGS
        ]
    )


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
            message = _warning_display_text(message)
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


def _warning_display_text(message: str) -> str:
    """#1066: a Codex warning as shown in Telegram — no home or host paths.

    A path inside the run's project becomes relative, one under the home
    directory becomes ``~/…`` (so ``~/.codex/config.toml`` still tells the
    user which file to fix), any other absolute path ``[path]``, and URLs
    ``[url]`` — the same treatment engine stderr gets (``_sanitise_stderr``,
    #208 / #1033), kept readable for a row the user acts on.
    """
    text = _URL_RE.sub("[url]", message)
    home = os.path.expanduser("~")

    def _replace(match: re.Match[str]) -> str:
        path = match.group(0)
        if path.count("/") < 2:
            return path  # ``/help``-style words, not a filesystem path
        relative = relativize_path(path)
        if relative != path:
            return relative
        if home not in ("", "/") and (path == home or path.startswith(f"{home}/")):
            return f"~{path[len(home) :]}"
        return "[path]"

    return _WARNING_PATH_RE.sub(_replace, text)


# #1240: codex-cli >= 0.169.0 prints this on stderr only, before the JSON
# stream, when an untrusted project has a .codex/ folder. Match the stable
# prefix; the rest of the sentence changes between versions.
_UNTRUSTED_PROJECT_PREFIX = "warning: Untrusted project-local configuration is disabled"
_UNTRUSTED_PROJECT_NOTE = (
    "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Codex skipped this project's"
    " `.codex/` settings (MCP servers, hooks, rules) because the project isn't"
    " trusted. Untether doesn't trust projects for you: add"
    ' `[projects."<repo root>"] trust_level = "trusted"` to'
    " `~/.codex/config.toml`."
)


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
    # #1240: Codex said on stderr that it disabled the project's .codex/ layer.
    untrusted_project_config: bool = False
    untrusted_note_sent: bool = False
    # #1360: the started event named a model (else one may follow later).
    model_shown: bool = False


class CodexRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: EngineId = ENGINE
    resume_re = _RESUME_RE
    model: str | None = None
    logger = logger
    # How failures name the process ("codex app-server" on #962's transport).
    process_label = "codex exec"
    # #421: which tiers /config offers and whether diff preview applies.
    transport = "exec"

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

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        # #421: exec can't ask, so the prompting tiers are refused here.
        refusal = codex_mode_needs_app_server_event(self._permission_mode(), resume)
        if refusal is not None:
            yield refusal
            return
        async with contextlib.aclosing(
            JsonlSubprocessRunner.run_impl(self, prompt, resume)
        ) as events:
            async for evt in events:
                yield evt

    def check_resume_value(self, value: Any) -> str:
        """#1065: only a thread UUID — anything else would start an orphan
        thread (see ``_CODEX_THREAD_ID_RE``)."""
        engine_argv.check_resume(value)
        if _CODEX_THREAD_ID_RE.fullmatch(value) is None:
            raise engine_argv.EngineArgvError("resume", "not_a_session")
        return value

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
                # #1034: validated + joined, never a separate value token.
                args.append(
                    engine_argv.joined(
                        "--model", engine_argv.check_model(str(run_options.model))
                    )
                )
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
        raw_mode = run_options.permission_mode if run_options is not None else None
        webhook = _webhook_safe_trigger(run_options)
        if webhook is not None and raw_mode is not None:
            _warn_webhook_unknown_mode(webhook, raw_mode)
        mode = self._permission_mode()
        if mode == CODEX_SAFE_PERMISSION_MODE:
            # Must sit before `resume`: `codex exec resume` has no --sandbox.
            args.extend(["--sandbox", _CODEX_SAFE_SANDBOX])
        elif mode is not None and _is_unknown_permission_mode(mode):
            _warn_unknown_permission_mode(mode)
        if resume:
            if resume.is_continue:
                args.extend(["resume", "--last", "-"])
            else:
                # #1034: the session id is positional, so there is no joined
                # form — clap (0.160.1) reads a dash-leading one as an option
                # of `exec resume`. Validation is the whole defence here.
                args.extend(["resume", self.check_resume_value(resume.value), "-"])
        else:
            args.append("-")
        if isinstance(state, CodexRunState):
            state.argv = list(args)
        return args

    def _permission_mode(self) -> str | None:
        """The run's Codex mode: the per-chat (or cron) value, except that a
        webhook with an unknown mode runs ``safe`` (#1142)."""
        run_options = get_run_options()
        if _webhook_safe_trigger(run_options) is not None:
            return CODEX_SAFE_PERMISSION_MODE
        return run_options.permission_mode if run_options is not None else None

    def new_state(self, prompt: str, resume: ResumeToken | None) -> CodexRunState:
        return CodexRunState(factory=EventFactory(ENGINE))

    def _late_model_meta(
        self, state: CodexRunState, found_session: ResumeToken | None
    ) -> list[UntetherEvent]:
        """#1360: the first run's warm-up read usually lands after
        ``thread.started``; a supplementary started event carries the model
        once it is known (3-event contract allows meta-only repeats)."""
        if state.model_shown or found_session is None:
            return []
        model = self._codex_config_model()
        if not model:
            return []
        state.model_shown = True
        return [
            state.factory.started(
                found_session, title=self.session_title, meta={"model": model}
            )
        ]

    def _codex_config_model(self) -> str | None:
        """#1360: the model Codex runs with no chat override, from the cached
        config/read + model/list for this run's cwd. Peek only."""
        if "--profile" in self.extra_args:
            # shortcut: a profile can name its own model, and the read has no
            # --profile (DEC-19); show no model rather than a wrong one.
            return None
        from . import codex_reads

        return codex_reads.peek_effective_model(
            self.codex_cmd, str(get_run_base_dir() or Path.cwd())
        )

    def on_spawned(self, *, state: Any, pid: int) -> None:
        # DEC-27 (#966/#846): fill the quota and catalogue caches in the
        # background once the run's own process exists; never awaited.
        from . import codex_reads

        codex_reads.maybe_warm_up(self, str(get_run_base_dir() or Path.cwd()))

    def on_stderr_line(self, line: str, *, state: Any, proc: Any) -> None:
        # #1240: Untether never grants trust (no `--trust`); it only reports it.
        if state.untrusted_project_config or not line.startswith(
            _UNTRUSTED_PROJECT_PREFIX
        ):
            return
        state.untrusted_project_config = True
        logger.info("codex.project_config_untrusted", cwd=str(get_run_base_dir()))

    def _untrusted_note(self, state: CodexRunState) -> list[UntetherEvent]:
        """The #1240 ⚠️ row, at most once per run."""
        if not state.untrusted_project_config or state.untrusted_note_sent:
            return []
        state.untrusted_note_sent = True
        return [
            state.factory.action_completed(
                action_id="codex.untrusted_project",
                kind="warning",
                title=_UNTRUSTED_PROJECT_NOTE,
                ok=True,
                level="warning",
            )
        ]

    def env(self, *, state: Any) -> dict[str, str] | None:
        """#1241: the allowlisted env, not Untether's whole service env.
        Codex's own ``shell_environment_policy`` stays the user's second layer."""
        user_exact, user_prefix = load_env_extras()
        log_user_extensions_once(user_exact, user_prefix)
        env = filtered_env(
            extra_allow=(*user_exact, *_CODEX_ENV_EXTRAS),
            extra_prefix=(*user_prefix, *_CODEX_ENV_PREFIXES),
        )
        log_dropped_once(ENGINE, os.environ, env)
        return env

    @staticmethod
    def _untrusted_usage(
        state: CodexRunState, usage: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """#1240: rows aren't kept in the final, so the bridge's footer line
        reads this flag from ``usage["project_config_untrusted"]``."""
        if not state.untrusted_project_config:
            return usage
        return {**(usage or {}), "project_config_untrusted": True}

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
        return f"{self.process_label} failed to open subprocess pipes"

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
                    *self._untrusted_note(state),
                    *self._late_model_meta(state, found_session),
                    factory.completed_error(
                        error=error.message,
                        answer=state.final_answer or "",
                        resume=resume_for_completed,
                        usage=self._untrusted_usage(state, None),
                    ),
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
                    *self._untrusted_note(state),
                    *self._late_model_meta(state, found_session),
                    factory.completed_ok(
                        answer=state.final_answer or "",
                        resume=resume_for_completed,
                        usage=self._untrusted_usage(state, msgspec.to_builtins(usage)),
                    ),
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

        # Build meta from run options. #1360: exec JSON never names the model,
        # so the footer shows the chat's override, else the model Codex's own
        # config/catalogue resolves to (cached read), else no model at all —
        # never a guess.
        meta: dict[str, Any] = {}
        run_options = get_run_options()
        model = (run_options.model if run_options is not None else None) or self.model
        if model is None and isinstance(data, codex_schema.ThreadStarted):
            model = self._codex_config_model()
        if model:
            meta["model"] = str(model)
            if isinstance(data, codex_schema.ThreadStarted):
                state.model_shown = True
        if run_options is not None and run_options.reasoning:
            meta["effort"] = run_options.reasoning
        if self._permission_mode() == CODEX_SAFE_PERMISSION_MODE:
            meta["permissionMode"] = CODEX_SAFE_PERMISSION_MODE

        events = translate_codex_event(
            data,
            title=self.session_title,
            factory=factory,
            meta=meta or None,
        )
        if isinstance(data, codex_schema.ThreadStarted):
            # #1240: the row goes right after StartedEvent (3-event contract).
            events.extend(self._untrusted_note(state))
        return events

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: CodexRunState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        parts = [f"{self.process_label} failed ({_rc_label(rc)})."]
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

    # #962: install-wide transport; exec stays the default (DEC-2).
    transport = config.get("transport", "exec")
    if transport not in ("exec", "app-server"):
        logger.warning(
            "codex.config.invalid",
            error="transport must be exec or app-server",
            config_path=str(config_path),
        )
        raise ConfigError(
            f'Invalid `codex.transport` in {config_path}; expected "exec" or'
            ' "app-server".'
        )
    app_server = transport == "app-server"

    blocked = find_blocked_codex_args(extra_args)
    if app_server:
        blocked = dedupe_hits([*blocked, *find_non_app_server_args(extra_args)])
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
    if profile_value and app_server:
        # DEC-19: `codex app-server` has no --profile; don't guess a mapping.
        logger.warning(
            "codex.config.invalid",
            error="profile is not supported on the app-server transport",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `codex.profile` in {config_path}: `codex app-server` has no"
            ' `--profile`. Remove it, or use `transport = "exec"`.'
        )
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

    if app_server:
        from .codex_app_server import CodexAppServerRunner  # imports this module

        return CodexAppServerRunner(codex_cmd=codex_cmd, extra_args=extra_args)
    return CodexRunner(codex_cmd=codex_cmd, extra_args=extra_args, title=title)


BACKEND = EngineBackend(
    id="codex",
    build_runner=build_runner,
    install_cmd="npm install -g @openai/codex",
    # #419: turn.completed.usage is the thread's total, across exec resume.
    usage_scope={"tokens": "thread_cumulative"},
)
