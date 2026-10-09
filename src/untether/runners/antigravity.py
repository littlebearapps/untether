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

Safety: this runner never passes ``--dangerously-skip-permissions`` (the
explicit permission modes land with phase 02), refuses to run without a
project directory, and refuses agy older than 1.3.1.
"""

from __future__ import annotations

import contextlib
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
from ..model import Action, ActionKind, EngineId, ResumeToken, UntetherEvent
from ..runner import (
    PRESPAWN_BLOCKED_KEY,
    JsonlSubprocessRunner,
    ResumeTokenMixin,
    Runner,
    _rc_label,
    _session_label,
    _stderr_excerpt,
)
from ..schemas import antigravity as agy_schema
from ..utils.paths import get_run_base_dir
from .run_options import get_run_options
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
NO_PROJECT_TEXT = (
    "Antigravity needs a project — bind this chat with /ctx set … or a "
    "[projects.*] entry. It won't run in the bot's own directory, because it "
    "can edit files there."
)
_DENIED_TEXT = "denied by Antigravity's headless permission policy"
NO_PROJECT_BLOCK = "no_project"
UNSUPPORTED_VERSION_BLOCK = "unsupported_version"

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
# ``result.denied_actions[].action`` → the tool names it covers.
_DENIED_ACTION_TOOLS: dict[str, frozenset[str]] = {
    "command": frozenset({"run_command"}),
}

# 08 §6 (#975): tools that can hold agy's single result while they run.
_BACKGROUND_TOOLS = frozenset({"run_command", "schedule", "manage_task"})

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
    """A ``result.error`` of any shape → text (None when absent/empty)."""
    if error is None or error == "":
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("error")
        if isinstance(message, str) and message:
            return message
    try:
        return json.dumps(error, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(error)


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
    # 08 §2/§6: descendant PIDs swept by manage_subprocess at teardown
    # (collected by 08 §6; empty until then).
    orphan_pid_snapshot: list[int] = field(default_factory=list)
    agy_pid: int | None = None
    agy_version: str | None = None
    t_spawn: float = 0.0
    t_init: float | None = None
    resumed: bool = False
    argv: list[str] | None = None

    def has_live_background_work(self) -> bool:
        """REVIEW-2 B1: answers ``runner_bridge.engine_background_busy``."""
        return bool(self.bg_steps)


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
    session_title: str = "antigravity"
    logger = logger
    _EXPOSE_ENGINE_STATE = True

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
        version_block = await self._unsupported_version_event(resume)
        if version_block is not None:
            yield version_block
            return
        _log_tos_notice_once()
        # Explicit parent ref: zero-arg super() breaks in @dataclass(slots=True).
        async with contextlib.aclosing(
            JsonlSubprocessRunner.run_impl(self, prompt, resume)
        ) as events:
            async for evt in events:
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
        # instead of an rc 2 exit (P22, D30). Never bypass permissions here.
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
        if isinstance(state, AntigravityStreamState):
            state.argv = list(args)
        return args

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
        return AntigravityStreamState(
            factory=EventFactory(ENGINE),
            expected_resume=expected or None,
            resumed=resume is not None,
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

    def _meta(self, state: AntigravityStreamState) -> dict[str, Any] | None:
        model = self._model() or state.init_model
        return {"model": model} if model else None

    def translate(
        self,
        data: agy_schema.AntigravityEvent,
        *,
        state: AntigravityStreamState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[UntetherEvent]:
        if state.conversation_missing:
            return []
        match data:
            case agy_schema.Init(conversation_id=cid, init=payload):
                if payload is not None and payload.model:
                    state.init_model = payload.model
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
            )
        ]

    def _conversation_gone(
        self, state: AntigravityStreamState, cid: str
    ) -> list[UntetherEvent]:
        """G8: agy silently starts a new conversation for an unknown
        ``--conversation`` id. Stop it (the user's turn must not run in a
        conversation they didn't pick) and say so."""
        assert state.expected_resume is not None
        state.conversation_missing = True
        logger.warning(
            "antigravity.conversation.missing",
            expected=state.expected_resume,
            got=cid,
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
        if (output is None or output == "") and tool_name in _DENIABLE_TOOLS:
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
        denied: list[agy_schema.DeniedAction] | None = None,
    ) -> list[UntetherEvent]:
        """Close every row still open so none is left "running" at the end."""
        out: list[UntetherEvent] = []
        if state.verdict_pending:
            denied_ids = _denied_action_ids(state.verdict_pending, denied or [])
            for action_id, action in list(state.verdict_pending.items()):
                if action_id in denied_ids:
                    out.append(
                        self._complete(
                            action,
                            state,
                            ok=False,
                            message=_DENIED_TEXT,
                            extra={"denied": True},
                        )
                    )
                else:
                    out.append(self._complete(action, state, ok=True))
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
        status = res.status or ""
        error = _error_text(res.error)
        interrupted = status == "ERROR" and error == "interrupted"
        out.extend(
            self._settle_open_actions(
                state,
                ok=status == "SUCCESS",
                message="interrupted" if interrupted else error,
                denied=res.denied_actions,
            )
        )
        answer = self._answer(state, res.response)
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
            out.append(
                factory.completed_error(
                    error=error
                    or f"antigravity ended with status {status or 'unknown'}",
                    answer=answer,
                    resume=resume_token,
                    usage=usage,
                )
            )
        return out

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
        parts = [f"antigravity failed ({_rc_label(rc)})."]
        session = _session_label(found_session, resume)
        if session:
            parts.append(f"session: {session}")
        excerpt = _stderr_excerpt(stderr_lines)
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

    def stream_end_events(
        self,
        *,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
        state: AntigravityStreamState,
        stderr_lines: list[str] | None = None,
    ) -> list[UntetherEvent]:
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
        excerpt = _stderr_excerpt(stderr_lines)
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


def _denied_action_ids(
    parked: dict[str, Action], denied: list[agy_schema.DeniedAction]
) -> set[str]:
    """Which parked rows ``result.denied_actions`` covers. Known actions match
    their tool family; an unknown one falls back to the last parked row (agy
    1.3.1 ends the turn at the first denial)."""
    if not denied or not parked:
        return set()
    ids: set[str] = set()
    unmatched = False
    for item in denied:
        tools = _DENIED_ACTION_TOOLS.get(item.action or "")
        hits = (
            [a for a, act in parked.items() if act.detail.get("tool_name") in tools]
            if tools
            else []
        )
        if hits:
            ids.update(hits)
        else:
            unmatched = True
    if unmatched:
        ids.add(max(parked, key=_step_of))
    return ids


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    """Build an ``AntigravityRunner`` from ``[antigravity]`` config.

    rc1 keys: ``model`` (str), ``cmd`` (str, ``~`` expanded). Unknown keys —
    including PR #766's ``dangerously_skip_permissions`` / ``antigravity_cmd``
    — are ignored; permissions come from phase 02's explicit modes.
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
    cmd = os.path.expanduser(raw_cmd) if raw_cmd else default_antigravity_cmd()
    return AntigravityRunner(antigravity_cmd=cmd, model=model or None)


BACKEND = EngineBackend(
    id="antigravity",
    build_runner=build_runner,
    cli_cmd="agy",
    install_cmd="curl -fsSL https://antigravity.google/cli/install.sh | bash",
)
