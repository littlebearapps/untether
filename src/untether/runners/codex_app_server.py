"""Codex ``app-server`` transport (#962) and its support gate (#961).

Detect, don't assume: before the first app-server spawn per installed binary,
Untether asks the CLI for its own protocol schema (``codex app-server
generate-json-schema``, stable and ``--experimental``; zero tokens, ~0.2 s
each) and checks every method it depends on is there at the expected tier
(``schemas/codex_app_server.py``). A CLI that fails the check is refused
before spawn; the version is only quoted in the message.

``CodexAppServerRunner`` (``[engines.codex] transport = "app-server"``) runs
one private ``codex app-server --listen stdio://`` per run over the
single-reader JSON-RPC peer in ``codex_rpc.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import msgspec
from anyio.streams.buffered import BufferedByteReceiveStream

from .. import __version__
from ..events import EventFactory
from ..interactive.registry import drop_session, register, withdraw
from ..logging import get_logger
from ..markdown import format_duration
from ..model import (
    ActionEvent,
    ActionPhase,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    UntetherEvent,
)
from ..runner import PRESPAWN_BLOCKED_KEY, JsonlStreamState, JsonlSubprocessRunner
from ..schemas import codex as codex_schema
from ..schemas import codex_app_server as cas
from ..schemas.codex_app_server import (
    CODEX_APP_SERVER_SENDS,
    CODEX_APP_SERVER_TYPES,
    CODEX_APP_SERVER_USED,
    MIN_TESTED_APP_SERVER_VERSION,
)
from ..utils.antigravity_argv import COMPACT_COMMAND, native_compact_prompt
from ..utils.antigravity_context import context_pct
from ..utils.error_display import redact_url_secrets
from ..utils.paths import get_run_base_dir, relativize_path
from ..utils.streams import _MAX_LINE_BYTES
from ..utils.subprocess import kill_process, terminate_process
from . import codex_approvals as approvals
from . import codex_reads
from .codex import (
    ENGINE,
    CodexRunner,
    CodexRunState,
    _AgentMessageSummary,
    _parse_reconnect_message,
    _select_final_answer,
    _translate_item_event,
    _warn_webhook_unknown_mode,
    _warning_display_text,
    _webhook_safe_trigger,
)
from .codex_rpc import (
    DECLINE_HANDLERS,
    DEFER,
    WRITE_TIMEOUT_S,
    CodexRpcClosed,
    CodexRpcPeer,
    CodexRpcWriteTimeout,
    Notification,
    Response,
    ServerRequest,
)
from .run_options import CODEX_PLAN_MODES, get_run_options

logger = get_logger(__name__)

APP_SERVER_UNSUPPORTED_BLOCK = "codex_app_server_unsupported"
SCHEMA_BUNDLE_FILE = "codex_app_server_protocol.schemas.json"
_PROBE_TIMEOUT_S = 30.0
_UNIONS = {
    "client_request": "ClientRequest",
    "client_notification": "ClientNotification",
    "server_notification": "ServerNotification",
    "server_request": "ServerRequest",
}

type Bundle = dict[str, Any]
# key → (tier, {name: tier}); see ``schema_subset``.
type Subset = dict[str, dict[str, Any]]


# --- schema subset ----------------------------------------------------------


def _resolve(bundle: Bundle, ref: str) -> dict[str, Any] | None:
    node: Any = bundle
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _definition(bundle: Bundle, name: str) -> dict[str, Any] | None:
    defs = bundle.get("definitions", {})
    found = defs.get(name)
    if found is None:
        found = defs.get("v2", {}).get(name)
    return found if isinstance(found, dict) else None


def _props(defn: dict[str, Any] | None) -> set[str]:
    return set((defn or {}).get("properties", {}))


def _string_values(defn: dict[str, Any] | None) -> set[str]:
    if defn is None:
        return set()
    values = {v for v in defn.get("enum", ()) if isinstance(v, str)}
    for variant in defn.get("oneOf", ()):
        values |= {v for v in variant.get("enum", ()) if isinstance(v, str)}
        # #963: an object variant (``{"acceptWithExecpolicyAmendment": …}``)
        # counts by its only required key.
        required = variant.get("required", ())
        if variant.get("type") == "object" and len(required) == 1:
            values.add(required[0])
    return values


def _flat_view(bundle: Bundle) -> dict[str, set[str]]:
    """One bundle as ``key → names`` for everything Untether depends on."""
    view: dict[str, set[str]] = {}
    for kind, union_name in _UNIONS.items():
        union = _definition(bundle, union_name) or {}
        for variant in union.get("oneOf", ()):
            props = variant.get("properties", {})
            methods = props.get("method", {}).get("enum", ())
            if len(methods) != 1 or (kind, methods[0]) not in CODEX_APP_SERVER_USED:
                continue
            names: set[str] = set()
            params_ref = props.get("params", {}).get("$ref")
            params = _resolve(bundle, params_ref) if params_ref else None
            names |= {f"params.{p}" for p in _props(params)}
            if kind.endswith("request") and params is not None:
                title = str(params.get("title", ""))
                response = _definition(
                    bundle, title.removesuffix("Params") + "Response"
                )
                names |= {f"result.{p}" for p in _props(response)}
            view[f"{kind} {methods[0]}"] = names
    for type_name, variants in CODEX_APP_SERVER_TYPES.items():
        defn = _definition(bundle, type_name)
        if defn is None:
            continue
        if not variants:
            view[f"type {type_name}"] = _props(defn)
            continue
        for variant in defn.get("oneOf", ()):
            tags = variant.get("properties", {}).get("type", {}).get("enum", ())
            if len(tags) == 1 and tags[0] in variants:
                view[f"type {type_name}.{tags[0]}"] = _props(variant)
    for enum_name in CODEX_APP_SERVER_SENDS:
        values = _string_values(_definition(bundle, enum_name))
        if values:
            view[f"enum {enum_name}"] = values
    return view


def schema_subset(stable: Bundle, experimental: Bundle) -> Subset:
    """The tiered subset of both bundles: ``{key: {"tier", "names"}}``.

    A key or name in the stable bundle is ``S``; one only in the experimental
    bundle is ``E``. This is what the committed fixture holds and what the
    drift test and the gate compare.
    """
    s_view, e_view = _flat_view(stable), _flat_view(experimental)
    subset: Subset = {}
    for key in sorted(s_view.keys() | e_view.keys()):
        s_names = s_view.get(key, set())
        names = s_names | e_view.get(key, set())
        subset[key] = {
            "tier": "S" if key in s_view else "E",
            "names": {n: ("S" if n in s_names else "E") for n in sorted(names)},
        }
    return subset


def unsupported_methods(subset: Subset) -> list[str]:
    """What blocks the transport: a used method missing, or one Untether
    uses as stable that is now experimental-only."""
    problems: list[str] = []
    for (kind, method), tier in CODEX_APP_SERVER_USED.items():
        found = subset.get(f"{kind} {method}")
        if found is None:
            problems.append(f"`{method}` missing")
        elif tier == "S" and found["tier"] != "S":
            problems.append(f"`{method}` is experimental-only")
    return problems


# --- probing the installed CLI ----------------------------------------------


def _run(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 — fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        check=False,
    )


def generate_schema_bundle(cmd: str, out_dir: Path, *, experimental: bool) -> Bundle:
    """Run ``codex app-server generate-json-schema`` and load the bundle.

    Raises ``RuntimeError`` (rc != 0, no bundle) or ``OSError`` /
    ``subprocess.TimeoutExpired`` (could not run).
    """
    argv = [cmd, "app-server", "generate-json-schema", "--out", str(out_dir)]
    if experimental:
        argv.insert(3, "--experimental")
    proc = _run(argv, str(out_dir.parent))
    bundle_path = out_dir / SCHEMA_BUNDLE_FILE
    if proc.returncode != 0 or not bundle_path.is_file():
        raise RuntimeError(f"generate-json-schema exited rc={proc.returncode}")
    return json.loads(bundle_path.read_text(encoding="utf-8"))


def probe_schema_subset(cmd: str) -> Subset:
    """Both bundles from the installed CLI → ``schema_subset`` (temp dir cleaned)."""
    with tempfile.TemporaryDirectory(prefix="untether-codex-schema-") as tmp:
        root = Path(tmp)
        stable = generate_schema_bundle(cmd, root / "stable", experimental=False)
        experimental = generate_schema_bundle(cmd, root / "exp", experimental=True)
    return schema_subset(stable, experimental)


@dataclass(frozen=True, slots=True)
class AppServerSupport:
    version: str | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


_SUPPORT_CACHE: dict[tuple[str, float], AppServerSupport] = {}


def _is_compact(prompt: str) -> bool:
    """#964: only a bare ``/compact`` (or ``/compact@bot``) is Codex's
    compaction; ``/compact <text>`` stays an ordinary prompt (D6)."""
    return native_compact_prompt(prompt) == COMPACT_COMMAND


def _cli_version(path: str) -> str | None:
    try:
        proc = _run([path, "--version"], tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out.removeprefix("codex-cli ").strip() or None


def codex_app_server_support(cmd: str) -> AppServerSupport:
    """#961: can this Codex CLI run Untether's app-server transport?

    Cached per (binary path, mtime), so a Codex auto-update re-checks. A
    probe that could not run (timeout, OS error) is not cached. Blocking:
    call it off the event loop.
    """
    path = shutil.which(cmd) or (cmd if os.path.isabs(cmd) else None)
    if path is None:
        return AppServerSupport(None, (f"`{cmd}` not found on PATH",))
    try:
        real = os.path.realpath(path)
        key = (real, os.stat(real).st_mtime)
    except OSError as exc:
        return AppServerSupport(None, (f"cannot stat `{cmd}`: {exc.strerror}",))
    cached = _SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    version = _cli_version(path)
    try:
        problems = tuple(unsupported_methods(probe_schema_subset(path)))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "codex.app_server.probe_failed", version=version, error=type(exc).__name__
        )
        return AppServerSupport(version, ("schema probe could not run",))
    except (RuntimeError, ValueError) as exc:
        # Old CLI without the subcommand, or an unreadable bundle.
        problems = (f"schema unavailable ({exc})",)
    support = AppServerSupport(version, problems)
    _SUPPORT_CACHE[key] = support
    if support.ok:
        logger.info("codex.app_server.ready", version=version)
    else:
        logger.error(
            "codex.app_server.unsupported", version=version, problems=list(problems)
        )
    return support


def unsupported_message(support: AppServerSupport) -> str:
    version = support.version or "unknown version"
    detail = "; ".join(support.problems[:3])
    return (
        f"🛑 This Codex CLI ({version}) doesn't provide the app-server protocol "
        f"Untether needs ({detail}), so this run wasn't started. The app-server "
        f"transport is tested with codex-cli {MIN_TESTED_APP_SERVER_VERSION} or "
        'later: upgrade Codex, or switch to `transport = "exec"` under `[engines.codex]`.'
    )


async def app_server_unsupported_event(
    cmd: str, resume: ResumeToken | None
) -> CompletedEvent | None:
    """The pre-spawn refusal for ``run_impl``, or None when supported."""
    support = await anyio.to_thread.run_sync(codex_app_server_support, cmd)
    if support.ok:
        return None
    return EventFactory("codex").completed_error(
        error=unsupported_message(support),
        resume=resume,
        # Never ran the engine: keep the chat's saved session (#838).
        usage={PRESPAWN_BLOCKED_KEY: APP_SERVER_UNSUPPORTED_BLOCK},
    )


# --- the transport (#962) ---------------------------------------------------

# Streams Untether never renders (it reads completed items, not deltas).
OPT_OUT_NOTIFICATIONS: tuple[str, ...] = (
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "item/commandExecution/outputDelta",
    "item/fileChange/outputDelta",
)
# #965: an experimental connection (plan mode) also gets these; Untether
# reads neither (Δ D40, F19).
_EXPERIMENTAL_OPT_OUTS: tuple[str, ...] = (
    "turn/moderationMetadata",
    "thread/readState/changed",
)
# /continue: without sourceKinds the server lists only cli + vscode, which
# would miss exec-made threads. Sub-agent kinds stay out.
CONTINUE_SOURCE_KINDS: tuple[str, ...] = ("cli", "vscode", "exec", "appServer")
# The `sandbox` echo is a SandboxPolicy object; Untether sends SandboxMode.
_SANDBOX_POLICY_MODES = {
    "readOnly": "read-only",
    "workspaceWrite": "workspace-write",
    "dangerFullAccess": "danger-full-access",
    "externalSandbox": "danger-full-access",
}
_WRITABLE_SANDBOX_MODES = frozenset({"workspace-write", "danger-full-access"})
_EXIT_GRACE_S = 5.0
_STOPPED_INPUT = "codex app-server stopped accepting input"
_EXITED_EARLY = "codex app-server exited before the turn finished"
_UNTRUSTED_READ_ONLY_NOTE = (
    "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} This project isn't in Codex's"
    " trusted list, and a writable sandbox would make Codex trust it for this and"
    " future chats. Untether doesn't trust projects for you, so this run is"
    ' read-only. To allow writes, add `[projects."<repo root>"] trust_level ='
    ' "trusted"` to `~/.codex/config.toml`.'
)
_NO_EARLIER_SESSION_NOTE = (
    "No earlier Codex session in this project; started a new one."
)
_UNKNOWN_MODE_SAFE_WARNED: set[str] = set()
_INTERRUPT_BOUND_S = 3.0
_WARNING = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
# #1240 on app-server: the untrusted-project notice is a configWarning here,
# not exec's stderr line. Stable prefix only (codex-cli 0.170.0).
_UNTRUSTED_CONFIG_WARNING_PREFIX = (
    "Project-local config, hooks, and exec policies are disabled"
)
# Before the run has a thread nobody can be shown a card (never seen: Codex
# asks only mid-turn), so the peer's fail-closed decline stands.
_APPROVAL_DECLINED_NOTE = (
    f"{_WARNING} Codex asked for approval before the run started; declined"
)
_STOPPED_BY_CARD = (
    "\N{OCTAGONAL SIGN} Stopped: tell Codex what to do differently in your"
    " next message."
)
_WITHDRAWN = "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16} Withdrawn by Codex"
# Items with no row: the prompt echo and hook text (plan: ``_item_rows``, #965).
_SILENT_ITEM_TYPES = frozenset({"userMessage", "hookPrompt"})
# #832: Claude's 🗜️ wording (#819), so both engines read the same.
_COMPACTING_TITLE = "\N{COMPRESSION}\N{VARIATION SELECTOR-16} Compacting context…"
_COMPACTED_TITLE = "\N{COMPRESSION}\N{VARIATION SELECTOR-16} Context compacted"
# #964: a bare /compact with no saved Codex session (same pre-spawn key as
# Antigravity's #1114, so the bridge answers it as plain text).
NOTHING_TO_COMPACT_TEXT = (
    "Nothing to compact: this chat has no Codex session yet. "
    "Send a normal message to start one."
)
COMPACT_NO_CONVERSATION_BLOCK = "compact_no_conversation"
# #964: set by ``codex.build_runner`` when ``transport = "app-server"``; the
# steer target answers "not supported" on exec.
APP_SERVER_ACTIVE = False
# #964: how long a steer waits for Codex's answer (Codex answers in ms, P-S1).
STEER_RESPONSE_S = 5.0
# #964-3a: the same bound for a follow-up turn/start.
FOLLOWUP_RESPONSE_S = 5.0
_TURN_EXITED = "codex app-server exited before the turn finished"
_SYSTEM_ERROR_NOTE = (
    "Codex reported a system error on this thread; the turn may not finish."
)
_CONTEXT_ACTION_ID = "codex.context"
# #965 D1: the lost-marker fallback (restart, /continue, a TUI-planned thread).
_PLAN_STUCK_NOTE = (
    f"{_WARNING} This Codex thread was still in Plan mode, so this turn planned"
    " instead of acting; send your message again"
)
_PLAN_STEP_MARKS = {"completed": "✓", "inProgress": "▸", "pending": "·"}
_UNKNOWN_ITEMS_LOGGED: set[str] = set()
# App-server names -> exec's, so exec's title helpers give the same rows.
_EXEC_STATUS = {"inProgress": "in_progress"}
_EXEC_WEB_ACTION = {"openPage": "open_page", "findInPage": "find_in_page"}


def codex_error_code(info: Any) -> str | None:
    """``TurnError.codexErrorInfo`` as one name: a string variant as is, an
    object variant (``{"httpConnectionFailed": {...}}``) by its only key."""
    if isinstance(info, str):
        return info or None
    if isinstance(info, dict) and len(info) == 1:
        key = next(iter(info))
        return key if isinstance(key, str) else None
    return None


def _exec_status(status: str) -> Any:
    return _EXEC_STATUS.get(status, status)


def _exec_item(item: cas.ThreadItem) -> codex_schema.ThreadItem | None:
    """The exec item an app-server item corresponds to, so exec's own
    translation renders it; None when exec has no such item."""
    match item:
        case cas.AgentMessageItem():
            return codex_schema.AgentMessageItem(
                id=item.id, text=item.text, phase=item.phase
            )
        case cas.CommandExecutionItem():
            return codex_schema.CommandExecutionItem(
                id=item.id,
                command=item.command,
                aggregated_output=item.aggregated_output or "",
                exit_code=item.exit_code,
                status=_exec_status(item.status),
            )
        case cas.FileChangeItem():
            # Each change's ``kind`` is ``{"type": "add"}`` here, ``"add"`` on exec.
            changes = [
                {**c, "kind": c["kind"].get("type")}
                if isinstance(c, dict) and isinstance(c.get("kind"), dict)
                else c
                for c in item.changes
            ]
            return codex_schema.FileChangeItem(
                id=item.id, changes=changes, status=_exec_status(item.status)
            )
        case cas.McpToolCallItem():
            result, error = item.result, item.error
            if isinstance(error, dict):
                error = error.get("message")
            return codex_schema.McpToolCallItem(
                id=item.id,
                server=item.server,
                tool=item.tool,
                arguments=item.arguments,
                result=codex_schema.McpToolCallItemResult(
                    content=result.get("content") or [],
                    structured_content=result.get("structuredContent"),
                )
                if isinstance(result, dict)
                else None,
                error=None
                if error is None
                else codex_schema.McpToolCallItemError(message=str(error)),
                status=_exec_status(item.status),
            )
        case cas.WebSearchItem():
            action = item.action
            if isinstance(action, dict) and action.get("type") in _EXEC_WEB_ACTION:
                action = {**action, "type": _EXEC_WEB_ACTION[action["type"]]}
            return codex_schema.WebSearchItem(
                id=item.id, query=item.query, action=action, results=item.results
            )
        case cas.ReasoningItem():
            text = "\n".join(s for s in item.summary if isinstance(s, str)).strip()
            return codex_schema.ReasoningItem(id=item.id, text=text)
    return None


def sandbox_policy_mode(policy: Any) -> str | None:
    """A ``SandboxPolicy`` echo (``{"type": "readOnly", …}``) as the
    kebab-case ``SandboxMode`` Untether sends; a string passes through."""
    if isinstance(policy, str):
        return policy
    if isinstance(policy, dict):
        return _SANDBOX_POLICY_MODES.get(policy.get("type"))  # type: ignore[arg-type]
    return None


def selected_project_entry(projects: Any, cwd: str, repo_root: str | None) -> Any:
    """The ``[projects]`` entry (from ``config/read``) Codex applies to *cwd*:
    the first key present, in ``ProjectTrustLookup::from_paths`` order
    (codex-cli 0.171.0): canonical cwd, cwd, canonical repo root, repo root.
    No parent walk, and a cwd entry shadows the root's even with no
    ``trust_level`` (#1408). config/read has no trust field, so Untether
    mirrors the lookup (gap register)."""
    if not isinstance(projects, dict) or not cwd:
        return None
    for path in (cwd, repo_root):
        if not path:
            continue
        for key in dict.fromkeys((str(Path(path).resolve()), path)):
            if key in projects:
                return projects[key]
    return None


def git_repo_root(cwd: Path) -> str | None:
    """*cwd*'s repo root as Codex finds it for trust (codex-cli 0.171.0
    ``git-utils/src/trust.rs`` ``resolve_root``): walk up from *cwd* to the
    first ``.git``; a ``.git`` directory without ``HEAD`` is skipped. Only a
    real ``.git`` directory with ``HEAD`` yields a root: a ``.git`` file
    (linked worktree, submodule, ``--separate-git-dir``) or any symlink fails
    closed (#1408; worktrees: #1413). No ``git`` call, so no hang and no
    divergence from Codex's filesystem walk."""
    base = cwd if cwd.is_dir() else cwd.parent
    for candidate in (base, *base.parents):
        dot_git = candidate / ".git"
        if not os.path.lexists(dot_git):
            continue
        if dot_git.is_symlink() or not dot_git.is_dir():
            return None
        head = dot_git / "HEAD"
        if head.is_symlink():
            return None
        if os.path.lexists(head):
            return str(candidate)
    return None


@dataclass(frozen=True, slots=True)
class Tier:
    """One Codex permission tier (#421) as ``thread/start`` fields.
    ``sandbox`` None = Codex's own setting (Full auto). A writable sandbox
    (or None) is sent only where ``config/read`` lists the project (DEC-7, D1)."""

    approval_policy: str
    sandbox: str | None
    reviewer: str = "user"
    # Footer label; None for Full auto (the default shows none).
    label: str | None = None


# #421 / #963 (DEC-5 labels): mode → tier. Unknown modes run as Safe (DEC-5).
TIERS: dict[str, Tier] = {
    "auto": Tier("never", None),
    "safe": Tier("never", "read-only", label="safe"),
    "ask": Tier("on-request", "workspace-write", label="ask me"),
    "ask-writes": Tier("on-request", "read-only", label="ask before writes"),
    "review": Tier("on-request", "workspace-write", "auto_review", "auto-review"),
    # #965 D3: plan turns run the Ask before writes triple.
    "plan": Tier("on-request", "read-only", label="plan first"),
    "plan-auto": Tier("on-request", "read-only", label="plan first (auto)"),
}


def _tier(mode: str | None, *, unattended: bool) -> Tier:
    tier = TIERS.get("auto" if mode is None else mode)
    if tier is None:
        _warn_unknown_mode_safe(str(mode))  # DEC-5: fail closed
        tier = TIERS["safe"]
    if unattended and tier.approval_policy != "never":
        # #835 / D4: nobody can answer; Codex asks nothing and actions that
        # need approval fail inside the sandbox.
        tier = Tier("never", tier.sandbox, "user", tier.label)
    return tier


def _warn_unknown_mode_safe(mode: str) -> None:
    if mode in _UNKNOWN_MODE_SAFE_WARNED:
        return
    _UNKNOWN_MODE_SAFE_WARNED.add(mode)
    logger.warning(
        "codex.app_server.unknown_mode_safe",
        mode=mode,
        note="unknown Codex permission_mode runs read-only on app-server (DEC-5)",
    )


def _run_model() -> str | None:
    """The chat's /model override (or a trigger's model), if any."""
    run_options = get_run_options()
    if run_options is not None and run_options.model:
        return str(run_options.model)
    return None


def _config_model(result: Any) -> str | None:
    """``config.model`` from a config/read result (#1369).

    shortcut: with no model in Codex's config a resumed thread stays on its
    saved model (the modelNotFound hint says /new); model/list's
    ``isDefault`` would cover it at one more request."""
    config = result.get("config") if isinstance(result, dict) else None
    model = config.get("model") if isinstance(config, dict) else None
    return model if isinstance(model, str) and model else None


@dataclass(slots=True)
class AppServerRunState(CodexRunState):
    prompt: str = ""
    cwd: str = ""
    # #421: the run's tier; ``sandbox`` is what thread/start|resume sends
    # (None = Codex's own setting), read-only where trust would be saved.
    tier: Tier = field(default_factory=lambda: TIERS["safe"])
    sandbox: str | None = None
    # Writable tiers: config/read decides whether that sandbox is safe to send.
    check_trust: bool = False
    # #1408: the cwd's git repo root as Codex's trust lookup sees it.
    repo_root: str | None = None
    # #1369: Codex's configured model (config/read), sent on a resume with no
    # /model override, since a resumed thread otherwise keeps its saved model.
    config_model: str | None = None
    proc: Any = None
    peer: CodexRpcPeer | None = None
    reader: BufferedByteReceiveStream | None = None
    # The message the current line decoded to (read by translate()).
    inbound: Response | Notification | None = None
    # Writes translate() asks for: (kind, method, params), flushed after it.
    outbox: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    pending_rows: list[UntetherEvent] = field(default_factory=list)
    thread_id: str | None = None
    turn_id: str | None = None
    # Set when a write failed: the run ends with this error.
    stopped: str | None = None
    seen_unknown: set[str] = field(default_factory=set)
    # item id -> (kind, title) of rows started but not completed (§0.5 rule 4).
    open_items: dict[str, tuple[str, str]] = field(default_factory=dict)
    retry_seen: bool = False
    moderation_logged: bool = False
    server_requests: int = 0
    model: str | None = None
    # #832: the latest thread/tokenUsage/updated. ``tokens`` is its
    # thread-cumulative ``total`` (exec's footer meaning, PLAN Q3);
    # ``context`` is (pct, used, window) from ``last``.
    tokens: cas.TokenUsageBreakdown | None = None
    context: tuple[int, int, int] | None = None
    ctx_emitted: tuple[int, int] | None = None
    compactions: int = 0
    # #964: a bare /compact run (thread/compact/start in place of turn/start).
    manual_compact: bool = False
    # #963: open approval cards (set once the thread is known; attended runs
    # only), fileChange items by id for their diff (cached before the
    # sub-agent filter), the chat's Diff preview, and an unattended run's
    # trigger + declined request kinds (#835 posture).
    cards: approvals.CodexApprovals | None = None
    file_changes: dict[str, list[Any]] = field(default_factory=dict)
    diff_preview: bool | None = None
    unattended: str | None = None
    unattended_denied: list[str] = field(default_factory=list)
    # A card's Stop (``cancel``) interrupted the turn on purpose.
    card_stop: bool = False
    # #1412: the main thread's turn/completed arrived (any status); mirrored
    # onto ``JsonlStreamState.saw_result`` for session.summary.
    saw_result: bool = False
    # #964 steer: waiters by request id, and the text of each steer sent but
    # not yet seen as a userMessage (by its clientUserMessageId).
    steer_waits: dict[int, _SteerWait] = field(default_factory=dict)
    steer_texts: dict[str, str] = field(default_factory=dict)
    # #1412 item 1: thread/status/changed activeFlags per thread (sub-agent
    # threads too), set while ``active``.
    waiting: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # #964-3a turn state machine (plan §16 items 2-5). ``live_mode``: the
    # process stays up after the first turn (964-3b decides when); later
    # turns are TurnEvent segments. ``turn_open`` spans turn/started ..
    # turn/completed of the main thread; ``completed_turns`` counts them.
    # ``live_mode`` / ``turn_open`` / ``completed_turns`` are duck-typed by
    # the bridge's live-session predicates.
    live_mode: bool = False
    turn_open: bool = False
    completed_turns: int = 0
    turn_reason: str = "unknown"
    turn_command_uuid: str | None = None
    # The follow-up turn/start in flight (one at a time), and the request id
    # of every follow-up start not yet answered: a late answer to one whose
    # waiter was dropped is recognised and ignored (review H1).
    pending_start: _StartWait | None = None
    followup_rids: set[int] = field(default_factory=set)
    # Set when the run ends; the thread stays owned until the process exits.
    closing: bool = False
    # ``clock.sleep`` items by id → monotonic deadline; duck-typed by the
    # bridge's stall monitor as an expected wait (``_has_pending_wakeup``)
    # and its countdown.
    live_wakeups: dict[str, float] = field(default_factory=dict)
    # #964-3b live session (the registry entry is this state, keyed by thread
    # in ``_RUNS_BY_THREAD``). ``lock`` serialises a follow-up write against
    # a close; ``spawn_options`` / ``spawn_cwd`` are compared by the bridge's
    # follow-up path (#996); ``live_close_reason`` / ``live_exit_cause`` are
    # duck-typed by the bridge. Idle grace and cap: ``[watchdog]``
    # ``post_result_limbo_grace`` / ``live_session_max_s`` (D11).
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    spawn_options: Any = None
    spawn_cwd: Path | None = None
    spawned_at: float = field(default_factory=time.monotonic)
    idle_grace_s: float = 60.0
    live_max_s: float = 14400.0
    idle_since: float | None = None
    closed_at: float | None = None
    abs_cap_deferred: bool = False
    steer_closed: bool = False
    live_close_reason: str | None = None
    live_exit_cause: str | None = None
    # A compact follow-up's turn (``thread/compact/start``) is open.
    turn_compact: bool = False
    # §16 item 6: the trust Codex applied at spawn (``_trust_view``; writable
    # tiers only), re-read before each follow-up, and the waiters of those
    # re-reads by request id (kept until answered: a late answer is dropped).
    spawn_trust: tuple[Any, ...] | None = None
    read_waits: dict[int, _ReadWait] = field(default_factory=dict)
    listeners: list[Any] = field(default_factory=list)
    # #965: a plan-mode run (``plan`` / ``plan-auto``); ``experimental`` is the
    # connection's ``experimentalApi`` (plan mode or a D1 reset, fixed at
    # spawn); ``reset`` = the first turn sends ``collaborationMode default``;
    # ``plan_text`` = this turn's completed ``plan`` item.
    plan: bool = False
    experimental: bool = False
    reset: bool = False
    plan_text: str | None = None
    # The chat's effort at spawn (a live follow-up has no run options).
    effort: str | None = None

    # Duck-typed by the stall watchdogs (``runner._approval_pending``, the
    # bridge's ``_has_pending_approval``): Codex is waiting on the user.
    def awaiting_user_approval(self) -> bool:
        return bool(self.waiting)


@dataclass(slots=True)
class _SteerWait:
    """One ``turn/steer`` (§16 item 1): unsent, sent and unconfirmed, or
    answered (``accepted`` / ``error``)."""

    command_uuid: str
    sent: bool = False
    accepted: bool = False
    error: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)


