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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import msgspec
from anyio.streams.buffered import BufferedByteReceiveStream

from .. import __version__
from ..events import EventFactory
from ..logging import get_logger
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
from ..utils.antigravity_context import context_pct
from ..utils.error_display import redact_url_secrets
from ..utils.paths import get_run_base_dir, relativize_path
from ..utils.streams import _MAX_LINE_BYTES
from ..utils.subprocess import terminate_process
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
    WRITE_TIMEOUT_S,
    CodexRpcClosed,
    CodexRpcPeer,
    CodexRpcWriteTimeout,
    Notification,
    Response,
)
from .run_options import get_run_options

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
_APPROVAL_REQUESTS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "mcpServer/elicitation/request",
    }
)
_APPROVAL_DECLINED_NOTE = (
    f"{_WARNING} Codex asked for approval; declined: this transport doesn't"
    " support approvals yet"
)
# Items with no row: the prompt echo and hook text, plus plan (#965).
_SILENT_ITEM_TYPES = frozenset({"userMessage", "hookPrompt", "plan"})
# #832: Claude's 🗜️ wording (#819), so both engines read the same.
_COMPACTING_TITLE = "\N{COMPRESSION}\N{VARIATION SELECTOR-16} Compacting context…"
_COMPACTED_TITLE = "\N{COMPRESSION}\N{VARIATION SELECTOR-16} Context compacted"
_CONTEXT_ACTION_ID = "codex.context"
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


def has_trust_entry(projects: Any, cwd: str) -> bool:
    """Whether Codex's ``[projects]`` table (from ``config/read``) has an
    entry, trusted or not, for *cwd* or a parent. Codex keeps an explicit
    entry and writes one only when there's none (P1, 0.170.0)."""
    if not isinstance(projects, dict) or not cwd:
        return False
    paths: set[Path] = set()
    for path in (Path(cwd), Path(cwd).resolve()):
        paths.add(path)
        paths.update(path.parents)
    return any(isinstance(projects.get(str(p)), dict) for p in paths)


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


