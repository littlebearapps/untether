"""Antigravity CLI (``agy``) runner (#558).

Rebuilt from contributor PR #766 (Manuel Naranjo): the schema, tool-name
mapping and decode handling are his; the integration is rewritten for
agy 1.3.x and Untether's safety rules.

One ``agy`` process per run. The prompt goes on **stdin** as a single
stream-json user line (never in argv), the stream comes back as
``{"event": "init" | "step_update" | "result" | "command_result", …}``:

- ``init`` — conversation id (absent on early errors; ``model`` from 1.3.2)
- ``step_update`` — ``step_type`` ``user_input`` / ``agent_response`` /
  ``tool`` / ``subagent`` / ``system_message`` / ``checkpoint`` / ``finish``
  / ``unknown`` with ``state`` ``ACTIVE`` / ``DONE`` / ``ERROR``
- ``result`` — ``status`` ``SUCCESS`` / ``ERROR``; ``usage``, ``num_turns``
  and ``duration_seconds`` are **session-cumulative** across resumes

Safety (phase 02, D21): ``--dangerously-skip-permissions`` is passed only
when a human explicitly chose **Full access** (``/config``, ``[antigravity]
permission_mode = "full"`` or a cron's own ``permission_mode``); unknown
values fail closed to Workspace, unattended runs never inherit Full access
(08 §10), and "Ask me" / "Plan first" are refused until Untether's approval
gate ships. The runner also refuses to run without a project directory or on
agy older than 1.3.1, cross-checks agy's own settings (``-p /config``) and the
reported ``init.permission_mode``, and warns about agy hooks, plugins and MCP
servers it doesn't manage (REVIEW-2 B2).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import msgspec

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..model import (
    Action,
    ActionKind,
    CompletedEvent,
    EngineId,
    ResumeToken,
    UntetherEvent,
)
from ..runner import (
    PRESPAWN_BLOCKED_KEY,
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
)
from ..schemas import antigravity as agy_schema
from ..utils import antigravity_quota, antigravity_scan
from ..utils.antigravity_redact import redact_agy_text
from ..utils.antigravity_state import (
    NOTICES_FILENAME,
    SEEN_CONFIG_FILENAME,
    NoticeStore,
    SeenConfigStore,
)
from ..utils.paths import get_run_base_dir, get_run_channel_id
from .run_options import ANTIGRAVITY_PERMISSION_MODES, get_run_options
from .tool_actions import tool_input_path, tool_kind_and_title

logger = get_logger(__name__)

ENGINE: EngineId = "antigravity"

# Conversation ids are UUIDs; require ≥ 8 chars so a stray word never parses.
_RESUME_RE = re.compile(
    r"(?im)^\s*`?(?:agy|antigravity)\s+--conversation\s+"
    r"(?P<token>[0-9A-Za-z][0-9A-Za-z_-]{7,})`?\s*$"
)

# Matched by ``runner_bridge._RESUME_FAILURE_RE`` ("antigravity conversation
# not found"), so the chat's dead session is cleared (#952; agy never reports
# a usable ``num_turns``).
CONVERSATION_GONE_TEXT = (
    "That Antigravity conversation no longer exists (antigravity conversation "
    "not found), so it wasn't resumed — send your message again to start a "
    "new one."
)
INTERRUPTED_TEXT = (
    "Antigravity was interrupted — the conversation can be resumed by "
    "replying to its resume line."
)
# Phase 03 (D27): distinct, non-retryable error cards for the stderr kill
# classes. None of them matches ``_RESUME_FAILURE_RE``, so the chat keeps its
# saved session.
AUTH_TEXT = (
    "Antigravity CLI isn't signed in on this host — agy wanted a browser "
    "sign-in, which a bot can't complete. Run `agy` once in a terminal on the "
    "host and finish the Google sign-in, then retry. On a keyring desktop, also "
    "check that `DBUS_SESSION_BUS_ADDRESS` reaches Untether. For servers, the "
    "Gemini API key route avoids sign-in (see the Antigravity runner docs)."
)
ACCOUNT_BLOCKED_TEXT = (
    "Google is asking this account to verify itself or appeal a Terms of "
    "Service block. Sign in with `agy` in a terminal on the host to see "
    "Google's link. Untether won't retry."
)
QUOTA_TEXT = (
    "This Antigravity quota is used up. /usage shows when each group resets; "
    "Untether won't retry."
)
CREDITS_TEXT = (
    "Antigravity's AI credits balance is too low to continue. Top up or wait "
    "for the quota reset (/usage)."
)
NO_PROJECT_TEXT = (
    "Antigravity needs a project — bind this chat with /ctx set … or a "
    "[projects.*] entry. It won't run in the bot's own directory, because it "
    "can edit files there."
)
NO_PROJECT_BLOCK = "no_project"
UNSUPPORTED_VERSION_BLOCK = "unsupported_version"
GATE_MISSING_BLOCK = "gate_missing"
CONFIG_CHANGED_BLOCK = "config_changed"
ALWAYS_PROCEED_BLOCK = "agy_always_proceed"

# PR #766's mapping onto the shared tool vocabulary (real parameter names).
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

_PATH_KEYS = (
    "TargetFile",
    "AbsolutePath",
    "DirectoryPath",
    "SearchPath",
    "SearchDirectory",
    "file_path",
    "path",
    "filePath",
)

# 1.3.1 drift 1: a soft-denied step is ``DONE`` with no output and no error;
# only ``result.denied_actions`` says it was denied. A DONE step of these
# families with empty output is parked until a later step or the result.
_DENIABLE_TOOLS = frozenset(
    {
        "run_command",
        "read_url_content",
        "search_web",
        "call_mcp_tool",
        "view_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "sed_file",
        "list_dir",
        "find_by_name",
        "grep_search",
    }
)
_FILE_TOOLS = frozenset(
    {
        "view_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "sed_file",
        "list_dir",
        "find_by_name",
        "grep_search",
    }
)
# ``result.denied_actions[].action`` → the tool names it covers (a trailing
# ``*`` is a prefix) and how Telegram names it.
_DENIED_FAMILY: dict[str, frozenset[str]] = {
    "command": frozenset({"run_command"}),
    "unsandboxed": frozenset({"run_command"}),
    "read_url": frozenset({"read_url_content"}),
    "execute_url": frozenset({"browser_*"}),
    "mcp": frozenset({"call_mcp_tool"}),
    "read_file": _FILE_TOOLS,
    "write_file": _FILE_TOOLS,
}
_DENIED_LABEL: dict[str, str] = {
    "command": "shell command",
    "unsandboxed": "shell command",
    "mcp": "MCP tool",
    "read_url": "web fetch",
    "execute_url": "browser action",
    "read_file": "file outside the project",
    "write_file": "file outside the project",
}

# ── stderr kill classes (phase 03, D27) ─────────────────────────────────────

# Only this much of a stderr line is matched (a hostile or chatty child can
# write arbitrarily long lines); an ``AGY_ERROR`` payload is parsed only up
# to this size.
_STDERR_HOOK_MAX_CHARS = 4096
_AGY_ERROR_MAX_CHARS = 16384
_STDERR_LOG_CHARS = 200
_ERROR_TEXT_LINES = 3

# Lines agy prints when it would otherwise wait (or retry) forever with
# ``--print-timeout 0``. The auth prefixes are anchored so an MCP server's
# own "authentication required" chatter can't stop the run.
_AUTH_PREFIXES = (
    "authentication required",
    "error: authentication required",
    "waiting for authentication",
)
_TOS_WORDS = ("block", "appeal", "violat", "suspend")
_KILL_TEXT: dict[str, str] = {
    "auth": AUTH_TEXT,
    "account_blocked": ACCOUNT_BLOCKED_TEXT,
    "quota": QUOTA_TEXT,
    "credits": CREDITS_TEXT,
    "conversation_gone": CONVERSATION_GONE_TEXT,
}
_KILL_DETECTED_EVENT: dict[str, str] = {
    "auth": "antigravity.auth.required",
    "account_blocked": "antigravity.account.blocked",
    "quota": "antigravity.quota.exhausted",
    "credits": "antigravity.credits.low",
}
_KILL_DONE_EVENT: dict[str, str] = {
    "auth": "antigravity.auth.killed",
    "account_blocked": "antigravity.account.killed",
    "quota": "antigravity.quota.killed",
    "credits": "antigravity.credits.killed",
    "conversation_gone": "antigravity.conversation.killed",
}


def stderr_kill_kind(line: str) -> str | None:
    """The D27 kill class a stderr line announces, or None."""
    lower = line[:_STDERR_HOOK_MAX_CHARS].strip().lower()
    if lower.startswith(_AUTH_PREFIXES):
        return "auth"
    if "verify your account" in lower or (
        "terms of service" in lower and any(w in lower for w in _TOS_WORDS)
    ):
        return "account_blocked"
    if "individual quota reached" in lower:
        return "quota"
    if "ai credits balance is too low" in lower:
        return "credits"
    if lower.startswith('warning: conversation "') and '" not found' in lower:
        return "conversation_gone"
    return None


# Every piece of agy text that reaches a Telegram card or an INFO+ log goes
# through ``redact_agy_text`` (utils/antigravity_redact.py) — never the shared
# stderr sanitiser, which lets a URL's query string through. Redact first,
# truncate afterwards. ``tests/test_antigravity_redact.py`` fails if a new
# surface bypasses it.
_STDERR_EXCERPT_CHARS = 300


def _agy_stderr_excerpt(lines: list[str] | None) -> str | None:
    """The first ~300 chars of captured stderr for an error card."""
    if not lines:
        return None
    text = redact_agy_text("\n".join(line[:_STDERR_HOOK_MAX_CHARS] for line in lines))
    if len(text) > _STDERR_EXCERPT_CHARS:
        text = text[:_STDERR_EXCERPT_CHARS] + "…"
    return text


def _first_argv_error(lines: list[str] | None) -> str | None:
    """agy's (Go flag package) unknown-flag line, redacted, or None."""
    for line in lines or ():
        if "flags provided but not defined" in line:
            return redact_agy_text(line[:_STDERR_HOOK_MAX_CHARS]).strip()[
                :_STDERR_LOG_CHARS
            ]
    return None