@dataclass(slots=True)
class _StartWait:
    """One follow-up ``turn/start`` (§16 item 2). ``rid`` is set before the
    write; ``turn_id`` by its response or an earlier ``turn/started``."""

    command_uuid: str
    text: str
    rid: int | None = None
    turn_id: str | None = None
    sent: bool = False
    accepted: bool = False
    absorbed: bool = False
    timed_out: bool = False
    # Its turn/started has been seen (attributed).
    started: bool = False
    error: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    # #964-3b: ``thread/compact/start`` (its answer is ``{}``, no turn id).
    compact: bool = False
    # When it was registered: a start whose turn never comes expires.
    at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class _ReadWait:
    """A ``config/read`` sent from outside the reader (§16 item 6)."""

    result: Any = None
    error: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)


def _trust_view(cwd: str, result: Any) -> tuple[Any, ...]:
    """What the spawn's sandbox decision rests on, from a ``config/read``
    result: the ``trust_level`` of the ``[projects]`` entry Codex selects for
    ``cwd`` (#1408 lookup and repo root), its ``sandbox_mode`` and model."""
    config = result.get("config") if isinstance(result, dict) else None
    config = config if isinstance(config, dict) else {}
    entry = selected_project_entry(
        config.get("projects"), cwd, git_repo_root(Path(cwd))
    )
    level = entry.get("trust_level") if isinstance(entry, dict) else None
    return (level, config.get("sandbox_mode"), _config_model(result))