class CodexAppServerRunner(CodexRunner):
    """``codex app-server`` per run. Keeps the base ``run_impl`` (guards,
    watchdog, stderr, exit handling); overrides only the stdin and stdout
    seams so the process stays open for JSON-RPC."""

    process_label = "codex app-server"
    transport = "app-server"
    # Instance-tunable for tests.
    exit_grace_s = _EXIT_GRACE_S
    write_timeout_s = WRITE_TIMEOUT_S
    interrupt_bound_s = _INTERRUPT_BOUND_S

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
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
        return AppServerRunState(factory=EventFactory(ENGINE), prompt=prompt)

    def build_args(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> list[str]:
        run_options = get_run_options()
        raw_mode = run_options.permission_mode if run_options is not None else None
        webhook = _webhook_safe_trigger(run_options)
        if webhook is not None and raw_mode is not None:
            _warn_webhook_unknown_mode(webhook, raw_mode)
        unattended = run_options is not None and bool(run_options.unattended_trigger)
        state.tier = _tier(self._permission_mode(), unattended=unattended)
        state.sandbox = state.tier.sandbox
        state.check_trust = state.sandbox != "read-only"
        state.cwd = str(get_run_base_dir() or Path.cwd())
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
                        "experimentalApi": False,
                        "optOutNotificationMethods": list(OPT_OUT_NOTIFICATIONS),
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
        try:
            while state.stopped is None and not stream.did_emit_completed:
                try:
                    line = await state.reader.receive_until(b"\n", _MAX_LINE_BYTES)
                except (anyio.IncompleteRead, anyio.ClosedResourceError):
                    break
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
                    while state.outbox and not stream.did_emit_completed:
                        kind, method, params = state.outbox.pop(0)
                        if kind == "notify":
                            await peer.notify(method, params)
                        else:
                            await peer.submit(method, params)
                except (CodexRpcClosed, CodexRpcWriteTimeout) as exc:
                    self._stop(state, exc)
        except (anyio.get_cancelled_exc_class(), GeneratorExit):
            # /cancel: the bridge cancelled the run (at a read) or closed this
            # generator (at a yield). Stop the turn natively, then re-raise.
            await self._interrupt(state, stream)
            raise
        peer.close("done" if stream.did_emit_completed else "eof")
        await self._close_process(state.proc)

    async def _interrupt(
        self, state: AppServerRunState, stream: JsonlStreamState
    ) -> None:
        """``turn/interrupt``, bounded and shielded: Codex ends the turn in
        ~0.1 s and exits once stdin closes, so teardown finds it gone. On
        timeout the base teardown's SIGTERM is the fallback."""
        peer = state.peer
        if (
            stream.did_emit_completed
            or state.stopped is not None
            or state.thread_id is None
            or state.turn_id is None
        ):
            peer.close("cancelled")  # handshake cancel: plain teardown
            return
        started = time.monotonic()
        ok = False
        with (
            anyio.CancelScope(shield=True),
            anyio.move_on_after(self.interrupt_bound_s),
        ):
            try:
                # Never await the answer: this loop is the only reader.
                await peer.submit(
                    "turn/interrupt",
                    {"threadId": state.thread_id, "turnId": state.turn_id},
                )
                # The same buffered reader, so a part-read line isn't lost.
                while not ok:
                    line = await state.reader.receive_until(b"\n", _MAX_LINE_BYTES)
                    msg = await peer.feed(line)
                    # An error answer ("no active turn") is ignored; any
                    # turn/completed (interrupted, or a finish that won the
                    # race) ends the turn.
                    ok = (
                        isinstance(msg, Notification)
                        and msg.method == "turn/completed"
                        and msg.params.get("threadId") == state.thread_id
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
            elapsed_s=round(time.monotonic() - started, 3),
            open_items_closed=closed,
        )

    async def _close_process(self, proc: Any) -> None:
        """Close stdin (the app-server exits in ~0.1 s), then a bounded wait:
        the base's own ``proc.wait()`` has no bound."""
        with contextlib.suppress(Exception):
            await proc.stdin.aclose()
        with anyio.move_on_after(self.exit_grace_s):
            await proc.wait()
            return
        logger.warning("codex.app_server.exit_timeout", pid=proc.pid)
        terminate_process(proc)

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
            # Untether never starts one itself (manual /compact is #964).
            out["compaction"] = {"count": state.compactions, "trigger": "auto"}
        out.update(usage or {})
        return self._untrusted_usage(state, out or None)

    def _on_server_request(self, state: AppServerRunState, method: str) -> None:
        """The peer already declined it; this only adds the row."""
        state.server_requests += 1
        title = (
            _APPROVAL_DECLINED_NOTE
            if method in _APPROVAL_REQUESTS
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
        match item:
            case cas.ContextCompactionItem():
                # #832: Codex compacting mid-turn (auto); same row as Claude.
                kind = "note"
                title = _COMPACTING_TITLE
                if phase == "completed":
                    state.compactions += 1
                    title = _COMPACTED_TITLE
                    logger.info(
                        "codex.compaction.completed",
                        thread_id=state.thread_id,
                        count=state.compactions,
                    )
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
        if selected is None and messages:
            selected = messages[-1].text
        return selected or ""

    def _on_response(
        self, msg: Response, *, state: AppServerRunState, resume: ResumeToken | None
    ) -> list[UntetherEvent]:
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
                logger.info("codex.app_server.initialized", user_agent=init.user_agent)
                state.outbox.append(("notify", "initialized", {}))
                if state.check_trust or (resume is not None and not _run_model()):
                    state.outbox.append(("request", "config/read", {"cwd": state.cwd}))
                else:
                    self._open_thread(state, resume)
            case "config/read":
                if state.check_trust:
                    self._decide_sandbox(state, msg.result)
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
        return []

    def _decide_sandbox(self, state: AppServerRunState, result: Any) -> None:
        """Full auto omits ``sandbox`` (Codex's own setting), and Ask me /
        Auto-review send ``workspace-write``, only where that can't make Codex
        trust the project (DEC-7, P1, D1): a writable sandbox in a project
        with no ``[projects]`` entry saves it as trusted."""
        config = result.get("config") if isinstance(result, dict) else None
        config = config if isinstance(config, dict) else {}
        if has_trust_entry(config.get("projects"), state.cwd):
            return
        sandbox_mode = config.get("sandbox_mode")
        wanted = state.sandbox or sandbox_mode
        state.sandbox = "read-only"
        logger.info(
            "codex.app_server.untrusted_read_only",
            cwd=state.cwd,
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
        turn: dict[str, Any] = {
            "threadId": result.thread.id,
            "input": [{"type": "text", "text": state.prompt}],
            "turnTrigger": "untether",
        }
        if run_options is not None and run_options.reasoning:
            turn["effort"] = run_options.reasoning
        state.outbox.append(("request", "turn/start", turn))
        return events

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
        logger.info(
            "codex.app_server.permissions", requested=requested, effective=effective
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
                state.turn_id = params.turn.id
                state.turn_agent_messages.clear()
            case "item/started":
                return self._item_rows("started", params.item, state=state)
            case "item/completed":
                return self._item_rows("completed", params.item, state=state)
            case "error":
                return self._error_row(params, state=state)
            case "warning":
                return self._warning_rows(state, msg.method, params.message)
            case "configWarning" | "deprecationNotice":
                return self._warning_rows(state, msg.method, params.summary)
            case "thread/tokenUsage/updated":
                return self._on_token_usage(params.token_usage, state=state)
            case "turn/completed":
                return self._on_turn_completed(
                    params.turn, state=state, resume=resume, found_session=found_session
                )
        return []

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
        if turn.status == "completed":
            return [
                state.factory.completed_ok(
                    answer=self._answer(state),
                    resume=found_session or resume,
                    usage=self._run_usage(state, None),
                )
            ]
        rows = self._close_open_items(
            state, "cancelled" if turn.status == "interrupted" else "failed"
        )
        error = (turn.error.message if turn.error else "") or (
            f"Codex turn {turn.status}"
        )
        code = codex_error_code(turn.error.codex_error_info) if turn.error else None
        usage = {"codex_error_info": code} if code else None
        return [*rows, self._error(state, resume, error, usage)]

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