def _trim_error_lines(text: str, limit: int = _ERROR_TEXT_LINES) -> str:
    """agy's own error text (already redacted by ``_error_text``), first
    *limit* lines + "…" (model/effort errors list every valid option)."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join([*lines[:limit], "…"])


def _log_scalar(value: Any) -> Any:
    """An ``AGY_ERROR`` field safe to log: short scalars only, sanitised."""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_agy_text(value)[:80]
    return None


# 08 §6 (#975): tools that can hold agy's single result while they run.
_BACKGROUND_TOOLS = frozenset({"run_command", "schedule", "manage_task"})
# 08 §6 (#975, D26/D27): agy holds its answer for a background task for at
# most 30 min (1.2.9); past that + 60 s grace the wait is no longer expected
# and ordinary liveness / stall handling resumes.
_AGY_BACKGROUND_CAP_S = 1860.0
# D33: the stream carries no ``WaitMsBeforeAsync``, so a background-capable
# step ACTIVE for longer than this is shown as a background row.
_BACKGROUND_TITLE_AFTER_S = 5.0
_BACKGROUND_TITLE_CHARS = 120
# Descendant PIDs handed to the #590 teardown sweep: bounded, and each one
# pinned by its /proc start time so a recycled PID is never signalled.
_ORPHAN_SNAPSHOT_MAX = 256
_DESCENDANT_SCAN_MAX = 1024
# Phase 05 (D29): a model id that already names its effort
# (``gemini-3.8-flash-high``); agy refuses a different ``--effort`` with it.
_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$")
_MODEL_ROW_CHARS = 80

_OUTPUT_PREVIEW_CHARS = 500
_ERROR_MESSAGE_CHARS = 300
_INVALID_LINE_CHARS = 200

# D31/D33: agy-only env names (keyring over D-Bus, the Enterprise ADC route,
# the API-key base URL). Passed as per-runner extras so no other engine's
# environment changes. ``GEMINI_API_KEY``, ``GOOGLE_CLOUD_LOCATION`` and
# ``XDG_RUNTIME_DIR`` are already global.
_AGY_ENV_EXTRAS: tuple[str, ...] = (
    "DBUS_SESSION_BUS_ADDRESS",
    "AGY_ADC_AUTH",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_GEMINI_BASE_URL",
)


# ── version guard (08 §4, #976) ─────────────────────────────────────────────

_MIN_AGY_VERSION: tuple[int, ...] = (1, 3, 1)
# The newest agy the fixtures and drift notes were checked against.
PROBED_CLI_VERSION = "1.3.2"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_VERSION_PROBE_TIMEOUT_S = 10.0
_VERSION_CACHE: dict[tuple[str, float], str | None] = {}


def parse_agy_version(output: str) -> tuple[int, ...] | None:
    """``agy --version`` output (a bare ``1.3.1``) → ``(1, 3, 1)``, or None."""
    match = _VERSION_RE.search(output or "")
    if match is None:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


def _run_agy_version(path: str) -> str | None:
    """Run ``<agy> --version`` (no model call, no quota). None on failure."""
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


# Indirection so tests stub the probe (``tests/conftest.py``) without
# losing the real implementation.
_probe_agy_version = _run_agy_version


def _cache_key(cmd: str) -> tuple[str, float] | None:
    path = shutil.which(cmd) or (cmd if os.path.isabs(cmd) else None)
    if path is None:
        return None
    try:
        real = os.path.realpath(path)
        return (real, os.stat(real).st_mtime)
    except OSError:
        return None


def agy_cli_version(cmd: str) -> str | None:
    """The installed agy's version, probed once per (binary, mtime).

    agy self-updates in place, so the mtime key picks up an update.
    Blocking: call it off the event loop.
    """
    key = _cache_key(cmd)
    if key is None:
        return None
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    output = _probe_agy_version(key[0])
    if output is None:
        # Not cached: a transient failure re-probes next run.
        logger.warning("antigravity.version.probe_failed", cli_path=key[0])
        return None
    parsed = parse_agy_version(output)
    version = ".".join(str(n) for n in parsed) if parsed is not None else output
    _VERSION_CACHE[key] = version
    logger.info("antigravity.version.probe", cli_path=key[0], version=version)
    return version


def cached_agy_version(cmd: str) -> str | None:
    """The cached version for ``cmd`` (never spawns), or None."""
    key = _cache_key(cmd)
    return _VERSION_CACHE.get(key) if key is not None else None


def unsupported_version_message(version: str) -> str:
    minimum = ".".join(str(n) for n in _MIN_AGY_VERSION)
    return (
        f"🛑 Antigravity CLI {version} is older than {minimum}, which this "
        "Untether version needs. Run `agy update` on the host, then retry."
    )


# ── permission modes (phase 02, D21; 08 §10) ────────────────────────────────

BYPASS_FLAG = "--dangerously-skip-permissions"
_GATE_MODES = frozenset({"ask", "plan"})
_GATE_MODE_NAMES = {"ask": "Ask me", "plan": "Plan first"}

# agy ``toolPermission`` / ``init.permission_mode`` values a Workspace run
# may proceed under. Anything else — ``always-proceed`` (D22.7),
# ``proceed-in-sandbox`` (auto-runs sandboxed shell) or a value a newer agy
# invents — fails closed: the label would otherwise promise "no shell".
_WORKSPACE_TOOL_PERMISSIONS = frozenset({"request-review", "strict"})
CONFIG_UNCHECKED_BLOCK = "config_unchecked"


def permission_override_message(value: str) -> str:
    return (
        f"agy's own settings change its permission checks (toolPermission: "
        f"{value}), so Untether won't run Workspace mode — set it back to "
        "request-review or pick Full access in /config."
    )


ALWAYS_PROCEED_TEXT = permission_override_message("always-proceed")

_UNKNOWN_MODE_WARNED: set[str] = set()
_UNATTENDED_DOWNGRADE_WARNED: set[tuple[str, str]] = set()
_WARNED_MAX = 256
_FULL_FROM_TOML_WARNED = False
_BYPASS_UNREPORTED_WARNED = False


def _reset_permission_warnings() -> None:
    """Tests: forget every warn-once marker."""
    global _FULL_FROM_TOML_WARNED, _BYPASS_UNREPORTED_WARNED
    _UNKNOWN_MODE_WARNED.clear()
    _UNATTENDED_DOWNGRADE_WARNED.clear()
    _FULL_FROM_TOML_WARNED = False
    _BYPASS_UNREPORTED_WARNED = False


def gate_missing_message(mode: str) -> str:
    name = _GATE_MODE_NAMES.get(mode, mode)
    return (
        f"{name} needs Untether's approval gate, which arrives in a later "
        "0.36.1 release — switch to Workspace or Full access in /config."
    )


def _warn_unknown_mode(value: str) -> None:
    """Once per distinct value per process: an unknown mode runs as
    Workspace (fails closed — unlike Codex, which fails open)."""
    if value in _UNKNOWN_MODE_WARNED:
        return
    if len(_UNKNOWN_MODE_WARNED) >= _WARNED_MAX:
        _UNKNOWN_MODE_WARNED.clear()
    _UNKNOWN_MODE_WARNED.add(value)
    logger.warning(
        "antigravity.permission_mode.unknown",
        value=value,
        note="unknown Antigravity permission_mode runs as Workspace; valid: "
        + ", ".join(sorted(ANTIGRAVITY_PERMISSION_MODES)),
    )


def _warn_full_access_from_toml(config_path: Path) -> None:
    """REVIEW-2 m6: a host-wide bypass default must show in the journal."""
    global _FULL_FROM_TOML_WARNED
    if _FULL_FROM_TOML_WARNED:
        return
    _FULL_FROM_TOML_WARNED = True
    logger.warning(
        "antigravity.full_access_from_toml",
        config_path=str(config_path),
        note=(
            "[antigravity] permission_mode = 'full' passes "
            "--dangerously-skip-permissions to every attended agy run that "
            "has no chat override"
        ),
    )


def _warn_bypass_unreported() -> None:
    """D22.7: we passed the bypass flag but agy reported request-review."""
    global _BYPASS_UNREPORTED_WARNED
    if _BYPASS_UNREPORTED_WARNED:
        return
    _BYPASS_UNREPORTED_WARNED = True
    logger.warning(
        "antigravity.permission_mode.bypass_unreported",
        note="--dangerously-skip-permissions passed but agy reported "
        "request-review; continuing",
    )


def _mode_label(mode: str) -> str:
    from ..telegram.commands._antigravity_mode_text import antigravity_mode_label

    return antigravity_mode_label(mode)


def _join_labels(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def denial_paragraph(labels: list[str], *, mode: str, trigger: str | None) -> str:
    """Why the run stopped and how to allow it — trigger-aware (audit §6)."""
    what = f"⚠️ Antigravity was blocked from using a {_join_labels(labels)}"
    rule = "add an allow rule such as `command(git status)` to agy's settings file"
    if mode == "full":
        return f"{what} by agy's own deny rules (`permissions.deny` in agy's settings file)."
    head = f"{what} — headless runs can't ask for approval, so it stopped there."
    if trigger is not None and trigger.startswith("webhook:"):
        return f"{head} Webhook runs never get Full access; {rule}, or run it from the chat."
    if trigger is not None:
        return (
            f'{head} To allow it, set `permission_mode = "full"` on this cron, '
            f"or {rule}."
        )
    return f"{head} To allow it: /config → Permission mode → Full access, or {rule}."


def _paths_text(paths: list[str], limit: int = 5) -> str:
    shown = ", ".join(paths[:limit])
    more = len(paths) - limit
    return f"{shown} and {more} more" if more > 0 else shown


def config_changed_message(paths: list[str]) -> str:
    return (
        "agy's workspace config changed since someone last ran Antigravity in "
        f"this chat's project ({_paths_text(paths)}). Send any message in the "
        "chat to review it; the schedule runs again after that."
    )


def config_unchecked_message(reason: str) -> str:
    return (
        "Untether couldn't check agy's hooks, plugins and MCP servers in this "
        f"chat's project ({reason}), so this scheduled run is held. Send any "
        "message in the chat to look at the project, or trim its .agents/ "
        "folder."
    )


def config_unchecked_row(reason: str) -> str:
    return (
        "⚠️ Untether couldn't check this project's agy hooks, plugins and MCP "
        f"servers ({reason}). They run their own commands in every mode, "
        "including Workspace, and scheduled runs stay held until a check "
        "succeeds."
    )


OAUTH_NOTICE_TEXT = (
    "⚠️ This host signs Antigravity in with a Google account. Google's "
    "Antigravity terms say third-party tools such as Untether mustn't use "
    "that sign-in, and Google may suspend the account. A Gemini API key or "
    "Enterprise sign-in avoids this: "
    "https://littlebearapps.com/help/untether/switch-engines/ "
    "(Shown once in this chat.)"
)


@dataclass(slots=True)
class _RunPrecheck:
    """What ``run_impl`` learnt before spawn, handed to ``new_state``."""

    cwd: Path | None = None
    scan: antigravity_scan.ScanResult | None = None
    first_sight: bool = False  # show the "hooks … Untether doesn't manage" row
    # The planted-config scan failed or was truncated: show a ⚠️ row (never
    # record a digest as seen); unattended runs were already refused.
    scan_problem: str | None = None
    effective_mode: str = "workspace"  # set by new_state (for logs)
    config: antigravity_quota.AgyConfig | None = None
    config_key: tuple[str, str] | None = None  # (project root, settings digest)
    config_rows: list[tuple[str, str]] = field(default_factory=list)
    label_suffix: str = ""
    auth_route: str = "oauth"


# Set by ``run_impl`` right before it delegates to the base ``run_impl``,
# consumed (and cleared) by ``new_state`` in the same task.
_PRECHECK: contextvars.ContextVar[_RunPrecheck | None] = contextvars.ContextVar(
    "untether_agy_precheck", default=None
)


# ── one-time notices ────────────────────────────────────────────────────────

_TOS_NOTICE_LOGGED = False


def _log_tos_notice_once() -> None:
    """D6: log (once per process) where the ToS / login-route notes live.
    The chat-facing OAuth notice is phase 02's."""
    global _TOS_NOTICE_LOGGED
    if _TOS_NOTICE_LOGGED:
        return
    _TOS_NOTICE_LOGGED = True
    logger.info(
        "antigravity.tos_notice", docs="docs/reference/runners/antigravity/runner.md"
    )


