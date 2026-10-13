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

Safety (phase 02, D21; phase 14): ``--dangerously-skip-permissions`` is
passed only through :func:`decide_antigravity_mode` — when a human
explicitly chose **Full access** (``/config``, ``[antigravity]
permission_mode = "full"`` or a cron's own ``permission_mode``), or chose
**Ask me** and Untether's approval gate is ready for the project (#980: the
gate then answers every tool, and anything less refuses the run). Unknown
values fail closed to Workspace, and unattended runs never inherit Full
access or Ask me (08 §10).
"Plan first" (#981) runs agy's own ``--mode=plan`` under the no-shell agent:
the turn stops after writing its plan, the plan is posted with Approve /
Deny / Let's discuss, and Approve resumes the conversation in Workspace
(``utils/antigravity_plan.py``). The runner also refuses to run without a project directory or on
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import anyio
import msgspec

from ..backends import EngineBackend, EngineConfig
from ..background_status import escape_markdown, live_shown
from ..background_tasks import EngineBackgroundTask
from ..config import ConfigError
from ..events import EventFactory
from ..logging import get_logger
from ..markdown import HARD_BREAK, inline_code
from ..model import (
    Action,
    ActionEvent,
    ActionKind,
    CompletedEvent,
    EngineId,
    ResumeToken,
    UntetherEvent,
)
from ..runner import (
    PRESPAWN_BLOCKED_KEY,
    RETIRED_CARDS_KEY,
    REVIEW_CARD_KEY,
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
)
from ..schemas import antigravity as agy_schema
from ..session_costs import RUN_TOKENS_KEY
from ..utils import antigravity_context, antigravity_quota, antigravity_scan
from ..utils.antigravity_redact import redact_agy_text
from ..utils.antigravity_state import (
    NOTICES_FILENAME,
    SEEN_CONFIG_FILENAME,
    NoticeStore,
    SeenConfigStore,
)
from ..utils.paths import (
    get_run_base_dir,
    get_run_channel_id,
    get_run_trigger_source,
)
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
# The published help page with agy's install, sign-in routes and terms note
# (GitHub-slugger anchor; pinned by ``tests/test_config_help_links.py``).
SIGN_IN_HELP_URL = (
    "https://littlebearapps.com/help/untether/switch-engines/#antigravity-cli"
)
# Phase 03 (D27): distinct, non-retryable error cards for the stderr kill
# classes. None of them matches ``_RESUME_FAILURE_RE``, so the chat keeps its
# saved session.
AUTH_TEXT = (
    "Antigravity CLI isn't signed in on this host — agy wanted a browser "
    "sign-in, which a bot can't complete. Run `agy` once in a terminal on the "
    "host and finish the Google sign-in, then retry. On a keyring desktop, also "
    "check that `DBUS_SESSION_BUS_ADDRESS` reaches Untether. For servers, the "
    f"Gemini API key route avoids sign-in: {SIGN_IN_HELP_URL}"
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
# /continue (#1039): agy's own ``--continue`` also resumes a parent or child
# directory's conversation, so Untether looks this directory up itself
# (``utils/antigravity_continue.py``) and refuses when there is nothing it
# can trust.
NO_CONVERSATION_TEXT = (
    "No Antigravity conversation for this project yet — send a normal message "
    "to start one."
)
CONTINUE_SHARED_TEXT = (
    "Antigravity's most recent conversation in this folder has also been run "
    "from another folder, so Untether can't tell which project it belongs to "
    "and didn't resume it. Send a normal message instead, or reply to a "
    "resume line to carry on a particular conversation."
)
# The id came from agy's own record, never from the chat, so this neither
# matches ``runner_bridge._RESUME_FAILURE_RE`` nor claims a session was
# cleared: the chat's saved conversation is a different one and is kept.
CONTINUE_GONE_TEXT = (
    "The conversation Antigravity had recorded for this project no longer "
    "exists, so nothing was resumed. Send a normal message to carry on from "
    "this chat's own conversation or start a new one, or reply to a resume "
    "line to pick a particular conversation."
)
CONTINUE_UNREADABLE_TEXT = (
    "Untether couldn't read Antigravity's record of recent conversations, so "
    "/continue didn't resume anything. Send a normal message instead, or "
    "reply to a resume line to carry on a particular conversation."
)
CONTINUE_BLOCK = "continue_unresolved"
INVALID_ARGUMENT_BLOCK = "invalid_argument"
# A model or conversation id that agy could read as a flag
# (``utils/antigravity_argv.py``): refused before anything spawns.
INVALID_MODEL_TEXT = (
    "🛑 That model id isn't valid for Antigravity, so nothing was started. "
    "A model id uses letters, digits and . _ - : / only, with no spaces and "
    "no leading dash (for example gemini-3.8-flash). Set one with /model set, "
    "or go back to the default with /model clear."
)
INVALID_CONVERSATION_TEXT = (
    "🛑 That Antigravity conversation id isn't valid, so nothing was started. "
    "Send /new to start a fresh conversation."
)
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
# #1073: in Plan first a refused write is plan mode itself (agy denies every
# file change but the plan, inside the project too: the rc4 QA run's copy to
# ``.untether-outbox/``). A refused read is still outside the project (the
# QA ``ListDir`` was the project's parent); in-project reads go ahead.
_PLAN_WRITE_LABEL = "file change"
_OUTSIDE_LABEL = "file outside the project"


def _denied_label(action: str, mode: str | None) -> str:
    if mode == "plan" and action == "write_file":
        return _PLAN_WRITE_LABEL
    return _DENIED_LABEL.get(action, "tool")


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
# #980 (13 §7): only a run whose safety rests on the gate (phase 14's gated
# modes, ``GateRun.required``) is stopped by these.
GATE_NOT_RUNNING_TEXT = (
    "Telegram approvals aren't running for this agy run (its hooks never"
    " reached Untether), so Untether stopped it before any change. Run"
    " `untether agy-gate status` on the host."
)
GATE_TRIPWIRE_TEXT = (
    "Telegram approvals stopped responding during this agy run, so Untether"
    " stopped it. Run `untether agy-gate status` on the host."
)
_KILL_TEXT: dict[str, str] = {
    "auth": AUTH_TEXT,
    "account_blocked": ACCOUNT_BLOCKED_TEXT,
    "quota": QUOTA_TEXT,
    "credits": CREDITS_TEXT,
    "conversation_gone": CONVERSATION_GONE_TEXT,
    "gate_not_running": GATE_NOT_RUNNING_TEXT,
    "gate_tripwire": GATE_TRIPWIRE_TEXT,
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
    "gate_not_running": "antigravity.gate.killed",
    "gate_tripwire": "antigravity.gate.killed",
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
# D33 / P-B6 (#978): agy picks ``WaitMsBeforeAsync`` per ``run_command`` call
# (5000, 10000 and 0 seen on 1.3.3) but the 1.3.3 stream carries only
# ``CommandLine``. Where the parameters do carry it, that value decides when
# the command counts as background; otherwise it does after this long.
_BACKGROUND_TITLE_AFTER_S = 5.0
# #1062: agy (1.3.3, 1.3.4) ends a step it killed (``manage_task``) DONE with
# ``tool_info.error.message`` "context canceled", and names the task it
# kills as ``<conversation id>/task-<step index>``.
_CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
_KILLED_TASK_RE = re.compile(r"(?:^|/)task-(\d+)$")
_ASYNC_WAIT_MAX_MS = 600_000
_BACKGROUND_TITLE_CHARS = 120
# Finished entries are dropped from the task map beyond this many.
_TASKS_MAX = 64
# Descendant PIDs handed to the #590 teardown sweep: bounded, and each one
# pinned by its /proc start time so a recycled PID is never signalled.
_ORPHAN_SNAPSHOT_MAX = 256
_DESCENDANT_SCAN_MAX = 1024
# Phase 05 (D29): a model id that already names its effort
# (``gemini-3.8-flash-high``); agy refuses a different ``--effort`` with it.
_EFFORT_SUFFIX_RE = antigravity_context.EFFORT_SUFFIX_RE
_MODEL_ROW_CHARS = 80
# #1058: the started title (``/export``'s "Model:") when no model was passed
# and ``init`` named none (agy names its model only when ``--model`` is set).
DEFAULT_MODEL_TITLE = "agy default"
# #979: the ``telemetry`` action behind the header's ``~N% ctx``.
_CONTEXT_ACTION_ID = "antigravity.context"

_OUTPUT_PREVIEW_CHARS = 500
# 13 §7.1: steps only a model call produces. ``user_input``,
# ``system_message`` and ``checkpoint`` don't count: a resumed run emits a
# ``system_message`` before its first model call.
_MODEL_STEP_TYPES = frozenset({"agent_response", "tool", "subagent", "finish"})
_STEER_SNIPPET_CHARS = 80
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
PROBED_CLI_VERSION = "1.3.4"
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
# What agy's ``init.permission_mode`` says under the bypass flag (agy 1.3.3,
# probes pb4/hook_deny_bypass).
BYPASS_REPORTED = "always-proceed"


def ask_mode_mismatch_message(reported: str) -> str:
    return (
        f"agy started in a permission mode Untether didn't ask for "
        f"({reported}), so this Ask me run was stopped before any tool ran. "
        "Check for an Untether update, or pick Workspace in /config."
    )


# Workspace = agy's own accept-edits mode. With no flag, print mode
# soft-denies every file write outside a temp folder, so the project itself
# is read-only (agy 1.3.3, probed 2026-10-11 in a non-temp repo; the rc1
# probes all ran under /tmp and missed it). accept-edits allows edits inside
# the project only: shell, web, MCP and files outside it stay blocked, and
# ``init.permission_mode`` still reports ``request-review``. A fixed value,
# never a user's; ``agy_flag`` allows nothing else for this flag.
WORKSPACE_AGY_MODE = "accept-edits"
# Modes that run on agy's own permission checks (no bypass flag), so agy's
# tool-permission setting must be its default or stricter (#1044, D22.7).
_NO_BYPASS_MODES = frozenset({"workspace", "plan"})

# agy ``toolPermission`` / ``init.permission_mode`` values a Workspace run
# may proceed under. Anything else — ``always-proceed`` (D22.7),
# ``proceed-in-sandbox`` (auto-runs sandboxed shell) or a value a newer agy
# invents — fails closed: the label would otherwise promise "no shell".
_WORKSPACE_TOOL_PERMISSIONS = frozenset({"request-review", "strict"})
CONFIG_UNCHECKED_BLOCK = "config_unchecked"
# #1044: neither the ``-p /config`` check nor ``init`` gave a usable value.
TOOL_PERMISSION_UNKNOWN_BLOCK = "tool_permission_unknown"
_CHECK_FAILED = "the check failed"
_NO_VALUE = "it returned no toolPermission value"


def permission_override_message(value: str) -> str:
    return (
        f"agy's own settings change its permission checks (toolPermission: "
        f"{value}), so Untether won't run Workspace mode — set it back to "
        "request-review or pick Full access in /config."
    )


ALWAYS_PROCEED_TEXT = permission_override_message("always-proceed")

# #1044. Two checks read agy's tool-permission value: ``agy -p /config``
# before the run and ``init.permission_mode`` in the stream. Either may be
# unavailable on its own (attended runs go on, with a ⚠️ row); both together
# stop a Workspace run, and an unattended one is held on the first alone.
PERMISSION_UNCONFIRMED_TEXT = (
    "Untether couldn't confirm agy's tool-permission setting: the check "
    "before the run didn't give one and agy didn't report one when it "
    "started. Workspace mode won't run without it — try again in a few "
    "minutes, or pick Full access in /config. If it keeps happening, agy may "
    "have changed; check for an Untether update."
)


def permission_unknown_held_message(reason: str) -> str:
    return (
        "Untether couldn't read agy's tool-permission setting before this run "
        f"({reason}), so this scheduled run is held. Nothing needs changing: "
        "Untether checks again on the next run, and the schedule works once "
        "the check succeeds."
    )


def permission_precheck_unknown_row(reason: str, reported: str) -> str:
    row = (
        "⚠️ Untether couldn't read agy's tool-permission setting before this "
        f"run ({reason}). agy reported {reported} when it started, so the run "
        "continues."
    )
    if reason == _CHECK_FAILED:
        row += " agy's allow rules and outside-project access weren't checked."
    return row


def permission_unreported_row(checked: str) -> str:
    return (
        "⚠️ agy didn't report its permission mode when it started, so this "
        "run relies on the check Untether made before it (toolPermission: "
        f"{checked})."
    )


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


# ── Ask me (#980, phase 14): the one place that decides bypass ───────────────

# Pre-spawn blocks of an Ask me run whose gate isn't ready (answered as a
# plain reply, #1047; the chat's session is kept).
GATE_NOT_READY_BLOCK = "gate_not_ready"
GATE_FOREIGN_BLOCK = "gate_foreign_config"
GATE_SELF_PROTECT_BLOCK = "gate_self_protect"
_ASK_ELSEWHERE = "Nothing was run. Workspace and Full access still work here."
GATE_MISSING_TEXT = (
    "Ask me needs Telegram approvals, which aren't set up on this host. Run "
    "`untether agy-gate install` on the host (`untether agy-gate status` "
    "shows what's missing; /config → 🔐 Telegram approvals shows the same). "
    + _ASK_ELSEWHERE
)


def gate_not_ready_message(reason: str) -> str:
    return (
        "Ask me needs Telegram approvals, and they aren't ready for this "
        f"project: {reason}. Run `untether agy-gate status --project <folder>` "
        "on the host to see what to repair. " + _ASK_ELSEWHERE
    )


def _gate_paths(
    config_path: Path | None, cwd: Path | None
) -> tuple[str, tuple[Path, ...]]:
    """Security review 3 (H1, M5): this instance's ``files.outbox_dir`` and
    every project's ``@branch`` worktree root, from the live config. The
    default ``.worktrees`` in the run's folder counts too unless the run's
    folder is a configured project; with no readable config, the defaults."""
    from ..antigravity import gate_policy

    try:
        from ..settings import load_settings_if_exists

        loaded = load_settings_if_exists(config_path)
    except Exception:  # noqa: BLE001 — the safe defaults below still apply
        logger.warning("antigravity.gate.settings_unreadable", exc_info=True)
        loaded = None
    outbox_dir = gate_policy.DEFAULT_OUTBOX_DIR
    roots: list[Path] = []
    projects: set[str] = set()
    if loaded is not None:
        settings, path = loaded
        outbox_dir = settings.transports.telegram.files.outbox_dir
        for entry in settings.projects.values():
            project = Path(entry.path).expanduser()
            if not project.is_absolute():
                project = path.parent / project
            projects.add(os.path.realpath(project))
            worktrees = Path(entry.worktrees_dir).expanduser()
            roots.append(worktrees if worktrees.is_absolute() else project / worktrees)
    if cwd is not None and os.path.realpath(cwd) not in projects:
        roots.append(cwd / ".worktrees")
    return outbox_dir, tuple(roots)


def gate_self_protect_message(path: str) -> str:
    return (
        "Ask me isn't available in this project: it contains Untether's own "
        f"code ({path}), which agy could edit to switch Telegram approvals "
        "off. Use Workspace or Full access here."
    )


def gate_foreign_message(paths: list[str]) -> str:
    return (
        "Ask me isn't available in this project: agy loads hooks, plugins, "
        "agents or MCP servers here that Untether doesn't manage "
        f"({_paths_text(paths)}), and they run their own commands without "
        "asking in Telegram. " + _ASK_ELSEWHERE + " Remove them (or move them "
        "out of the project) to use Ask me."
    )


@dataclass(frozen=True, slots=True)
class GateFacts:
    """What run_impl learnt about the gate before spawn (Ask me only)."""

    installed: bool = False  # the plugin folder holds Untether's files
    problem: str | None = None  # why the plugin or agy's listing isn't ready
    self_protect: str | None = None  # REVIEW-2 B3 path
    foreign: tuple[str, ...] = ()  # untrusted config in scope (REVIEW-2 B2)
    listening: bool = False  # this run's socket is bound


@dataclass(frozen=True, slots=True)
class ModeDecision:
    """How an agy run starts. ``build_args`` reads only this."""

    mode: str  # workspace / ask / plan / full (after fail-closed mapping)
    bypass: bool = False  # --dangerously-skip-permissions
    gated: bool = False  # GateRun.required: the gate is the only check
    no_shell_agent: bool = True  # --agent=untether-workspace (when it can)
    refusal: str | None = None  # refuse before spawn with this text
    block: str | None = None  # its PRESPAWN_BLOCKED_KEY value


def decide_antigravity_mode(mode: str, gate: GateFacts | None = None) -> ModeDecision:
    """Phase 14 §1: the single decision point for the bypass flag.

    ``mode`` is :meth:`AntigravityRunner._effective_mode`'s answer (the
    unattended downgrade and the fail-closed mapping already applied).

    - Workspace / Plan first: agy's own checks, Untether's no-shell agent.
    - Full access: bypass, agy's default agent; the gate (if installed) only
      asks questions.
    - Ask me: bypass **only** when the gate is ready — plugin installed and
      listed by agy for this project (``gate_precheck.check(...).ok``), no
      untrusted hook / plugin / agent / MCP server in scope, the project
      doesn't hold Untether's own code, and the socket is listening. Then the
      gate is required (deny-by-default) and agy runs on its default agent
      with no ``--mode`` (G1/G2: the no-shell agent would leave nothing to
      ask about, and ``--mode`` takes one value). Anything less refuses the
      run — never a silent downgrade (REVIEW-2 M17).
    - Anything else fails closed to Workspace (D21).
    """
    if mode == "full":
        return ModeDecision(mode="full", bypass=True, no_shell_agent=False)
    if mode == "plan":
        return ModeDecision(mode="plan")
    if mode != "ask":
        return ModeDecision(mode="workspace")
    refused = ModeDecision(mode="ask", no_shell_agent=False)
    if gate is None or not gate.installed:
        return replace(refused, refusal=GATE_MISSING_TEXT, block=GATE_MISSING_BLOCK)
    if gate.problem is not None:
        return replace(
            refused,
            refusal=gate_not_ready_message(gate.problem),
            block=GATE_NOT_READY_BLOCK,
        )
    if gate.self_protect is not None:
        return replace(
            refused,
            refusal=gate_self_protect_message(gate.self_protect),
            block=GATE_SELF_PROTECT_BLOCK,
        )
    if gate.foreign:
        return replace(
            refused,
            refusal=gate_foreign_message(list(gate.foreign)),
            block=GATE_FOREIGN_BLOCK,
        )
    if not gate.listening:
        return replace(
            refused,
            refusal=gate_not_ready_message("Untether couldn't open its socket"),
            block=GATE_NOT_READY_BLOCK,
        )
    return ModeDecision(mode="ask", bypass=True, gated=True, no_shell_agent=False)


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


def denial_paragraph(
    labels: list[str],
    *,
    mode: str,
    trigger: str | None,
    kept_shell: bool = False,
) -> str:
    """Why the run stopped and how to allow it — trigger-aware (audit §6).

    ``kept_shell`` (#1060): a resumed Workspace or Plan first run under
    Untether's no-shell agent still called the shell, so the conversation started under another
    agent (Full access, usually) and agy kept it — agy ignores ``--agent`` on
    a resume. Full access then /new is the wrong advice there."""
    what = f"⚠️ Antigravity was blocked from using a {_join_labels(labels)}"
    if mode == "full":
        return f"{what} by agy's own deny rules (`permissions.deny` in agy's settings file)."
    if mode == "plan":
        # #981: a plan turn may only read and write its plan.
        text = _plan_denial_text(labels)
        if kept_shell and trigger is None:
            # #1060: it reached for a shell the no-shell agent doesn't have.
            text += (
                " This conversation started without the no-shell tools (in Full"
                " access, for example), and agy keeps the tools a conversation"
                " started with. Send /new to plan in a fresh conversation."
            )
        return text
    head = f"{what} — headless runs can't ask for approval, so it stopped there."
    # An allow rule only helps for a shell command (#1037): agy has no rule
    # that lets a run reach a file outside the project.
    rule = (
        ", or add an allow rule such as `command(git status)` to agy's settings file"
        if "shell command" in labels
        else ""
    )
    if trigger is not None and trigger.startswith("webhook:"):
        tail = (
            f"{rule.removeprefix(', or ')}, or run it from the chat"
            if rule
            else "run it from the chat"
        )
        return f"{head} Webhook runs never get Full access; {tail}."
    if trigger is not None:
        return f'{head} To allow it, set `permission_mode = "full"` on this cron{rule}.'
    if kept_shell:
        return (
            f"{head} This conversation started without Workspace's no-shell"
            " tools (in Full access, for example), and agy keeps the tools a"
            " conversation started with, so it still tried the shell. Send /new"
            " to start a Workspace conversation, or pick Full access in /config"
            " to let it run."
        )
    # A conversation that started in Workspace keeps its no-shell agent.
    return (
        f"{head} To allow it: /config → Permission mode → Full access, then /new{rule}."
    )


def _plan_denial_text(labels: list[str]) -> str:
    """#1073: a Plan first denial names what plan mode refused — a file
    change anywhere (the plan is the only write), a file outside the project
    (as in Workspace) — rather than calling every file one outside."""
    others = [label for label in labels if label != _PLAN_WRITE_LABEL]
    parts = ["changing a file"] if _PLAN_WRITE_LABEL in labels else []
    if others:
        parts.append(f"using a {_join_labels(others)}")
    what = "⚠️ Antigravity was blocked from " + " and from ".join(parts)
    if _PLAN_WRITE_LABEL in labels:
        return f"{what} — Plan first doesn't change files until you approve the plan."
    if others == [_OUTSIDE_LABEL]:
        return f"{what} — Plan first works only with the project's own files."
    return (
        f"{what} — Plan first doesn't let it change anything until you approve a plan."
    )


def _paths_text(paths: list[str], limit: int = 5) -> str:
    shown = ", ".join(paths[:limit])
    more = len(paths) - limit
    return f"{shown} and {more} more" if more > 0 else shown


def config_changed_message(paths: list[str]) -> str:
    return (
        "agy's workspace config changed since someone last ran Antigravity in "
        f"this chat's project ({_paths_text(paths)}), so this scheduled run is "
        "held. The next message someone sends in the chat will run with the "
        "changed config and show what changed in a ⚠️ row; the schedule runs "
        "again after that. Check those files first if you didn't change them."
    )


PLUGIN_OUTDATED_ROW = (
    "⚠️ Untether's agy plugin (untether-gate) is from another Untether "
    "version. Telegram questions, steering and Ask me are off until you run "
    "`untether agy-gate install` on the host."
)


def gate_plugin_outdated(install: Any) -> bool:
    """Installed by Untether and unedited, and byte-identical to what an
    earlier Untether wrote (an upgrade changed the templates: run ``untether
    agy-gate install``). Files changed together with their record never
    count: they get the "changed" wording."""
    return (
        install.present
        and install.managed
        and not install.edited
        and install.problem is None
        and not install.current
        and getattr(install, "previous_version", None) is not None
    )


def gate_plugin_changed(install: Any) -> bool:
    """A present plugin folder that isn't byte-for-byte what this Untether
    installed (``gate_plugin.InstallState``; fail closed)."""
    return install.present and (
        not install.managed
        or bool(install.edited)
        or install.problem is not None
        or not install.current
    )


def gate_plugin_held_message(install: Any) -> str:
    """#1078: a scheduled run held because Untether's agy plugin isn't the
    one it installed. Unlike a changed workspace config, a chat message
    doesn't clear this hold: every unattended run stays held until the
    plugin is reinstalled.

    Markdown (#1083): sent as the held run's answer as well as its error
    (:func:`_plugin_held`), so the commands render as code, like the 🔐
    page's, instead of as literal backticks inside the error card's code
    block. The summary can name files someone added to the folder, so it is
    escaped."""
    if gate_plugin_outdated(install):
        return (
            "Untether's agy plugin (untether-gate) is from another Untether "
            "version, so this scheduled run is held. Scheduled Antigravity runs "
            "stay held until you run `untether agy-gate install` on the host; a "
            "message in the chat doesn't release them."
        )
    force = not install.managed or bool(install.edited) or install.problem is not None
    command = "untether agy-gate install" + (" --force" if force else "")
    return (
        "Untether's agy plugin (untether-gate) isn't what Untether installed "
        f"({escape_markdown(install.summary())}), so this scheduled run is "
        "held. Its hooks run in every agy session on this host, so scheduled "
        "Antigravity runs stay held until the plugin is restored; a message in "
        "the chat doesn't release them. Check what changed in "
        f"{inline_code(str(install.path))}, then run `{command}` on the host."
    )


def _plugin_held(install: Any, resume: ResumeToken | None) -> CompletedEvent:
    """A scheduled run held by :func:`gate_plugin_held_message`. The text is
    the answer too (one line, so the bridge shows it once, rendered, with no
    code block under it: ``runner_bridge`` dedupes an error whose first line
    is the answer's)."""
    text = gate_plugin_held_message(install)
    return EventFactory(ENGINE).completed_error(
        error=text,
        answer=text,
        resume=resume,
        usage={PRESPAWN_BLOCKED_KEY: CONFIG_CHANGED_BLOCK},
    )


def config_unchecked_message(reason: str) -> str:
    return (
        "Untether couldn't check agy's hooks, plugins and MCP servers in this "
        f"chat's project ({reason}), so scheduled runs are held until a check "
        "succeeds. A message sent in the chat still runs, with a ⚠️ row saying "
        "the config wasn't checked; trimming the project's .agents/ folder "
        "usually lets the check finish."
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
    f"{SIGN_IN_HELP_URL} "
    "(Shown once in this chat.)"
)


@dataclass(slots=True)
class _RunPrecheck:
    """What ``run_impl`` learnt before spawn, handed to ``new_state``."""

    cwd: Path | None = None
    scan: antigravity_scan.ScanResult | None = None
    first_sight: bool = False  # show the "hooks … Untether doesn't manage" row
    # #1083: the ``antigravity.gate.changed`` row covers Untether's own
    # plugin folder, so the row above leaves those files out (the scan still
    # tracks them: the digest and the cron hold are unchanged).
    gate_plugin_row: bool = False
    # The planted-config scan failed or was truncated: show a ⚠️ row (never
    # record a digest as seen); unattended runs were already refused.
    scan_problem: str | None = None
    effective_mode: str = "workspace"  # set by new_state (for logs)
    config: antigravity_quota.AgyConfig | None = None
    config_key: tuple[str, str] | None = None  # (project root, settings digest)
    config_rows: list[tuple[str, str]] = field(default_factory=list)
    label_suffix: str = ""
    auth_route: str = "oauth"
    # #1044 (Workspace only): why the check before the run gave no usable
    # ``toolPermission``; None when it gave one (``config.tool_permission``).
    permission_unknown: str | None = None


# Set by ``run_impl`` right before it delegates to the base ``run_impl``,
# consumed (and cleared) by ``new_state`` in the same task.
_PRECHECK: contextvars.ContextVar[_RunPrecheck | None] = contextvars.ContextVar(
    "untether_agy_precheck", default=None
)
# #1039: same hand-off — whether this run's conversation id was looked up for
# /continue (``AntigravityStreamState.from_continue``).
_FROM_CONTINUE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "untether_agy_from_continue", default=False
)
# #980: same hand-off — the run's gate, bound before spawn (its socket path
# goes into agy's env).
_GATE: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "untether_agy_gate", default=None
)
# #980 phase 14: same hand-off — how the run starts (``build_args``).
_DECISION: contextvars.ContextVar[ModeDecision | None] = contextvars.ContextVar(
    "untether_agy_decision", default=None
)


def _conversation_gone_text(state: AntigravityStreamState) -> str:
    return CONTINUE_GONE_TEXT if state.from_continue else CONVERSATION_GONE_TEXT


def _gone_resume(state: AntigravityStreamState) -> ResumeToken | None:
    """The token on a "conversation no longer exists" reply: the id the chat
    asked for, or none when /continue looked it up (no resume line for an id
    the chat never named)."""
    if state.from_continue or not state.expected_resume:
        return None
    return ResumeToken(engine=ENGINE, value=state.expected_resume)


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
    if tool_name == "invoke_subagent":
        # agy 1.3.3: the spawn is a ``tool`` step while ACTIVE; its roles are
        # in the parameters (P-B1).
        return "subagent", _subagent_title(_subagent_specs(tool_input, None))
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


# One subagent of an ``invoke_subagent`` step: (role, type, child conversation id).
_SubagentSpec = tuple[str | None, str | None, str | None]


def _subagent_specs(params: Any, info: Any) -> list[_SubagentSpec]:
    """The subagents one ``invoke_subagent`` step names: from
    ``subagent_info.subagents[]`` when the step has it (DONE on 1.3.3, ACTIVE
    too on 1.3.1), else from the ACTIVE ``tool_info.parameters.Subagents[]``."""
    for holder, key, names in (
        (info, "subagents", ("role", "type_name", "conversation_id")),
        (params, "Subagents", ("Role", "TypeName", "ConversationId")),
    ):
        entries = holder.get(key) if isinstance(holder, dict) else None
        if not isinstance(entries, list) or not entries:
            continue
        specs: list[_SubagentSpec] = []
        for entry in entries:
            role, type_name, child = (
                value if isinstance(value, str) and value else None
                for value in (
                    entry.get(name) if isinstance(entry, dict) else None
                    for name in names
                )
            )
            specs.append((role, type_name, child))
        return specs
    return []


def _subagent_title(specs: list[_SubagentSpec]) -> str:
    if not specs:
        return "subagent"
    role, type_name, _ = specs[0]
    if role and type_name:
        title = f"{type_name}: {role}"
    else:
        title = role or type_name or "subagent"
    return f"{title} +{len(specs) - 1}" if len(specs) > 1 else title


def _async_wait_s(params: dict[str, Any]) -> float:
    """Seconds a ``run_command`` may stay ACTIVE before it counts as
    background: agy's own per-call values when the stream carries them
    (``IsDaemon`` → at once, ``WaitMsBeforeAsync``), else the default."""
    if params.get("IsDaemon") is True:
        return 0.0
    raw = params.get("WaitMsBeforeAsync")
    if (
        isinstance(raw, int | float)
        and not isinstance(raw, bool)
        and 0 <= raw <= _ASYNC_WAIT_MAX_MS
    ):
        return raw / 1000.0
    return _BACKGROUND_TITLE_AFTER_S


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
    names must not collide with the Claude live-session probes the bridge
    duck-types (``live_mode``, ``completed_turns``, ``turn_open``,
    ``result_received_at``, ``live_wakeups`` …).

    ``tasks`` is the one deliberate exception (#978): it is the neutral
    background-task map (``background_tasks.BackgroundTaskView``) that
    ``runner_bridge._bg_tasks`` renders as the ``⏳ background`` block. Its
    other readers were checked for that: ``running_task_is_idle_after_result``
    needs ``live_mode`` first and reads ``holds_session`` (always False
    here); the orphan-approval lookup is keyed by a Claude agent id; and the
    post-result status message opens only for live tasks, which the end of a
    run never leaves."""

    factory: EventFactory
    note_seq: int = 0
    session_id: str | None = None  # init / step / result; never ""
    expected_resume: str | None = None  # resume.value when resuming by id
    conversation_missing: bool = False  # G8: init id != the resumed id
    # #1039: the id was looked up for /continue, not named by the chat.
    from_continue: bool = False
    # init.model: agy ≥ 1.3.2, and only when ``--model`` was passed (it
    # echoes the flag). Nothing else in the stream names the model.
    init_model: str | None = None
    # #979: input side of the latest model call (``agent_response`` usage:
    # input + cache-read tokens), the ``(pct, window)`` last sent to the
    # header, and the once-per-run log latches.
    ctx_used: int | None = None
    ctx_emitted: tuple[int, int] | None = None
    ctx_miss_logged: bool = False
    ctx_over_warned: bool = False
    # #1062: this run's own token use — the sum of its model calls'
    # ``agent_response`` usage. ``result.usage`` is the conversation's running
    # total, which also counts a cancelled run that never reported a result.
    run_tokens: dict[str, int] = field(default_factory=dict)
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
    # #978: the rows of the ``⏳ background`` block, built from the stream
    # only — ``cmd:<step>`` (run_command), ``timer:<step>`` (schedule),
    # ``sub:<step>:<n>`` (one per subagent of an invoke_subagent step).
    # Display only: ``bg_steps`` stays the busy probe (D33: a subagent can't
    # mark a run busy).
    tasks: dict[str, EngineBackgroundTask] = field(default_factory=dict)
    # ``system_message`` steps still expected for a finished background
    # command / timer; those are not a subagent finishing.
    bg_notices_owed: int = 0
    # #1062: a command / timer step that ended cancelled before it counted
    # as background, held (not listed) in case a ``manage_task`` kill names
    # it next.
    cancelled_steps: dict[str, EngineBackgroundTask] = field(default_factory=dict)
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
    # #1044: what the check before the run found — the reason it gave no
    # usable value, or the value itself. Both None only when a test drives
    # ``translate`` without ``run_impl`` (nothing is asserted then).
    permission_unknown: str | None = None
    permission_prechecked: str | None = None
    # True once ``init`` (or the first step of a stream with no ``init``)
    # has been checked; the ⚠️ row owed when one of the two checks was missing.
    permission_checked: bool = False
    permission_note: str | None = None
    # ``RunContext.trigger_source``: None when a person's message started the
    # run. Only then does the run record changed agy config as seen.
    trigger_source: str | None = None
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
    # #981 Plan first: ``TargetFile`` of every finished write in a plan run
    # (the plan is the one inside ``brain/<conversation id>/``), and the
    # plan an Approve resumed this run for (its file name).
    plan_writes: list[str] = field(default_factory=list)
    approved_plan: str | None = None
    # #1074: plan cards this run's plan turn replaced (request ids).
    superseded_cards: list[str] = field(default_factory=list)
    # #980/#981: this run's gate (``antigravity/gate_server.GateRun``) when
    # Untether's agy plugin is installed and ``-p /hooks`` lists it; None
    # otherwise (no marker env reaches agy, nothing changes).
    gate: Any = None
    gate_liveness_logged: bool = False
    # #980 phase 14: how this run started (``decide_antigravity_mode``).
    decision: ModeDecision | None = None
    # #1080: parent tool steps waiting on an Ask me approval card, and the
    # wait each held command had before it counts as background.
    approval_steps: set[int] = field(default_factory=set)
    approval_held: dict[str, float | None] = field(default_factory=dict)

    def note_approval_wait(self, step: int, waiting: bool) -> None:
        """#1080: a step waiting on an approval card isn't background work —
        agy hasn't run it yet. Its rows stay out of the ⏳ block until the
        card is answered, and their clocks start then (``GateRun``)."""
        if waiting:
            self.approval_steps.add(step)
        else:
            self.approval_steps.discard(step)
        now = time.monotonic()
        for key, task in self.tasks.items():
            if task.status != "running" or not (
                key in (f"cmd:{step}", f"timer:{step}")
                or key.startswith(f"sub:{step}:")
            ):
                continue
            if waiting:
                self.hold_for_approval(task)
            elif key in self.approval_held:
                offset = self.approval_held.pop(key)
                task.started_at = now
                task.background_from = None if offset is None else now + offset
        if not waiting and step in self.bg_steps:
            self.bg_steps[step] = now

    def hold_for_approval(self, task: EngineBackgroundTask) -> None:
        if task.task_id in self.approval_held:
            return
        start = task.background_from
        self.approval_held[task.task_id] = (
            None if start is None else max(0.0, start - task.started_at)
        )
        task.background_from = float("inf")

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

    # #1061: agy prints nothing while it holds its answer for background
    # work, and a cancel during the hold ends with an empty ``response``
    # (probe rc4-h1), so the answer can't be shown from the stream. These two
    # lines, duck-typed by ``runner_bridge``, say so where the user looks.

    def background_hold_note(self) -> str:
        """The last line of the ``⏳ background`` block. With the gate, agy's
        own ``Stop`` hook says when the model has finished its turn and agy
        is only waiting for background work (``fullyIdle: false``)."""
        gate = self.gate
        if gate is not None and gate.stop_not_idle:
            return BACKGROUND_TURN_DONE_NOTE
        return BACKGROUND_HOLD_NOTE

    # Duck-typed by the watchdog (``runner._approval_pending``) and the
    # bridge's stall / unanswerable-request monitors (#684): a question held
    # by the gate is an expected wait, not a hang.

    def awaiting_user_approval(self) -> bool:
        gate = self.gate
        if gate is None:
            return False
        from ..interactive import pending_control_requests_for_session

        return pending_control_requests_for_session(gate.session_key) > 0

    def control_request_snapshot(self, now: float | None = None) -> list[Any]:
        gate = self.gate
        if gate is None:
            return []
        from ..interactive import snapshot

        return snapshot(gate.session_key, now=now)

    def cancelled_hold_note(self) -> str | None:
        """The cancelled progress message's note, when the run was cancelled
        while a listed background task was still running."""
        if not live_shown(self.tasks.values()):
            return None
        return CANCELLED_HOLD_NOTE


# #1061. The held answer stays in agy's conversation (probe rc4-h2: a
# resumed "repeat your previous answer" returned it word for word).
BACKGROUND_HOLD_NOTE = "agy sends its answer when this work finishes (up to 30 min)"
# The gate's ``Stop`` hook reported ``fullyIdle: false`` (probe pb2: the
# model's turn is over and agy waits for a timer or a background task, which
# may still start another model call).
BACKGROUND_TURN_DONE_NOTE = (
    "agy has finished its turn and sends the answer when this work finishes"
    " (up to 30 min)"
)
CANCELLED_HOLD_NOTE = (
    "⏳ Cancelled while agy was holding its answer for background work, so"
    " it wasn't shown. The conversation kept it: send a message such as"
    ' "repeat your last answer" to see it.'
)


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
    # ``[antigravity] approval_timeout_s`` (#980): Ask me's approval wait,
    # capped by the gate plugin's own (set at ``untether agy-gate install``).
    approval_timeout_s: int | None = None
    # untether.toml; the small state files live beside chat_prefs.json.
    config_path: Path | None = None
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
        from_continue = resume is not None and resume.is_continue
        if from_continue:
            # #1039: from here on the run is an ordinary resume by id.
            resume, continue_block = await self._resolve_continue()
            if continue_block is not None:
                yield continue_block
                return
        argv_block = self._invalid_argument_refusal(resume)
        if argv_block is not None:
            yield argv_block
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
        gate_block, gate, decision = await self._gate_setup(precheck, resume)
        if gate_block is not None:
            yield gate_block
            return
        if decision.mode != "ask" and resume is not None:
            # "Always allow this command" lasts while the conversation stays
            # in Ask me (phase 14 §5).
            from ..antigravity import gate_policy

            gate_policy.forget_grants(resume.value)
        # Consumed by ``new_state`` inside the base run_impl (same task).
        _PRECHECK.set(precheck)
        _FROM_CONTINUE.set(from_continue)
        _GATE.set(gate)
        _DECISION.set(decision)
        _log_tos_notice_once()
        # #1045: the ⚠️ rows shown on the progress message, for the final.
        warnings: list[str] = []
        try:
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
                            _note_warning(warnings, row)
                            yield row
                    if isinstance(evt, CompletedEvent):
                        evt = _with_warning_block(evt, warnings)
                    else:
                        _note_warning(warnings, evt)
                    yield evt
        finally:
            _GATE.set(None)
            _DECISION.set(None)
            if gate is not None:
                with anyio.CancelScope(shield=True):
                    await gate.aclose()

    def _no_project_refusal(self, resume: ResumeToken | None) -> UntetherEvent | None:
        """REVIEW B1 / D16: agy edits files in its cwd, so never run it in the
        bot's own directory, the home directory or ``/``."""
        base = get_run_base_dir()
        reason = None
        if base is None:
            reason = "no_project"
        else:
            try:
                resolved = base.resolve()
                forbidden = {Path("/"), Path.home().resolve(), Path.cwd().resolve()}
                if resolved in forbidden:
                    reason = "forbidden_dir"
            except (OSError, RuntimeError):
                # #1044: a directory that can't be compared can't be cleared.
                # Python 3.12's ``resolve()`` raises RuntimeError for a
                # symlink loop (OSError from 3.13).
                reason = "unresolved_dir"
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

    async def _resolve_continue(
        self,
    ) -> tuple[ResumeToken | None, UntetherEvent | None]:
        """``/continue`` → this directory's own conversation, or a refusal.

        #1039: agy's ``--continue`` also resumes a conversation from a parent
        or child directory and has no flag to stop that, so the id comes from
        agy's last-conversations file for the exact run directory (custom:
        see the register in ``docs/reference/runners/antigravity/runner.md``).
        Never falls back to ``--continue``.
        """
        from ..utils import antigravity_continue

        base = get_run_base_dir()
        reason = "no_run_dir"
        if base is not None:
            try:
                # Off the event loop: one small, bounded file read.
                found = await anyio.to_thread.run_sync(
                    antigravity_continue.lookup_conversation, base
                )
            except Exception as exc:  # noqa: BLE001 - never spawn on a surprise
                logger.warning(
                    "antigravity.continue.lookup_failed",
                    error_type=type(exc).__name__,
                )
                reason = "lookup_failed"
            else:
                if found.conversation_id is not None:
                    logger.info("antigravity.continue.resolved", base=str(base))
                    return (
                        ResumeToken(engine=ENGINE, value=found.conversation_id),
                        None,
                    )
                reason = found.reason
        if reason in antigravity_continue.MISSING_REASONS:
            text = NO_CONVERSATION_TEXT
        elif reason == antigravity_continue.SHARED_ID:
            text = CONTINUE_SHARED_TEXT
        else:
            text = CONTINUE_UNREADABLE_TEXT
        logger.warning(
            "antigravity.continue.refused",
            reason=reason,
            base=str(base) if base is not None else None,
        )
        return None, EventFactory(ENGINE).completed_error(
            error=text,
            resume=None,
            usage={PRESPAWN_BLOCKED_KEY: CONTINUE_BLOCK},
        )

    def _invalid_argument_refusal(
        self, resume: ResumeToken | None
    ) -> UntetherEvent | None:
        """Refuse a model or conversation id agy could parse as a flag
        (``--model --dangerously-skip-permissions``). ``build_args`` would
        raise on the same value; this turns it into one plain card and a log
        line that never echoes the raw value."""
        from ..utils import antigravity_argv

        checks: list[tuple[str, Any, Any, str]] = []
        model = self._model()
        if model is not None and model != "":
            checks.append(
                ("model", model, antigravity_argv.check_model, INVALID_MODEL_TEXT)
            )
        if resume is not None and not resume.is_continue:
            checks.append(
                (
                    "conversation",
                    resume.value,
                    antigravity_argv.check_conversation,
                    INVALID_CONVERSATION_TEXT,
                )
            )
        for field_name, value, check, text in checks:
            try:
                check(value)
            except antigravity_argv.AgyArgvError as exc:
                logger.warning(
                    "antigravity.argv.invalid_value",
                    field=field_name,
                    reason=exc.reason,
                    preview=antigravity_argv.safe_preview(value),
                )
                # Never hand a bad id back as the run's resume token.
                return EventFactory(ENGINE).completed_error(
                    error=text,
                    resume=None,
                    usage={PRESPAWN_BLOCKED_KEY: INVALID_ARGUMENT_BLOCK},
                )
        return None

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
            and mode in {"full", "ask"}
            and (options is None or options.trigger_permission_mode != mode)
        ):
            # REVIEW M3: a chat's (or untether.toml's) bypass mode never
            # reaches a cron or webhook run; only the trigger's own setting
            # does. Ask me runs agy with the bypass flag too (phase 14 §1).
            key = (trigger, f"{origin}:{mode}")
            if key not in _UNATTENDED_DOWNGRADE_WARNED:
                if len(_UNATTENDED_DOWNGRADE_WARNED) >= _WARNED_MAX:
                    _UNATTENDED_DOWNGRADE_WARNED.clear()
                _UNATTENDED_DOWNGRADE_WARNED.add(key)
                logger.warning(
                    f"antigravity.unattended_{mode}_downgraded",
                    trigger=trigger,
                    origin=origin,
                    note="unattended runs get Full access or Ask me only from "
                    "the trigger's own permission_mode",
                )
            mode = "workspace"
        return mode

    def _gate_missing_refusal(self, resume: ResumeToken | None) -> UntetherEvent | None:
        """Ask me with no gate plugin on this host: refuse at once with the
        install hint (before the version check and the scans). A plugin that
        is there is checked properly in :meth:`_gate_setup`."""
        if self._effective_mode() != "ask":
            return None
        from ..antigravity import gate_plugin

        if gate_plugin.inspect_install().present:
            return None
        return self._decision_refusal(decide_antigravity_mode("ask", None), resume)

    @staticmethod
    def _decision_refusal(
        decision: ModeDecision, resume: ResumeToken | None
    ) -> UntetherEvent:
        assert decision.refusal is not None
        logger.warning(
            "antigravity.permission_mode.gate_refused",
            mode=decision.mode,
            reason=decision.block,
        )
        return EventFactory(ENGINE).completed_error(
            error=decision.refusal,
            resume=resume,
            usage={PRESPAWN_BLOCKED_KEY: decision.block},
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
                from ..antigravity import gate_plugin

                # #1078: when the change includes Untether's own plugin, a
                # chat message doesn't release the hold (``_gate_setup``
                # keeps holding until it is reinstalled), so say that.
                install = await anyio.to_thread.run_sync(gate_plugin.inspect_install)
                if gate_plugin_changed(install):
                    return _plugin_held(install, resume), None
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
        tool_permission = config.tool_permission if config is not None else None
        if mode in _NO_BYPASS_MODES and not tool_permission:
            # #1044: no usable value. An attended run goes on — ``init``
            # still has to report a safe one (``_check_reported_mode``) —
            # but nobody reads an unattended run's ⚠️ row, so that is held,
            # like an unfinished planted-config scan above.
            pre.permission_unknown = _CHECK_FAILED if config is None else _NO_VALUE
            if trigger is not None:
                # WARNING, like the scan hold above: one failed probe on a
                # frequent cron is not an error report. The in-stream refusal
                # when ``init`` can't confirm it either stays at ERROR.
                logger.warning(
                    "antigravity.permission_mode.unverified",
                    source="config",
                    requested=mode,
                    reason=pre.permission_unknown,
                    trigger=trigger,
                )
                return (
                    EventFactory(ENGINE).completed_error(
                        error=permission_unknown_held_message(pre.permission_unknown),
                        resume=resume,
                        usage={PRESPAWN_BLOCKED_KEY: TOOL_PERMISSION_UNKNOWN_BLOCK},
                    ),
                    None,
                )
        elif (
            mode in _NO_BYPASS_MODES
            and tool_permission not in _WORKSPACE_TOOL_PERMISSIONS
        ):
            logger.error(
                "antigravity.permission_mode.mismatch",
                source="config",
                requested=mode,
                reported=tool_permission,
            )
            return (
                EventFactory(ENGINE).completed_error(
                    error=permission_override_message(str(tool_permission)),
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

    async def _gate_setup(
        self, pre: _RunPrecheck | None, resume: ResumeToken | None
    ) -> tuple[UntetherEvent | None, Any, ModeDecision]:
        """#980: bind this run's gate socket when Untether's agy plugin is
        installed and ``agy -p /hooks`` lists it for the run's cwd, and
        decide how the run starts (:func:`decide_antigravity_mode`).

        Workspace, Plan first and Full access don't rely on the gate for
        safety, so a gate that isn't ready only means no Telegram questions
        and no steering for the run (logged; the run goes on). Ask me relies
        on it entirely: every gap refuses the run before spawn. One more
        refusal for every mode: the plugin folder isn't what Untether
        installed and the run is unattended — the planted-config scanner
        leaves the plugin's own files to this check, and a changed hook runs
        its own commands."""
        from ..antigravity import gate_plugin, gate_precheck, gate_server

        mode = self._effective_mode()
        decision = decide_antigravity_mode(mode)
        if pre is None or pre.cwd is None:
            if mode == "ask":
                return self._decision_refusal(decision, resume), None, decision
            return None, None, decision
        install = await anyio.to_thread.run_sync(gate_plugin.inspect_install)
        if not install.present:
            if mode == "ask":
                return self._decision_refusal(decision, resume), None, decision
            return None, None, decision
        options = get_run_options()
        trigger = options.unattended_trigger if options is not None else None
        if gate_plugin_changed(install):
            # Fail closed: only files byte-identical to this version's own
            # templates count as Untether's. A manifest whose hashes match an
            # edited hooks.json proves nothing (whatever rewrote one could
            # rewrite both), and the planted-config scanner tracks such a
            # folder too (``gate_plugin.verified_own_files``).
            logger.warning(
                "antigravity.gate.plugin_changed",
                state=install.summary(),
                path=str(install.path),
                trigger=trigger,
            )
            outdated = gate_plugin_outdated(install)
            if trigger is not None:
                return _plugin_held(install, resume), None, decision
            if mode == "ask":
                reason = gate_precheck.OUTDATED if outdated else gate_precheck.EDITED
                refused = decide_antigravity_mode(
                    mode,
                    GateFacts(
                        installed=True, problem=gate_precheck.reason_text(reason)
                    ),
                )
                return self._decision_refusal(refused, resume), None, refused
            pre.gate_plugin_row = True
            pre.config_rows.append(
                (
                    "antigravity.gate.changed",
                    PLUGIN_OUTDATED_ROW
                    if outdated
                    else "⚠️ Untether's agy plugin (untether-gate) isn't what"
                    f" Untether installed: {install.summary()}. Its hooks run in"
                    " every agy session on this host. Telegram questions and"
                    " steering are off; run `untether agy-gate status` on the"
                    " host.",
                )
            )
            return None, None, decision
        scan = pre.scan
        result = await gate_precheck.check(
            self,
            pre.cwd,
            scan_digest=scan.agy_digest if scan is not None else None,
            root=scan.root if scan is not None else None,
        )
        if mode == "ask":
            facts = await self._ask_facts(pre, result)
            decision = decide_antigravity_mode(mode, facts)
            if decision.refusal is not None:
                return self._decision_refusal(decision, resume), None, decision
        elif not result.ok:
            return None, None, decision
        wait_s = result.wait_s
        if self.approval_timeout_s is not None:
            # ``[antigravity] approval_timeout_s``: shorter than the
            # plugin's own wait, never longer (agy's hook timeout is fixed
            # at install time).
            wait_s = min(wait_s, self.approval_timeout_s)
        outbox_dir, worktree_roots = _gate_paths(self.config_path, pre.cwd)
        gate = gate_server.GateRun(
            config_path=self.config_path,
            factory=EventFactory(ENGINE),
            wait_s=wait_s,
            unattended_trigger=trigger,
            ask_questions=options.ask_questions if options is not None else None,
            required=decision.gated,
            project_root=pre.cwd,
            diff_preview=options.diff_preview if options is not None else None,
            channel_id=get_run_channel_id(),
            outbox_dir=outbox_dir,
            outside=worktree_roots,
        )
        try:
            await gate.open()
        except (OSError, gate_server.GateSocketError) as exc:
            logger.warning(
                "antigravity.gate.socket_failed",
                error_type=exc.__class__.__name__,
                error=str(exc)[:200],
            )
            if mode == "ask":
                refused = decide_antigravity_mode(mode, replace(facts, listening=False))
                return self._decision_refusal(refused, resume), None, refused
            return None, None, decision
        return None, gate, decision

    async def _ask_facts(self, pre: _RunPrecheck, result: Any) -> GateFacts:
        """Ask me's gate facts: agy's own listing (``result``, a
        ``GatePrecheck``), Untether's own code in the project, and config that
        runs commands outside the gate (hooks, plugins, agents, MCP)."""
        from ..antigravity import gate_precheck

        if not result.ok:
            return GateFacts(
                installed=True, problem=gate_precheck.reason_text(result.reason)
            )
        if pre.scan_problem is not None or pre.scan is None:
            # The planted-config scan didn't finish: nothing proves the
            # project carries no hooks or MCP servers of its own.
            why = pre.scan_problem or "the check didn't run"
            return GateFacts(
                installed=True,
                problem=f"Untether couldn't check this project's agy config ({why})",
            )
        foreign = await gate_precheck.foreign_config(self, result, pre.scan)
        if foreign:
            logger.warning(
                "antigravity.gate.precheck_failed",
                reason="foreign_config",
                paths=list(foreign)[:20],
                cwd=str(pre.cwd),
            )
        if result.self_protect is not None:
            logger.warning(
                "antigravity.gate.precheck_failed",
                reason=gate_precheck.SELF_PROTECT,
                path=result.self_protect,
                cwd=str(pre.cwd),
            )
        return GateFacts(
            installed=True,
            self_protect=result.self_protect,
            foreign=foreign,
            listening=True,
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
        # Every value goes through ``agy_flag``: validated, and joined as
        # ``--flag=value`` so agy can't read it as another flag. It raises
        # ``AgyArgvError`` on a bad value; ``run_impl`` refused that run
        # before getting here.
        from ..utils import antigravity_agent, antigravity_plan
        from ..utils.antigravity_argv import AgyArgvError, agy_flag

        mode = self._effective_mode()
        args = [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            "0",
        ]
        if mode != "plan":
            # C7: the flag turns ``--mode=plan`` into a no-op, so a plan run
            # goes without it and escapes a ``/``-leading prompt instead
            # (``stdin_payload``, D30).
            args.append("--disable-slash-commands")
        if resume is not None:
            # #1039: agy's own continue flag also resumes a parent or child
            # directory's conversation, so it is never passed. ``run_impl``
            # swaps a /continue token (value "", #817) for this directory's
            # conversation id before getting here.
            if resume.is_continue:
                raise AgyArgvError("conversation", "unresolved_continue")
            args.append(agy_flag("--conversation", resume.value))
        model = self._model()
        if model:
            args.append(agy_flag("--model", model))
        effort = self._effort_arg(model, state)
        if effort:
            args.append(agy_flag("--effort", effort))
        # Phase 02 (D21) / phase 14: the bypass flag only as
        # ``decide_antigravity_mode`` says — an explicit Full access, or Ask
        # me with its gate ready (the decision ``run_impl`` made before
        # spawn). Without one (a direct call), Ask me decides with no gate
        # facts, which never bypasses. Plan first is agy's own plan mode
        # under the no-shell agent (#981, shape A); anything else fails
        # closed to Workspace.
        decision = (
            state.decision
            if isinstance(state, AntigravityStreamState)
            and state.decision is not None
            and state.decision.mode == mode
            else decide_antigravity_mode(mode)
        )
        if decision.gated and (
            not isinstance(state, AntigravityStreamState)
            or state.gate is None
            or not state.gate.required
        ):
            # Invariant: Ask me's bypass only ever travels with this run's
            # own required gate. Without it, fail closed.
            logger.error("antigravity.gate.bypass_without_gate", mode=mode)
            decision = decide_antigravity_mode(mode)
        if decision.bypass:
            # Full access and Ask me: agy's default agent (Ask me has
            # nothing to ask about under the no-shell agent, G1) and no
            # ``--mode`` (it takes one value, G2).
            args.append(BYPASS_FLAG)
        else:
            if mode == "ask":
                # Refused before spawn; never reached by a real run. Fail
                # closed to Workspace's flags all the same.
                mode = "workspace"
            agy_mode = (
                antigravity_plan.PLAN_AGY_MODE if mode == "plan" else WORKSPACE_AGY_MODE
            )
            args.append(agy_flag("--mode", agy_mode))
            # #1037: Workspace runs use Untether's no-shell agent, so the
            # model has no shell tool to open with (a soft-denied shell call
            # ends the turn). Skipped when the file can't be put in place, or
            # when the project ships an agent of the same name: agy resolves
            # agents by name and would not be running Untether's.
            if (
                not antigravity_agent.project_defines_agent(get_run_base_dir())
                and antigravity_agent.ensure_workspace_agent()
            ):
                args.append(agy_flag("--agent", antigravity_agent.WORKSPACE_AGENT_NAME))
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
        if isinstance(state, AntigravityStreamState) and state.effective_mode == "plan":
            from ..utils import antigravity_plan

            prompt = antigravity_plan.escape_slash(prompt)
        if isinstance(state, AntigravityStreamState):
            from ..utils import antigravity_agent

            note = antigravity_agent.scheduled_run_note(
                state.unattended, state.effective_mode
            )
            if note is not None:
                # #1055: the model can't otherwise tell a cron from a chat.
                prompt = f"{note}\n\n{prompt}"
                logger.info(
                    "antigravity.prompt.scheduled_note",
                    trigger=state.unattended,
                    permission_mode=state.effective_mode,
                )
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
        # #980 (D22): the gate's marker, socket and token reach agy only from
        # this run's own gate — never inherited from Untether's environment
        # (the ``UNTETHER_`` prefix would otherwise pass them through).
        from ..antigravity import gate_hook

        for key in [k for k in env if k.startswith(gate_hook.ENV_MARKER)]:
            del env[key]
        gate = getattr(state, "gate", None)
        if gate is not None:
            env.update(gate.env())
        return env

    async def _iter_jsonl_events(
        self,
        *,
        stdout: Any,
        stream: Any,
        state: Any,
        resume: ResumeToken | None,
        logger: Any,
        pid: int,
    ) -> AsyncIterator[UntetherEvent]:
        """#980: with a gate, merge the events its socket raises (question
        cards and their answers) into agy's own stream, and serve the socket
        and the tripwire for as long as agy's stdout is read."""
        base = JsonlSubprocessRunner._iter_jsonl_events(
            self,
            stdout=stdout,
            stream=stream,
            state=state,
            resume=resume,
            logger=logger,
            pid=pid,
        )
        gate = getattr(state, "gate", None)
        if gate is None:
            async with contextlib.aclosing(base) as events:
                async for evt in events:
                    yield evt
            return
        gate.on_fatal = lambda kind: self._gate_kill(state, kind)
        gate.on_approval_wait = state.note_approval_wait  # #1080
        # Security review 3 L1: hook connections must come from below agy.
        gate.agy_pid = pid
        send, receive = anyio.create_memory_object_stream[UntetherEvent](64)

        async def pump() -> None:
            async with send, contextlib.aclosing(base) as events:
                async for evt in events:
                    await send.send(evt)
            # agy's stream is over: nothing the gate raises now has a reader.
            gate.stop_emitting()

        async with anyio.create_task_group() as tg:
            tg.start_soon(gate.serve, send.clone())
            tg.start_soon(gate.watch)
            tg.start_soon(pump)
            async with receive:
                async for evt in receive:
                    yield evt
            tg.cancel_scope.cancel()

    def new_state(
        self, prompt: str, resume: ResumeToken | None
    ) -> AntigravityStreamState:
        expected = (
            resume.value if resume is not None and not resume.is_continue else None
        )
        precheck = _PRECHECK.get()
        _PRECHECK.set(None)
        from_continue = _FROM_CONTINUE.get()
        _FROM_CONTINUE.set(False)
        options = get_run_options()
        mode = self._effective_mode()
        label = _mode_label(mode)
        approved, superseded = self._plan_bookkeeping(mode, prompt, expected)
        if approved is not None:
            label += " · plan approved"
        if precheck is not None:
            label += precheck.label_suffix
            precheck.effective_mode = mode
        factory = EventFactory(ENGINE)
        gate = _GATE.get()
        if gate is not None:
            gate.factory = factory
        decision = _DECISION.get()
        if decision is None or decision.mode != mode:
            # A test driving ``translate`` / ``build_args`` without
            # ``run_impl``: decide without gate facts (Ask me then never
            # gets the bypass flag).
            decision = decide_antigravity_mode(mode)
        return AntigravityStreamState(
            factory=factory,
            gate=gate,
            decision=decision,
            expected_resume=expected or None,
            from_continue=from_continue,
            resumed=resume is not None,
            effective_mode=mode,
            mode_label=label,
            unattended=options.unattended_trigger if options is not None else None,
            precheck=precheck,
            permission_unknown=(
                precheck.permission_unknown if precheck is not None else None
            ),
            permission_prechecked=(
                precheck.config.tool_permission
                if precheck is not None and precheck.config is not None
                else None
            ),
            trigger_source=get_run_trigger_source(),
            approved_plan=approved,
            superseded_cards=superseded,
        )

    @staticmethod
    def _plan_bookkeeping(
        mode: str, prompt: str, conversation_id: str | None
    ) -> tuple[str | None, list[str]]:
        """#981, at the start of a run on a known conversation: a new plan
        turn retires the conversation's older plan cards (their ids, for the
        bridge to take their buttons off, #1074); the run an Approve queued
        (Workspace, Untether's approve prompt) takes the approval and returns
        the plan's file name for the footer and the final."""
        if conversation_id is None:
            return None, []
        from ..utils import antigravity_plan

        if mode == "plan":
            superseded = antigravity_plan.supersede(conversation_id)
            if superseded:
                logger.info("antigravity.plan.superseded", session_id=conversation_id)
            return None, superseded
        if mode != "workspace" or not prompt.rstrip().endswith(
            antigravity_plan.APPROVE_PROMPT
        ):
            return None, []
        plan = antigravity_plan.take_approved(conversation_id)
        return (plan.name if plan is not None else None), []

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

    def _gate_kill(self, state: AntigravityStreamState, kind: str) -> None:
        """13 §7: a gated run whose hooks aren't reaching Untether."""
        if state.kill_reason is not None or state.permission_refused:
            return
        state.kill_reason = kind
        state.kill_detected_at = time.monotonic()
        if state.agy_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(state.agy_pid, signal.SIGTERM)

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
            if kind == "conversation_gone":
                return _conversation_gone_text(state)
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
            token: ResumeToken | None = _gone_resume(state)
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
                if not state.permission_checked:
                    # #1044: a step with no ``init`` before it.
                    refused = self._check_reported_mode(
                        state, None, resume, source="no_init"
                    )
                    if refused is not None:
                        return refused
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
        if state.gate is not None:
            state.gate.set_conversation(cid)
        logger.info(
            "antigravity.session.started",
            session_id=cid,
            resumed=state.resumed,
            agy_version=state.agy_version,
        )
        token = ResumeToken(engine=ENGINE, value=cid)
        meta = self._meta(state)
        # #1058: the title is /export's "Model:" line — the model agy ran
        # (``--model`` / ``init.model``), never the engine id. With neither,
        # agy picked its own default and the stream doesn't say which.
        title = str(meta.get("model") or DEFAULT_MODEL_TITLE)
        return [
            state.factory.started(token, title=title, meta=meta),
            *self._startup_rows(state),
        ]

    def _check_reported_mode(
        self,
        state: AntigravityStreamState,
        reported: str | None,
        resume: ResumeToken | None,
        *,
        source: str = "init",
    ) -> list[UntetherEvent] | None:
        """D22.7: agy's ``toolPermission: always-proceed`` setting stands in
        for the bypass flag. Without our flag, anything but agy's default or
        a stricter policy (fail closed: ``proceed-in-sandbox``, a value a
        newer agy invents) stops the run before any tool (``init`` comes
        ≈ 2.7 s before the first model output).

        #1044: a missing value (a renamed field, or ``source="no_init"`` — a
        step with no ``init`` before it) is covered by the ``-p /config``
        check before the run. When that gave no usable value either, nothing
        has confirmed the setting and the run stops; when only one of the
        two is missing the run goes on with a ⚠️ row."""
        state.permission_checked = True
        state.permission_note = None
        decision = state.decision
        bypass = (
            decision.bypass if decision is not None else state.effective_mode == "full"
        )
        if bypass:
            # We passed the bypass flag (Full access, Ask me): agy reports
            # always-proceed. A stricter value only means agy's own checks
            # also apply (harmless; warned once). In Ask me anything else is
            # a mode Untether didn't ask for: stop (D22.7).
            if reported in _WORKSPACE_TOOL_PERMISSIONS:
                _warn_bypass_unreported()
            elif (
                state.effective_mode == "ask"
                and reported is not None
                and reported != BYPASS_REPORTED
            ):
                logger.error(
                    "antigravity.permission_mode.mismatch",
                    source="init",
                    requested="ask",
                    reported=reported,
                )
                return self._refuse_in_stream(
                    state, resume, ask_mode_mismatch_message(reported)
                )
            return None
        if reported is not None and reported not in _WORKSPACE_TOOL_PERMISSIONS:
            logger.error(
                "antigravity.permission_mode.mismatch",
                source="init",
                requested=state.effective_mode,
                reported=reported,
            )
            return self._refuse_in_stream(
                state, resume, permission_override_message(reported)
            )
        if reported is None and state.permission_unknown is not None:
            logger.error(
                "antigravity.permission_mode.unverified",
                source=source,
                requested=state.effective_mode,
                reason=state.permission_unknown,
            )
            return self._refuse_in_stream(state, resume, PERMISSION_UNCONFIRMED_TEXT)
        if reported is None and state.permission_prechecked:
            state.permission_note = permission_unreported_row(
                state.permission_prechecked
            )
        elif reported is not None and state.permission_unknown is not None:
            source = "config"
            state.permission_note = permission_precheck_unknown_row(
                state.permission_unknown, reported
            )
        if state.permission_note is not None:
            logger.warning(
                "antigravity.permission_mode.unconfirmed",
                source=source,
                requested=state.effective_mode,
                reported=reported,
                reason=state.permission_unknown,
            )
        return None

    def _refuse_in_stream(
        self, state: AntigravityStreamState, resume: ResumeToken | None, error: str
    ) -> list[UntetherEvent]:
        """Stop agy and end the run; every later line is ignored."""
        state.permission_refused = True
        if state.agy_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(state.agy_pid, signal.SIGTERM)
        keep = resume if resume is not None and not resume.is_continue else None
        return [state.factory.completed_error(error=error, resume=keep)]

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
        if state.permission_note is not None:
            out.append(
                self._warning_row(
                    state, "antigravity.permission.unconfirmed", state.permission_note
                )
            )
            state.permission_note = None
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
            # #1044: only a person's message acknowledges the config. An
            # ``/at`` or loop re-fire (``trigger_source`` set) still runs and
            # shows the row, but held crons stay held until someone has
            # been in the chat to see it.
            recorded = state.trigger_source is None and state.unattended is None
            logger.warning(
                "antigravity.workspace_config_present",
                paths=paths[:20],
                digest=scan.agy_digest[:12],
                permission_mode=state.effective_mode,
                recorded=recorded,
                trigger=state.trigger_source,
            )
            if recorded:
                self._seen().set(scan.root, scan.agy_digest)
            if pre.gate_plugin_row:
                # #1083: Untether's own plugin folder has its own row.
                paths = [
                    p
                    for p in paths
                    if not p.startswith(antigravity_scan.GATE_PLUGIN_DISPLAY_PREFIX)
                ]
            if paths:
                out.append(
                    self._warning_row(
                        state,
                        "antigravity.config.present",
                        "⚠️ This project has agy hooks, plugins or MCP servers "
                        f"that Untether doesn't manage ({_paths_text(paths)}). "
                        "They run their own commands in every mode, including "
                        "Workspace. Review them if you didn't add them.",
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
        return [
            state.factory.completed_error(
                error=_conversation_gone_text(state), resume=_gone_resume(state)
            )
        ]

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
        if idx is not None and step_type in {"tool", "subagent", "agent_response"}:
            self._note_overtaken(state, idx)
        if state.gate is not None:
            out.extend(self._gate_step(su, state))

        if step_type == "tool":
            if st == "DONE" and state.effective_mode == "plan":
                # #981: the plan artifact is a finished write into the
                # conversation's own folder (resolved at the result).
                from ..utils import antigravity_plan

                info = su.tool_info
                antigravity_plan.note_plan_write(
                    state.plan_writes,
                    su.tool_name or (info.name if info and info.name else ""),
                    info.parameters if info is not None else None,
                )
            out.extend(self._translate_tool(su, state, action_id))
            return out
        if step_type == "subagent":
            specs = _subagent_specs(None, su.subagent_info)
            title = _subagent_title(specs)
            if idx is not None:
                self._track_subagents(state, idx, specs, failed=st == "ERROR")
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
            if st == "DONE":
                _add_run_tokens(state.run_tokens, su.usage)
                out.extend(self._note_context(su.usage, state))
            return out
        # A tool step that turns into another type on completion (the
        # ``finish`` tool with --json-schema goes ACTIVE as ``tool``, DONE as
        # ``finish``) still closes its row.
        if terminal and action_id in state.pending_actions:
            action = state.pending_actions.pop(action_id)
            if idx is not None:
                state.bg_steps.pop(idx, None)
                self._task_step_ended(
                    state, idx, status="completed" if st == "DONE" else "failed"
                )
            out.append(self._complete(action, state, ok=st == "DONE"))
            return out
        if step_type == "system_message" and terminal:
            self._note_system_message(state)
        # user_input (incl. a hook-injected one: not a new run), system_message,
        # checkpoint, unknown, finish, anything newer.
        logger.debug("antigravity.step.ignored", step_type=step_type, state=st)
        return out

    # -- gate (#980, #981) ---------------------------------------------------

    def _gate_step(
        self, su: agy_schema.StepUpdatePayload, state: AntigravityStreamState
    ) -> list[UntetherEvent]:
        """Liveness, tripwire bookkeeping and steer delivery for one step."""
        gate = state.gate
        step_type = su.step_type
        idx = su.step_index
        if (
            step_type in _MODEL_STEP_TYPES
            and not gate.preinvocation_seen
            and not gate.dead
        ):
            # 13 §7.1: ``PreInvocation`` fires before every model call, so a
            # model step with none before it means the hooks don't run.
            gate.mark_dead()
            if gate.required:
                logger.error(
                    "antigravity.gate.not_running",
                    session_id=state.session_id,
                    step_type=step_type,
                )
                self._gate_kill(state, "gate_not_running")
            elif not state.gate_liveness_logged:
                state.gate_liveness_logged = True
                logger.warning(
                    "antigravity.gate.not_running",
                    session_id=state.session_id,
                    step_type=step_type,
                    enforced=False,
                )
        if step_type == "tool" and su.state == "ACTIVE" and idx is not None:
            if not gate.dead:
                info = su.tool_info
                gate.note_tool_step(
                    idx, su.tool_name or (info.name if info and info.name else "tool")
                )
            return []
        if (
            step_type == "user_input"
            and su.state in {"DONE", "ERROR"}
            and idx is not None
            and idx > 0
        ):
            item = gate.take_delivered()
            if item is not None:
                return self._steer_received(state, idx, item)
        return []

    @staticmethod
    def _steer_received(
        state: AntigravityStreamState, idx: int, item: Any
    ) -> list[UntetherEvent]:
        """P-H10: one ``user_input`` step per injected message. The bridge
        drops the message's reply anchor on ``absorbed_command_uuid`` (its
        answer is this turn's answer), as for a Claude steer (#775)."""
        logger.info(
            "antigravity.steer.delivered",
            step=idx,
            session_id=state.session_id,
            command_uuid=item.command_uuid,
        )
        snippet = " ".join(item.text.split())
        if len(snippet) > _STEER_SNIPPET_CHARS:
            snippet = snippet[: _STEER_SNIPPET_CHARS - 1] + "…"
        title = f"\N{RIGHTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16} steer received: {snippet}"
        state.note_seq += 1
        action_id = f"antigravity.steer.{state.note_seq}"
        detail = {"absorbed_command_uuid": item.command_uuid, "steer": True}
        factory = state.factory
        return [
            factory.action_started(
                action_id=action_id, kind="note", title=title, detail=detail
            ),
            factory.action_completed(
                action_id=action_id, kind="note", title=title, ok=True, detail=detail
            ),
        ]

    # -- context use (#979) ---------------------------------------------------

    @staticmethod
    def _context_value(
        state: AntigravityStreamState,
    ) -> tuple[int, int, str] | None:
        """``(pct, window, model)`` for the latest model call, or None.

        The model is agy's own ``init.model`` only — never the id Untether
        would pass or the configured default, which are not proof of what
        agy ran. The window is the published one from
        ``utils/antigravity_context.py`` (agy reports none); a model that is
        not in that table has no percentage."""
        used = state.ctx_used
        model = state.init_model
        if used is None or not model:
            return None
        window = antigravity_context.context_window_for(model)
        if window is None:
            if not state.ctx_miss_logged:
                state.ctx_miss_logged = True
                logger.debug(
                    "antigravity.context.window_miss",
                    model=model[:_MODEL_ROW_CHARS],
                )
            return None
        if used > window and not state.ctx_over_warned:
            state.ctx_over_warned = True
            logger.warning(
                "antigravity.context.over_window",
                session_id=state.session_id,
                model=model[:_MODEL_ROW_CHARS],
                used=used,
                context_window=window,
            )
        return antigravity_context.context_pct(used, window), window, model

    def _note_context(
        self, usage: agy_schema.StepUsage | None, state: AntigravityStreamState
    ) -> list[UntetherEvent]:
        """A finished model call's usage is the new context value: its
        ``input_tokens`` plus ``cache_read_tokens`` (agy reports cache reads
        apart from the input). ``result.usage`` is never used for this — it
        sums every call of the turn and, on a resume, the earlier turns.

        Emits the ``telemetry`` action the shared header path reads (#819),
        only when the shown value changed."""
        if usage is None or not _is_count(usage.input_tokens):
            return []
        used = usage.input_tokens
        if _is_count(usage.cache_read_tokens):
            used += usage.cache_read_tokens
        state.ctx_used = used
        value = self._context_value(state)
        if value is None:
            return []
        pct, window, model = value
        if state.ctx_emitted == (pct, window):
            return []
        state.ctx_emitted = (pct, window)
        return [
            state.factory.action_updated(
                action_id=_CONTEXT_ACTION_ID,
                kind="telemetry",
                title="context",
                detail={
                    "context_pct": pct,
                    "context_used": used,
                    "context_window": window,
                    "model": model,
                    "context_estimated": True,
                },
            )
        ]

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
            if idx is not None:
                self._track_tool_started(state, idx, tool_name, params)
            action = Action(id=action_id, kind=kind, title=title, detail=detail)
            state.pending_actions[action_id] = action
            return [
                factory.action_started(
                    action_id=action_id, kind=kind, title=title, detail=detail
                )
            ]
        err = info.error if info is not None else None
        # #1062: a step agy stopped (``manage_task`` kill) ends DONE but
        # carries ``tool_info.error`` ("context canceled", agy 1.3.3).
        failed = su.state == "ERROR" or (
            err is not None and bool(err.message or err.type)
        )
        end_status = _task_end_status(failed, err)
        if idx is not None:
            state.bg_steps.pop(idx, None)
            self._task_step_ended(state, idx, status=end_status)
            if su.state == "ERROR":
                self._track_subagents(state, idx, [], failed=True)
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
        if failed:
            message = _gate_block_text(
                err.message if err and err.message else "tool failed"
            )[:_ERROR_MESSAGE_CHARS]
            detail = dict(action.detail)
            if err is not None and err.type:
                detail["error_type"] = err.type
            if end_status == "stopped":
                # #1062: a step agy stopped (a ``manage_task`` kill) reads
                # ⏹️ on its row, not ✗; the ⏳ block lists only live tasks.
                detail["stopped"] = True
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

    # -- #978: the background task map (stream only) --------------------------

    @staticmethod
    def _add_task(state: AntigravityStreamState, task: EngineBackgroundTask) -> None:
        if task.task_id in state.tasks:
            return
        if len(state.tasks) >= _TASKS_MAX:
            for tid in [t for t, old in state.tasks.items() if old.status != "running"]:
                del state.tasks[tid]
        state.tasks[task.task_id] = task

    def _track_tool_started(
        self,
        state: AntigravityStreamState,
        idx: int,
        tool_name: str,
        params: dict[str, Any],
    ) -> None:
        """A tool step went ACTIVE: the model has moved on, so a completion
        notice still expected for an earlier task isn't coming; then register
        what this step may leave running."""
        state.bg_notices_owed = 0
        now = state.bg_steps.get(idx) or time.monotonic()
        if tool_name == "run_command":
            # Registered now, listed only from ``background_from``: agy prints
            # nothing while it holds its answer for an async command (A1), so
            # no later line could add the row.
            self._add_task(
                state,
                EngineBackgroundTask(
                    task_id=f"cmd:{idx}",
                    task_type="local_bash",
                    description=_strip_cr(str(params.get("CommandLine") or "command")),
                    started_at=now,
                    background_from=now + _async_wait_s(params),
                ),
            )
        elif tool_name == "schedule":
            # P-B2: ACTIVE for the timer's whole life, answer held.
            seconds = params.get("DurationSeconds")
            timed = isinstance(seconds, int | float) and not isinstance(seconds, bool)
            self._add_task(
                state,
                EngineBackgroundTask(
                    task_id=f"timer:{idx}",
                    task_type="schedule",
                    description=f"timer ({int(seconds)}s)" if timed else "timer",
                    started_at=now,
                ),
            )
            logger.info("antigravity.background.started", task_id=f"timer:{idx}")
        elif tool_name == "invoke_subagent":
            self._track_subagents(state, idx, _subagent_specs(params, None))
        elif tool_name == "manage_task" and params.get("Action") == "kill":
            self._task_killed(state, params.get("TaskId"))
        if idx in state.approval_steps:
            # #1080: the card came before the step's ACTIVE line.
            state.note_approval_wait(idx, True)

    @staticmethod
    def _task_killed(state: AntigravityStreamState, task_id: Any) -> None:
        """#1062: agy's ``manage_task`` kill names the task as
        ``<conversation>/task-<step>``. Only a task in the background can be
        killed, so a command or timer still running ends as stopped and is
        listed even if its wait hadn't passed; one its own cancelled DONE
        already closed keeps that status — or is listed now, if that DONE
        came before the background threshold (the kill's own DONE arrives
        with it, so a fast kill beats the 5 s default; rc4 QA on 1.3.4).
        agy sends a ``system_message`` for the kill (1.3.3 and 1.3.4), so
        one notice is owed."""
        match = _KILLED_TASK_RE.search(task_id) if isinstance(task_id, str) else None
        if match is None:
            return
        step = match.group(1)
        for key in (f"cmd:{step}", f"timer:{step}"):
            task = state.tasks.get(key)
            if task is None:
                task = state.cancelled_steps.pop(key, None)
                if task is None:
                    continue
                task.background_from = None
                AntigravityRunner._add_task(state, task)
            elif task.status == "stopped":
                state.bg_notices_owed += 1
                continue
            elif task.status != "running":
                continue
            else:
                task.background_from = None
                task.status = "stopped"
                task.ended_at = time.monotonic()
            state.bg_notices_owed += 1
            logger.info(
                "antigravity.background.finished",
                task_id=key,
                status="stopped",
                elapsed_s=round((task.ended_at or 0.0) - task.started_at, 1),
                source="manage_task",
            )

    def _track_subagents(
        self,
        state: AntigravityStreamState,
        idx: int,
        specs: list[_SubagentSpec],
        *,
        failed: bool = False,
    ) -> None:
        """One row per subagent of an ``invoke_subagent`` step. The spawn
        step is DONE in well under a second while the children work on, so
        only a failed spawn ends them here; each child's end is a parent
        ``system_message`` (``_note_system_message``)."""
        now = time.monotonic()
        for n, (role, type_name, child) in enumerate(specs):
            key = f"sub:{idx}:{n}"
            task = state.tasks.get(key)
            if task is None and not failed:
                label = f"{role} ({type_name})" if role and type_name else None
                task = EngineBackgroundTask(
                    task_id=key,
                    task_type="local_agent",
                    description=label or role or type_name or "subagent",
                    started_at=now,
                )
                self._add_task(state, task)
                logger.info("antigravity.background.started", task_id=key)
            if task is not None and child:
                task.ref = child
            if child and state.gate is not None:
                # P-H8: a child's tool calls reach the gate with its own id;
                # Ask me's approval card names the child by its role.
                state.gate.add_children([child], role=role or type_name or None)
        if failed:
            prefix = f"sub:{idx}:"
            for key, task in state.tasks.items():
                if key.startswith(prefix) and task.status == "running":
                    task.status = "failed"
                    task.ended_at = now

    @staticmethod
    def _note_overtaken(state: AntigravityStreamState, idx: int) -> None:
        """The model went on (a later tool, subagent or reply step) while a
        ``run_command`` is still ACTIVE: agy runs tool calls one at a time
        (P-H15), so that command went async (P-B4's daemon) — list it now
        rather than at its deadline."""
        for step in state.bg_steps:
            if step >= idx:
                continue
            task = state.tasks.get(f"cmd:{step}")
            if (
                task is not None
                and not task.is_backgrounded
                and step not in state.approval_steps  # #1080
            ):
                task.background_from = None

    @staticmethod
    def _task_step_ended(
        state: AntigravityStreamState, idx: int, *, status: str
    ) -> None:
        """The ``run_command`` / ``schedule`` step *idx* reached DONE / ERROR
        (``completed``, ``failed``, or ``stopped`` when agy killed it)."""
        for key in (f"cmd:{idx}", f"timer:{idx}"):
            task = state.tasks.get(key)
            if task is None or task.status != "running":
                continue
            if not task.is_backgrounded:
                del state.tasks[key]  # finished in the foreground: never a row
                if status == "stopped" and len(state.cancelled_steps) < _TASKS_MAX:
                    # #1062: unless a ``manage_task`` kill names it next.
                    task.status = status
                    task.ended_at = time.monotonic()
                    state.cancelled_steps[key] = task
                continue
            task.status = status
            task.ended_at = time.monotonic()
            if status != "stopped":
                # A kill's notice is owed by the kill (``_task_killed``).
                state.bg_notices_owed += 1
            logger.info(
                "antigravity.background.finished",
                task_id=key,
                status=task.status,
                elapsed_s=round(task.ended_at - task.started_at, 1),
            )

    @staticmethod
    def _note_system_message(state: AntigravityStreamState) -> None:
        """agy tells the parent about finished background work with one
        ``system_message`` step each, with no content in the stream (P-B1;
        the child's id is only in agy's transcript file, which Untether does
        not read). One owed to a command / timer that just ended is spent on
        it; otherwise it is a subagent finishing, and with nothing to say
        which, the oldest running one — first in, first out."""
        if state.bg_notices_owed > 0:
            state.bg_notices_owed -= 1
            return
        for key, task in state.tasks.items():
            if task.task_type == "local_agent" and task.status == "running":
                task.status = "completed"
                task.ended_at = time.monotonic()
                logger.info(
                    "antigravity.background.finished",
                    task_id=key,
                    status="completed",
                    elapsed_s=round(task.ended_at - task.started_at, 1),
                    attribution="fifo",
                )
                return

    @staticmethod
    def _end_tasks(state: AntigravityStreamState, *, ok: bool) -> None:
        """The run is over: nothing is listed as running after it. A
        ``run_command`` still ACTIVE at a successful result is a daemon task
        agy leaves behind (P-B4) or upstream #1044 — ended, not failed."""
        now = time.monotonic()
        left = [key for key, task in state.tasks.items() if task.status == "running"]
        for key in left:
            state.tasks[key].status = "ended"
            state.tasks[key].ended_at = now
        state.bg_notices_owed = 0
        state.cancelled_steps.clear()
        outlived = [key for key in left if key.startswith("cmd:")]
        if ok and outlived:
            logger.info("antigravity.background.outlived", tasks=outlived)

    @staticmethod
    def _background_title_updates(
        state: AntigravityStreamState,
    ) -> list[UntetherEvent]:
        """08 §6: retitle a background-capable step as ``⏳ background:
        <command>`` once it counts as background (a ``run_command`` when its
        task does, #978; the others after 5 s), once, when another stdout
        line arrives (no timers in the translator). The row closes under its
        original title."""
        if not state.bg_steps:
            return []
        now = time.monotonic()
        out: list[UntetherEvent] = []
        for idx, since in state.bg_steps.items():
            if idx in state.bg_retitled:
                continue
            task = state.tasks.get(f"cmd:{idx}")
            if task is not None:
                due = task.is_backgrounded
            else:
                due = now - since > _BACKGROUND_TITLE_AFTER_S
            if not due:
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
        self._end_tasks(state, ok=ok)
        return out

    def _answer(self, state: AntigravityStreamState, response: str | None) -> str:
        if response:
            return response
        return "\n\n".join(
            state.text_by_step[i].rstrip("\n") for i in sorted(state.text_by_step)
        )

    def _plan_review(
        self,
        state: AntigravityStreamState,
        answer: str,
        usage: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """#981: a finished Plan first turn → the plan in the final, plus the
        Approve / Deny / Let's discuss card (``usage[REVIEW_CARD_KEY]``, sent
        by the bridge right after the final). Unattended runs (#835) post the
        plan and change nothing: no card, a 🔒 footer instead. A turn that
        wrote no plan is an ordinary answer with a one-line note."""
        from ..utils import antigravity_plan
        from ..utils.paths import (
            get_run_context,
            get_run_sender_id,
            get_run_thread_id,
        )

        plan = antigravity_plan.find_plan(state.session_id, state.plan_writes)
        if plan is None:
            logger.info(
                "antigravity.plan.no_artifact",
                session_id=state.session_id,
                writes=len(state.plan_writes),
                trigger=state.unattended,
            )
            if state.unattended is None:
                answer = _append_paragraph(answer, antigravity_plan.NO_PLAN_NOTE)
            return answer, usage
        answer = antigravity_plan.plan_answer(plan, answer)
        logger.info(
            "antigravity.plan.detected",
            session_id=state.session_id,
            name=plan.name,
            chars=len(plan.body),
            truncated=plan.truncated,
            trigger=state.unattended,
        )
        if state.unattended is not None:
            label = antigravity_plan.UNATTENDED_DENIED_LABEL
            logger.warning(
                "permission.unattended_deny",
                tool_name=label,
                trigger_source=state.unattended,
                session_id=state.session_id,
                permission_mode="plan",
                reason="plan_mode",
            )
            usage = {
                **usage,
                "unattended": {
                    "trigger": state.unattended,
                    "mode": "plan",
                    "denied": {label: 1},
                    "hint": antigravity_plan.unattended_hint(state.unattended),
                },
            }
            return answer, usage
        base = get_run_base_dir()
        pending = antigravity_plan.register(
            conversation_id=state.session_id or "",
            name=plan.name,
            channel_id=get_run_channel_id(),
            thread_id=get_run_thread_id(),
            context=get_run_context(),
            cwd=str(base) if base is not None else None,
            originator=get_run_sender_id(),
        )
        logger.info(
            "antigravity.plan.card",
            session_id=state.session_id,
            request_id=pending.request_id,
        )
        state.superseded_cards.extend(pending.extra.get("superseded", ()))
        usage = {
            **usage,
            REVIEW_CARD_KEY: {
                "id": pending.request_id,
                "text": antigravity_plan.card_text(plan.name),
                "buttons": antigravity_plan.keyboard(pending.request_id),
            },
        }
        return answer, usage

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
        if state.gate is not None:
            # Nothing is injected after the result; a steer still queued (or
            # injected but never seen) is reported by the bridge's run-end
            # sweep ("send it again").
            state.gate.note_result()
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
                    denied_labels,
                    mode=state.effective_mode,
                    trigger=state.unattended,
                    kept_shell=_kept_shell_agent(state, denied_labels),
                ),
            )
        if status == "SUCCESS":
            notice = self._oauth_notice(state)
            if notice is not None:
                answer = _append_paragraph(answer, notice)
        usage = self._usage(res, state)
        self._log_timing(res, state)
        resume_token = self._resume_for_completed(state, resume)
        if state.gate is not None and (gated := state.gate.unattended_usage()):
            # #835 parity: an unattended Ask me run's gate denials → 🔒 footer.
            usage = {**usage, "unattended": gated}
        if status == "SUCCESS" and state.effective_mode == "plan":
            answer, usage = self._plan_review(state, answer, usage)
        elif status == "SUCCESS" and state.approved_plan is not None:
            from ..utils import antigravity_plan

            answer = _prepend_paragraph(
                answer, antigravity_plan.approved_header(state.approved_plan)
            )
        if state.superseded_cards:
            usage = _with_retired_cards(usage, state.superseded_cards)
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
            label = _denied_label(action, state.effective_mode)
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

    def _usage(
        self, res: agy_schema.ResultPayload, state: AntigravityStreamState
    ) -> dict[str, Any]:
        """Flat per-run usage. agy's ``usage`` is session-cumulative (04 turns
        it into a per-run delta); ``num_turns`` / ``duration_seconds`` are
        cumulative too and never reported (08 §8, #952). ``context`` (#979)
        is the latest model call's context value, for the
        ``runner.completed`` log."""
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
        if state.run_tokens:
            # #1062: the bridge's per-run figure (``_apply_token_delta``).
            usage[RUN_TOKENS_KEY] = dict(state.run_tokens)
        if state.t_spawn:
            usage["duration_ms"] = int((time.monotonic() - state.t_spawn) * 1000)
        context = self._context_value(state)
        if context is not None:
            pct, window, model = context
            usage["context"] = {
                "pct": pct,
                "used": state.ctx_used,
                "window": window,
                "model": model,
                "estimated": True,
            }
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


def _kept_shell_agent(state: AntigravityStreamState, labels: list[str]) -> bool:
    """#1060: a shell command was denied on a resumed run that named
    Untether's no-shell agent. That agent has no shell tool, so the
    conversation is still on the agent it started with (agy 1.3.3 ignores
    ``--agent`` on ``--conversation``; ``init.agent`` echoes the flag either
    way, so the stream can't say so before the turn)."""
    if not state.resumed or "shell command" not in labels:
        return False
    return any(arg.startswith("--agent=") for arg in state.argv or ())


# agy's error on a tool its PreToolUse hook denied (E2, agy 1.3.3).
_HOOK_DENIED_PREFIX = "tool call denied by pre-tool hook:"


def _gate_block_text(message: str) -> str:
    """#980: a tool Untether's gate blocked reads as that, not as agy's
    error plus the long reason meant for the model."""
    if not message.startswith(_HOOK_DENIED_PREFIX):
        return message
    from ..antigravity import gate_server

    reason = message[len(_HOOK_DENIED_PREFIX) :].strip()
    if reason.startswith(gate_server.DENY_TEXT[:40]):
        return "blocked: denied in Telegram"
    if reason.startswith("No answer in Telegram within"):
        return "blocked: no answer in Telegram in time"
    if reason.startswith("Untether: this is an unattended"):
        return "blocked: unattended run, nobody to approve"
    # Another hook's reason (or a question's answer): agy's own words.
    return message


def _add_run_tokens(totals: dict[str, int], usage: agy_schema.StepUsage | None) -> None:
    """#1062: add one model call's usage to the run's own totals, under the
    names ``_usage`` gives the result's."""
    if usage is None:
        return
    for key, value in (
        ("input_tokens", usage.input_tokens),
        ("output_tokens", usage.output_tokens),
        ("cache_read_tokens", usage.cache_read_tokens),
        ("reasoning_tokens", usage.thinking_tokens),
    ):
        if _is_count(value):
            totals[key] = totals.get(key, 0) + value


def _with_retired_cards(usage: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    """#1074: the replaced plan cards still showing buttons, for the bridge
    to edit (``usage[RETIRED_CARDS_KEY]``)."""
    from ..utils import antigravity_plan

    retired = antigravity_plan.retire(ids)
    return {**usage, RETIRED_CARDS_KEY: retired} if retired else usage


def _task_end_status(failed: bool, err: agy_schema.ToolError | None) -> str:
    """#1062: how a background task's step ended. agy reports a task it
    killed itself (``manage_task``) as DONE with a cancellation error."""
    if not failed:
        return "completed"
    message = (err.message or "") if err is not None else ""
    return "stopped" if _CANCELLED_RE.search(message) else "failed"


def _step_of(action_id: str) -> int:
    try:
        return int(action_id.removeprefix("step-"))
    except ValueError:
        return -1


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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


def _prepend_paragraph(answer: str, paragraph: str) -> str:
    if not answer.strip():
        return paragraph
    return f"{paragraph}\n\n{answer.lstrip()}"


# ── #1045: ⚠️ rows in the final answer ──────────────────────────────────────


def _note_warning(titles: list[str], evt: UntetherEvent) -> None:
    """Keep a ⚠️ row's text (``_warning_event``'s shape) for the final.

    Left out: a ``⚠️ Blocked`` row, which the denial paragraph already says
    in the answer (#1037), and the base runner's ✗ notes (``ok=False``:
    stderr, AGY_ERROR, bad JSON), whose text the error final carries."""
    if not isinstance(evt, ActionEvent) or evt.phase != "completed":
        return
    action = evt.action
    if action.kind != "warning" or evt.ok is not True or evt.level != "warning":
        return
    if action.detail.get("denied"):
        return
    title = str(action.title or "").strip()
    if title and title not in titles:
        titles.append(title)


# #1083: an Untether command a row names as code (`untether agy-gate status`).
_UNTETHER_COMMAND_SPAN = re.compile(r"`(untether [^`\n]+)`")


def _escape_keeping_commands(line: str) -> str:
    """Escaped, except the ``untether …`` commands a row writes as code,
    which stay code spans (as in the progress row and the 🔐 page) rather
    than showing their backticks."""
    out: list[str] = []
    pos = 0
    for match in _UNTETHER_COMMAND_SPAN.finditer(line):
        out.append(escape_markdown(line[pos : match.start()]))
        out.append(inline_code(match.group(1)))
        pos = match.end()
    out.append(escape_markdown(line[pos:]))
    return "".join(out)


def final_warning_block(titles: list[str]) -> str | None:
    """One line per distinct row, escaped: the rows carry project paths."""
    lines = list(dict.fromkeys(t for t in titles if t))
    if not lines:
        return None
    return HARD_BREAK.join(_escape_keeping_commands(line) for line in lines)


def _with_warning_block(evt: CompletedEvent, titles: list[str]) -> CompletedEvent:
    """The progress message carrying the rows is replaced by the final, so
    the final repeats them after the answer, like the denial paragraph and
    the OAuth notice (#1045). A person's run records new config as seen, so
    that person has to have been shown it."""
    block = final_warning_block(titles)
    if block is None:
        return evt
    return replace(evt, answer=_append_paragraph(evt.answer, block))


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    """Build an ``AntigravityRunner`` from ``[antigravity]`` config.

    Keys: ``model`` (str), ``cmd`` (str, ``~`` expanded),
    ``permission_mode`` (``workspace`` / ``ask`` / ``plan`` / ``full``,
    validated here; ``full`` warns once) and ``approval_timeout_s`` (#980:
    Ask me's approval wait, 60-3000 s, capped by the gate plugin's own
    wait). Unknown keys — including PR #766's
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
    if model is not None:
        from ..utils import antigravity_argv

        try:
            antigravity_argv.check_model(model)
        except antigravity_argv.AgyArgvError as exc:
            logger.warning(
                "antigravity.config.invalid",
                error=f"model is not a valid model id ({exc.reason})",
                config_path=str(config_path),
            )
            raise ConfigError(
                f"Invalid `antigravity.model` in {config_path}; expected a model "
                "id made of letters, digits and . _ - : / with no spaces and no "
                "leading dash (for example gemini-3.8-flash)."
            ) from None
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
    timeout = config.get("approval_timeout_s")
    if timeout is not None:
        from ..antigravity import gate_plugin

        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not gate_plugin.MIN_WAIT_S <= timeout <= gate_plugin.MAX_WAIT_S
        ):
            logger.warning(
                "antigravity.config.invalid",
                error="approval_timeout_s out of range",
                config_path=str(config_path),
            )
            raise ConfigError(
                f"Invalid `antigravity.approval_timeout_s` in {config_path}; "
                f"expected whole seconds from {gate_plugin.MIN_WAIT_S} to "
                f"{gate_plugin.MAX_WAIT_S}."
            )
    cmd = os.path.expanduser(raw_cmd) if raw_cmd else default_antigravity_cmd()
    return AntigravityRunner(
        antigravity_cmd=cmd,
        model=model or None,
        default_permission_mode=mode,
        config_path=config_path,
        approval_timeout_s=timeout,
    )


BACKEND = EngineBackend(
    id="antigravity",
    build_runner=build_runner,
    cli_cmd="agy",
    install_cmd="curl -fsSL https://antigravity.google/cli/install.sh | bash",
)