def _settle_start(state: AppServerRunState, wait: _StartWait) -> None:
    """Drop the in-flight start once nothing more can attribute to it:
    refused or absorbed, or accepted / timed out with its turn started. An
    accepted start whose turn hasn't started stays, so its turn/started (the
    answer can come first) is still the follow-up's."""
    if state.pending_start is not wait:
        return
    if (
        wait.error is not None
        or wait.absorbed
        or (wait.started and (wait.accepted or wait.timed_out))
    ):
        state.pending_start = None


# #965 D1: threads Untether saw in Plan mode (the mode is saved on the
# thread, P7). The next non-plan run opens an experimental connection and
# sends ``collaborationMode default`` once. In memory: a lost marker is
# caught by the resume echo (``_PLAN_STUCK_NOTE``).
_PLAN_THREADS: set[str] = set()


def _needs_mode_reset(state: AppServerRunState) -> bool:
    """A stable process can't switch its plan-touched thread back to default
    (D1): a live follow-up must respawn instead of reusing it."""
    return not state.experimental and state.thread_id in _PLAN_THREADS


def _collab_mode(state: AppServerRunState, mode: str) -> dict[str, Any]:
    """``turn/start.collaborationMode`` (experimental). ``settings.model``
    is required; the effort goes here because the field takes precedence
    over ``turn/start.effort``; ``developer_instructions: None`` = Codex's
    own template for the mode."""
    return {
        "mode": mode,
        "settings": {
            "model": state.model,
            "reasoning_effort": state.effort,
            "developer_instructions": None,
        },
    }


# #964: the run that owns each thread right now. Set when the thread opens;
# released only once the run's process has exited (§16 item 4), and only by
# the run that still owns it.
_RUNS_BY_THREAD: dict[str, AppServerRunState] = {}


def _release_thread(state: AppServerRunState) -> None:
    if state.thread_id and _RUNS_BY_THREAD.get(state.thread_id) is state:
        del _RUNS_BY_THREAD[state.thread_id]