# ── tool mapping ────────────────────────────────────────────────────────────


def _strip_cr(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _antigravity_tool_kind_and_title(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[ActionKind, str]:
    """Normalise agy tool names/params, then delegate to the shared helper."""
    if tool_name.startswith("browser_"):
        return "tool", f"browser: {tool_name.removeprefix('browser_')}"
    if tool_name == "call_mcp_tool":
        server = tool_input.get("ServerName") or tool_input.get("server_name")
        tool = tool_input.get("ToolName") or tool_input.get("tool_name")
        label = "/".join(str(p) for p in (server, tool) if p)
        return "tool", f"mcp: {label}" if label else "mcp tool"
    normalised = _TOOL_NAME_MAP.get(tool_name, tool_name.lower())
    params = dict(tool_input)
    if (
        normalised in {"bash", "shell"}
        and "command" not in params
        and "CommandLine" in params
    ):
        params["command"] = _strip_cr(str(params["CommandLine"]))
    if normalised in {"glob", "grep"} and "pattern" not in params:
        for key in ("Pattern", "Query"):
            if key in params:
                params["pattern"] = params[key]
                break
    if normalised == "websearch" and "query" not in params and "Query" in params:
        params["query"] = params["Query"]
    if normalised == "webfetch" and "url" not in params and "Url" in params:
        params["url"] = params["Url"]
    return tool_kind_and_title(
        normalised, params, path_keys=_PATH_KEYS, task_kind="subagent"
    )


def _subagent_title(info: dict[str, Any] | None) -> str:
    subagents = info.get("subagents") if isinstance(info, dict) else None
    first = subagents[0] if isinstance(subagents, list) and subagents else None
    if isinstance(first, dict):
        role = first.get("role")
        type_name = first.get("type_name")
        if role and type_name:
            return f"{type_name}: {role}"
        if role or type_name:
            return str(role or type_name)
    return "subagent"


def _error_text(error: Any) -> str | None:
    """A ``result.error`` of any shape → redacted text (None when
    absent/empty). The one place agy's own error text enters the runner."""
    if error is None or error == "":
        return None
    if isinstance(error, str):
        return redact_agy_text(error)
    if isinstance(error, dict):
        message = error.get("message") or error.get("error")
        if isinstance(message, str) and message:
            return redact_agy_text(message)
    try:
        return redact_agy_text(json.dumps(error, ensure_ascii=False))
    except (TypeError, ValueError):
        return redact_agy_text(str(error))


# ── state ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AntigravityStreamState:
    """Per-run state. Exposed as ``stream.engine_state`` (08 §2), so attribute
    names must not collide with the Claude probes the bridge duck-types
    (``tasks``, ``live_mode``, ``completed_turns`` …)."""

    factory: EventFactory
    note_seq: int = 0
    session_id: str | None = None  # init / step / result; never ""
    expected_resume: str | None = None  # resume.value when resuming by id
    conversation_missing: bool = False  # G8: init id != the resumed id
    init_model: str | None = None  # init.model (agy ≥ 1.3.2)
    text_by_step: dict[int, str] = field(default_factory=dict)
    pending_actions: dict[str, Action] = field(default_factory=dict)
    # DONE + empty output + deniable family → verdict unknown until a later
    # step (→ ok) or the result's ``denied_actions`` (→ denied).
    verdict_pending: dict[str, Action] = field(default_factory=dict)
    # 08 §6 (#975): step_index → monotonic time the background-capable tool
    # went ACTIVE. Cleared on its DONE/ERROR or at the result.
    bg_steps: dict[int, float] = field(default_factory=dict)
    # 08 §2/§6: descendant PIDs swept by manage_subprocess at teardown,
    # collected on ``run_command`` steps, with their /proc start times.
    orphan_pid_snapshot: list[int] = field(default_factory=list)
    orphan_pid_starttimes: dict[int, int] = field(default_factory=dict)
    # Background steps already retitled ``⏳ background: …`` (once each).
    bg_retitled: set[int] = field(default_factory=set)
    agy_pid: int | None = None
    # Phase 05: the ``--effort`` level actually passed (footer), and the one
    # ⚠️ row to show when a stored level was dropped for this model.
    effort: str | None = None
    effort_note: str | None = None
    agy_version: str | None = None
    t_spawn: float = 0.0
    t_init: float | None = None
    resumed: bool = False
    argv: list[str] | None = None
    # Phase 02: the mode agy actually runs in (``workspace`` / ``full`` in
    # rc1), its footer label, the unattended trigger (``cron:…`` /
    # ``webhook:…``) and what run_impl learnt before spawn.
    effective_mode: str = "workspace"
    mode_label: str = "workspace"
    unattended: str | None = None
    precheck: _RunPrecheck | None = None
    # D22.7: init reported always-proceed without our bypass flag → the run
    # was stopped; every later line is ignored.
    permission_refused: bool = False
    # Phase 03 (D27): a stderr line stopped agy (``auth`` / ``account_blocked``
    # / ``quota`` / ``credits`` / ``conversation_gone``); it decides the final
    # text whatever agy's result says.
    kill_reason: str | None = None
    kill_detected_at: float | None = None
    kill_logged: bool = False
    # First parseable ``AGY_ERROR: {json}`` stderr payload (keys unverified,
    # P19) and the one-shot stderr notices already logged.
    agy_error: dict[str, Any] | None = None
    stderr_flags: set[str] = field(default_factory=set)
    result_seen: bool = False

    def has_live_background_work(self) -> bool:
        """REVIEW-2 B1: answers ``runner_bridge.engine_background_busy``."""
        return bool(self.bg_steps)

    def background_count(self) -> int:
        return len(self.bg_steps)

    def awaiting_background(self) -> bool:
        """08 §6 (#975): agy is holding its answer for a background task —
        an expected wait for the liveness watchdog and the stall monitor.
        False once the oldest task is past agy's own cap (D27)."""
        if not self.bg_steps:
            return False
        oldest = min(self.bg_steps.values())
        return time.monotonic() - oldest < _AGY_BACKGROUND_CAP_S


# ── runner ──────────────────────────────────────────────────────────────────


def default_antigravity_cmd() -> str:
    """``agy`` on PATH, else ``~/.local/bin/agy`` if it exists, else ``agy``.

    Resolved lazily in ``build_runner`` (never at import time).
    """
    which_cmd = shutil.which("agy")
    if which_cmd:
        return which_cmd
    local_bin = Path.home() / ".local" / "bin" / "agy"
    if local_bin.exists():
        return str(local_bin)
    return "agy"


@dataclass(slots=True)
class AntigravityRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    """Runner for the Antigravity CLI (``agy``)."""

    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE
    antigravity_cmd: str = "agy"
    model: str | None = None
    # ``[antigravity] permission_mode`` (validated in build_runner).
    default_permission_mode: str | None = None
    # untether.toml; the small state files live beside chat_prefs.json.
    config_path: Path | None = None
    session_title: str = "antigravity"
    logger = logger
    _EXPOSE_ENGINE_STATE = True
    _seen_store: SeenConfigStore | None = field(default=None, init=False, repr=False)
    _notice_store: NoticeStore | None = field(default=None, init=False, repr=False)
    _config_rows_shown: set[tuple[str, str]] = field(
        default_factory=set, init=False, repr=False
    )

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`agy --conversation {token.value}`"

    def command(self) -> str:
        return self.antigravity_cmd

    def pipes_error_message(self) -> str:
        return "agy failed to open subprocess pipes"

    # -- spawn path ----------------------------------------------------------

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        # 08 §9 order. #838: the pre-spawn guard is the first statement.
        blocked = self._check_prespawn_ram_guard(resume)
        if blocked is not None:
            yield blocked
            return
        refusal = self._no_project_refusal(resume)
        if refusal is not None:
            yield refusal
            return
        gate_block = self._gate_missing_refusal(resume)
        if gate_block is not None:
            yield gate_block
            return
        version_block = await self._unsupported_version_event(resume)
        if version_block is not None:
            yield version_block
            return
        precheck_block, precheck = await self._precheck(resume)
        if precheck_block is not None:
            yield precheck_block
            return
        # Consumed by ``new_state`` inside the base run_impl (same task).
        _PRECHECK.set(precheck)
        _log_tos_notice_once()
        # Explicit parent ref: zero-arg super() breaks in @dataclass(slots=True).
        async with contextlib.aclosing(
            JsonlSubprocessRunner.run_impl(self, prompt, resume)
        ) as events:
            async for evt in events:
                if (
                    isinstance(evt, CompletedEvent)
                    and precheck is not None
                    and precheck.scan is not None
                ):
                    # Off the event loop: the re-scan is bounded but blocking.
                    rows: list[UntetherEvent] | None = None
                    with anyio.move_on_after(antigravity_scan.SCAN_TIMEOUT_S):
                        rows = await anyio.to_thread.run_sync(
                            self._config_change_rows,
                            precheck,
                            abandon_on_cancel=True,
                        )
                    if rows is None:
                        rows = [_recheck_warning("the check timed out")]
                    for row in rows:
                        yield row
                yield evt

    def _no_project_refusal(self, resume: ResumeToken | None) -> UntetherEvent | None:
        """REVIEW B1 / D16: agy edits files in its cwd, so never run it in the
        bot's own directory, the home directory or ``/``."""
        base = get_run_base_dir()
        reason = None
        if base is None:
            reason = "no_project"
        else:
            with contextlib.suppress(OSError):
                resolved = base.resolve()
                forbidden = {Path("/"), Path.home().resolve(), Path.cwd().resolve()}
                if resolved in forbidden:
                    reason = "forbidden_dir"
        if reason is None:
            return None
        logger.warning(
            "antigravity.no_project_dir",
            reason=reason,
            base=str(base) if base is not None else None,
        )
        return EventFactory(ENGINE).completed_error(
            error=NO_PROJECT_TEXT,
            resume=resume,
            usage={PRESPAWN_BLOCKED_KEY: NO_PROJECT_BLOCK},
        )

    # -- permission modes ---------------------------------------------------

    def _effective_mode(self) -> str:
        """The mode agy runs in: ``workspace`` / ``ask`` / ``plan`` / ``full``.

        Chat/topic override (or a cron's own mode) → ``[antigravity]
        permission_mode`` → Workspace. Unknown values fail **closed** to
        Workspace (warned once per value). 08 §10: an unattended run whose
        Full access isn't the trigger's own setting runs as Workspace.
        """
        options = get_run_options()
        raw = options.permission_mode if options is not None else None
        origin = "chat"
        if raw is None:
            raw, origin = self.default_permission_mode, "toml"
        if raw is None:
            mode = "workspace"
        elif raw in ANTIGRAVITY_PERMISSION_MODES:
            mode = raw
        else:
            _warn_unknown_mode(raw)
            mode = "workspace"
        trigger = options.unattended_trigger if options is not None else None
        if (
            trigger is not None
            and mode == "full"
            and (options is None or options.trigger_permission_mode != "full")
        ):
            key = (trigger, origin)
            if key not in _UNATTENDED_DOWNGRADE_WARNED:
                if len(_UNATTENDED_DOWNGRADE_WARNED) >= _WARNED_MAX:
                    _UNATTENDED_DOWNGRADE_WARNED.clear()
                _UNATTENDED_DOWNGRADE_WARNED.add(key)
                logger.warning(
                    "antigravity.unattended_full_downgraded",
                    trigger=trigger,
                    origin=origin,
                    note="unattended runs get Full access only from the "
                    "trigger's own permission_mode",
                )
            mode = "workspace"
        return mode

    def _gate_missing_refusal(self, resume: ResumeToken | None) -> UntetherEvent | None:
        """rc1: Ask me / Plan first need the approval gate (rc3) — refuse
        before spawn rather than run them as something else."""
        mode = self._effective_mode()
        if mode not in _GATE_MODES:
            return None
        logger.warning("antigravity.permission_mode.gate_missing", mode=mode)
        return EventFactory(ENGINE).completed_error(
            error=gate_missing_message(mode),
            resume=resume,
            usage={PRESPAWN_BLOCKED_KEY: GATE_MISSING_BLOCK},
        )

    def _seen(self) -> SeenConfigStore:
        if self._seen_store is None:
            path = (
                self.config_path.with_name(SEEN_CONFIG_FILENAME)
                if self.config_path is not None
                else None
            )
            self._seen_store = SeenConfigStore(path)
        return self._seen_store

    def _notices(self) -> NoticeStore:
        if self._notice_store is None:
            path = (
                self.config_path.with_name(NOTICES_FILENAME)
                if self.config_path is not None
                else None
            )
            self._notice_store = NoticeStore(path)
        return self._notice_store

    async def _precheck(
        self, resume: ResumeToken | None
    ) -> tuple[UntetherEvent | None, _RunPrecheck | None]:
        """Before spawn (08 §9 order): the planted-config scan and its
        unattended refusal (REVIEW-2 B2), then agy's own settings via the
        cached ``-p /config`` check (REVIEW-2 M5)."""
        mode = self._effective_mode()
        options = get_run_options()
        trigger = options.unattended_trigger if options is not None else None
        pre = _RunPrecheck()
        cwd = get_run_base_dir()
        if cwd is not None:
            pre.cwd = cwd
            try:
                # Bounded inside, but a read on a hung mount can't be
                # interrupted: abandon the thread and go on "not checked".
                with anyio.move_on_after(antigravity_scan.SCAN_TIMEOUT_S):
                    pre.scan = await anyio.to_thread.run_sync(
                        antigravity_scan.scan_workspace_config,
                        cwd,
                        abandon_on_cancel=True,
                    )
                if pre.scan is None:
                    pre.scan_problem = "the check timed out"
            except Exception as exc:  # noqa: BLE001 — fails closed below
                logger.warning(
                    "antigravity.workspace_config.scan_failed",
                    error_type=exc.__class__.__name__,
                )
                pre.scan_problem = "the check failed"
        scan = pre.scan
        if scan is not None and scan.unchecked_reason is not None:
            pre.scan_problem = scan.unchecked_reason
        if pre.scan_problem is not None:
            # Security review: an unfinished scan is "not checked", never a
            # stable digest — attended runs warn every time, unattended
            # runs are held.
            if trigger is not None:
                logger.warning(
                    "antigravity.workspace_config.unattended_refused",
                    trigger=trigger,
                    reason=pre.scan_problem,
                )
                return (
                    EventFactory(ENGINE).completed_error(
                        error=config_unchecked_message(pre.scan_problem),
                        resume=resume,
                        usage={PRESPAWN_BLOCKED_KEY: CONFIG_UNCHECKED_BLOCK},
                    ),
                    None,
                )
        elif (
            scan is not None
            and scan.agy
            and self._seen().get(scan.root) != scan.agy_digest
        ):
            if trigger is not None:
                paths = sorted(scan.agy)
                logger.warning(
                    "antigravity.workspace_config.unattended_refused",
                    trigger=trigger,
                    paths=paths[:20],
                    digest=scan.agy_digest[:12],
                )
                return (
                    EventFactory(ENGINE).completed_error(
                        error=config_changed_message(paths),
                        resume=resume,
                        usage={PRESPAWN_BLOCKED_KEY: CONFIG_CHANGED_BLOCK},
                    ),
                    None,
                )
            pre.first_sight = True
        config = await antigravity_quota.agy_config(self)
        pre.config = config
        pre.auth_route = antigravity_quota.auth_route(config)
        if config is None:
            return None, pre
        if (
            mode == "workspace"
            and config.tool_permission
            and config.tool_permission not in _WORKSPACE_TOOL_PERMISSIONS
        ):
            logger.error(
                "antigravity.permission_mode.mismatch",
                source="config",
                requested=mode,
                reported=config.tool_permission,
            )
            return (
                EventFactory(ENGINE).completed_error(
                    error=permission_override_message(config.tool_permission),
                    resume=resume,
                    usage={PRESPAWN_BLOCKED_KEY: ALWAYS_PROCEED_BLOCK},
                ),
                None,
            )
        self._note_config_widening(pre, mode)
        return None, pre

    def _note_config_widening(self, pre: _RunPrecheck, mode: str) -> None:
        config = pre.config
        if config is None:
            return
        widened = config.allow_non_workspace_access or config.allow_rules_count > 0
        if not widened:
            return
        if mode == "full":
            # Full access already allows everything: log only.
            logger.info(
                "antigravity.config.widened",
                key="full_access",
                allow_non_workspace_access=config.allow_non_workspace_access,
                count=config.allow_rules_count,
                permission_mode=mode,
            )
            return
        if config.allow_non_workspace_access:
            pre.label_suffix = " (agy allows files outside the project)"
        root = pre.scan.root if pre.scan is not None else pre.cwd
        pre.config_key = (str(root), config.digest)
        if pre.config_key in self._config_rows_shown:
            return
        if config.allow_non_workspace_access:
            logger.warning(
                "antigravity.config.widened",
                key="allowNonWorkspaceAccess",
                permission_mode=mode,
            )
            pre.config_rows.append(
                (
                    "antigravity.config.non_workspace",
                    "⚠️ agy's own settings let it read and write files outside "
                    "the project (allowNonWorkspaceAccess), so Workspace isn't "
                    "limited to this project.",
                )
            )
        if config.allow_rules_count:
            logger.info(
                "antigravity.config.widened",
                key="permissions.allow",
                count=config.allow_rules_count,
                permission_mode=mode,
            )
            pre.config_rows.append(
                (
                    "antigravity.config.allow_rules",
                    "⚠️ agy's own allow rules let it run "
                    f"{config.allow_rules_count} command pattern(s) without "
                    "asking (see agy's settings).",
                )
            )

    async def _unsupported_version_event(
        self, resume: ResumeToken | None
    ) -> UntetherEvent | None:
        version = await anyio.to_thread.run_sync(agy_cli_version, self.antigravity_cmd)
        parsed = parse_agy_version(version) if version else None
        if parsed is None:
            # Fail open: a slow or odd ``--version`` must never block a run;
            # a missing binary fails at spawn with the usual error.
            logger.warning(
                "antigravity.version.unknown", cmd=self.antigravity_cmd, version=version
            )
            return None
        if parsed < _MIN_AGY_VERSION:
            logger.error(
                "antigravity.version.unsupported",
                version=version,
                minimum=".".join(str(n) for n in _MIN_AGY_VERSION),
            )
            return EventFactory(ENGINE).completed_error(
                error=unsupported_version_message(str(version)),
                resume=resume,
                # Never ran the engine: keep the chat's saved session (#838).
                usage={PRESPAWN_BLOCKED_KEY: UNSUPPORTED_VERSION_BLOCK},
            )
        probed = parse_agy_version(PROBED_CLI_VERSION) or ()
        if parsed > probed:
            logger.info(
                "antigravity.version.newer_than_probed",
                version=version,
                probed=PROBED_CLI_VERSION,
            )
        return None

    def build_args(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> list[str]:
        # No prompt and no -p: the prompt goes on stdin. `--print-timeout 0`
        # is agy's default (unlimited) made explicit — on expiry agy reports
        # SUCCESS with a partial answer, so never rely on it (REVIEW m2).
        # `--disable-slash-commands` makes a `/`-leading prompt a normal turn
        # instead of an rc 2 exit (P22, D30).
        args = [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            "0",
            "--disable-slash-commands",
        ]
        if resume is not None:
            # #817: a /continue token has value "" — check it first.
            if resume.is_continue:
                args.append("--continue")
            else:
                args.extend(["--conversation", resume.value])
        model = self._model()
        if model:
            args.extend(["--model", model])
        effort = self._effort_arg(model, state)
        if effort:
            args.extend(["--effort", effort])
        # Phase 02 (D21): the bypass flag only for an explicit Full access.
        # Ask me / Plan first never get here in rc1 (refused before spawn);
        # anything else fails closed to Workspace (no flag).
        mode = self._effective_mode()
        if mode == "full":
            args.append(BYPASS_FLAG)
        if isinstance(state, AntigravityStreamState):
            state.effective_mode = mode
            state.argv = list(args)
        return args

    def _effort_arg(self, model: str | None, state: Any) -> str | None:
        """Phase 05 (D29): the ``--effort`` level to pass, if any.

        Only a level with an agy button (the executor already told the user
        about anything else, #416); never beside a model id that names its
        own effort; and never one the Effort page learnt this model refuses
        (``peek`` only — no probe per run). An unknown model passes it
        through: agy's own rc 1 maps to the error hint."""
        from ..telegram.engine_overrides import allowed_reasoning_levels
        from ..utils import antigravity_quota

        options = get_run_options()
        level = options.reasoning if options is not None else None
        if not level or level not in allowed_reasoning_levels(ENGINE):
            return None
        shown = (model or "")[:_MODEL_ROW_CHARS]
        if model and _EFFORT_SUFFIX_RE.search(model):
            logger.info("antigravity.effort.from_model_id", model=shown, effort=level)
            return None
        available = antigravity_quota.peek_model_efforts(self.antigravity_cmd, model)
        if available is not None and level not in available:
            logger.info("antigravity.effort.dropped", model=shown, effort=level)
            if isinstance(state, AntigravityStreamState):
                who = shown or "agy's default model"
                state.effort_note = (
                    f"⚠️ Effort {level} isn't available for {who}, so agy used"
                    " its default"
                )
            return None
        if isinstance(state, AntigravityStreamState):
            state.effort = level
        return level

    def stdin_payload(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: Any,
    ) -> bytes | None:
        line = {"event": "user", "message": {"content": prompt}}
        return (json.dumps(line, ensure_ascii=False) + "\n").encode()

    def env(self, *, state: Any) -> dict[str, str] | None:
        from ..utils.env_policy import (
            filtered_env,
            load_env_extras,
            log_user_extensions_once,
        )

        user_exact, user_prefix = load_env_extras()
        log_user_extensions_once(user_exact, user_prefix)
        env = filtered_env(
            extra_allow=(*user_exact, *_AGY_ENV_EXTRAS), extra_prefix=user_prefix
        )
        env.setdefault("NO_COLOR", "1")
        return env

    def new_state(
        self, prompt: str, resume: ResumeToken | None
    ) -> AntigravityStreamState:
        expected = (
            resume.value if resume is not None and not resume.is_continue else None
        )
        precheck = _PRECHECK.get()
        _PRECHECK.set(None)
        options = get_run_options()
        mode = self._effective_mode()
        label = _mode_label(mode)
        if precheck is not None:
            label += precheck.label_suffix
            precheck.effective_mode = mode
        return AntigravityStreamState(
            factory=EventFactory(ENGINE),
            expected_resume=expected or None,
            resumed=resume is not None,
            effective_mode=mode,
            mode_label=label,
            unattended=options.unattended_trigger if options is not None else None,
            precheck=precheck,
        )

    def start_run(
        self,
        prompt: str,
        resume: ResumeToken | None,
        *,
        state: AntigravityStreamState,
    ) -> None:
        state.t_spawn = time.monotonic()
        state.agy_version = cached_agy_version(self.antigravity_cmd)

    def on_spawned(self, *, state: Any, pid: int) -> None:
        if isinstance(state, AntigravityStreamState):
            state.agy_pid = pid

    # -- stderr (phase 03, D27) ---------------------------------------------

    def on_stderr_line(self, line: str, *, state: Any, proc: Any) -> None:
        """Kill agy on the stderr lines that announce a headless hang
        (signed out, account blocked, quota/credits used up, stale
        ``--conversation``) and note the rest. Never logs a raw line above
        DEBUG: the sign-in URL carries a PKCE challenge."""
        if not isinstance(state, AntigravityStreamState):
            return
        text = line[:_STDERR_HOOK_MAX_CHARS]
        kind = stderr_kill_kind(text)
        if kind is not None:
            self._stderr_kill(state, proc, kind)
            return
        stripped = text.strip()
        lower = stripped.lower()
        if stripped.startswith("AGY_ERROR:"):
            self._note_agy_error(state, line)
        elif lower.startswith("warning: unrecognized --mode value"):
            self._note_stderr_once(state, "antigravity.mode.rejected", stripped)
        elif lower.startswith("[agy] print timeout after"):
            # agy then reports SUCCESS with a partial answer (REVIEW m2).
            self._note_stderr_once(state, "antigravity.print_timeout", stripped)
        elif lower.startswith("jetski: no output produced"):
            state.stderr_flags.add("no_output")
            logger.debug("antigravity.stderr.no_output")
        elif lower.startswith("warning: ignoring unsupported stream input"):
            state.stderr_flags.add("stream_input_ignored")
            logger.debug("antigravity.stderr.stream_input_ignored")

    def _stderr_kill(self, state: AntigravityStreamState, proc: Any, kind: str) -> None:
        if state.kill_reason is not None or state.permission_refused:
            return
        state.kill_reason = kind
        state.kill_detected_at = time.monotonic()
        if kind == "conversation_gone":
            logger.warning(
                "antigravity.conversation.missing",
                expected=state.expected_resume,
                detected_by="stderr",
            )
        else:
            logger.warning(
                _KILL_DETECTED_EVENT[kind],
                pid=getattr(proc, "pid", None),
                elapsed_ms=self._kill_elapsed_ms(state),
            )
        if getattr(proc, "returncode", None) is None:
            self.kill_with_escalation(proc)

    def _note_stderr_once(
        self, state: AntigravityStreamState, event: str, text: str
    ) -> None:
        if event in state.stderr_flags:
            return
        state.stderr_flags.add(event)
        logger.warning(event, line=redact_agy_text(text)[:_STDERR_LOG_CHARS])

    def _note_agy_error(self, state: AntigravityStreamState, line: str) -> None:
        """``AGY_ERROR: {json}`` (rc 3, turn-level failure). Key names are
        unverified (P19): log key names and a few short scalars, never the
        payload."""
        payload = line.strip()[len("AGY_ERROR:") :].strip()
        parsed: Any = None
        if len(payload) <= _AGY_ERROR_MAX_CHARS:
            try:
                parsed = json.loads(payload)
            except (ValueError, RecursionError):
                parsed = None
        event = (
            "antigravity.stderr.post_result"
            if state.result_seen
            else "antigravity.agy_error"
        )
        if not isinstance(parsed, dict):
            state.stderr_flags.add("agy_error_unparsed")
            logger.warning(event, kind="agy_error", parsed=False)
            return
        if state.agy_error is None:
            state.agy_error = parsed
        fields = {
            key: _log_scalar(parsed.get(key)) for key in ("status", "code", "retryable")
        }
        log = logger.info if state.result_seen else logger.warning
        log(
            event,
            kind="agy_error",
            keys=sorted(str(k)[:40] for k in parsed)[:20],
            **fields,
        )

    @staticmethod
    def _kill_elapsed_ms(state: AntigravityStreamState) -> int | None:
        if not state.t_spawn or state.kill_detected_at is None:
            return None
        return int((state.kill_detected_at - state.t_spawn) * 1000)

    def _failure_text(
        self, state: AntigravityStreamState, error: str | None
    ) -> str | None:
        """The kill class's card (stderr kill wins over whatever agy's
        result says), else the auth card for agy's own auth failure."""
        kind = state.kill_reason
        if kind is not None:
            if not state.kill_logged:
                state.kill_logged = True
                logger.error(
                    _KILL_DONE_EVENT[kind],
                    elapsed_ms=self._kill_elapsed_ms(state),
                    session_id=state.session_id,
                )
            return _KILL_TEXT[kind]
        if error and "authentication failed or timed out" in error.lower():
            logger.error("antigravity.auth.failed", session_id=state.session_id)
            return AUTH_TEXT
        return None

    def _failure_completed(
        self,
        state: AntigravityStreamState,
        resume: ResumeToken | None,
        text: str,
        *,
        answer: str,
        usage: dict[str, Any] | None = None,
    ) -> list[UntetherEvent]:
        out = self._settle_open_actions(state, ok=False, message=state.kill_reason)
        if state.kill_reason == "conversation_gone" and state.expected_resume:
            state.conversation_missing = True
            token: ResumeToken | None = ResumeToken(
                engine=ENGINE, value=state.expected_resume
            )
            answer = ""
        else:
            token = self._resume_for_completed(state, resume)
        out.append(
            state.factory.completed_error(
                error=text, answer=answer, resume=token, usage=usage
            )
        )
        return out

    # -- decoding ------------------------------------------------------------

    def decode_jsonl(self, *, line: bytes) -> agy_schema.AntigravityEvent:
        try:
            return agy_schema.decode_event(line)
        except msgspec.DecodeError:
            text = line.decode("utf-8", errors="replace")
            brace = text.find("{")
            if brace > 0:
                return agy_schema.decode_event(text[brace:].encode("utf-8"))
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
            # Unknown event tags (a newer agy) are dropped, not shown.
            self.get_logger().warning(
                "jsonl.msgspec.invalid",
                tag=self.tag(),
                error=str(error),
                error_type=error.__class__.__name__,
            )
            return []
        return JsonlSubprocessRunner.decode_error_events(
            self, raw=raw, line=line, error=error, state=state
        )

    def invalid_json_events(
        self,
        *,
        raw: str,
        line: str,
        state: AntigravityStreamState,
    ) -> list[UntetherEvent]:
        message = "invalid JSON from antigravity; ignoring line"
        detail = {"line": raw[:_INVALID_LINE_CHARS]}
        return [self.note_event(message, state=state, detail=detail)]

    # -- translate -----------------------------------------------------------

    def _model(self) -> str | None:
        run_options = get_run_options()
        if run_options is not None and run_options.model:
            return str(run_options.model)
        return self.model

    def _meta(self, state: AntigravityStreamState) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        model = self._model() or state.init_model
        if model:
            meta["model"] = model
        if state.effort:  # the level actually passed (phase 05)
            meta["effort"] = state.effort
        meta["permissionMode"] = state.mode_label
        return meta

    def translate(
        self,
        data: agy_schema.AntigravityEvent,
        *,
        state: AntigravityStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        if state.conversation_missing or state.permission_refused:
            return []
        match data:
            case agy_schema.Init(conversation_id=cid, init=payload):
                if payload is not None and payload.model:
                    state.init_model = payload.model
                refused = self._check_reported_mode(
                    state, payload.permission_mode if payload else None, resume
                )
                if refused is not None:
                    return refused
                if not cid:
                    logger.warning("antigravity.init.no_conversation_id")
                    return []
                state.t_init = time.monotonic()
                return self._adopt_session(state, cid)
            case agy_schema.StepUpdate(step_update=su):
                if su is None:
                    return []
                out: list[UntetherEvent] = []
                if su.conversation_id and state.session_id is None:
                    out.extend(self._adopt_session(state, su.conversation_id))
                    if state.conversation_missing:
                        return out
                out.extend(self._translate_step(su, state))
                out.extend(self._background_title_updates(state))
                return out
            case agy_schema.AntigravityResult(result=res):
                if res is None:
                    return []
                return self._translate_result(res, state, resume)
            case _:
                # command_result (slash probes, 04/05) and the decode-only
                # ``error`` event carry nothing for a run.
                logger.debug(
                    "antigravity.event.ignored", event_type=type(data).__name__
                )
                return []

    def _adopt_session(
        self, state: AntigravityStreamState, cid: str
    ) -> list[UntetherEvent]:
        if state.session_id is not None:
            return []
        if state.expected_resume and cid != state.expected_resume:
            return self._conversation_gone(state, cid)
        state.session_id = cid
        logger.info(
            "antigravity.session.started",
            session_id=cid,
            resumed=state.resumed,
            agy_version=state.agy_version,
        )
        token = ResumeToken(engine=ENGINE, value=cid)
        return [
            state.factory.started(
                token, title=self.session_title, meta=self._meta(state)
            ),
            *self._startup_rows(state),
        ]

    def _check_reported_mode(
        self,
        state: AntigravityStreamState,
        reported: str | None,
        resume: ResumeToken | None,
    ) -> list[UntetherEvent] | None:
        """D22.7: agy's ``toolPermission: always-proceed`` setting stands in
        for the bypass flag. Without our flag, anything but agy's default or
        a stricter policy (fail closed: ``proceed-in-sandbox``, a value a
        newer agy invents) stops the run before any tool (``init`` comes
        ≈ 2.7 s before the first model output). A missing value can't be
        checked here; the ``-p /config`` check covers that host."""
        bypass = state.effective_mode == "full"
        if (
            not bypass
            and reported is not None
            and reported not in _WORKSPACE_TOOL_PERMISSIONS
        ):
            state.permission_refused = True
            logger.error(
                "antigravity.permission_mode.mismatch",
                source="init",
                requested=state.effective_mode,
                reported=reported,
            )
            if state.agy_pid is not None:
                with contextlib.suppress(OSError):
                    os.kill(state.agy_pid, signal.SIGTERM)
            keep = resume if resume is not None and not resume.is_continue else None
            return [
                state.factory.completed_error(
                    error=permission_override_message(reported), resume=keep
                )
            ]
        if bypass and reported == "request-review":
            _warn_bypass_unreported()
        return None

    def _warning_row(
        self,
        state: AntigravityStreamState,
        action_id: str,
        title: str,
        detail: dict[str, Any] | None = None,
    ) -> UntetherEvent:
        return _warning_event(state.factory, action_id, title, detail)

    def _startup_rows(self, state: AntigravityStreamState) -> list[UntetherEvent]:
        """⚠️ rows learnt before spawn, shown right after Started."""
        out: list[UntetherEvent] = []
        if state.effort_note is not None:
            out.append(
                self._warning_row(
                    state, "antigravity.effort.dropped", state.effort_note
                )
            )
            state.effort_note = None
        pre = state.precheck
        if pre is None:
            return out
        scan = pre.scan
        if pre.scan_problem is not None:
            out.append(
                self._warning_row(
                    state,
                    "antigravity.config.unchecked",
                    config_unchecked_row(pre.scan_problem),
                )
            )
        if pre.first_sight and scan is not None:
            pre.first_sight = False
            paths = sorted(scan.agy)
            logger.warning(
                "antigravity.workspace_config_present",
                paths=paths[:20],
                digest=scan.agy_digest[:12],
                permission_mode=state.effective_mode,
            )
            self._seen().set(scan.root, scan.agy_digest)
            out.append(
                self._warning_row(
                    state,
                    "antigravity.config.present",
                    "⚠️ This project has agy hooks, plugins or MCP servers that "
                    f"Untether doesn't manage ({_paths_text(paths)}). They run "
                    "their own commands in every mode, including Workspace. "
                    "Review them if you didn't add them.",
                    {"paths": paths},
                )
            )
        if pre.config_rows:
            out.extend(
                self._warning_row(state, action_id, title)
                for action_id, title in pre.config_rows
            )
            if pre.config_key is not None:
                if len(self._config_rows_shown) >= _WARNED_MAX:
                    self._config_rows_shown.clear()
                self._config_rows_shown.add(pre.config_key)
            pre.config_rows = []
        return out

    def _conversation_gone(
        self, state: AntigravityStreamState, cid: str
    ) -> list[UntetherEvent]:
        """G8: agy silently starts a new conversation for an unknown
        ``--conversation`` id. Stop it (the user's turn must not run in a
        conversation they didn't pick) and say so."""
        assert state.expected_resume is not None
        state.conversation_missing = True
        if state.kill_reason != "conversation_gone":
            logger.warning(
                "antigravity.conversation.missing",
                expected=state.expected_resume,
                got=cid,
                detected_by="init",
            )
        if state.agy_pid is not None:
            # agy answers SIGTERM with an "interrupted" result (rc 1).
            with contextlib.suppress(OSError):
                os.kill(state.agy_pid, signal.SIGTERM)
        old = ResumeToken(engine=ENGINE, value=state.expected_resume)
        return [state.factory.completed_error(error=CONVERSATION_GONE_TEXT, resume=old)]

    def _translate_step(
        self, su: agy_schema.StepUpdatePayload, state: AntigravityStreamState
    ) -> list[UntetherEvent]:
        factory = state.factory
        idx = su.step_index
        out: list[UntetherEvent] = []
        # A later step settles any parked verdict: the turn went on, so the
        # step was not denied (1.3.1 ends the turn at the first denial).
        if idx is not None and state.verdict_pending:
            for action_id in [a for a in state.verdict_pending if _step_of(a) < idx]:
                parked = state.verdict_pending.pop(action_id)
                out.append(self._complete(parked, state, ok=True))
        action_id = f"step-{idx}" if idx is not None else "step-?"
        step_type = su.step_type
        st = su.state
        terminal = st in {"DONE", "ERROR"}

        if step_type == "tool":
            out.extend(self._translate_tool(su, state, action_id))
            return out
        if step_type == "subagent":
            title = _subagent_title(su.subagent_info)
            if not terminal:
                action = Action(
                    id=action_id,
                    kind="subagent",
                    title=title,
                    detail={"tool_name": su.tool_name or "invoke_subagent"},
                )
                if action_id in state.pending_actions:
                    return out
                state.pending_actions[action_id] = action
                out.append(
                    factory.action_started(
                        action_id=action_id,
                        kind="subagent",
                        title=title,
                        detail=action.detail,
                    )
                )
                return out
            action = state.pending_actions.pop(action_id, None) or Action(
                id=action_id, kind="subagent", title=title, detail={}
            )
            out.append(self._complete(action, state, ok=st == "DONE"))
            return out
        if step_type == "agent_response":
            if su.text_delta and idx is not None:
                state.text_by_step[idx] = (
                    state.text_by_step.get(idx, "") + su.text_delta
                )
            return out
        # A tool step that turns into another type on completion (the
        # ``finish`` tool with --json-schema goes ACTIVE as ``tool``, DONE as
        # ``finish``) still closes its row.
        if terminal and action_id in state.pending_actions:
            action = state.pending_actions.pop(action_id)
            if idx is not None:
                state.bg_steps.pop(idx, None)
            out.append(self._complete(action, state, ok=st == "DONE"))
            return out
        # user_input (incl. a hook-injected one: not a new run), system_message,
        # checkpoint, unknown, finish, anything newer.
        logger.debug("antigravity.step.ignored", step_type=step_type, state=st)
        return out

    def _translate_tool(
        self,
        su: agy_schema.StepUpdatePayload,
        state: AntigravityStreamState,
        action_id: str,
    ) -> list[UntetherEvent]:
        factory = state.factory
        info = su.tool_info
        tool_name = (
            su.tool_name or (info.name if info and info.name else None) or "tool"
        )
        params = info.parameters if info and isinstance(info.parameters, dict) else {}
        idx = su.step_index
        if su.state not in {"DONE", "ERROR"}:
            kind, title = _antigravity_tool_kind_and_title(tool_name, params)
            detail: dict[str, Any] = {"tool_name": tool_name, "input": params}
            if kind == "file_change":
                path = tool_input_path(params, path_keys=_PATH_KEYS)
                if path:
                    detail["changes"] = [{"path": path, "kind": "update"}]
            if idx is not None and tool_name in _BACKGROUND_TOOLS:
                state.bg_steps.setdefault(idx, time.monotonic())
            if tool_name == "run_command":
                self._collect_descendants(state)
            if action_id in state.pending_actions:
                return []
            action = Action(id=action_id, kind=kind, title=title, detail=detail)
            state.pending_actions[action_id] = action
            return [
                factory.action_started(
                    action_id=action_id, kind=kind, title=title, detail=detail
                )
            ]
        if idx is not None:
            state.bg_steps.pop(idx, None)
        if tool_name == "run_command":
            # By DONE the background child exists (ACTIVE can precede it).
            self._collect_descendants(state)
        action = state.pending_actions.pop(action_id, None)
        if action is None:
            kind, title = _antigravity_tool_kind_and_title(tool_name, params)
            action = Action(
                id=action_id,
                kind=kind,
                title=title,
                detail={"tool_name": tool_name, "input": params},
            )
        if su.state == "ERROR":
            err = info.error if info is not None else None
            message = (err.message if err and err.message else "tool failed")[
                :_ERROR_MESSAGE_CHARS
            ]
            detail = dict(action.detail)
            if err is not None and err.type:
                detail["error_type"] = err.type
            action = Action(
                id=action.id, kind=action.kind, title=action.title, detail=detail
            )
            return [self._complete(action, state, ok=False, message=message)]
        output = info.output if info is not None else None
        if (output is None or output == "") and (
            tool_name in _DENIABLE_TOOLS or tool_name.startswith("browser_")
        ):
            state.verdict_pending[action_id] = action
            return []
        detail = dict(action.detail)
        if output is not None and output != "":
            text = _strip_cr(output if isinstance(output, str) else str(output))
            detail["output_preview"] = text[:_OUTPUT_PREVIEW_CHARS]
        action = Action(
            id=action.id, kind=action.kind, title=action.title, detail=detail
        )
        return [self._complete(action, state, ok=True)]

    @staticmethod
    def _collect_descendants(state: AntigravityStreamState) -> None:
        """D26 kill hygiene: remember agy's current descendants so the #590
        sweep reaches a background child that left agy's process group, on
        cancel and on clean exit. Only PIDs found under our own live agy
        process (``find_descendants``, depth 4) are recorded, each with its
        /proc start time — the sweep refuses a PID whose start time changed
        — and the list is capped. Best-effort; never raises."""
        pid = state.agy_pid
        if not pid or pid <= 0:
            return
        room = _ORPHAN_SNAPSHOT_MAX - len(state.orphan_pid_snapshot)
        if room <= 0:
            return
        try:
            from ..utils import proc_diag

            found = proc_diag.find_descendants(pid)[:_DESCENDANT_SCAN_MAX]
            for child in found:
                if room <= 0:
                    break
                if child == pid or child in state.orphan_pid_starttimes:
                    continue
                born = proc_diag.pid_starttime(child)
                if born is None:  # gone already, or no /proc: can't pin it
                    continue
                state.orphan_pid_snapshot.append(child)
                state.orphan_pid_starttimes[child] = born
                room -= 1
        except Exception:  # noqa: BLE001 — diagnostics must not break a run
            logger.debug("antigravity.descendants.scan_failed", exc_info=True)

    @staticmethod
    def _background_title_updates(
        state: AntigravityStreamState,
    ) -> list[UntetherEvent]:
        """08 §6: retitle a background-capable step that has been ACTIVE for
        more than 5 s as ``⏳ background: <command>``, once, when another
        stdout line arrives (no timers in the translator). The row closes
        under its original title."""
        if not state.bg_steps:
            return []
        now = time.monotonic()
        out: list[UntetherEvent] = []
        for idx, since in state.bg_steps.items():
            if idx in state.bg_retitled or now - since <= _BACKGROUND_TITLE_AFTER_S:
                continue
            action = state.pending_actions.get(f"step-{idx}")
            if action is None:
                continue
            state.bg_retitled.add(idx)
            out.append(
                state.factory.action_updated(
                    action_id=action.id,
                    kind=action.kind,
                    title=f"⏳ background: {action.title}"[:_BACKGROUND_TITLE_CHARS],
                    detail=action.detail,
                )
            )
        return out

    @staticmethod
    def _complete(
        action: Action,
        state: AntigravityStreamState,
        *,
        ok: bool,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> UntetherEvent:
        detail = dict(action.detail)
        if extra:
            detail.update(extra)
        return state.factory.action_completed(
            action_id=action.id,
            kind=action.kind,
            title=action.title,
            ok=ok,
            detail=detail,
            message=message,
        )

    def _settle_open_actions(
        self,
        state: AntigravityStreamState,
        *,
        ok: bool,
        message: str | None,
    ) -> list[UntetherEvent]:
        """Close every row still open so none is left "running" at the end.
        Parked rows ``denied_actions`` didn't claim weren't denied → ok."""
        out: list[UntetherEvent] = [
            self._complete(action, state, ok=True)
            for action in state.verdict_pending.values()
        ]
        state.verdict_pending.clear()
        out.extend(
            self._complete(action, state, ok=ok, message=message)
            for action in state.pending_actions.values()
        )
        state.pending_actions.clear()
        state.bg_steps.clear()
        return out

    def _answer(self, state: AntigravityStreamState, response: str | None) -> str:
        if response:
            return response
        return "\n\n".join(
            state.text_by_step[i].rstrip("\n") for i in sorted(state.text_by_step)
        )

    def _resume_for_completed(
        self, state: AntigravityStreamState, resume: ResumeToken | None
    ) -> ResumeToken | None:
        if state.session_id:
            return ResumeToken(engine=ENGINE, value=state.session_id)
        if resume is not None and not resume.is_continue:
            return resume
        return None

    def _translate_result(
        self,
        res: agy_schema.ResultPayload,
        state: AntigravityStreamState,
        resume: ResumeToken | None,
    ) -> list[UntetherEvent]:
        out: list[UntetherEvent] = []
        if res.conversation_id and state.session_id is None:
            out.extend(self._adopt_session(state, res.conversation_id))
            if state.conversation_missing:
                return out
        state.result_seen = True
        status = res.status or ""
        error = _error_text(res.error)
        failure = self._failure_text(state, error) if status != "SUCCESS" else None
        if failure is None and state.kill_reason is not None:
            failure = self._failure_text(state, None)
        if failure is not None:
            usage = self._usage(res, state)
            self._log_timing(res, state)
            out.extend(
                self._failure_completed(
                    state,
                    resume,
                    failure,
                    answer=self._answer(state, res.response),
                    usage=usage,
                )
            )
            return out
        interrupted = status == "ERROR" and error == "interrupted"
        denial_rows, denied_labels = self._resolve_denials(
            state, res.denied_actions or []
        )
        out.extend(denial_rows)
        out.extend(
            self._settle_open_actions(
                state,
                ok=status == "SUCCESS",
                message="interrupted" if interrupted else error,
            )
        )
        answer = self._answer(state, res.response)
        if denied_labels:
            answer = _append_paragraph(
                answer,
                denial_paragraph(
                    denied_labels, mode=state.effective_mode, trigger=state.unattended
                ),
            )
        if status == "SUCCESS":
            notice = self._oauth_notice(state)
            if notice is not None:
                answer = _append_paragraph(answer, notice)
        usage = self._usage(res, state)
        self._log_timing(res, state)
        resume_token = self._resume_for_completed(state, resume)
        factory = state.factory
        if status == "SUCCESS":
            out.append(
                factory.completed_ok(answer=answer, resume=resume_token, usage=usage)
            )
        elif interrupted:
            out.append(
                factory.completed_error(
                    error=INTERRUPTED_TEXT,
                    answer=answer,
                    resume=resume_token,
                    usage=usage,
                )
            )
        else:
            if status == "ERROR" and error:
                message = _trim_error_lines(error)
            else:
                # CANCELED / INVALID / WAITING / RUNNING / anything newer
                # (upstream #902: long turns sometimes end CANCELED).
                message = f"antigravity ended with status {status or 'unknown'}"
                if error:
                    message += f": {_trim_error_lines(error)}"
            out.append(
                factory.completed_error(
                    error=message,
                    answer=answer,
                    resume=resume_token,
                    usage=usage,
                )
            )
        return out

    def _resolve_denials(
        self,
        state: AntigravityStreamState,
        denied: list[agy_schema.DeniedAction],
    ) -> tuple[list[UntetherEvent], list[str]]:
        """``result.denied_actions`` → ⚠️ rows (the parked step of that tool
        family, else a standalone row) and the labels for the final text.

        Matching is by tool family (agy gives no step id); with 1.3.1's
        stop-at-first-denial that is exact (02 risks)."""
        if not denied:
            return [], []
        out: list[UntetherEvent] = []
        labels: list[str] = []
        actions: list[str] = []
        names: list[str] = []
        for item in denied:
            action = item.action or "unknown"
            display = item.display_name or action
            label = _DENIED_LABEL.get(action, "tool")
            family = _DENIED_FAMILY.get(action, frozenset())
            hits = [
                action_id
                for action_id, parked in state.verdict_pending.items()
                if _in_family(parked.detail.get("tool_name"), family)
            ]
            detail = {"denied": True, "denied_action": action}
            for action_id in hits:
                parked = state.verdict_pending.pop(action_id)
                out.append(
                    self._warning_row(
                        state,
                        action_id,
                        f"⚠️ Blocked: {label} ({display}) — {parked.title}",
                        {**parked.detail, **detail},
                    )
                )
            if not hits:
                out.append(
                    self._warning_row(
                        state,
                        f"antigravity.denied.{action}",
                        f"⚠️ Blocked: {label} ({display})",
                        detail,
                    )
                )
            if label not in labels:
                labels.append(label)
            actions.append(action)
            names.append(display)
        logger.info(
            "antigravity.denied_actions",
            actions=actions,
            display_names=names,
            permission_mode=state.effective_mode,
            trigger=state.unattended,
            session_id=state.session_id,
        )
        return out, labels

    def _config_change_rows(self, pre: _RunPrecheck) -> list[UntetherEvent]:
        """Planted config changed during the run (either set) → one ⚠️ row
        per set (REVIEW B2, REVIEW-2 M4). Never refuses anything here; a
        re-check that fails or is truncated says so (fail visible)."""
        if pre.scan is None or pre.cwd is None:
            return []
        factory = EventFactory(ENGINE)
        problem: str | None = None
        try:
            after = antigravity_scan.scan_workspace_config(pre.cwd)
        except Exception as exc:  # noqa: BLE001 — a warning must never break a run
            logger.warning(
                "antigravity.workspace_config.scan_failed",
                error_type=exc.__class__.__name__,
            )
            after = None
            problem = "the check failed"
        if after is not None and after.unchecked_reason is not None:
            problem = after.unchecked_reason
        elif after is not None and after.cross_truncated:
            problem = antigravity_scan.TOO_MANY
        out: list[UntetherEvent] = []
        if problem is not None:
            out.append(_recheck_warning(problem, factory))
        if after is None:
            return out
        for set_name, before_set, after_set in (
            ("agy", pre.scan.agy, after.agy),
            ("cross_engine", pre.scan.cross, after.cross),
        ):
            paths = antigravity_scan.changed_paths(before_set, after_set)
            if not paths:
                continue
            logger.warning(
                "antigravity.workspace_config_changed",
                paths=paths[:20],
                set=set_name,
                permission_mode=pre.effective_mode,
            )
            out.append(
                _warning_event(
                    factory,
                    f"antigravity.config.changed.{set_name}",
                    f"⚠️ Antigravity changed {_paths_text(paths)} — agy (or "
                    "another engine) will load it on the next run. Review it "
                    "before sending another message.",
                    {"paths": paths, "set": set_name},
                )
            )
        return out

    def _oauth_notice(self, state: AntigravityStreamState) -> str | None:
        """REVIEW-2 M14: once per chat, only on a Google sign-in host (a
        failed ``-p /config`` check counts as one)."""
        pre = state.precheck
        if pre is None or pre.auth_route != "oauth":
            return None
        chat_id = get_run_channel_id()
        if chat_id is None:
            return None
        store = self._notices()
        if store.seen(chat_id):
            return None
        store.mark(chat_id)
        logger.info(
            "antigravity.tos_notice.shown", chat_id=chat_id, auth_route=pre.auth_route
        )
        return OAUTH_NOTICE_TEXT

    @staticmethod
    def _usage(
        res: agy_schema.ResultPayload, state: AntigravityStreamState
    ) -> dict[str, Any]:
        """Flat per-run usage. agy's ``usage`` is session-cumulative (04 turns
        it into a per-run delta); ``num_turns`` / ``duration_seconds`` are
        cumulative too and never reported (08 §8, #952)."""
        usage: dict[str, Any] = {}
        stats = res.usage
        if stats is not None:
            usage = {
                key: value
                for key, value in (
                    ("input_tokens", stats.input_tokens),
                    ("output_tokens", stats.output_tokens),
                    ("cache_read_tokens", stats.cache_read_tokens),
                    ("reasoning_tokens", stats.thinking_tokens),
                )
                if isinstance(value, int)
            }
        if state.t_spawn:
            usage["duration_ms"] = int((time.monotonic() - state.t_spawn) * 1000)
        return usage

    @staticmethod
    def _log_timing(
        res: agy_schema.ResultPayload, state: AntigravityStreamState
    ) -> None:
        now = time.monotonic()

        def _ms(a: float | None, b: float | None) -> int | None:
            if not a or not b:
                return None
            return int((b - a) * 1000)

        logger.info(
            "antigravity.run.timing",
            spawn_to_init_ms=_ms(state.t_spawn, state.t_init),
            init_to_result_ms=_ms(state.t_init, now),
            total_ms=_ms(state.t_spawn, now),
            resumed=state.resumed,
            session_id=state.session_id,
            status=res.status,
            cumulative_input_tokens=res.usage.input_tokens if res.usage else None,
            cumulative_num_turns=res.num_turns,
        )

    # -- ends without a result ------------------------------------------------

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: AntigravityStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        # Only reached without a ``result`` (the base returns early after a
        # CompletedEvent), so stderr never overrides a real result (R1).
        failure = self._failure_text(state, None)
        if failure is not None:
            logger.info("antigravity.process.killed", rc=rc, reason=state.kill_reason)
            return self._failure_completed(
                state, resume, failure, answer=self._answer(state, None)
            )
        if state.agy_error is not None or rc == 3:
            parts = [self._agy_error_summary(state, rc)]
        else:
            parts = [f"antigravity failed ({_rc_label(rc)})."]
        if rc == 2:
            argv_error = _first_argv_error(stderr_lines)
            if argv_error is not None:
                logger.error(
                    "antigravity.argv.rejected",
                    rc=rc,
                    first_error_line=argv_error,
                    args=state.argv,
                )
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _agy_stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        message = "\n".join(parts)
        logger.error("antigravity.process.failed", rc=rc, session_id=state.session_id)
        out = self._settle_open_actions(state, ok=False, message=f"rc={rc}")
        out.append(self.note_event(message, state=state, ok=False))
        out.append(
            state.factory.completed_error(
                error=message,
                answer=self._answer(state, None),
                resume=found_session or self._resume_for_completed(state, resume),
            )
        )
        return out

    @staticmethod
    def _agy_error_summary(state: AntigravityStreamState, rc: int) -> str:
        info = state.agy_error or {}
        status = _log_scalar(info.get("status")) or _log_scalar(info.get("code"))
        retryable = info.get("retryable")
        retry = str(retryable).lower() if isinstance(retryable, bool) else "unknown"
        return (
            f"agy reported a model/agent error ({status or 'unknown'}, "
            f"retryable={retry}; {_rc_label(rc)})."
        )

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: AntigravityStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        failure = self._failure_text(state, None)
        if failure is not None:
            return self._failure_completed(
                state, resume, failure, answer=self._answer(state, None)
            )
        out = self._settle_open_actions(state, ok=False, message="no result")
        session = found_session or self._resume_for_completed(state, resume)
        if state.session_id is None:
            # Keeps the existing "finished but no session_id" auto-clear
            # alternative in ``_RESUME_FAILURE_RE`` (audit R7).
            logger.warning("antigravity.stream.no_session")
            parts = ["antigravity finished but no session_id was captured"]
        else:
            parts = ["antigravity finished without a result event"]
        label = _session_label(found_session, resume)
        if label:
            parts.append(f"session: {label}")
        excerpt = _agy_stderr_excerpt(stderr_lines)
        if excerpt:
            parts.append(excerpt)
        out.append(
            state.factory.completed_error(
                error="\n".join(parts),
                answer=self._answer(state, None),
                resume=session,
            )
        )
        return out


def _step_of(action_id: str) -> int:
    try:
        return int(action_id.removeprefix("step-"))
    except ValueError:
        return -1


def _recheck_warning(
    problem: str, factory: EventFactory | None = None
) -> UntetherEvent:
    return _warning_event(
        factory or EventFactory(ENGINE),
        "antigravity.config.recheck",
        "⚠️ Untether couldn't re-check this project's agy and other engines' "
        f"config after the run ({problem}). Review .agents/ and files like "
        ".claude/ or .envrc before sending another message.",
    )


def _warning_event(
    factory: EventFactory,
    action_id: str,
    title: str,
    detail: dict[str, Any] | None = None,
) -> UntetherEvent:
    # #868/#987: an ok=True warning that leads with ⚠️ renders the ⚠️ as its
    # status (never ✓ / ✗).
    return factory.action_completed(
        action_id=action_id,
        kind="warning",
        title=title,
        ok=True,
        detail=detail or {},
        level="warning",
    )


def _in_family(tool_name: Any, family: frozenset[str]) -> bool:
    if not isinstance(tool_name, str):
        return False
    return any(
        tool_name.startswith(entry[:-1]) if entry.endswith("*") else tool_name == entry
        for entry in family
    )


def _append_paragraph(answer: str, paragraph: str) -> str:
    if not answer.strip():
        return paragraph
    return f"{answer.rstrip()}\n\n{paragraph}"


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    """Build an ``AntigravityRunner`` from ``[antigravity]`` config.

    rc1 keys: ``model`` (str), ``cmd`` (str, ``~`` expanded) and
    ``permission_mode`` (``workspace`` / ``ask`` / ``plan`` / ``full``,
    validated here; ``full`` warns once). Unknown keys — including PR #766's
    ``dangerously_skip_permissions`` / ``antigravity_cmd`` — are ignored.
    """
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
    raw_cmd = config.get("cmd")
    if raw_cmd is not None and not isinstance(raw_cmd, str):
        logger.warning(
            "antigravity.config.invalid",
            error="cmd must be a string",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `antigravity.cmd` in {config_path}; expected a string."
        )
    mode = config.get("permission_mode")
    if mode is not None and (
        not isinstance(mode, str) or mode not in ANTIGRAVITY_PERMISSION_MODES
    ):
        allowed = ", ".join(sorted(ANTIGRAVITY_PERMISSION_MODES))
        logger.warning(
            "antigravity.config.invalid",
            error="unknown permission_mode",
            config_path=str(config_path),
        )
        raise ConfigError(
            f"Invalid `antigravity.permission_mode` {mode!r} in {config_path}; "
            f"expected one of: {allowed}."
        )
    if mode == "full":
        _warn_full_access_from_toml(config_path)
    cmd = os.path.expanduser(raw_cmd) if raw_cmd else default_antigravity_cmd()
    return AntigravityRunner(
        antigravity_cmd=cmd,
        model=model or None,
        default_permission_mode=mode,
        config_path=config_path,
    )


BACKEND = EngineBackend(
    id="antigravity",
    build_runner=build_runner,
    cli_cmd="agy",
    install_cmd="curl -fsSL https://antigravity.google/cli/install.sh | bash",
)
