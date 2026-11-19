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
from ..model import CompletedEvent, ResumeToken, UntetherEvent
from ..runner import PRESPAWN_BLOCKED_KEY, JsonlStreamState, JsonlSubprocessRunner
from ..schemas import codex_app_server as cas
from ..schemas.codex_app_server import (
    CODEX_APP_SERVER_SENDS,
    CODEX_APP_SERVER_TYPES,
    CODEX_APP_SERVER_USED,
    MIN_TESTED_APP_SERVER_VERSION,
)
from ..utils.paths import get_run_base_dir
from ..utils.streams import _MAX_LINE_BYTES
from ..utils.subprocess import terminate_process
from .codex import (
    _CODEX_FULL_AUTO_MODES,
    CODEX_SAFE_PERMISSION_MODE,
    ENGINE,
    CodexRunner,
    CodexRunState,
    _AgentMessageSummary,
    _select_final_answer,
    _warn_webhook_unknown_mode,
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


def _warn_unknown_mode_safe(mode: str) -> None:
    if mode in _UNKNOWN_MODE_SAFE_WARNED:
        return
    _UNKNOWN_MODE_SAFE_WARNED.add(mode)
    logger.warning(
        "codex.app_server.unknown_mode_safe",
        mode=mode,
        note="unknown Codex permission_mode runs read-only on app-server (DEC-5)",
    )


@dataclass(slots=True)
class AppServerRunState(CodexRunState):
    prompt: str = ""
    cwd: str = ""
    # Sandbox sent on thread/start|resume; None = Codex's own setting.
    sandbox: str | None = None
    # Full auto: config/read decides whether omitting the sandbox is safe.
    check_trust: bool = False
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


class CodexAppServerRunner(CodexRunner):
    """``codex app-server`` per run. Keeps the base ``run_impl`` (guards,
    watchdog, stderr, exit handling); overrides only the stdin and stdout
    seams so the process stays open for JSON-RPC."""

    process_label = "codex app-server"
    # Instance-tunable for tests.
    exit_grace_s = _EXIT_GRACE_S
    write_timeout_s = WRITE_TIMEOUT_S

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
        mode = self._permission_mode()
        if mode is None or mode in _CODEX_FULL_AUTO_MODES:
            state.check_trust = True
        else:
            if mode != CODEX_SAFE_PERMISSION_MODE:
                _warn_unknown_mode_safe(mode)  # DEC-5: fail closed
            state.sandbox = "read-only"
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
        state.peer = CodexRpcPeer(proc.stdin.send, write_timeout=self.write_timeout_s)
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
        while state.stopped is None and not stream.did_emit_completed:
            try:
                line = await state.reader.receive_until(b"\n", _MAX_LINE_BYTES)
            except (anyio.IncompleteRead, anyio.ClosedResourceError):
                break
            try:
                state.inbound = await peer.feed(line)
                if state.inbound is not None:
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
        peer.close("done" if stream.did_emit_completed else "eof")
        await self._close_process(state.proc)

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
        if isinstance(msg, Notification):
            return self._on_notification(
                msg, state=state, resume=resume, found_session=found_session
            )
        return []

    def _error(
        self, state: AppServerRunState, resume: ResumeToken | None, error: str
    ) -> CompletedEvent:
        token = (
            ResumeToken(engine=ENGINE, value=state.thread_id)
            if state.thread_id
            else resume
        )
        return state.factory.completed_error(
            error=error, answer=self._answer(state), resume=token
        )

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
                if state.check_trust:
                    state.outbox.append(("request", "config/read", {"cwd": state.cwd}))
                else:
                    self._open_thread(state, resume)
            case "config/read":
                self._decide_sandbox(state, msg.result)
                self._open_thread(state, resume)
            case "thread/list":
                listed = cas.decode_result("thread/list", msg.result)
                if listed.data:
                    params = {"threadId": listed.data[0].id, **self._overrides(state)}
                    state.outbox.append(("request", "thread/resume", params))
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
        """Full auto omits ``sandbox`` (Codex's own setting) only where that
        can't make Codex trust the project (DEC-7, P1): a writable sandbox in
        a project with no ``[projects]`` entry saves it as trusted."""
        config = result.get("config") if isinstance(result, dict) else None
        config = config if isinstance(config, dict) else {}
        if has_trust_entry(config.get("projects"), state.cwd):
            return
        state.sandbox = "read-only"
        sandbox_mode = config.get("sandbox_mode")
        logger.info(
            "codex.app_server.untrusted_read_only",
            cwd=state.cwd,
            sandbox_mode=sandbox_mode,
        )
        if sandbox_mode in _WRITABLE_SANDBOX_MODES:
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
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
        }
        if state.sandbox is not None:
            params["sandbox"] = state.sandbox
        run_options = get_run_options()
        if run_options is not None and run_options.model:
            params["model"] = str(run_options.model)
        return params

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
            params = {"threadId": resume.value, **self._overrides(state)}
            state.outbox.append(("request", "thread/resume", params))

    def _on_thread(
        self, result: cas.ThreadOpenResult, *, state: AppServerRunState
    ) -> list[UntetherEvent]:
        state.thread_id = result.thread.id
        run_options = get_run_options()
        meta: dict[str, Any] = {"model": result.model or result.thread.model or "codex"}
        if run_options is not None and run_options.reasoning:
            meta["effort"] = run_options.reasoning
        if self._permission_mode() == CODEX_SAFE_PERMISSION_MODE:
            meta["permissionMode"] = CODEX_SAFE_PERMISSION_MODE
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
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
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
        if isinstance(params, cas.TurnParams) and params.thread_id != state.thread_id:
            return []  # a sub-agent thread's turn, not this run's
        match msg.method:
            case "turn/started":
                state.turn_id = params.turn.id
                state.turn_agent_messages.clear()
            case "item/completed":
                item = cas.decode_thread_item(params.item)
                if isinstance(item, cas.AgentMessageItem) and (
                    not params.thread_id or params.thread_id == state.thread_id
                ):
                    state.turn_agent_messages.append(
                        _AgentMessageSummary(text=item.text, phase=item.phase)
                    )
            case "turn/completed":
                turn = params.turn
                token = found_session or resume
                if turn.status == "completed":
                    return [
                        state.factory.completed_ok(
                            answer=self._answer(state), resume=token
                        )
                    ]
                error = (turn.error.message if turn.error else "") or (
                    f"Codex turn {turn.status}"
                )
                return [self._error(state, resume, error)]
        return []

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