class CodexAppServerRunner(CodexRunner):
    """``codex app-server`` per run. Keeps the base ``run_impl`` (guards,
    watchdog, stderr, exit handling); overrides only the stdin and stdout
    seams so the process stays open for JSON-RPC."""

    process_label = "codex app-server"
    transport = "app-server"
    # #1412 item 1: the watchdogs read ``awaiting_user_approval``.
    _EXPOSE_ENGINE_STATE = True
    # #964: this runner may keep a run's process after its first turn (a
    # live session). ``codex.build_runner`` turns it on for the app-server
    # transport; each run then decides (``build_args``: ``[watchdog]
    # live_sessions``, attended runs only).
    keep_live = False
    # Instance-tunable for tests.
    exit_grace_s = _EXIT_GRACE_S
    write_timeout_s = WRITE_TIMEOUT_S
    interrupt_bound_s = _INTERRUPT_BOUND_S
    # #964-3b lifecycle: how often a live run checks its timers, and how long
    # past ``live_session_max_s`` a running turn may finish (#887's grace).
    live_poll_s = 1.0
    abs_cap_turn_grace_s = 1800.0

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        if resume is None and _is_compact(prompt):
            # #964: no thread, nothing to compact; never an empty run or probe.
            logger.info("codex.app_server.compact_no_conversation")
            yield EventFactory(ENGINE).completed_error(
                error=NOTHING_TO_COMPACT_TEXT,
                resume=None,
                usage={PRESPAWN_BLOCKED_KEY: COMPACT_NO_CONVERSATION_BLOCK},
            )
            return
        # #961: refuse a CLI without the protocol before anything spawns.
        refusal = await app_server_unsupported_event(self.codex_cmd, resume)
        if refusal is not None:
            yield refusal
            return
        async with contextlib.aclosing(
            JsonlSubprocessRunner.run_impl(self, prompt, resume)
        ) as events:
            async for evt in events:
                yield evt

    def new_state(self, prompt: str, resume: ResumeToken | None) -> AppServerRunState:
        return AppServerRunState(
            factory=EventFactory(ENGINE),
            prompt=prompt,
            manual_compact=resume is not None and _is_compact(prompt),
        )

    def build_args(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> list[str]:
        run_options = get_run_options()
        raw_mode = run_options.permission_mode if run_options is not None else None
        webhook = _webhook_safe_trigger(run_options)
        if webhook is not None and raw_mode is not None:
            _warn_webhook_unknown_mode(webhook, raw_mode)
        state.unattended = run_options.unattended_trigger if run_options else None
        state.diff_preview = run_options.diff_preview if run_options else None
        mode = self._permission_mode()
        state.tier = _tier(mode, unattended=bool(state.unattended))
        # #965: experimental only for plan mode, or once to reset a thread
        # Untether saw in plan mode (D1). Fixed here: it is per connection.
        state.plan = mode in CODEX_PLAN_MODES
        state.reset = (
            not state.plan
            and resume is not None
            and not resume.is_continue
            and resume.value in _PLAN_THREADS
        )
        state.experimental = state.plan or state.reset
        state.effort = (run_options.reasoning if run_options else None) or None
        state.sandbox = state.tier.sandbox
        state.check_trust = state.sandbox != "read-only"
        state.cwd = str(get_run_base_dir() or Path.cwd())
        # #964-3b: only an attended run on a live-capable runner goes live
        # (D5: a cron/webhook run's ``never`` policy must not take a chat
        # follow-up); ``[watchdog] live_sessions = false`` keeps per-run (D1).
        enabled, state.idle_grace_s, state.live_max_s = _live_settings()
        state.live_mode = self.keep_live and enabled and state.unattended is None
        if state.live_mode:
            state.spawn_options = run_options
            state.spawn_cwd = get_run_base_dir()  # #996
        args = [*self.extra_args, "app-server", "--listen", "stdio://"]
        state.argv = list(args)
        return args

    def stdin_payload(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> bytes | None:
        return None  # the prompt goes in turn/start

    def decode_jsonl(self, *, line: bytes) -> Any:
        # The peer already decoded the line; translate() reads state.inbound.
        return line

    async def _send_payload(
        self,
        proc: Any,
        payload: bytes | None,
        *,
        logger: Any,
        resume: ResumeToken | None,
        state: Any = None,
    ) -> None:
        # Stdin stays open. Only *write* initialize: the base starts reading
        # stdout after this returns, so nothing may wait for an answer here.
        state.proc = proc
        state.spawned_at = time.monotonic()
        state.peer = CodexRpcPeer(
            proc.stdin.send,
            write_timeout=self.write_timeout_s,
            on_server_request=lambda method, _params: self._on_server_request(
                state, method
            ),
        )
        try:
            await state.peer.submit(
                "initialize",
                {
                    "clientInfo": {
                        "name": "untether",
                        "title": "Untether",
                        "version": __version__,
                    },
                    "capabilities": {
                        "experimentalApi": state.experimental,
                        "optOutNotificationMethods": [
                            *OPT_OUT_NOTIFICATIONS,
                            *(_EXPERIMENTAL_OPT_OUTS if state.experimental else ()),
                        ],
                    },
                },
            )
        except (CodexRpcClosed, CodexRpcWriteTimeout) as exc:
            self._stop(state, exc)

    def _stop(self, state: AppServerRunState, exc: Exception) -> None:
        logger.warning(
            "codex.app_server.write_failed",
            error=type(exc).__name__,
            detail=str(exc),
        )
        state.stopped = _STOPPED_INPUT

    async def _iter_jsonl_events(
        self,
        *,
        stdout: Any,
        stream: JsonlStreamState,
        state: Any,
        resume: ResumeToken | None,
        logger: Any,
        pid: int,
    ) -> AsyncIterator[UntetherEvent]:
        # One buffered reader on the run state, so a later reader (cancel,
        # 962-4) never loses bytes this one buffered.
        state.reader = BufferedByteReceiveStream(stdout)
        peer: CodexRpcPeer = state.peer
        # #964-3a: a live run keeps reading after its first turn.
        stream.followup_turns = state.live_mode
        cancelled = False
        idle_at_eof = False
        next_tick = time.monotonic() + self.live_poll_s
        try:
            # A run that ended before any turn (a refused thread/resume, an
            # unreadable result) has no session to keep: it ends as per-run.
            while state.stopped is None and (
                not stream.did_emit_completed
                or (state.live_mode and state.completed_turns > 0)
            ):
                line: bytes | None = None
                try:
                    # #964-3b: a live run's lifecycle runs between reads, on
                    # its own deadline whether or not frames keep arriving
                    # (one reader; a cancelled read loses nothing: the
                    # buffered stream keeps a part-read line).
                    with anyio.move_on_after(
                        max(0.0, next_tick - time.monotonic())
                        if state.live_mode
                        else None
                    ):
                        line = await state.reader.receive_until(b"\n", _MAX_LINE_BYTES)
                except (anyio.IncompleteRead, anyio.ClosedResourceError):
                    break
                if state.live_mode and time.monotonic() >= next_tick:
                    next_tick = time.monotonic() + self.live_poll_s
                    if await self._live_tick(state):
                        break
                if line is None:
                    continue
                try:
                    state.inbound = await peer.feed(line)
                    # A server request yields no message, but its row does.
                    if state.inbound is not None or (
                        state.pending_rows and state.thread_id is not None
                    ):
                        for evt in self._handle_jsonl_line(
                            raw_line=line,
                            stream=stream,
                            state=state,
                            resume=resume,
                            logger=logger,
                            pid=pid,
                        ):
                            yield evt
                    state.inbound = None
                    while state.outbox and (
                        state.live_mode or not stream.did_emit_completed
                    ):
                        kind, method, params = state.outbox.pop(0)
                        if kind == "notify":
                            await peer.notify(method, params)
                        elif kind == "reply":  # #963: an unreadable request
                            await peer.reply(method, params)
                        else:
                            await peer.submit(method, params)
                except (CodexRpcClosed, CodexRpcWriteTimeout) as exc:
                    self._stop(state, exc)
            idle_at_eof = _is_idle(state)
            if state.turn_open and state.completed_turns:
                # A later turn the process never finished (EOF / exit).
                yield self._turn_completed_event(
                    state, ok=False, answer=self._answer(state), error=_TURN_EXITED
                )
        except (anyio.get_cancelled_exc_class(), GeneratorExit):
            # /cancel: the bridge cancelled the run (at a read) or closed this
            # generator (at a yield). Stop the turn natively, then re-raise.
            cancelled = True
            await self._interrupt(state)
            raise
        finally:
            self._end_run(state)
            peer.close("done" if stream.did_emit_completed else "eof")
            # §16 item 4: the thread is released only once the process is
            # gone (bounded; SIGTERM after the grace), so a run resuming it
            # never meets a process that still holds it.
            with anyio.CancelScope(shield=True):
                gone = await self._close_process(state.proc)
            if gone:
                _release_thread(state)
            else:
                # Review H3: fail closed. The thread stays owned (closing: no
                # steer, no follow-up) rather than let another run resume it
                # while this process may still serve it.
                logger.warning(
                    "codex.app_server.thread_kept",
                    thread_id=state.thread_id,
                    pid=getattr(state.proc, "pid", None),
                )
            if state.live_mode and gone:
                with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                    await _live_ended(state, died=idle_at_eof and not cancelled)

    async def _live_tick(self, state: AppServerRunState) -> bool:
        """#964-3b: one lifecycle check of a live run, between reads. True
        ends the read loop (a close the process didn't honour in the exit
        grace: teardown escalates). Idle (no turn, no follow-up pending, no
        waiting flag) for ``idle_grace_s`` → ``idle_no_tasks``;
        ``live_max_s`` from spawn → ``abs_cap``, deferred to the turn
        boundary for at most ``abs_cap_turn_grace_s`` (#887)."""
        now = time.monotonic()
        if state.closing:
            return (
                state.closed_at is not None
                and now - state.closed_at > self.exit_grace_s
            )
        if state.lock.locked():
            # A follow-up is being written and its answer is read by this
            # task: waiting for the lock here would stall both.
            return False
        _expire_stale_start(state, now)
        over = now - state.spawned_at - state.live_max_s
        if state.live_max_s > 0 and over >= 0:
            busy = not _is_idle(state)
            if not busy or over >= self.abs_cap_turn_grace_s:
                await close_live_session(
                    state.thread_id or "",
                    "abs_cap",
                    notice=busy,
                    only_if_idle=not busy,
                )
                return False
            if not state.abs_cap_deferred:
                state.abs_cap_deferred = True
                logger.info(
                    "codex.live_session.abs_cap_deferred",
                    session_id=state.thread_id,
                    cap_s=state.live_max_s,
                    grace_s=self.abs_cap_turn_grace_s,
                )
        if not _is_idle(state) or state.waiting:
            state.idle_since = None  # a turn, a follow-up or an approval
            return False
        if state.idle_since is None:
            state.idle_since = now
        elif now - state.idle_since >= state.idle_grace_s:
            await close_live_session(
                state.thread_id or "", "idle_no_tasks", only_if_idle=True
            )
        return False

    @staticmethod
    def _end_run(state: AppServerRunState) -> None:
        """The run is over: no more steers into it (#964; a steer still
        waiting stays unconfirmed, never re-queued), no waiting flags, and a
        later tap on one of its cards reads "session ended" (#963)."""
        state.closing = True
        for wait in state.steer_waits.values():
            wait.done.set()
        state.steer_waits.clear()
        if state.pending_start is not None:
            state.pending_start.done.set()
            state.pending_start = None
        state.waiting.clear()
        state.live_wakeups.clear()
        cards = state.cards
        if cards is None:
            return
        approvals._RUNS.pop(cards.session_key, None)
        dropped = drop_session(cards.session_key)
        if dropped:
            logger.info("codex.app_server.approvals_dropped", cleaned=dropped)

    async def _interrupt(self, state: AppServerRunState) -> None:
        """``turn/interrupt`` for the *current* turn, bounded and shielded:
        Codex ends it in ~0.1 s and exits once stdin closes, so teardown finds
        it gone. On timeout the base teardown's SIGTERM is the fallback.

        §16 item 3: the target is the open turn by its id (a later turn of a
        live run too); in the window where a follow-up is written but its
        turn isn't known yet, that turn is waited for first. Idle: nothing
        to interrupt. One reader: the run's own buffered stream."""
        peer = state.peer
        target = state.turn_id if state.turn_open or not state.completed_turns else None
        if (
            state.stopped is not None
            or state.thread_id is None
            or (target is None and state.pending_start is None)
        ):
            peer.close("cancelled")  # handshake or idle: plain teardown
            return
        started = time.monotonic()
        ok = False
        with (
            anyio.CancelScope(shield=True),
            anyio.move_on_after(self.interrupt_bound_s),
        ):
            try:
                while target is None:
                    target = self._started_turn(await self._read_message(state), state)
                # Never await the answer: this loop is the only reader.
                await peer.submit(
                    "turn/interrupt", {"threadId": state.thread_id, "turnId": target}
                )
                while not ok:
                    msg = await self._read_message(state)
                    # An error answer ("no active turn") is ignored; the
                    # target's turn/completed (interrupted, or a finish that
                    # won the race) ends it. Another turn's never does.
                    ok = (
                        isinstance(msg, Notification)
                        and msg.method == "turn/completed"
                        and msg.params.get("threadId") == state.thread_id
                        and (msg.params.get("turn") or {}).get("id") == target
                    )
                peer.close("cancelled")
                with contextlib.suppress(Exception):
                    await state.proc.stdin.aclose()
                await state.proc.wait()
            except Exception as exc:  # noqa: BLE001 — best effort; teardown follows
                logger.warning(
                    "codex.app_server.interrupt_failed", error=type(exc).__name__
                )
        peer.close("cancelled")
        # Codex never completes items open at an interrupt (D19); the bridge
        # has stopped reading, so they're closed here and not shown.
        closed = len(state.open_items)
        state.open_items.clear()
        logger.info(
            "codex.app_server.interrupt",
            ok=ok,
            turn_id=target,
            elapsed_s=round(time.monotonic() - started, 3),
            open_items_closed=closed,
        )

    @staticmethod
    async def _read_message(state: AppServerRunState) -> Any:
        """One message through the run's buffered reader (a part-read line
        is never lost) and its peer."""
        line = await state.reader.receive_until(b"\n", _MAX_LINE_BYTES)
        return await state.peer.feed(line)

    @staticmethod
    def _started_turn(msg: Any, state: AppServerRunState) -> str | None:
        """While cancelling a written follow-up: the id of the turn it
        started (its turn/started or its turn/start answer), else None."""
        if (
            isinstance(msg, Notification)
            and msg.method == "turn/started"
            and msg.params.get("threadId") == state.thread_id
        ):
            return (msg.params.get("turn") or {}).get("id")
        wait = state.pending_start
        if isinstance(msg, Response) and wait is not None and msg.id == wait.rid:
            if msg.error is not None:
                raise RuntimeError("follow-up refused")  # nothing started
            turn = (
                (msg.result or {}).get("turn") if isinstance(msg.result, dict) else None
            )
            return turn.get("id") if isinstance(turn, dict) else None
        return None

    async def _close_process(self, proc: Any) -> bool:
        """Close stdin (the app-server exits in ~0.1 s), then bounded waits:
        the exit grace, SIGTERM and ``_KILL_ESCALATION_S``, then SIGKILL and
        the same again to reap it. True once the process is confirmed gone
        (review H3: only then may its thread be released)."""
        with contextlib.suppress(Exception):
            await proc.stdin.aclose()
        if await self._exited(proc, self.exit_grace_s):
            return True
        logger.warning("codex.app_server.exit_timeout", pid=proc.pid)
        terminate_process(proc)
        if await self._exited(proc, self._KILL_ESCALATION_S):
            return True
        logger.warning("codex.app_server.exit_killed", pid=proc.pid)
        kill_process(proc)
        return await self._exited(proc, self._KILL_ESCALATION_S)

    @staticmethod
    async def _exited(proc: Any, timeout: float) -> bool:  # noqa: ASYNC109
        with anyio.move_on_after(timeout):
            await proc.wait()
        return proc.returncode is not None

    # --- translation ---------------------------------------------------------

    def translate(
        self,
        data: Any,
        *,
        state: AppServerRunState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        msg = state.inbound
        if isinstance(msg, Response):
            try:
                return self._on_response(msg, state=state, resume=resume)
            except (msgspec.ValidationError, TypeError, ValueError) as exc:
                # A handshake step that can't go on would leave the run
                # waiting forever: end it instead.
                logger.error(
                    "codex.app_server.bad_result", method=msg.method, error=str(exc)
                )
                return [
                    self._error(
                        state,
                        resume,
                        f"codex app-server sent an unreadable {msg.method} result",
                    )
                ]
        events: list[UntetherEvent] = []
        if isinstance(msg, Notification):
            events = self._on_notification(
                msg, state=state, resume=resume, found_session=found_session
            )
        elif isinstance(msg, ServerRequest):
            events = self._approval_card(msg, state)
        return self._flush_rows(state, events)

    @staticmethod
    def _flush_rows(
        state: AppServerRunState, events: list[UntetherEvent]
    ) -> list[UntetherEvent]:
        """Rows raised outside a translated line (warnings before the thread,
        server requests) go out once the StartedEvent has (3-event contract)."""
        if state.thread_id is None or not state.pending_rows:
            return events
        if any(isinstance(e, StartedEvent) for e in events):
            return events  # _on_thread placed them after it
        rows = list(state.pending_rows)
        state.pending_rows.clear()
        return [*rows, *events]

    def _rows(
        self, state: AppServerRunState, rows: list[UntetherEvent]
    ) -> list[UntetherEvent]:
        if state.thread_id is None:
            state.pending_rows.extend(rows)
            return []
        return rows

    def _error(
        self,
        state: AppServerRunState,
        resume: ResumeToken | None,
        error: str,
        usage: dict[str, Any] | None = None,
    ) -> CompletedEvent:
        token = (
            ResumeToken(engine=ENGINE, value=state.thread_id)
            if state.thread_id
            else resume
        )
        # An answer that arrived before the failure is kept; the bridge
        # appends the error to it (#53760).
        return state.factory.completed_error(
            error=error,
            answer=self._answer(state),
            resume=token,
            usage=self._run_usage(state, usage),
        )

    def _run_usage(
        self, state: AppServerRunState, usage: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """``CompletedEvent.usage``: exec's five token keys from ``total``
        (thread-cumulative, the backend's declared scope, #850), plus
        ``context`` and ``compaction`` for the ``runner.completed`` log
        (#832), *usage* and the #1240 flag."""
        out: dict[str, Any] = {}
        if state.tokens is not None:
            out = {f: getattr(state.tokens, f) for f in codex_schema.CODEX_USAGE_FIELDS}
        if state.context is not None:
            pct, used, window = state.context
            out["context"] = {
                "pct": pct,
                "used": used,
                "window": window,
                "model": state.model,
            }
        if state.compactions:
            # Mid-turn (auto); a /compact run overrides this (#964).
            out["compaction"] = {"count": state.compactions, "trigger": "auto"}
        if state.unattended_denied:
            # #835: the bridge's footer line for an unattended run's denials.
            denied: dict[str, int] = {}
            for kind in state.unattended_denied:
                denied[kind] = denied.get(kind, 0) + 1
            out["unattended"] = {
                "trigger": state.unattended,
                "mode": self._permission_mode(),
                "denied": denied,
            }
        out.update(usage or {})
        return self._untrusted_usage(state, out or None)

    def _on_server_request(self, state: AppServerRunState, method: str) -> Any:
        """The peer's observer: an approval in an attended run is deferred to
        a card (#963); anything else the peer declines, and this adds the
        row (🔒 once per run when unattended, #835)."""
        if method in approvals.APPROVAL_METHODS:
            if state.cards is not None:
                return DEFER
            if state.unattended is not None:
                self._unattended_decline(state, method)
                return None
        state.server_requests += 1
        title = (
            _APPROVAL_DECLINED_NOTE
            if method in approvals.APPROVAL_METHODS
            else f"{_WARNING} Codex sent a request Untether doesn't support yet"
            f" (`{method}`); declined"
        )
        row = state.factory.action_completed(
            action_id=f"codex.server_request.{state.server_requests}",
            kind="warning",
            title=title,
            detail={"method": method},
            ok=True,
            level="warning",
        )
        state.pending_rows.append(row)
        return None

    # --- #963 approval cards ---------------------------------------------------

    def _unattended_decline(self, state: AppServerRunState, method: str) -> None:
        """A request in a cron/webhook run (none expected under ``never``):
        the peer declines it; Claude's #835 row, log and footer count."""
        kind = approvals.kind_name(method)
        state.unattended_denied.append(kind)
        logger.warning(
            "permission.unattended_deny",
            tool_name=kind,
            trigger_source=state.unattended,
            session_id=state.thread_id,
            permission_mode=self._permission_mode(),
            turn_denials=len(state.unattended_denied),
        )
        if len(state.unattended_denied) > 1:
            return
        state.pending_rows.append(
            state.factory.action_completed(
                action_id="codex.unattended_deny",
                kind="note",
                title=f"\N{LOCK} Unattended run — declined a {kind} request:"
                " nobody to approve it",
                ok=False,
                level="warning",
            )
        )

    def _approval_card(
        self, msg: ServerRequest, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """A deferred request → its card, registered for a tap."""
        cards = state.cards
        params = cas.decode_request(msg.method, msg.params)
        if cards is None or params is None:
            # Shape Untether can't read: decline it, as the peer would have.
            logger.warning("codex.app_server.approval_unreadable", method=msg.method)
            decline = DECLINE_HANDLERS[msg.method](msg.params)
            state.outbox.append(("reply", msg.id, decline))  # type: ignore[arg-type]
            return []
        request_id = approvals.new_request_id()
        file_changes = (
            state.file_changes.get(params.item_id)
            if isinstance(params, cas.FileChangeApprovalParams)
            else None
        )
        card = approvals.build_card(
            request_id,
            msg.method,
            params,
            subagent=params.thread_id != state.thread_id,
            file_changes=file_changes,
            diff_preview=state.diff_preview,
        )
        action_id = f"codex.approval.{request_id}"
        register(
            request_id,
            session_key=cards.session_key,
            responder=approvals.RESPONDER,
            tool_name=approvals.kind_name(msg.method),
            tool_input={"decisions": card.decisions},
            kind="tool",
        )
        cards.pending[request_id] = approvals.Pending(
            rpc_id=msg.id,
            method=msg.method,
            action_id=action_id,
            label=card.label,
            decisions=card.decisions,
            # §16 item 5: the main turn it arrived in (a sub-agent's card too).
            turn_id=state.turn_id,
        )
        cards.by_rpc[msg.id] = request_id
        logger.info(
            "codex.app_server.approval",
            request_id=request_id,
            method=msg.method,
            subagent=params.thread_id != state.thread_id,
            decisions=sorted(card.decisions),
        )
        return [
            state.factory.action_started(
                action_id=action_id,
                kind="warning",
                title=card.title,
                detail={
                    "request_id": request_id,
                    "request_type": msg.method,
                    "inline_keyboard": {"buttons": card.buttons},
                    "line_breaks": True,
                },
            )
        ]

    def _on_request_resolved(
        self, params: cas.ServerRequestResolvedParams, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """Codex is done with a request: after our answer (close the card with
        it) or on its own, e.g. the turn ended (withdraw, #684)."""
        cards = state.cards
        request_id = cards.by_rpc.pop(params.request_id, None) if cards else None
        pending = cards.pending.pop(request_id, None) if cards and request_id else None
        if pending is None or request_id is None:
            return []
        return [self._close_card(state, request_id, pending)]

    def _close_card(
        self, state: AppServerRunState, request_id: str, pending: approvals.Pending
    ) -> UntetherEvent:
        if pending.answer is None:
            withdraw(request_id)
            logger.info("codex.app_server.approval.withdrawn", request_id=request_id)
            title, ok = f"{_WITHDRAWN}: {pending.label}", False
        else:
            title = f"{approvals.ANSWER_TITLES[pending.answer]}: {pending.label}"
            ok = pending.answer in {"approve", "session", "always"}
            state.card_stop = state.card_stop or pending.answer == "stop"
        return state.factory.action_completed(
            action_id=pending.action_id,
            kind="warning",
            title=title,
            ok=ok,
            detail={"request_id": request_id},
        )

    def _retire_cards(
        self, state: AppServerRunState, turn_id: str | None = None
    ) -> list[UntetherEvent]:
        """Cards still open when their turn ends: Codex has cleared them.
        Only that turn's (§16 item 5); ``None`` retires every card."""
        cards = state.cards
        if cards is None or not cards.pending:
            return []
        ending = [
            rid
            for rid, p in cards.pending.items()
            if turn_id is None or p.turn_id in (None, turn_id)
        ]
        rows = []
        for rid in ending:
            pending = cards.pending.pop(rid)
            cards.by_rpc.pop(pending.rpc_id, None)
            rows.append(self._close_card(state, rid, pending))
        return rows

    def _review_rows(
        self, msg: Notification, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """🛡️ rows for Codex's auto-review: no card, no push (the reviewer
        decides). The payload is ``[UNSTABLE]``: read loosely, never fail."""
        phase = "started" if msg.method.endswith("/started") else "completed"
        row_id, title, ok, warn = approvals.review_row(phase, msg.params)
        review = msg.params.get("review")
        logger.info(
            "codex.app_server.auto_review",
            phase=phase,
            status=review.get("status") if isinstance(review, dict) else None,
            row=row_id,
        )
        if phase == "started":
            state.open_items[row_id] = ("note", title)
            row = state.factory.action_started(
                action_id=row_id, kind="note", title=title
            )
        else:
            state.open_items.pop(row_id, None)
            row = state.factory.action_completed(
                action_id=row_id,
                kind="note",
                title=title,
                ok=ok,
                level="warning" if warn else "info",
            )
        return self._rows(state, [row])

    @staticmethod
    def _cache_file_change(state: AppServerRunState, method: str, params: Any) -> None:
        """Keep each fileChange item's changes for its card's diff, from any
        thread (a sub-agent's request arrives on this connection too). P-B
        (0.171.0): ``item/started`` comes ~5 ms before the request."""
        if isinstance(params, cas.PatchUpdatedParams):
            state.file_changes[params.item_id] = list(params.changes)
            return
        item = params.item if isinstance(params, cas.ItemParams) else {}
        item_id = item.get("id")
        if item.get("type") != "fileChange" or not isinstance(item_id, str):
            return
        if method == "item/completed":
            state.file_changes.pop(item_id, None)
        else:
            changes = item.get("changes")
            state.file_changes[item_id] = changes if isinstance(changes, list) else []

    def _warning_rows(
        self, state: AppServerRunState, method: str, text: str
    ) -> list[UntetherEvent]:
        """warning / configWarning / deprecationNotice: one ⚠️ row per
        message per run (#987), paths and URLs masked (#1066, #1151)."""
        if method == "deprecationNotice":
            logger.info("codex.app_server.deprecation", summary=text[:200])
        if not text or text in state.seen_warnings:
            return []
        state.seen_warnings.add(text)
        if text.startswith(_UNTRUSTED_CONFIG_WARNING_PREFIX):
            # #1240: the same row and footer as exec's stderr notice.
            state.untrusted_project_config = True
            logger.info("codex.project_config_untrusted", cwd=state.cwd)
            return self._rows(state, self._untrusted_note(state))
        message = redact_url_secrets(_warning_display_text(text))
        row = state.factory.action_completed(
            action_id=f"codex.warning.{len(state.seen_warnings)}",
            kind="warning",
            title=f"{_WARNING} {message}",
            detail={"message": message, "source": method},
            ok=True,
            message=message,
            level="warning",
        )
        return self._rows(state, [row])

    def _item_rows(
        self, phase: ActionPhase, raw: Any, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        item = cas.decode_thread_item(raw)
        if isinstance(item, cas.AgentMessageItem):
            # Text arrives whole on item/completed (deltas are opted out).
            if phase != "completed":
                return []
            self._note_moderation(item, state)
            state.turn_agent_messages.append(
                _AgentMessageSummary(text=item.text, phase=item.phase)
            )
        if isinstance(item, cas.GenericItem) and item.type == "plan":
            # #965: the completed text is authoritative (deltas opted out).
            text = item.raw.get("text")
            if phase == "completed" and isinstance(text, str) and text.strip():
                state.plan_text = text
            return []
        exec_item = _exec_item(item)
        if isinstance(exec_item, codex_schema.ReasoningItem) and not exec_item.text:
            return []  # summaries arrive on item/completed, when enabled
        if exec_item is not None:
            rows = _translate_item_event(phase, exec_item, factory=state.factory)
        else:
            rows = self._other_item_rows(phase, item, state=state)
        for row in rows:
            if not isinstance(row, ActionEvent):
                continue
            if row.phase == "started":
                state.open_items[row.action.id] = (row.action.kind, row.action.title)
            elif row.phase == "completed":
                state.open_items.pop(row.action.id, None)
        return rows

    def _other_item_rows(
        self, phase: ActionPhase, item: cas.ThreadItem, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """Items exec never emits: sub-agent calls, image views, and a
        generic row for any type Untether doesn't model (logged once)."""
        if item.type in _SILENT_ITEM_TYPES or phase == "updated":
            return []
        kind = "tool"
        status = ""
        detail: dict[str, Any] | None = None
        match item:
            case cas.ContextCompactionItem():
                # #832: Codex compacting mid-turn (auto); same row as Claude.
                kind = "note"
                title = _COMPACTING_TITLE
                if phase == "completed":
                    state.compactions += 1
                    title = _COMPACTED_TITLE
                    # #1182: the event the session compaction count counts.
                    detail = {"compaction": True}
                    logger.info(
                        "codex.compaction.completed",
                        thread_id=state.thread_id,
                        count=state.compactions,
                    )
            case cas.SleepItem():
                # Codex's ``clock.sleep`` (seen in 964-0): an expected wait
                # with a known end, read by the bridge's stall monitor.
                title = f"\N{HOURGLASS WITH FLOWING SAND} sleeping {format_duration(item.duration_ms / 1000)}"
                if phase == "started":
                    state.live_wakeups[item.id] = (
                        time.monotonic() + item.duration_ms / 1000
                    )
                else:
                    state.live_wakeups.pop(item.id, None)
            case cas.CollabAgentToolCallItem():
                kind, status = "subagent", item.status
                title = item.prompt or item.tool or "sub-agent"
            case cas.ImageViewItem():
                title = f"view image {relativize_path(item.path)}".strip()
            case _:
                item_type = item.type or "item"
                if item_type not in _UNKNOWN_ITEMS_LOGGED:
                    _UNKNOWN_ITEMS_LOGGED.add(item_type)
                    logger.info("codex.app_server.unknown_item", item_type=item_type)
                raw_status = (
                    item.raw.get("status")
                    if isinstance(item, cas.GenericItem)
                    else None
                )
                status = raw_status if isinstance(raw_status, str) else ""
                title = item_type
        action_id = getattr(item, "id", None) or f"codex.item.{item.type}"
        if phase == "started":
            return [
                state.factory.action_started(
                    action_id=action_id, kind=kind, title=title
                )
            ]
        return [
            state.factory.action_completed(
                action_id=action_id,
                kind=kind,
                title=title,
                ok=status in {"", "completed"},
                detail=detail,
            )
        ]

    def _note_moderation(
        self, item: cas.AgentMessageItem, state: AppServerRunState
    ) -> None:
        # PLAN Q6: shown as sent; logged once per run (display is untyped, E2).
        moderation = item.moderation or {}
        if state.moderation_logged or moderation.get("moderated") is not True:
            return
        state.moderation_logged = True
        display = moderation.get("display")
        logger.info(
            "codex.app_server.moderated",
            mode=display.get("mode") if isinstance(display, dict) else None,
        )

    def _close_open_items(
        self, state: AppServerRunState, status: str
    ) -> list[UntetherEvent]:
        """Codex leaves items open when a turn fails or is interrupted (D19):
        close each before the final, so no row stays running."""
        rows: list[UntetherEvent] = [
            state.factory.action_completed(
                action_id=action_id,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                detail={"status": status},
                ok=False,
            )
            for action_id, (kind, title) in state.open_items.items()
        ]
        state.open_items.clear()
        return rows

    @staticmethod
    def _answer(state: AppServerRunState) -> str:
        messages = state.turn_agent_messages
        selected = _select_final_answer(messages)
        if selected is None and state.plan_text:
            return state.plan_text  # #965: a plan turn has no agent message
        if selected is None and messages:
            selected = messages[-1].text
        return selected or ""

    def _on_response(
        self, msg: Response, *, state: AppServerRunState, resume: ResumeToken | None
    ) -> list[UntetherEvent]:
        if msg.method == "turn/steer":
            # #964: the steer's own answer; never the run's error.
            self._on_steer_response(msg, state)
            return []
        if msg.method == "turn/interrupt":
            # #964-3b: a live /cancel's interrupt; its turn/completed ends it.
            return []
        read = state.read_waits.pop(msg.id, None)  # type: ignore[arg-type]
        if read is not None:
            # §16 item 6: a follow-up's trust re-read, never the handshake's.
            read.result = msg.result
            read.error = msg.error.message if msg.error is not None else None
            read.done.set()
            return []
        if msg.id in state.followup_rids:
            # A follow-up's answer (turn/start, or thread/compact/start for
            # an injected /compact): never the run's error, never the turn id.
            state.followup_rids.discard(msg.id)  # type: ignore[arg-type]
            wait = state.pending_start
            if wait is not None and msg.id == wait.rid:
                return self._on_start_response(msg, state, wait)
            logger.info("codex.app_server.followup_late_answer", rid=msg.id)
            return []
        if msg.error is not None:
            # Codex's own text: the bridge matches "no rollout found" (#952).
            return [
                self._error(
                    state,
                    resume,
                    f"codex app-server {msg.method} failed: {msg.error.message}",
                )
            ]
        match msg.method:
            case "initialize":
                init = cas.decode_result("initialize", msg.result)
                logger.info(
                    "codex.app_server.initialized",
                    user_agent=init.user_agent,
                    experimental=state.experimental,
                )
                state.outbox.append(("notify", "initialized", {}))
                if state.check_trust or (resume is not None and not _run_model()):
                    state.outbox.append(("request", "config/read", {"cwd": state.cwd}))
                else:
                    self._open_thread(state, resume)
            case "config/read":
                if state.check_trust:
                    self._decide_sandbox(state, msg.result)
                    state.spawn_trust = _trust_view(state.cwd, msg.result)
                state.config_model = _config_model(msg.result)
                self._open_thread(state, resume)
            case "thread/list":
                listed = cas.decode_result("thread/list", msg.result)
                if listed.data:
                    state.outbox.append(
                        (
                            "request",
                            "thread/resume",
                            self._resume_params(state, listed.data[0].id),
                        )
                    )
                else:
                    state.pending_rows.append(
                        self.note_event(_NO_EARLIER_SESSION_NOTE, state=state, ok=True)
                    )
                    state.outbox.append(
                        ("request", "thread/start", self._overrides(state))
                    )
            case "thread/start" | "thread/resume":
                return self._on_thread(
                    cas.decode_result(msg.method, msg.result), state=state
                )
            case "turn/start":
                started = cas.decode_result("turn/start", msg.result)
                state.turn_id = started.turn.id
                if state.reset and state.thread_id:
                    _PLAN_THREADS.discard(state.thread_id)  # D1: reset done
        return []

    @staticmethod
    def _on_steer_response(msg: Response, state: AppServerRunState) -> None:
        wait = state.steer_waits.pop(msg.id, None)  # type: ignore[arg-type]
        if wait is None:
            return
        if msg.error is not None:
            wait.error = msg.error.message
            state.steer_texts.pop(wait.command_uuid, None)
        else:
            wait.accepted = True
        wait.done.set()

    def _on_start_response(
        self, msg: Response, state: AppServerRunState, wait: _StartWait
    ) -> list[UntetherEvent]:
        """§16 item 2: Codex's answer to a follow-up turn/start. It names the
        turn the text went into: a new one (its turn/started is, or was,
        attributed to the follow-up) or, through ``start_or_steer_turn``, a
        turn already running for someone else (absorbed: one row, no turn)."""
        rows: list[UntetherEvent] = []
        if wait.compact:
            # thread/compact/start answers ``{}``: its turn is the next one.
            if msg.error is not None:
                wait.error = msg.error.message
            else:
                wait.accepted = True
            _settle_start(state, wait)
            wait.done.set()
            return []
        turn = msg.result.get("turn") if isinstance(msg.result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if (
            msg.error is None
            and isinstance(turn_id, str)
            and wait.turn_id not in (None, turn_id)
        ):
            # Review H2: the turn attributed to it wasn't its turn. That one
            # is Codex's own from here; the named turn is the follow-up's.
            logger.warning(
                "codex.app_server.followup_turn_mismatch",
                attributed=wait.turn_id,
                answered=turn_id,
            )
            if state.turn_open and state.turn_id == wait.turn_id:
                state.turn_reason, state.turn_command_uuid = "unknown", None
            wait.turn_id, wait.started = None, False
        if msg.error is not None or not isinstance(turn_id, str):
            wait.error = msg.error.message if msg.error else "no turn id"
        elif wait.turn_id is None and state.turn_open and state.turn_id == turn_id:
            wait.absorbed = True
            logger.info(
                "codex.app_server.followup_absorbed",
                turn_id=turn_id,
                command_uuid=wait.command_uuid,
            )
            state.note_seq += 1
            rows = state.factory.steer_received(
                action_id=f"codex.followup.{state.note_seq}",
                text=wait.text,
                command_uuid=wait.command_uuid,
            )
        else:
            wait.turn_id = turn_id
            wait.accepted = True
        _settle_start(state, wait)
        wait.done.set()
        return self._rows(state, rows)

    def _decide_sandbox(self, state: AppServerRunState, result: Any) -> None:
        """Full auto omits ``sandbox`` (Codex's own setting), and Ask me /
        Auto-review send ``workspace-write``, only where that can't make Codex
        trust the project (DEC-7, P1, D1, #1408): a writable sandbox where
        Codex's selected ``[projects]`` entry has no ``trust_level`` (or there
        is none) saves the project as trusted."""
        config = result.get("config") if isinstance(result, dict) else None
        config = config if isinstance(config, dict) else {}
        # Resolved here, right before the sandbox goes out, to keep the
        # window before Codex's own lookup small (#1408).
        state.repo_root = git_repo_root(Path(state.cwd))
        entry = selected_project_entry(
            config.get("projects"), state.cwd, state.repo_root
        )
        # Codex saves trust only where the selected entry has no trust_level.
        if isinstance(entry, dict) and entry.get("trust_level"):
            return
        sandbox_mode = config.get("sandbox_mode")
        wanted = state.sandbox or sandbox_mode
        state.sandbox = "read-only"
        logger.info(
            "codex.app_server.untrusted_read_only",
            cwd=state.cwd,
            repo_root=state.repo_root,
            sandbox_mode=sandbox_mode,
            mode_sandbox=wanted,
        )
        if wanted in _WRITABLE_SANDBOX_MODES:
            state.pending_rows.append(
                state.factory.action_completed(
                    action_id="codex.untrusted_read_only",
                    kind="warning",
                    title=_UNTRUSTED_READ_ONLY_NOTE,
                    ok=True,
                    level="warning",
                )
            )

    def _overrides(self, state: AppServerRunState) -> dict[str, Any]:
        """Sent on every thread/start and thread/resume: resume restores the
        thread's saved settings, so anything omitted is inherited."""
        params: dict[str, Any] = {
            "cwd": state.cwd,
            "approvalPolicy": state.tier.approval_policy,
            "approvalsReviewer": state.tier.reviewer,
        }
        if state.sandbox is not None:
            params["sandbox"] = state.sandbox
        model = _run_model() or state.config_model
        if model:
            params["model"] = model
        # #423: validated by Codex; on resume too, or the thread keeps its own.
        run_options = get_run_options()
        if run_options is not None and run_options.web_search is not None:
            params["config"] = {
                "web_search": "live" if run_options.web_search else "disabled"
            }
        return params

    def _resume_params(
        self, state: AppServerRunState, thread_id: str
    ) -> dict[str, Any]:
        # ``excludeTurns`` (stable, 0.169.0+): Untether never reads the
        # history, and full hydration draws a deprecationNotice (#962 QA).
        return {"threadId": thread_id, "excludeTurns": True, **self._overrides(state)}

    def _open_thread(
        self, state: AppServerRunState, resume: ResumeToken | None
    ) -> None:
        if resume is None:
            state.outbox.append(("request", "thread/start", self._overrides(state)))
        elif resume.is_continue:
            state.outbox.append(
                (
                    "request",
                    "thread/list",
                    {
                        "cwd": state.cwd,
                        "sourceKinds": list(CONTINUE_SOURCE_KINDS),
                        "sortKey": "updated_at",
                        "sortDirection": "desc",
                        "limit": 1,
                        "archived": False,
                    },
                )
            )
        else:
            state.outbox.append(
                ("request", "thread/resume", self._resume_params(state, resume.value))
            )

    def _on_thread(
        self, result: cas.ThreadOpenResult, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        state.thread_id = result.thread.id
        _RUNS_BY_THREAD[result.thread.id] = state  # #964 steer target
        if state.unattended is None and state.peer is not None:
            # #963: from here an approval request becomes a card.
            state.cards = approvals.CodexApprovals(
                session_key=f"{ENGINE}:{result.thread.id}", peer=state.peer
            )
            approvals._RUNS[state.cards.session_key] = state.cards
        run_options = get_run_options()
        meta: dict[str, Any] = {"model": result.model or result.thread.model or "codex"}
        state.model = meta["model"]
        if run_options is not None and run_options.reasoning:
            meta["effort"] = run_options.reasoning
        if state.tier.label is not None:
            meta["permissionMode"] = state.tier.label
        token = ResumeToken(engine=ENGINE, value=result.thread.id)
        events: list[UntetherEvent] = [
            state.factory.started(token, title=self.session_title, meta=meta)
        ]
        events.extend(state.pending_rows)
        state.pending_rows.clear()
        events.extend(self._echo_check(result, state=state))
        events.extend(self._mode_echo_check(result, state=state))
        if state.manual_compact:
            # #964: Codex's own compaction (stable), not the text as a prompt.
            logger.info("codex.app_server.compact_started", thread_id=result.thread.id)
            state.outbox.append(
                ("request", "thread/compact/start", {"threadId": result.thread.id})
            )
            return events
        turn: dict[str, Any] = {
            "threadId": result.thread.id,
            "input": [{"type": "text", "text": state.prompt}],
            "turnTrigger": "untether",
        }
        if run_options is not None and run_options.reasoning:
            turn["effort"] = run_options.reasoning
        if state.plan:
            turn["collaborationMode"] = _collab_mode(state, "plan")
            _PLAN_THREADS.add(result.thread.id)
        elif state.reset:
            turn["collaborationMode"] = _collab_mode(state, "default")
            logger.info("codex.app_server.plan_mode_reset", thread_id=result.thread.id)
        state.outbox.append(("request", "turn/start", turn))
        return events

    def _mode_echo_check(
        self, result: cas.ThreadOpenResult, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """#965 D1 fallback: a stable resume of a thread still in Plan mode
        (the marker was lost) runs this turn as a plan turn, and only an
        experimental connection can switch it back. Say so once, and mark
        the thread so the next run resets it. Reads ``mode`` only."""
        echo = getattr(result, "collaboration_mode", None)
        if not isinstance(echo, dict) or echo.get("mode") != "plan":
            return []
        thread_id = result.thread.id
        if state.experimental:
            return []  # a plan turn, or the reset this turn already sends
        _PLAN_THREADS.add(thread_id)
        logger.warning("codex.app_server.plan_mode_stuck", thread_id=thread_id)
        return [
            state.factory.action_completed(
                action_id="codex.plan_mode_stuck",
                kind="warning",
                title=_PLAN_STUCK_NOTE,
                ok=True,
                level="warning",
            )
        ]

    def _echo_check(
        self, result: cas.ThreadOpenResult, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """Requested vs effective, on the stable echo fields only."""
        requested: dict[str, Any] = {
            "approvalPolicy": state.tier.approval_policy,
            "approvalsReviewer": state.tier.reviewer,
        }
        if state.sandbox is not None:
            requested["sandbox"] = state.sandbox
        effective = {
            "approvalPolicy": result.approval_policy,
            "approvalsReviewer": result.approvals_reviewer,
            "sandbox": sandbox_policy_mode(result.sandbox),
        }
        # #1412: name the config.web_search sent too; Codex doesn't echo it,
        # so it stays out of the mismatch check below.
        logged = dict(requested)
        run_options = get_run_options()
        if run_options is not None and run_options.web_search is not None:
            logged["web_search"] = "live" if run_options.web_search else "disabled"
        logger.info(
            "codex.app_server.permissions", requested=logged, effective=effective
        )
        mismatched = sorted(k for k, v in requested.items() if effective[k] != v)
        if not mismatched:
            return []
        logger.warning(
            "codex.permission_mode.mismatch",
            fields=mismatched,
            requested={k: requested[k] for k in mismatched},
            effective={k: effective[k] for k in mismatched},
        )
        detail = ", ".join(
            f"{k} {effective[k]!s} (asked {requested[k]})" for k in mismatched
        )
        return [
            state.factory.action_completed(
                action_id="codex.permission_mismatch",
                kind="warning",
                title=(
                    "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Codex is running with"
                    f" different permissions than Untether asked for: {detail}"
                ),
                ok=True,
                level="warning",
            )
        ]

    def _on_notification(
        self,
        msg: Notification,
        *,
        state: AppServerRunState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        if msg.method in cas.DICT_NOTIFICATIONS:
            return self._review_rows(msg, state)  # #963 Auto-review
        if msg.method == cas.SETTINGS_NOTIFICATION:
            self._on_thread_settings(msg.params, state)
            return []
        if msg.method not in cas.NOTIFICATION_TYPES:
            if msg.method not in state.seen_unknown:
                state.seen_unknown.add(msg.method)
                logger.debug("codex.app_server.notification_ignored", method=msg.method)
            return []
        try:
            params = cas.decode_notification(msg.method, msg.params)
        except msgspec.ValidationError as exc:
            logger.warning(
                "codex.app_server.bad_notification", method=msg.method, error=str(exc)
            )
            if msg.method == "turn/completed":
                return [
                    self._error(
                        state,
                        resume,
                        "codex app-server sent an unreadable turn/completed",
                    )
                ]
            return []
        if isinstance(params, cas.ItemParams | cas.PatchUpdatedParams):
            self._cache_file_change(state, msg.method, params)  # #963, any thread
        if (
            isinstance(params, cas.TurnParams | cas.TokenUsageUpdatedParams)
            and params.thread_id != state.thread_id
        ):
            return []  # a sub-agent thread's turn or usage, not this run's
        if (
            isinstance(params, cas.ItemParams | cas.ErrorParams)
            and params.thread_id
            and params.thread_id != state.thread_id
        ):
            return []  # a sub-agent thread's item or error
        match msg.method:
            case "turn/started":
                return self._on_turn_started(params.turn, state=state)
            case "thread/status/changed":
                return self._on_thread_status(params, state=state)
            case "item/started":
                if params.item.get("type") == "userMessage":
                    return self._steer_received(params.item, state=state)
                return self._item_rows("started", params.item, state=state)
            case "item/completed":
                return self._item_rows("completed", params.item, state=state)
            case "error":
                return self._error_row(params, state=state)
            case "warning":
                return self._warning_rows(state, msg.method, params.message)
            case "configWarning" | "deprecationNotice":
                return self._warning_rows(state, msg.method, params.summary)
            case "serverRequest/resolved":
                return self._on_request_resolved(params, state)
            case "guardianWarning":
                return self._warning_rows(state, msg.method, params.message)
            case "thread/tokenUsage/updated":
                return self._on_token_usage(params.token_usage, state=state)
            case "account/rateLimits/updated":
                # #966: feeds the quota footer (sparse; merged, never cleared).
                codex_reads.merge_push(self.codex_cmd, msg.params)
            case "model/rerouted":
                return self._on_rerouted(params, state=state)
            case "turn/plan/updated":
                return self._plan_checklist(params, state=state)
            case "turn/completed":
                return self._on_turn_completed(
                    params.turn, state=state, resume=resume, found_session=found_session
                )
        return []

    def _on_turn_started(
        self, turn: cas.Turn, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """Opens the main thread's turn and resets turn-local state (§16
        item 5). In a live run every turn after the first is a TurnEvent
        segment: ``followup`` with its message's ``command_uuid`` when it is
        the written follow-up's turn (the answer can come before or after
        this, P9), else ``unknown`` (a turn Codex started itself, Δ D43)."""
        state.turn_id = turn.id
        state.turn_open = True
        state.turn_agent_messages.clear()
        state.plan_text = None
        state.card_stop = False
        if not (state.live_mode and state.completed_turns):
            return []
        state.ctx_emitted = None  # re-show % ctx for this turn
        state.compactions = 0  # per turn: else later turns log "auto" (#964)
        state.idle_since = None
        wait = state.pending_start
        reason, command_uuid = "unknown", None
        started_ago_s: float | None = None
        state.turn_compact = False
        # A turn that starts while the follow-up is still unwritten is
        # Codex's own (review H2); the answer reconciles the rest.
        if wait is not None and wait.sent and wait.turn_id in (None, turn.id):
            wait.turn_id, wait.started = turn.id, True
            reason, command_uuid = "followup", wait.command_uuid
            # #1425 (#815 parity): the reader can pause while the bridge
            # delivers the previous final, so this event may be read long
            # after the turn began: time it from the write.
            started_ago_s = max(0.0, time.monotonic() - wait.at)
            state.turn_compact = wait.compact
            _settle_start(state, wait)
        state.turn_reason, state.turn_command_uuid = reason, command_uuid
        logger.info(
            "codex.app_server.turn_started",
            turn=state.completed_turns + 1,
            turn_id=turn.id,
            reason=reason,
            command_uuid=command_uuid,
        )
        return [
            state.factory.turn_started(
                turn=state.completed_turns + 1,
                reason=reason,  # type: ignore[arg-type]
                command_uuid=command_uuid,
                started_ago_s=started_ago_s,
            )
        ]

    def _turn_completed_event(
        self,
        state: AppServerRunState,
        *,
        ok: bool,
        answer: str,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> UntetherEvent:
        """A later turn's closing TurnEvent; the turn is over."""
        state.turn_open = False
        state.completed_turns += 1
        command_uuid = state.turn_command_uuid
        state.turn_command_uuid = None
        return state.factory.turn_completed(
            turn=state.completed_turns,
            ok=ok,
            answer=answer,
            reason=state.turn_reason,  # type: ignore[arg-type]
            error=error,
            usage=self._run_usage(state, usage),
            command_uuid=command_uuid,
        )

    def _on_thread_status(
        self, params: cas.ThreadStatusChangedParams, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """#1412 item 1: ``active{activeFlags}`` (``waitingOnApproval`` /
        ``waitingOnUserInput``) on any thread, sub-agents included, means
        Codex is waiting on the user; any other status clears it. Probe
        1412-1 (0.171.0): the flag arrives just before the approval request
        and clears once it is answered."""
        status = params.status
        kind = status.get("type")
        flags = status.get("activeFlags") if kind == "active" else None
        if isinstance(flags, list) and flags:
            state.waiting[params.thread_id] = tuple(str(f) for f in flags)
        else:
            state.waiting.pop(params.thread_id, None)
        if kind == "systemError" and params.thread_id == state.thread_id:
            logger.warning("codex.app_server.system_error", thread_id=state.thread_id)
            return self._warning_rows(
                state, "thread/status/changed", _SYSTEM_ERROR_NOTE
            )
        return []

    @staticmethod
    def _steer_received(
        item: dict[str, Any], *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """#964: Codex echoes a steer as a ``userMessage`` whose ``clientId``
        is the steer's ``clientUserMessageId`` (P-S1). The row's
        ``absorbed_command_uuid`` drops its reply anchor: the answer is this
        turn's answer."""
        command_uuid = item.get("clientId")
        if not isinstance(command_uuid, str):
            return []
        text = state.steer_texts.pop(command_uuid, None)
        if text is None:
            return []
        logger.info(
            "codex.app_server.steer_delivered",
            thread_id=state.thread_id,
            command_uuid=command_uuid,
        )
        state.note_seq += 1
        return state.factory.steer_received(
            action_id=f"codex.steer.{state.note_seq}",
            text=text,
            command_uuid=command_uuid,
        )

    @staticmethod
    def _on_thread_settings(params: Any, state: AppServerRunState) -> None:
        """#965: ``thread/settings/updated`` echoes Codex's whole mode
        template in ``developer_instructions``: read ``collaborationMode.mode``
        and nothing else, and never log the params."""
        if not isinstance(params, dict) or params.get("threadId") != state.thread_id:
            return
        settings = params.get("threadSettings")
        collab = (
            settings.get("collaborationMode") if isinstance(settings, dict) else None
        )
        mode = collab.get("mode") if isinstance(collab, dict) else None
        if not isinstance(mode, str):
            return
        logger.info(
            "codex.app_server.thread_settings", thread_id=state.thread_id, mode=mode
        )
        if mode == "plan" and state.thread_id:
            _PLAN_THREADS.add(state.thread_id)

    def _plan_checklist(
        self, params: cas.TurnPlanUpdatedParams, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """#965 D6: Codex's ``update_plan`` checklist as one row per turn,
        replaced on each update. Model-dependent (P7: not emitted on
        ``gpt-6.1-sol``)."""
        if params.thread_id != state.thread_id:
            return []
        steps = [
            f"{_PLAN_STEP_MARKS.get(str(p.get('status')), '·')} {p.get('step')}"
            for p in params.plan
            if isinstance(p, dict) and p.get("step")
        ]
        title = "\N{CLIPBOARD} Plan"
        if steps:
            title += ": " + "; ".join(steps)
        row = state.factory.action_completed(
            action_id=f"codex.plan.{params.turn_id or state.turn_id}",
            kind="note",
            title=title,
            detail={"explanation": params.explanation, "steps": params.plan},
            ok=True,
        )
        return self._rows(state, [row])

    def _on_rerouted(
        self, params: cas.ModelReroutedParams, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """#846: Codex ran another model than it echoed on thread/start; the
        footer shows the one that actually ran (supplementary started meta)."""
        if params.thread_id != state.thread_id or not params.to_model:
            return []
        logger.info(
            "codex.app_server.model_rerouted",
            from_model=params.from_model,
            to_model=params.to_model,
            reason=params.reason,
        )
        state.model = params.to_model
        token = ResumeToken(engine=ENGINE, value=params.thread_id)
        row = state.factory.started(
            token, title=self.session_title, meta={"model": params.to_model}
        )
        return self._rows(state, [row])

    def _on_token_usage(
        self, usage: cas.ThreadTokenUsage, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """#832: the header's ``N% ctx`` from this update's ``last`` (one
        model request) and its own window; ``total`` is thread-cumulative
        and kept for the footer. The telemetry action goes out only when
        (pct, window) changes."""
        state.tokens = usage.total
        window = usage.model_context_window
        if not window or window <= 0:
            return []
        used = usage.last.total_tokens
        pct = context_pct(used, window)  # half up, clamped to 100, as the other engines
        state.context = (pct, used, window)
        if state.ctx_emitted == (pct, window):
            return []
        state.ctx_emitted = (pct, window)
        row = state.factory.action_updated(
            action_id=_CONTEXT_ACTION_ID,
            kind="telemetry",
            title="context",
            detail={
                "context_pct": pct,
                "context_used": used,
                "context_window": window,
                "model": state.model,
            },
        )
        return self._rows(state, [row])

    def _error_row(
        self, params: cas.ErrorParams, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """``error`` mid-turn. ``willRetry``: exec's reconnect row. Otherwise
        a failed note, as exec shows its ``error`` line; turn/completed
        carries the error that ends the run."""
        message = params.error.message
        if not params.will_retry:
            return self._rows(state, [self.note_event(message, state=state, ok=False)])
        reconnect = _parse_reconnect_message(message)
        if reconnect is not None:
            attempt, max_attempts = reconnect
            phase: ActionPhase = "started" if attempt <= 1 else "updated"
            detail: dict[str, Any] = {"attempt": attempt, "max": max_attempts}
        else:
            phase = "updated" if state.retry_seen else "started"
            detail = {}
        state.retry_seen = True
        code = codex_error_code(params.error.codex_error_info)
        if code:
            detail["codex_error_info"] = code
        row = state.factory.action(
            phase=phase,
            action_id="codex.reconnect",
            kind="note",
            title=redact_url_secrets(message),
            detail=detail,
            level="info",
        )
        return self._rows(state, [row])

    def _on_turn_completed(
        self,
        turn: cas.Turn,
        *,
        state: AppServerRunState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        if turn.id and state.turn_id and turn.id != state.turn_id:
            # §16 item 3/5: an earlier turn's late completion never ends (or
            # retires the cards of) the current one.
            logger.info("codex.app_server.stale_turn_completed", turn_id=turn.id)
            return []
        state.saw_result = True  # sub-agent turns are filtered out upstream
        cards = self._retire_cards(state, turn.id)  # #963: none keep buttons
        state.file_changes.clear()  # §16 item 5: per turn, bounded
        state.live_wakeups.clear()
        if state.live_mode and state.completed_turns:
            return [*cards, *self._later_turn_completed(turn, state)]
        state.turn_open = False
        state.completed_turns += 1
        if turn.status == "completed":
            usage = None
            if state.manual_compact:
                # #964: the bridge answers "🗜️ Context compacted" for this.
                # No token figures: an excludeTurns resume replays no usage,
                # and the compact turn's ``last`` is the compaction request.
                usage = {
                    "compaction": {
                        "count": state.compactions,
                        "trigger": "manual",
                        "manual_success": True,
                    }
                }
            return [
                *cards,
                state.factory.completed_ok(
                    answer=self._answer(state),
                    resume=found_session or resume,
                    usage=self._run_usage(state, usage),
                ),
            ]
        rows = cards + self._close_open_items(
            state, "cancelled" if turn.status == "interrupted" else "failed"
        )
        error = (turn.error.message if turn.error else "") or (
            f"Codex turn {turn.status}"
        )
        if state.card_stop and turn.status == "interrupted":
            error = _STOPPED_BY_CARD
        code = codex_error_code(turn.error.codex_error_info) if turn.error else None
        usage: dict[str, Any] | None = {"codex_error_info": code} if code else None
        if usage is not None and code == "usageLimitExceeded":
            # #966: the hint names the reset, from this run's pushes or the cache.
            cached = codex_reads.peek_usage(self.codex_cmd)
            resets_at = codex_reads.limit_resets_at(cached) if cached else None
            if resets_at:
                usage["codex_resets_at"] = resets_at
        return [*rows, self._error(state, resume, error, usage)]

    def _later_turn_completed(
        self, turn: cas.Turn, state: AppServerRunState
    ) -> list[UntetherEvent]:
        """A live run's later turn: its TurnEvent (never a CompletedEvent)."""
        answer = self._answer(state)
        if turn.status == "completed":
            usage = None
            if state.turn_compact:
                # #964-3b: an injected /compact; the bridge answers
                # "🗜️ Context compacted" for it, as for a /compact run.
                usage = {"compaction": {"trigger": "manual", "manual_success": True}}
            return [
                self._turn_completed_event(state, ok=True, answer=answer, usage=usage)
            ]
        rows = self._close_open_items(
            state, "cancelled" if turn.status == "interrupted" else "failed"
        )
        error = (turn.error.message if turn.error else "") or (
            f"Codex turn {turn.status}"
        )
        if state.card_stop and turn.status == "interrupted":
            error = _STOPPED_BY_CARD
        code = codex_error_code(turn.error.codex_error_info) if turn.error else None
        usage = {"codex_error_info": code} if code else None
        return [
            *rows,
            self._turn_completed_event(
                state, ok=False, answer=answer, error=error, usage=usage
            ),
        ]

    # --- process end -----------------------------------------------------------

    def process_error_events(
        self,
        rc: int,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: AppServerRunState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        if state.stopped is not None:
            return [self._error(state, found_session or resume, state.stopped)]
        return super().process_error_events(
            rc,
            resume=resume,
            found_session=found_session,
            state=state,
            stderr_lines=stderr_lines,
        )

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: AppServerRunState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
        # The exec version reports success whenever a session id exists; here
        # only turn/completed ends a run well.
        error = state.stopped or _EXITED_EARLY
        logger.warning("codex.app_server.stream_end", error=error)
        return [self._error(state, found_session or resume, error)]


# --- steer target (#964; ``live_sessions.steer_backend``) ---------------------


class _CodexSteer:
    """Steer into a running app-server turn with ``turn/steer`` (stable).

    A steer-only target until live sessions (964-3): it writes into the
    per-run process while its turn runs. Codex folds the text in at its next
    model call (P6) and rejects it when the turn can't take one; those
    rejections fall back to the queue."""

    engine = ENGINE
    display_name = "Codex"
    log_ns = ENGINE

    @property
    def supports_steer(self) -> bool:
        return APP_SERVER_ACTIVE

    def get(self, session_id: str) -> AppServerRunState | None:
        state = _RUNS_BY_THREAD.get(session_id)
        if (
            state is None
            or state.closing
            or state.steer_closed
            or state.stopped is not None
            or not state.turn_open
            or state.turn_id is None
            or state.peer is None
            or state.peer.closed_reason is not None
        ):
            return None
        return state

    async def steer(self, session_id: str, text: str, **kwargs: Any) -> str:
        """``steered``: Codex took it, or it was sent and never answered
        (never queued twice, §16 item 1). ``window_closed``: no running turn,
        not sent, or Codex refused it (the caller queues it)."""
        state = self.get(session_id)
        if state is None or state.peer is None:
            return "window_closed"
        command_uuid = str(kwargs.get("command_uuid") or "")
        wait = _SteerWait(command_uuid=command_uuid)
        rids: list[int] = []

        def register(rid: int) -> None:
            rids.append(rid)
            state.steer_waits[rid] = wait
            state.steer_texts[command_uuid] = text

        def window_open() -> bool:
            # Re-checked under the writer lock: a /cancel that closed the
            # window while this waited for the writer stops it (#775).
            return not (state.closing or state.steer_closed)

        params = {
            "threadId": session_id,
            "expectedTurnId": state.turn_id,
            "input": [{"type": "text", "text": text}],
            "clientUserMessageId": command_uuid,
        }
        try:
            await state.peer.submit(
                "turn/steer", params, before_send=register, guard=window_open
            )
        except CodexRpcClosed:
            state.steer_texts.pop(command_uuid, None)
            for rid in rids:
                state.steer_waits.pop(rid, None)
            logger.info("codex.app_server.steer_unsent", command_uuid=command_uuid)
            return "window_closed"
        except CodexRpcWriteTimeout:
            pass  # part of it may have gone out: unconfirmed, below
        else:
            wait.sent = True
        with anyio.move_on_after(STEER_RESPONSE_S):
            await wait.done.wait()
        if wait.accepted:
            logger.info(
                "codex.app_server.steer_accepted",
                thread_id=session_id,
                turn_id=params["expectedTurnId"],
                command_uuid=command_uuid,
            )
            return "steered"
        if wait.error is not None:
            logger.info(
                "codex.app_server.steer_refused",
                thread_id=session_id,
                command_uuid=command_uuid,
                reason=wait.error,
            )
            return "window_closed"
        logger.warning(
            "codex.app_server.steer_unconfirmed",
            thread_id=session_id,
            command_uuid=command_uuid,
            sent=wait.sent,
        )
        return "steered"


STEER_BACKEND = _CodexSteer()


async def start_followup(
    state: AppServerRunState, text: str, *, command_uuid: str, compact: bool = False
) -> str:
    """#964-3a: a follow-up ``turn/start`` on a live run's idle thread
    (964-3b's ``inject_when_idle`` calls it under the session lock).

    Only the thread and the text: the thread keeps the approval policy,
    sandbox and model ``thread/start|resume`` set (§5.4). Outcomes:
    ``started`` (its turn is, or will be, the follow-up's TurnEvent),
    ``absorbed`` (Codex put it into a turn already running), ``busy`` (a turn
    is open or another start is in flight; nothing written), ``closed`` /
    ``unsent`` (not written), ``refused`` (Codex said no), ``unconfirmed``
    (sent, never answered: treat as written, never send it twice).

    ``compact`` (#964-3b, an injected bare ``/compact``): Codex's native
    ``thread/compact/start`` in place of ``turn/start``; its turn is the
    follow-up's like any other."""
    peer = state.peer
    if (
        not state.live_mode
        or state.closing
        or state.stopped is not None
        or peer is None
        or peer.closed_reason is not None
    ):
        return "closed"
    if state.turn_open or _expire_stale_start(state):
        return "busy"
    wait = _StartWait(command_uuid=command_uuid, text=text, compact=compact)

    def register(rid: int) -> None:
        # Before the write, with no await since the checks above.
        wait.rid = rid
        state.pending_start = wait
        state.followup_rids.add(rid)

    def written(_rid: int) -> None:
        # Only from here can a turn/started be this follow-up's (review H2).
        wait.sent = True

    method, params = (
        "turn/start",
        {
            "threadId": state.thread_id,
            "input": [{"type": "text", "text": text}],
            "clientUserMessageId": command_uuid,
        },
    )
    if compact:
        method, params = "thread/compact/start", {"threadId": state.thread_id}
    elif state.plan:
        params["collaborationMode"] = _collab_mode(state, "plan")  # #965
    try:
        await peer.submit(method, params, before_send=register, after_send=written)
    except CodexRpcClosed:
        if state.pending_start is wait:
            state.pending_start = None
        logger.info("codex.app_server.followup_unsent", command_uuid=command_uuid)
        return "unsent"
    except CodexRpcWriteTimeout:
        pass  # part of it may have gone out: unconfirmed, below
    with anyio.move_on_after(FOLLOWUP_RESPONSE_S):
        await wait.done.wait()
    if wait.absorbed:
        return "absorbed"
    if wait.error is not None:
        logger.info(
            "codex.app_server.followup_refused",
            command_uuid=command_uuid,
            reason=wait.error,
        )
        return "refused"
    if wait.accepted or wait.turn_id is not None:
        if not wait.accepted:  # its turn started; the answer was lost
            wait.timed_out = True
            _settle_start(state, wait)
        logger.info(
            "codex.app_server.followup_written",
            command_uuid=command_uuid,
            turn_id=wait.turn_id,
        )
        return "started"
    wait.timed_out = True
    _settle_start(state, wait)
    logger.warning(
        "codex.app_server.followup_unconfirmed",
        command_uuid=command_uuid,
        sent=wait.sent,
    )
    return "unconfirmed"


# --- live sessions (#964-3b; ``live_sessions._CodexLive``) --------------------
#
# The registry entry is the run's own ``AppServerRunState`` in
# ``_RUNS_BY_THREAD`` (kept until its process has exited). Only a run with
# ``live_mode`` takes follow-ups; a per-run one is visible only to steer.

# A follow-up whose turn never starts (its answer lost, or accepted and no
# turn/started) stops counting as pending after this, so the session can't
# look busy forever (Claude's ``_INJECTED_TURN_TIMEOUT_S``).
FOLLOWUP_TURN_TIMEOUT_S = 120.0
HANDOFF_POLL_S = 0.25
_INJECT_OK = frozenset({"started", "absorbed", "unconfirmed"})
_UNSET: Any = object()


def _live_settings() -> tuple[bool, float, float]:
    """``[watchdog] live_sessions``, ``post_result_limbo_grace`` and
    ``live_session_max_s``, read per spawn like Claude's (D1, D11)."""
    enabled, grace, cap = True, 60.0, 14400.0
    try:
        from ..settings import load_settings_if_exists

        loaded = load_settings_if_exists()
        if loaded is not None:
            watchdog = loaded[0].watchdog
            enabled = bool(watchdog.live_sessions)
            grace = float(watchdog.post_result_limbo_grace)
            cap = float(watchdog.live_session_max_s)
    except Exception:  # noqa: BLE001 — settings errors must not block a run
        logger.debug("codex.live_session.settings_load_failed", exc_info=True)
    return enabled, grace, cap


def _expire_stale_start(state: AppServerRunState, now: float | None = None) -> bool:
    """Whether a follow-up is still pending; one whose turn hasn't started
    ``FOLLOWUP_TURN_TIMEOUT_S`` after it was registered is dropped (logged).
    A turn that starts later anyway is Codex's own (``unknown``)."""
    wait = state.pending_start
    if wait is None:
        return False
    waited = (time.monotonic() if now is None else now) - wait.at
    if wait.started or waited < FOLLOWUP_TURN_TIMEOUT_S:
        return True
    state.pending_start = None
    # Its rid stays in ``followup_rids`` (the tombstone): a late answer is
    # then ignored, never read as the first turn's (review of 3b, item 3).
    wait.timed_out = True
    wait.done.set()
    logger.warning(
        "codex.live_session.followup_turn_timeout",
        session_id=state.thread_id,
        command_uuid=wait.command_uuid,
        accepted=wait.accepted,
        waited_s=round(waited, 1),
    )
    return False


def _is_idle(state: AppServerRunState) -> bool:
    """Between turns: the first turn done, none open, no follow-up pending."""
    return (
        state.completed_turns > 0
        and not state.turn_open
        and state.pending_start is None
    )


def _live(session_id: str) -> AppServerRunState | None:
    state = _RUNS_BY_THREAD.get(session_id)
    return state if state is not None and state.live_mode else None


def _accepting(state: AppServerRunState | None) -> bool:
    return (
        state is not None
        and state.live_mode
        and not state.closing
        and state.stopped is None
        and state.peer is not None
        and state.peer.closed_reason is None
    )


async def _notify(state: AppServerRunState, kind: str, payload: dict[str, Any]) -> None:
    for callback in list(state.listeners):
        try:
            result = callback(kind, payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001 — a listener must never break teardown
            logger.warning(
                "codex.live_session.listener_failed",
                session_id=state.thread_id,
                kind=kind,
                exc_info=True,
            )


async def close_live_session(
    session_id: str,
    reason: str,
    *,
    notice: bool = False,
    only_if_idle: bool = False,
) -> bool:
    """Close a live session's stdin; Codex exits in ~0.1 s and the run ends
    (teardown escalates if it doesn't). Idempotent; False when there is
    nothing (left) to close, or ``only_if_idle`` and a turn or follow-up is
    in flight (re-checked under the lock a follow-up is written under).
    Never quarantined: Codex rollouts are durable, interrupted turns too."""
    state = _live(session_id)
    if state is None:
        return False
    async with state.lock:
        if state.closing:
            return False
        if only_if_idle and not _is_idle(state):
            return False
        mid_turn = not _is_idle(state)
        state.closing = True
        state.closed_at = time.monotonic()
        state.live_close_reason = reason
    logger.info(
        "codex.live_session.stdin_closed",
        session_id=session_id,
        reason=reason,
        turn=state.completed_turns,
        turn_idle=not mid_turn,
        age_s=round(time.monotonic() - state.spawned_at, 1),
    )
    if notice:
        payload: dict[str, Any] = {"reason": reason, "tasks": []}
        if reason == "abs_cap":
            payload["mid_turn"] = mid_turn
            payload["max_s"] = state.live_max_s
        await _notify(state, "closing", payload)
    with contextlib.suppress(Exception):
        await state.proc.stdin.aclose()
    return True


async def _live_ended(state: AppServerRunState, *, died: bool) -> None:
    """After a live run's process is gone: one ``closed`` notice for an
    Untether close, or the #1001 ``died`` notice for an exit nobody asked
    for while idle (a crash, ``kill``; Codex never exits on its own while
    its stdin is open, so rc 0 counts too)."""
    reason = state.live_close_reason
    if reason is not None:
        logger.info(
            "codex.live_session.closed",
            session_id=state.thread_id,
            close_reason=reason,
            quarantined=False,
        )
        await _notify(
            state, "closed", {"reason": reason, "quarantined": False, "tasks": []}
        )
        return
    from ..shutdown import is_shutting_down

    if not died or is_shutting_down():
        return
    from .claude import _exit_cause, _exit_signal, _signal_name

    rc = getattr(state.proc, "returncode", None)
    sig = _exit_signal(rc)
    state.live_exit_cause = _exit_cause(rc)
    logger.warning(
        "codex.live_session.died_unexpectedly",
        session_id=state.thread_id,
        pid=getattr(state.proc, "pid", None),
        rc=rc,
        signal_name=_signal_name(sig),
        exit_cause=state.live_exit_cause,
        age_s=round(time.monotonic() - state.spawned_at, 1),
    )
    await _notify(
        state,
        "died",
        {
            "rc": rc,
            "signal": sig,
            "signal_name": _signal_name(sig),
            "exit_cause": state.live_exit_cause,
            "tasks": [],
            "task_ids": [],
            "quarantined": False,
        },
    )


async def _trust_drift(state: AppServerRunState) -> str | None:
    """§16 item 6: before a follow-up, re-read Codex's config for the
    session's cwd and compare what the spawn's sandbox decision rested on.
    None: unchanged (or a tier that never decided: read-only). ``drift`` or
    ``unverifiable`` (no answer, an error): the caller stops accepting, so
    the message resumes through a fresh, validated spawn (fail closed)."""
    if not state.check_trust:
        return None
    reason: str | None = "unverifiable"
    wait = _ReadWait()

    def register(rid: int) -> None:
        state.read_waits[rid] = wait

    try:
        await state.peer.submit(  # type: ignore[union-attr]
            "config/read", {"cwd": state.cwd}, before_send=register
        )
    except (CodexRpcClosed, CodexRpcWriteTimeout):
        wait.done.set()
    with anyio.move_on_after(FOLLOWUP_RESPONSE_S):
        await wait.done.wait()
    if wait.done.is_set() and wait.error is None and state.spawn_trust is not None:
        changed = _trust_view(state.cwd, wait.result) != state.spawn_trust
        reason = "drift" if changed else None
    if reason is not None:
        logger.warning(
            "codex.live_session.trust_changed",
            session_id=state.thread_id,
            reason=reason,
            error=wait.error,
        )
    return reason


async def inject_when_idle(
    session_id: str,
    text: str,
    *,
    command_uuid: str,
    poll_s: float = 0.2,
    cancel: Any = None,
    recheck: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Queue semantics (D7): wait until the live session is idle, then write
    ``text`` as a follow-up ``turn/start``, or Codex's own
    ``thread/compact/start`` for a bare ``/compact``, under the session
    lock. False: the caller keeps the message for the resume path. A
    refusal closes the idle session first, so that resume isn't left behind
    the idle grace (and nothing is retried into it). ``recheck`` (the
    caller's options/cwd/daily-gate checks) and the transport run again under
    the lock, just before the write: the wait can span a whole turn (#1433,
    #1431)."""
    close_reason = "error"
    while True:
        if cancel is not None and cancel.cancelled:
            return False
        state = _live(session_id)
        if state is None or not _accepting(state):
            return False
        if not _expire_stale_start(state) and _is_idle(state):
            async with state.lock:
                if not _accepting(state):
                    return False
                if _is_idle(state):
                    if await _trust_drift(state) is not None:
                        close_reason = "trust_changed"
                        break
                    if not APP_SERVER_ACTIVE or (
                        recheck is not None and not await recheck()
                    ):
                        close_reason = "options_changed"
                        break
                    if cancel is not None and not cancel.claim():
                        return False  # #935: cancelled from its queued card
                    outcome = await start_followup(
                        state,
                        text,
                        command_uuid=command_uuid,
                        compact=_is_compact(text),
                    )
                    if outcome in _INJECT_OK:
                        state.idle_since = None
                        return True
                    if outcome != "busy":
                        break
        await anyio.sleep(poll_s)
    await close_live_session(
        session_id,
        close_reason,
        notice=close_reason == "options_changed",
        only_if_idle=True,
    )
    return False


async def steer_into_session(
    session_id: str,
    text: str,
    *,
    command_uuid: str = "",
    run_options: Any = _UNSET,
    cwd: Any = _UNSET,
    **_: Any,
) -> str:
    """Steer mode into a live session: mid-turn → ``turn/steer`` (964-2's
    target, ``steered`` / ``window_closed``); idle → the same follow-up
    write as the queue (``written_idle``), unless the chat's options or
    directory changed since spawn (``options_changed``: the queue path
    closes and resumes, D8). A per-run (not live) run is steer-only."""
    state = _RUNS_BY_THREAD.get(session_id)
    live = state is not None and state.live_mode
    trust_changed = False
    if state is not None and live:
        async with state.lock:
            if state.closing or state.steer_closed:
                return "window_closed"
            if _is_idle(state):
                if (
                    run_options is not _UNSET and run_options != state.spawn_options
                ) or _needs_mode_reset(state):  # #965 D1
                    return "options_changed"
                if cwd is not _UNSET and cwd != state.spawn_cwd:
                    logger.info(
                        "codex.live_session.context_changed",
                        session_id=session_id,
                        source="steer",
                        spawn_cwd=None
                        if state.spawn_cwd is None
                        else str(state.spawn_cwd),
                        cwd=None if cwd is None else str(cwd),
                    )
                    return "options_changed"
                trust_changed = await _trust_drift(state) is not None
                if not trust_changed:
                    outcome = await start_followup(
                        state, text, command_uuid=command_uuid
                    )
                    if outcome not in _INJECT_OK:
                        return "window_closed"
                    state.idle_since = None
                    logger.info(
                        "codex.live_session.steered",
                        session_id=session_id,
                        command_uuid=command_uuid,
                        mid_turn=False,
                    )
                    return "written_idle"
        if trust_changed:
            # Closed outside the lock it takes; the queue path resumes.
            await close_live_session(session_id, "trust_changed", only_if_idle=True)
            return "options_changed"
    outcome = await STEER_BACKEND.steer(session_id, text, command_uuid=command_uuid)
    if outcome == "steered" and live:
        logger.info(
            "codex.live_session.steered",
            session_id=session_id,
            command_uuid=command_uuid,
            mid_turn=True,
        )
    return outcome


async def close_steer_window(session_id: str, reason: str) -> bool:
    """No steer lands in a run being cancelled (#775): set under the lock
    a live steer checks; the steer target's ``get`` refuses it too."""
    state = _RUNS_BY_THREAD.get(session_id)
    if state is None:
        return False
    async with state.lock:
        state.steer_closed = True
    if state.peer is not None:
        # A steer already queued behind the writer sees the flag (its
        # guard) or has been written before this returns.
        await state.peer.write_barrier()
    logger.info(
        "codex.live_session.steer_window_closed", session_id=session_id, reason=reason
    )
    return True


async def interrupt_pending_turn(
    session_id: str, *, any_turn: bool = False, bound_s: float = _INTERRUPT_BOUND_S
) -> bool:
    """#845 for Codex: end a live session's open turn that is waiting on the
    user (``waiting`` flags), or any open turn with ``any_turn`` (#1125),
    with ``turn/interrupt``; True once its turn/completed arrived (the
    reader routes both). A per-run run keeps 962-4's path: the cancelled
    reader interrupts."""
    state = _live(session_id)
    if (
        state is None
        or not _accepting(state)
        or not state.turn_open
        or state.turn_id is None
        or not (any_turn or state.waiting)
    ):
        return False
    target = state.turn_id
    try:
        await state.peer.submit(  # type: ignore[union-attr]
            "turn/interrupt", {"threadId": session_id, "turnId": target}
        )
    except (CodexRpcClosed, CodexRpcWriteTimeout):
        return False
    # Polled: the reader (another task) closes the turn on its turn/completed.
    with anyio.move_on_after(bound_s):
        while True:
            if not (state.turn_open and state.turn_id == target):
                logger.info("codex.live_session.interrupted", session_id=session_id)
                return True
            await anyio.sleep(0.02)
    logger.warning("codex.live_session.interrupt_no_result", session_id=session_id)
    return False


async def close_idle_for_continue(cwd: Path | None) -> int:
    """#1275: close the idle live sessions spawned in ``cwd``."""
    closed = 0
    for sid, state in list(_RUNS_BY_THREAD.items()):
        if state.live_mode and state.spawn_cwd == cwd and not state.waiting:
            closed += await close_live_session(sid, "continue", only_if_idle=True)
    return closed


async def wait_for_handoff(
    session_id: str, timeout_s: float, *, poll_s: float = HANDOFF_POLL_S
) -> str:
    """#633 for Codex: ``free`` (no process owns the thread), ``exited``
    (one did and is gone) or ``timed_out``. Ownership ends only once the
    process has exited (§16 item 4)."""
    if session_id not in _RUNS_BY_THREAD:
        return "free"
    started = time.monotonic()
    with anyio.move_on_after(max(0.0, timeout_s)):
        while True:
            if session_id not in _RUNS_BY_THREAD:
                break
            await anyio.sleep(poll_s)
    outcome = "timed_out" if session_id in _RUNS_BY_THREAD else "exited"
    logger.info(
        "session.handoff_wait_done",
        session_id=session_id,
        outcome=outcome,
        elapsed_s=round(time.monotonic() - started, 1),
    )
    return outcome
