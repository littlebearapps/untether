"""The per-run Unix socket Untether's agy gate hook talks to (#980, #981).

One :class:`GateRun` per agy run whose gate is ready. It is bound before agy
is spawned (agy's env names its path), served for the whole run from the
runner's event loop and closed in the runner's ``finally``. Only agy runs
Untether starts carry the marker env, so only their hooks connect.

What it answers:

- ``PreToolUse`` for ``ask_question`` → the existing question card (option
  buttons + typed answer, :mod:`untether.interactive`); the hook is held
  until the user answers, the request's deadline passes, or the hook goes
  away. The answer reaches the model as a ``deny`` whose reason carries it
  (E3: agy shows the reason to the model and the turn carries on).
- ``PreToolUse`` for any other tool: in Workspace, Plan first and Full
  access **no decision** (empty reply) — agy's own permission check decides,
  exactly as without the gate. In **Ask me** (``required``: agy runs with
  ``--dangerously-skip-permissions``, phase 14) the gate is the only check:
  :mod:`gate_policy`'s safe list (reads, in-project edits) gets ``allow``,
  everything else an approval card (Approve / Deny, and "Always allow this
  command" for a shell command) and the user's answer; a deny, a timeout or
  an unattended run blocks the tool.
- ``PreInvocation`` → the steer queue, drained into ``injectSteps`` (#981).
- ``Stop`` → whether agy is still waiting for background work (``fullyIdle``).

Security (D22): the socket lives in a 0700 directory of this Untether
instance (``<inst8>`` = the config path's hash, so instances sharing a
``HOME`` never share a directory), is 0600, and is unlinked when the run
ends. Each connection must present the run's random token (constant-time
compare) and, on Linux, come from this uid (``SO_PEERCRED``) and (security
review 3 L1) from a process below the agy pid this run spawned whose command
line is exactly the plugin's hook for the event it sends — an Ask me run
refuses every connection until that pid is known. The token is never
logged. A tool process agy runs inherits agy's env (agy 1.3.4 has no way to
keep variables from its tools) and is a child of agy too, so a command the
user already allowed could still exec the hook's command line itself: it can
only *ask* (a spoofed card, a drained steer, a pre-claimed step), never
grant itself anything — documented in ``security.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import struct
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketAttribute

from ..events import EventFactory
from ..interactive import (
    AskQuestionState,
    Decision,
    approval_keyboard,
    expire,
    format_question_message,
    get_question_option_buttons,
    register,
    unattended_deny_message,
    withdraw,
)
from ..interactive.questions import (
    _ASK_QUESTION_FLOWS,
    _PENDING_ASK_REQUESTS,
    _record_answered_ask_flow,
)
from ..logging import get_logger
from ..model import UntetherEvent
from ..utils.paths import get_run_channel_id
from . import gate_hook, gate_plugin, gate_policy

logger = get_logger(__name__)

ENGINE = "antigravity"
CALLBACK_PREFIX = "ia"

MAX_REQUEST_BYTES = 2 * gate_hook.MAX_PAYLOAD_BYTES
READ_TIMEOUT_S = 5.0
STALE_SOCKET_S = 60.0
# A question's options as Telegram shows them (as for Claude, claude.py).
MAX_OPTIONS = 4
MAX_OPTION_CHARS = 60
MAX_QUESTIONS = 8
# Spoofed-card brake (13 §5): at most this many question cards per minute.
MAX_CARDS_PER_MINUTE = 20
# A tap writing its answer when the deadline passes gets this long to land.
_LATE_TAP_GRACE_S = 5.0
# How long a request from a not-yet-known conversation waits for ``init``.
_CONVERSATION_WAIT_S = 3.0
# 13 §7 (P-H6 measured ≈ 65-75 ms per round trip): a tool step's PreToolUse
# request must arrive within this long of its ACTIVE line.
TRIPWIRE_GRACE_S = 3.0
_TRIPWIRE_POLL_S = 0.5

STEER_PREFIX = "Message from the user, sent via Telegram while you were working: "
ASK_OFF_TEXT = (
    "Questions are turned off in this chat. Proceed with reasonable defaults "
    "and state your assumptions."
)
TIMEOUT_TEXT = (
    "No answer from the user in Telegram; use your best judgement and say "
    "what you assumed."
)
NO_ANSWER_TEXT = (
    "The user closed the question in Telegram without an answer; use your "
    "best judgement and say what you assumed."
)
TOO_MANY_TEXT = (
    "Too many questions in a short time; carry on with reasonable defaults "
    "and state your assumptions."
)
DUPLICATE_TEXT = "Untether saw this tool call twice, so it was blocked."
# Phase 14 (Ask me): what agy shows the model when a tool is blocked.
AMBIGUOUS_TEXT = (
    "Untether's gate can't tell how agy would read this tool call ({why}), so "
    "it was blocked. Call the tool again with each argument once, spelled "
    "exactly as the tool defines it."
)
DENY_TEXT = (
    "The user denied this in Telegram (Untether). They can't see your tool "
    "calls — explain in your reply what you were trying to do and ask how to "
    "proceed. Don't retry the same call."
)
TOO_MANY_APPROVALS_TEXT = (
    "Too many approval requests in a short time, so this one was blocked. "
    "Don't retry it; say in your reply what you needed it for."
)
ALWAYS_LABEL = "✅ Always allow this command here"
UNREADABLE_QUESTION_TEXT = (
    "Untether couldn't read this question, so it wasn't shown in Telegram. "
    "Ask again with a question text and up to four short options, or carry "
    "on with reasonable defaults and state your assumptions."
)
_SEND_MESSAGE_KEYS = frozenset({"Recipient", "Message"})


def timeout_text(wait_s: int) -> str:
    return (
        f"No answer in Telegram within {_minutes(wait_s)} — the tool was "
        "blocked. Don't retry it; say what you needed it for."
    )


def _minutes(wait_s: int) -> str:
    if wait_s < 120:
        return f"{wait_s} s"
    return f"{round(wait_s / 60)} min"


def unattended_hint(trigger: str) -> str:
    """The 🔒 footer's remedy for an unattended Ask me run (#835)."""
    if trigger.startswith("webhook:"):
        return (
            "Ask me needs a person to approve these \N{EM DASH} run it from "
            "the chat instead"
        )
    return (
        "Ask me needs a person to approve these \N{EM DASH} run it from the "
        'chat, or set permission_mode = "full" on the cron to allow them'
    )


def _command_sha(args: Any) -> str:
    """Logs name a granted command by hash only, never its text."""
    command = args.get("CommandLine") if isinstance(args, dict) else None
    text = command.strip() if isinstance(command, str) else ""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


ANSWER_TAIL = (
    "Use this answer and continue. Do not call ask_question again for this question."
)


class GateSocketError(Exception):
    """The socket directory isn't private (or can't be made)."""


# ── socket directory ────────────────────────────────────────────────────────


def instance_id(config_path: Path | None) -> str:
    """8 hex of the instance's config path: one socket folder per instance."""
    material = str(config_path.resolve()) if config_path is not None else "-"
    return hashlib.sha256(material.encode()).hexdigest()[:8]


def socket_base() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isabs(runtime) and os.path.isdir(runtime):
        return Path(runtime) / "untether"
    return Path(tempfile.gettempdir()) / f"untether-{os.getuid()}"


def _private_dir(path: Path) -> None:
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise GateSocketError(f"{path} is not a folder")
    if st.st_uid != os.getuid():
        raise GateSocketError(f"{path} belongs to another user")
    if st.st_mode & 0o077:
        raise GateSocketError(f"{path} is open to other users")


def socket_dir(config_path: Path | None) -> Path:
    """Create (0700) and verify this instance's socket folder."""
    base = socket_base()
    folder = base / instance_id(config_path)
    _private_dir(base)
    _private_dir(folder)
    return folder


_SWEPT: set[str] = set()


def sweep_stale_sockets(folder: Path, *, now: float | None = None) -> int:
    """Remove ``agy-*.sock`` left by a crashed run of *this* instance: older
    than a minute and with no listener (connect refused). Never touches a
    young socket (another run may be between bind and listen)."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if not (entry.name.startswith("agy-") and entry.name.endswith(".sock")):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISSOCK(st.st_mode) or now - st.st_mtime < STALE_SOCKET_S:
            continue
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(entry.path)
        except (ConnectionRefusedError, FileNotFoundError):
            with contextlib.suppress(OSError):
                os.unlink(entry.path)
                removed += 1
        except OSError:
            pass
        finally:
            probe.close()
    if removed:
        logger.info("antigravity.gate.stale_sockets_removed", count=removed)
    return removed


# ── held requests and steers ────────────────────────────────────────────────


@dataclass(slots=True)
class _Held:
    request_id: str
    action_id: str
    decided: anyio.Event = field(default_factory=anyio.Event)
    decision: Decision | None = None
    gone: bool = False  # the hook's connection closed first


@dataclass(slots=True)
class SteerItem:
    text: str
    command_uuid: str
    queued_at: float
    invocation: int | None = None  # set when injected


def _option_text(opt: Any) -> str | None:
    if isinstance(opt, str):
        text = opt
    elif isinstance(opt, dict) and isinstance(opt.get("label"), str):
        text = opt["label"]
    else:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > MAX_OPTION_CHARS:
        text = text[: MAX_OPTION_CHARS - 1] + "…"
    return text


def normalise_questions(args: Any) -> list[dict[str, Any]]:
    """agy's ``ask_question`` args → the registry's flow shape:
    ``{"question", "options": [{"label"}], "multiSelect"}``. Options are
    plain strings in agy (E3); at most four are kept, each ≤ 60 chars."""
    raw = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(raw, list):
        single = args.get("question") if isinstance(args, dict) else None
        raw = [{"question": single}] if isinstance(single, str) else []
    out: list[dict[str, Any]] = []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        options = item.get("options")
        labels = (
            [
                text
                for text in (_option_text(o) for o in (options or [])[:MAX_OPTIONS])
                if text is not None
            ]
            if isinstance(options, list)
            else []
        )
        out.append(
            {
                "question": question.strip(),
                "options": [{"label": text} for text in labels],
                "multiSelect": item.get("is_multi_select") is True,
            }
        )
    return out


def answer_reason(decision: Decision) -> str:
    """The text agy shows the model (E3: "You chose blue.")."""
    answers = decision.answers or {}
    if decision.answer is not None:
        return f'The user answered via Telegram: "{decision.answer}". {ANSWER_TAIL}'
    if len(answers) == 1:
        (only,) = answers.values()
        return f'The user answered via Telegram: "{only}". {ANSWER_TAIL}'
    if answers:
        lines = [
            f'{n}. {question} → "{answer}"'
            for n, (question, answer) in enumerate(answers.items(), start=1)
        ]
        return (
            "The user answered via Telegram:\n" + "\n".join(lines) + f"\n{ANSWER_TAIL}"
        )
    return NO_ANSWER_TEXT


def _answer_title(decision: Decision) -> str:
    if decision.answer is not None:
        shown = decision.answer
    elif decision.answers:
        shown = "; ".join(decision.answers.values())
    else:
        return "❓ question closed without an answer"
    shown = " ".join(shown.split())
    if len(shown) > 80:
        shown = shown[:79] + "…"
    return f"❓ answered: {shown}"


# ── the run ─────────────────────────────────────────────────────────────────

_RUNS: dict[str, GateRun] = {}
_BY_CONVERSATION: dict[str, GateRun] = {}


def _peer_creds(stream: Any) -> tuple[int, int] | None:
    """``(pid, uid)`` of the connecting process (``SO_PEERCRED``), or None
    when they can't be read."""
    raw = stream.extra(SocketAttribute.raw_socket, None)  # nosec B610 — anyio, not Django
    if raw is None:
        return None
    try:
        creds = raw.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, _gid = struct.unpack("3i", creds)
    except (OSError, struct.error):
        return None
    return pid, uid


def _peer_ok(stream: Any) -> bool:
    """The connecting process runs as this uid. Where the platform has
    ``SO_PEERCRED`` (Linux) a peer whose credentials can't be read is
    refused — fail closed; elsewhere (macOS) the 0700 folder and the token
    are the check."""
    if not hasattr(socket, "SO_PEERCRED"):
        return True
    creds = _peer_creds(stream)
    return creds is not None and creds[1] == os.getuid()


_HOOK_MODULE = "untether.antigravity.gate_hook"
_MAX_ANCESTRY = 64


def _parent_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat_line = handle.read(4096)
    except OSError:
        return None
    # ``pid (comm) state ppid …``; comm may hold spaces and parentheses.
    try:
        return int(stat_line[stat_line.rindex(b")") + 2 :].split()[1])
    except (ValueError, IndexError):
        return None


def _descends_from(pid: int, ancestor: int) -> bool:
    current = pid
    for _ in range(_MAX_ANCESTRY):
        parent = _parent_pid(current)
        if parent is None or parent <= 1:
            return False
        if parent == ancestor:
            return True
        current = parent
    return False


def peer_hook_problem(pid: int, agy_pid: int, event: str) -> str | None:
    """Security review 3 L1 (Linux): the peer must be a process of this
    run's agy (a descendant of the pid Untether spawned) running exactly the
    plugin's hook command — ``<this interpreter> -I -m
    untether.antigravity.gate_hook <event>`` — for the event it sends. Why
    not, or None. Best effort: an approved shell command is a descendant
    of agy too and could exec the same command line (``security.md``)."""
    if pid == agy_pid or not _descends_from(pid, agy_pid):
        return "lineage"
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read(65536).split(b"\0")
        exe = os.path.realpath(f"/proc/{pid}/exe")
    except OSError:
        return "unreadable"
    expected = [sys.executable, "-I", "-m", _HOOK_MODULE, event]
    if [part.decode("utf-8", "replace") for part in argv if part] != expected:
        return "command"
    if exe != os.path.realpath(sys.executable):
        return "interpreter"
    return None


class GateRun:
    """One agy run's gate: socket, token, held questions, steer queue."""

    def __init__(
        self,
        *,
        config_path: Path | None,
        factory: EventFactory,
        wait_s: int = gate_plugin.DEFAULT_WAIT_S,
        unattended_trigger: str | None = None,
        ask_questions: bool | None = None,
        required: bool = False,
        project_root: Path | None = None,
        diff_preview: bool | None = None,
        channel_id: int | None = None,
        outbox_dir: str = gate_policy.DEFAULT_OUTBOX_DIR,
        outside: tuple[Path, ...] = (),
    ) -> None:
        self.key = secrets.token_hex(6)
        self.session_key = f"agy-gate:{self.key}"
        self._token = secrets.token_hex(32)
        self.config_path = config_path
        self.factory = factory
        self.wait_s = int(wait_s)
        self.unattended_trigger = unattended_trigger
        self.ask_questions = ask_questions
        # Phase 14 (Ask me): agy runs with the bypass flag and this gate is
        # the only check. Liveness and tripwire failures stop the run only
        # then, and only then does the approval policy answer tools.
        self.required = required
        self.project_root = project_root
        self.diff_preview = diff_preview
        self.channel_id = channel_id
        # Security review 3: the instance's ``files.outbox_dir`` (protected)
        # and the projects' ``@branch`` worktree roots (outside the project).
        self.outbox_dir = outbox_dir
        self.outside = outside
        # The agy process this run spawned (L1: hook connections must come
        # from below it); set by the runner at spawn.
        self.agy_pid: int | None = None
        # Unattended Ask me (trigger-level ``ask``): what the gate denied,
        # for the final's 🔒 footer (#835 parity).
        self.unattended_denials: list[str] = []
        self.sock_path: Path | None = None
        self._listener: Any = None
        self._send: Any = None
        self.conversation_id: str | None = None
        self._conversation_known = anyio.Event()
        self.children: set[str] = set()
        # P-H8: a child's tool calls reach the gate with its own id; the
        # card names the subagent's role.
        self.child_roles: dict[str, str] = {}
        self._held: dict[str, _Held] = {}
        self.steer_queue: deque[SteerItem] = deque()
        self.injected: deque[SteerItem] = deque()
        self.steer_open = True
        self.closed = False
        self.dead = False  # hooks don't run for this run (liveness)
        self.preinvocation_seen = False
        self.stop_not_idle: bool | None = None
        self._tool_requests: set[tuple[str, int]] = set()
        self._tool_steps: dict[int, tuple[float, str]] = {}
        self._card_times: deque[float] = deque()
        self._tripwire_logged: set[int] = set()
        self.on_fatal: Callable[[str], None] | None = None
        # #1080: (parent step, waiting) while an approval card is up, so the
        # runner doesn't list a step agy hasn't run yet as background work.
        self.on_approval_wait: Callable[[int, bool], None] | None = None

    # -- lifecycle -----------------------------------------------------------

    async def open(self) -> None:
        folder = socket_dir(self.config_path)
        if str(folder) not in _SWEPT:
            _SWEPT.add(str(folder))
            sweep_stale_sockets(folder)
        path = folder / f"agy-{self.key}.sock"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._listener = await anyio.create_unix_listener(path, mode=0o600)
        self.sock_path = path
        _RUNS[self.session_key] = self
        logger.info(
            "antigravity.gate.listening",
            run=self.key,
            wait_s=self.wait_s,
            required=self.required,
            token_len=len(self._token),
        )

    def env(self) -> dict[str, str]:
        """The marker env for the agy process this run spawns (only)."""
        assert self.sock_path is not None
        return {
            gate_hook.ENV_MARKER: gate_hook.PROTOCOL,
            gate_hook.ENV_SOCK: str(self.sock_path),
            gate_hook.ENV_TOKEN: self._token,
            gate_hook.ENV_PY: sys.executable,
            gate_hook.ENV_DEADLINE: str(self.wait_s),
        }

    async def serve(self, send_stream: Any) -> None:
        """Accept hook connections until cancelled; events go to
        *send_stream* (the run's merged event stream)."""
        self._send = send_stream
        listener = self._listener
        if listener is None:
            return
        async with anyio.create_task_group() as tg:
            while True:
                try:
                    conn = await listener.accept()
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    return
                tg.start_soon(self._handle, conn)

    def stop_emitting(self) -> None:
        send, self._send = self._send, None
        if send is not None:
            send.close()

    async def aclose(self) -> None:
        """Run end (any path): stop steering, retire held questions, close
        and unlink the socket. Idempotent."""
        if self.closed:
            return
        self.closed = True
        self.steer_open = False
        for held in list(self._held.values()):
            withdraw(held.request_id)
            held.gone = True
            held.decided.set()
        self._held.clear()
        if self._listener is not None:
            with contextlib.suppress(Exception):
                await self._listener.aclose()
        if self.sock_path is not None:
            with contextlib.suppress(OSError):
                self.sock_path.unlink()
        _RUNS.pop(self.session_key, None)
        if (
            self.conversation_id is not None
            and _BY_CONVERSATION.get(self.conversation_id) is self
        ):
            del _BY_CONVERSATION[self.conversation_id]
        self.stop_emitting()
        logger.info(
            "antigravity.gate.closed",
            run=self.key,
            steers_undelivered=len(self.steer_queue) + len(self.injected),
        )

    # -- runner hooks ----------------------------------------------------------

    def set_conversation(self, conversation_id: str) -> None:
        if self.conversation_id is not None:
            return
        self.conversation_id = conversation_id
        _BY_CONVERSATION[conversation_id] = self
        self._conversation_known.set()

    def add_children(self, ids: list[str], role: str | None = None) -> None:
        for child in ids:
            if not child:
                continue
            self.children.add(child)
            if role:
                self.child_roles[child] = role

    def note_result(self) -> None:
        """agy's result arrived: nothing more can be injected."""
        self.steer_open = False

    def note_tool_step(self, step: int, tool: str) -> None:
        """A parent-stream tool step went ACTIVE (13 §7 tripwire)."""
        if (self.conversation_id or "", step) in self._tool_requests:
            return
        self._tool_steps.setdefault(step, (time.monotonic(), tool))

    def take_delivered(self) -> SteerItem | None:
        """A ``user_input`` step after an injection: the oldest injected
        item reached the conversation (P-H10: one step per message)."""
        return self.injected.popleft() if self.injected else None

    def mark_dead(self) -> None:
        if self.dead:
            return
        self.dead = True
        self.steer_open = False

    def overdue_tool_steps(self, *, now: float | None = None) -> list[tuple[int, str]]:
        now = time.monotonic() if now is None else now
        return [
            (step, tool)
            for step, (at, tool) in self._tool_steps.items()
            if now - at >= TRIPWIRE_GRACE_S
        ]

    async def watch(self) -> None:
        """Poll the tripwire while the run lasts."""
        while not self.closed:
            await anyio.sleep(_TRIPWIRE_POLL_S)
            for step, tool in self.overdue_tool_steps():
                self._tool_steps.pop(step, None)
                if step in self._tripwire_logged:
                    continue
                self._tripwire_logged.add(step)
                if self.required:
                    logger.error(
                        "antigravity.gate.tripwire", run=self.key, step=step, tool=tool
                    )
                    if self.on_fatal is not None:
                        self.on_fatal("gate_tripwire")
                else:
                    logger.info(
                        "antigravity.gate.tripwire",
                        run=self.key,
                        step=step,
                        tool=tool,
                        enforced=False,
                    )

    # -- steer (#981) ----------------------------------------------------------

    def queue_steer(self, text: str, command_uuid: str) -> bool:
        if not self.steer_open or self.closed or self.dead:
            return False
        self.steer_queue.append(
            SteerItem(text=text, command_uuid=command_uuid, queued_at=time.monotonic())
        )
        logger.info(
            "antigravity.steer.queued",
            run=self.key,
            session_id=self.conversation_id,
            command_uuid=command_uuid,
            text_len=len(text),
        )
        return True

    @property
    def closing(self) -> bool:
        """``LiveSessionView``: no more steers go in."""
        return not self.steer_open or self.closed or self.dead

    # -- responder -------------------------------------------------------------

    def resolve(self, request_id: str, decision: Decision) -> bool:
        held = self._held.get(request_id)
        if held is None or held.decided.is_set():
            return False
        held.decision = decision
        held.decided.set()
        return True

    # -- connections -----------------------------------------------------------

    async def _emit(self, *events: UntetherEvent) -> None:
        send = self._send
        if send is None:
            return
        for evt in events:
            with contextlib.suppress(
                anyio.ClosedResourceError, anyio.BrokenResourceError
            ):
                await send.send(evt)

    async def _read_line(self, stream: Any) -> bytes | None:
        buf = bytearray()
        with anyio.move_on_after(READ_TIMEOUT_S):
            while True:
                try:
                    chunk = await stream.receive(65536)
                except (anyio.EndOfStream, anyio.BrokenResourceError):
                    return None
                buf.extend(chunk)
                newline = buf.find(b"\n")
                if newline >= 0:
                    return bytes(buf[:newline])
                if len(buf) > MAX_REQUEST_BYTES:
                    return None
        return None

    async def _handle(self, stream: Any) -> None:
        """One hook call. A failure here closes the connection without a
        reply, which the hook turns into a ``deny`` for a tool (fail
        closed) — it never takes the run down."""
        try:
            await self._serve_one(stream)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "antigravity.gate.handler_failed",
                run=self.key,
                error_type=exc.__class__.__name__,
            )

    def _lineage_problem(self, stream: Any, event: str, request: dict) -> str | None:
        """L1: on Linux, the peer is this run's agy hook (see
        :func:`peer_hook_problem`). An Ask me run whose agy pid isn't known
        yet refuses (fail closed); elsewhere the check needs the pid."""
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        if self.agy_pid is None:
            return "no_agy_pid" if self.required else None
        creds = _peer_creds(stream)
        if creds is None:
            return "unreadable"
        pid = creds[0]
        if request.get("pid") != pid:
            return "pid"
        return peer_hook_problem(pid, self.agy_pid, event)

    async def _serve_one(self, stream: Any) -> None:
        async with stream:
            if not _peer_ok(stream):
                logger.warning("antigravity.gate.peer_refused", run=self.key)
                return
            raw = await self._read_line(stream)
            if raw is None:
                logger.warning("antigravity.gate.bad_request", run=self.key, why="read")
                return
            try:
                request = json.loads(raw)
            except ValueError:
                request = None
            if not isinstance(request, dict) or request.get("v") != int(
                gate_hook.PROTOCOL
            ):
                logger.warning(
                    "antigravity.gate.bad_request", run=self.key, why="shape"
                )
                return
            token = request.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(
                token.encode(), self._token.encode()
            ):
                logger.warning("antigravity.gate.token_refused", run=self.key)
                return
            event = request.get("event")
            payload = request.get("payload")
            if event not in gate_hook.EVENTS or not isinstance(payload, dict):
                logger.warning(
                    "antigravity.gate.bad_request", run=self.key, why="event"
                )
                return
            problem = _LINEAGE_CHECK(self, stream, event, request)
            if problem is not None:
                logger.warning(
                    "antigravity.gate.peer_refused", run=self.key, why=problem
                )
                return
            if event == "PreInvocation":
                out: dict[str, Any] | None = self._on_pre_invocation(payload)
            elif event == "Stop":
                out = self._on_stop(payload)
            else:
                out = await self._on_pre_tool_use(payload, stream)
            if out is None:
                return
            with contextlib.suppress(
                anyio.BrokenResourceError, anyio.ClosedResourceError, OSError
            ):
                await stream.send((json.dumps({"v": 1, "out": out}) + "\n").encode())

    def _own(self, conversation_id: Any) -> bool:
        return isinstance(conversation_id, str) and (
            conversation_id == self.conversation_id or conversation_id in self.children
        )

    def _on_pre_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.preinvocation_seen = True
        cid = payload.get("conversationId")
        invocation = payload.get("invocationNum")
        if (
            not self.steer_queue
            or not self.steer_open
            or cid is None
            or cid != self.conversation_id
        ):
            return {}
        items: list[SteerItem] = []
        while self.steer_queue and len(items) < gate_hook.MAX_INJECT_STEPS:
            item = self.steer_queue.popleft()
            item.invocation = invocation if isinstance(invocation, int) else None
            items.append(item)
        self.injected.extend(items)
        logger.info(
            "antigravity.steer.injected",
            run=self.key,
            session_id=self.conversation_id,
            count=len(items),
            invocation=item.invocation,
            command_uuids=[i.command_uuid for i in items],
        )
        return {
            "injectSteps": [
                {"userMessage": (STEER_PREFIX + i.text)[: gate_hook.MAX_INJECT_CHARS]}
                for i in items
            ]
        }

    def _on_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("conversationId") == self.conversation_id:
            fully_idle = payload.get("fullyIdle")
            if isinstance(fully_idle, bool):
                self.stop_not_idle = not fully_idle
            logger.debug(
                "antigravity.gate.stop",
                run=self.key,
                fully_idle=fully_idle,
                reason=str(payload.get("terminationReason"))[:40],
            )
        return {}

    async def _on_pre_tool_use(
        self, payload: dict[str, Any], stream: Any
    ) -> dict[str, Any] | None:
        call = payload.get("toolCall")
        call = call if isinstance(call, dict) else {}
        tool = call.get("name") if isinstance(call.get("name"), str) else ""
        step = payload.get("stepIdx")
        cid = payload.get("conversationId")
        if self.conversation_id is None:
            with anyio.move_on_after(_CONVERSATION_WAIT_S):
                await self._conversation_known.wait()
        own = self._own(cid)
        logger.info(
            "antigravity.gate.request",
            run=self.key,
            hook_event="PreToolUse",
            tool=tool[:60],
            step=step if isinstance(step, int) else None,
            child=own and cid != self.conversation_id,
            foreign=not own,
        )
        if isinstance(step, int) and isinstance(cid, str):
            key = (cid, step)
            if key in self._tool_requests:
                logger.error(
                    "antigravity.gate.duplicate_request",
                    run=self.key,
                    step=step,
                    tool=tool[:60],
                )
                return {"decision": "deny", "reason": DUPLICATE_TEXT}
            self._tool_requests.add(key)
            if cid == self.conversation_id:
                self._tool_steps.pop(step, None)
        if tool == "ask_question" and own:
            return await self._ask(call.get("args"), step, stream)
        if not self.required:
            # No policy on other tools — agy's own check decides (Workspace /
            # Plan first: agy's permission check; Full access: the user
            # approved every tool).
            return {}
        # Ask me: agy runs with the bypass flag, so an empty reply would let
        # the tool run. Every tool gets an explicit answer (D22.3).
        return await self._policy(
            tool, call.get("args"), payload, step, cid, own, stream
        )

    async def _policy(
        self,
        tool: str,
        args: Any,
        payload: dict[str, Any],
        step: Any,
        cid: Any,
        own: bool,
        stream: Any,
    ) -> dict[str, Any] | None:
        try:
            return await self._decide(tool, args, payload, step, cid, own, stream)
        except Exception as exc:  # noqa: BLE001 — Ask me fails closed
            logger.error(
                "antigravity.gate.policy_failed",
                run=self.key,
                tool=tool[:60],
                error_type=exc.__class__.__name__,
            )
            return {"decision": "deny", "reason": gate_hook.BAD_PAYLOAD}

    async def _decide(
        self,
        tool: str,
        raw_args: Any,
        payload: dict[str, Any],
        step: Any,
        cid: Any,
        own: bool,
        stream: Any,
    ) -> dict[str, Any] | None:
        # Read the arguments the way agy (a Go program) does; arguments that
        # could mean two things are refused before anything else.
        args, ambiguous = gate_policy.normalise_args(raw_args)
        if args is None:
            self._decision_log("deny", "policy", step, tool=tool, source="ambiguous")
            logger.warning(
                "antigravity.gate.ambiguous_args", run=self.key, tool=tool[:60]
            )
            return {
                "decision": "deny",
                "reason": AMBIGUOUS_TEXT.format(why=ambiguous or "unreadable"),
            }
        verdict = (
            gate_policy.classify(
                tool,
                args,
                root=self.project_root,
                conversation_id=cid if isinstance(cid, str) else None,
                workspace_paths=payload.get("workspacePaths"),
                diff_preview=self.diff_preview,
                outbox_dir=self.outbox_dir,
                outside=self.outside,
                config_path=self.config_path,
                artifact_path=payload.get("artifactDirectoryPath"),
            )
            if own
            # A conversation Untether didn't start (13 §5): never on the
            # safe list.
            else gate_policy.Verdict(False, "foreign")
        )
        if verdict.allow:
            self._decision_log("allow", "policy", step, tool=tool, source="policy")
            return {"decision": "allow"}
        if own and tool == "send_message" and self._internal_message(args):
            # A subagent reporting to its parent (or the parent writing to
            # its own child): agent-internal, nothing runs.
            self._decision_log("allow", "policy", step, tool=tool, source="internal")
            return {"decision": "allow"}
        label = gate_policy.tool_label(tool, args)
        if self.unattended_trigger is not None:
            return await self._unattended_deny(tool, label, step)
        if (
            tool == "run_command"
            and own
            and gate_policy.granted(self.channel_id, self.conversation_id, args)
        ):
            self._decision_log("allow", "approval", step, tool=tool, source="always")
            return {"decision": "allow"}
        return await self._approve(tool, args, verdict, step, cid, own, stream)

    def _internal_message(self, args: Any) -> bool:
        """``send_message`` to this run's own conversation or a known
        subagent, carrying only agy's own fields (security review 3 L5:
        ``Recipient`` and ``Message``, agy 1.3.4's schema)."""
        if not isinstance(args, dict):
            return False
        if not set(args) <= _SEND_MESSAGE_KEYS | gate_policy._META_KEYS:
            return False
        recipient = args.get("Recipient")
        message = args.get("Message")
        return (
            isinstance(recipient, str)
            and (message is None or isinstance(message, str))
            and (recipient == self.conversation_id or recipient in self.children)
        )

    async def _unattended_deny(
        self, tool: str, label: str, step: Any
    ) -> dict[str, Any]:
        """#835 parity: a cron / webhook run has nobody to tap Approve, so
        the tool is denied at once and counted for the 🔒 footer."""
        trigger = self.unattended_trigger or "?"
        self.unattended_denials.append(label)
        self._decision_log("deny", "approval", step, tool=tool, source="unattended")
        logger.warning(
            "permission.unattended_deny",
            engine=ENGINE,
            tool_name=tool[:60],
            trigger_source=trigger,
            session_id=self.conversation_id,
            permission_mode="ask",
            reason="approval",
            turn_denials=len(self.unattended_denials),
        )
        if len(self.unattended_denials) == 1:
            title = f"\N{LOCK} Unattended run — denied {label}: nobody to approve it"
            action_id = f"antigravity.unattended_deny.{self.key}"
            detail = {"trigger": trigger, "tool_name": tool, "reason": "approval"}
            await self._emit(
                self.factory.action_started(
                    action_id=action_id, kind="note", title=title, detail=detail
                ),
                self.factory.action_completed(
                    action_id=action_id,
                    kind="note",
                    title=title,
                    ok=True,
                    level="warning",
                    detail=detail,
                ),
            )
        return {
            "decision": "deny",
            "reason": unattended_deny_message(trigger, label, "approval"),
        }

    def unattended_usage(self) -> dict[str, Any] | None:
        """``usage["unattended"]`` for the final's 🔒 footer, or None."""
        if self.unattended_trigger is None or not self.unattended_denials:
            return None
        denied: dict[str, int] = {}
        for label in self.unattended_denials:
            denied[label] = denied.get(label, 0) + 1
        return {
            "trigger": self.unattended_trigger,
            "mode": "ask",
            "denied": denied,
            "hint": unattended_hint(self.unattended_trigger),
        }

    async def _approve(
        self,
        tool: str,
        args: Any,
        verdict: gate_policy.Verdict,
        step: Any,
        cid: Any,
        own: bool,
        stream: Any,
    ) -> dict[str, Any] | None:
        """The approval card (phase 14 §4): held until Approve / Deny, the
        deadline, or the hook going away."""
        if not self._cards_left():
            self._decision_log("deny", "approval", step, tool=tool, source="rate")
            return {"decision": "deny", "reason": TOO_MANY_APPROVALS_TEXT}
        request_id = f"agy_{secrets.token_hex(6)}"
        action_id = f"antigravity.approval.{request_id}"
        tool_input = args if isinstance(args, dict) else {}
        child_role = (
            self.child_roles.get(cid) or "subagent"
            if own and isinstance(cid, str) and cid != self.conversation_id
            else None
        )
        # Everything that reads the model's arguments runs before the
        # request is registered: a failure here leaves nothing pending, and
        # the hook's closed connection is a deny.
        title = gate_policy.card_title(
            tool,
            args,
            verdict,
            root=self.project_root,
            child_role=child_role,
            foreign=not own,
        )
        offer_always = (
            tool == "run_command" and own and gate_policy.always_allowable(args)
        )
        register(
            request_id,
            session_key=self.session_key,
            responder=RESPONDER,
            tool_name=tool,
            tool_input=tool_input,
            kind="tool",
        )
        buttons = approval_keyboard(request_id)
        if offer_always:
            buttons.append(
                [
                    {
                        "text": ALWAYS_LABEL,
                        "callback_data": f"{CALLBACK_PREFIX}:always:{request_id}",
                    }
                ]
            )
        held = _Held(request_id=request_id, action_id=action_id)
        self._held[request_id] = held
        label = gate_policy.tool_label(tool, args)
        await self._emit(
            self.factory.action_started(
                action_id=action_id,
                kind="warning",
                title=title,
                detail={
                    "request_id": request_id,
                    "request_type": "PreToolUse",
                    "tool_name": tool,
                    "inline_keyboard": {"buttons": buttons},
                    "line_breaks": True,  # #1080: one card line per line
                },
            )
        )
        logger.info(
            "antigravity.gate.card",
            run=self.key,
            request_id=request_id,
            tool=tool[:60],
            why=verdict.why,
            child=child_role is not None,
            foreign=not own,
        )
        parent_step = (
            step
            if own and cid == self.conversation_id and isinstance(step, int)
            else None
        )
        self._note_approval_wait(parent_step, True)
        settled = False
        try:
            await self._wait(held, stream)
            if held.decision is None and not held.gone:
                # Deadline. A tap writing its answer right now wins.
                if expire(request_id, action="gate_timeout"):
                    settled = True
                else:
                    with anyio.move_on_after(_LATE_TAP_GRACE_S):
                        await held.decided.wait()
            settled = True
            decision = held.decision
            if decision is not None and decision.approved:
                source = "user"
                if (
                    decision.action == "always"
                    and offer_always
                    and gate_policy.grant(
                        self.channel_id, self.conversation_id or "", args
                    )
                ):
                    source = "always"
                    logger.info(
                        "antigravity.gate.always_granted",
                        run=self.key,
                        command_sha=_command_sha(args),
                    )
                self._decision_log("allow", "approval", step, tool=tool, source=source)
                done = "✅ Always allowed" if source == "always" else "✅ Approved"
                await self._card_done(
                    action_id, f"{done}: {label}", ok=True, request_id=request_id
                )
                return {"decision": "allow"}
            if decision is not None:
                self._decision_log("deny", "approval", step, tool=tool, source="user")
                await self._card_done(
                    action_id, f"❌ Denied: {label}", ok=False, request_id=request_id
                )
                return {"decision": "deny", "reason": DENY_TEXT}
            if held.gone:
                withdraw(request_id)
                logger.info(
                    "antigravity.gate.withdrawn", run=self.key, request_id=request_id
                )
                await self._card_done(
                    action_id,
                    f"⏹️ Withdrawn — agy stopped waiting: {label}",
                    ok=False,
                    request_id=request_id,
                )
                return None
            self._decision_log("deny", "approval", step, tool=tool, source="timeout")
            await self._card_done(
                action_id,
                f"⏱️ Timed out: denied after {_minutes(self.wait_s)} — {label}",
                ok=False,
                request_id=request_id,
            )
            return {"decision": "deny", "reason": timeout_text(self.wait_s)}
        finally:
            self._held.pop(request_id, None)
            self._note_approval_wait(parent_step, False)
            if not settled:
                # Cancelled (the run ended under it): no tap can land now.
                withdraw(request_id)

    def _note_approval_wait(self, step: int | None, waiting: bool) -> None:
        callback = self.on_approval_wait
        if step is None or callback is None:
            return
        try:
            callback(step, waiting)
        except Exception:  # noqa: BLE001 — display bookkeeping only
            logger.debug("antigravity.gate.approval_wait_failed", exc_info=True)

    async def _card_done(
        self, action_id: str, title: str, *, ok: bool, request_id: str
    ) -> None:
        await self._emit(
            self.factory.action_completed(
                action_id=action_id,
                kind="warning",
                title=title,
                ok=ok,
                detail={"request_id": request_id},
            )
        )

    def _cards_left(self) -> bool:
        now = time.monotonic()
        while self._card_times and now - self._card_times[0] > 60:
            self._card_times.popleft()
        if len(self._card_times) >= MAX_CARDS_PER_MINUTE:
            return False
        self._card_times.append(now)
        return True

    def _decision_log(
        self,
        decision: str,
        kind: str,
        step: Any,
        *,
        tool: str = "ask_question",
        source: str | None = None,
    ) -> None:
        logger.info(
            "antigravity.gate.decision",
            run=self.key,
            decision=decision,
            kind=kind,
            tool=tool[:60],
            source=source,
            step=step if isinstance(step, int) else None,
        )

    async def _ask(self, args: Any, step: Any, stream: Any) -> dict[str, Any] | None:
        if self.unattended_trigger is not None:
            self._decision_log("deny", "unattended", step)
            return {
                "decision": "deny",
                "reason": unattended_deny_message(
                    self.unattended_trigger, "AskUserQuestion", ""
                ),
            }
        if self.ask_questions is False:
            self._decision_log("deny", "ask_off", step)
            return {"decision": "deny", "reason": ASK_OFF_TEXT}
        questions = normalise_questions(args)
        if not questions:
            logger.warning("antigravity.gate.ask_unreadable", run=self.key)
            if self.required:
                # Ask me (security review 3 L4): no decision would let agy
                # run the tool under its bypass flag.
                self._decision_log("deny", "unreadable", step)
                return {"decision": "deny", "reason": UNREADABLE_QUESTION_TEXT}
            return {}
        if not self._cards_left():
            self._decision_log("deny", "rate_limited", step)
            return {"decision": "deny", "reason": TOO_MANY_TEXT}
        request_id = f"agy_{secrets.token_hex(6)}"
        action_id = f"antigravity.ask.{request_id}"
        channel = get_run_channel_id() or 0
        register(
            request_id,
            session_key=self.session_key,
            responder=RESPONDER,
            tool_name="ask_question",
            tool_input={"questions": questions},
            kind="ask",
        )
        flow = AskQuestionState(
            request_id=request_id, channel_id=channel, questions=questions
        )
        _ASK_QUESTION_FLOWS[request_id] = flow
        _PENDING_ASK_REQUESTS[request_id] = (channel, questions[0]["question"])
        held = _Held(request_id=request_id, action_id=action_id)
        self._held[request_id] = held
        title = format_question_message(flow)
        await self._emit(
            self.factory.action_started(
                action_id=action_id,
                kind="warning",
                title=title,
                detail={
                    "request_id": request_id,
                    "tool_name": "ask_question",
                    "ask_question": questions[0]["question"],
                    "ask_flow": True,
                    "inline_keyboard": {"buttons": get_question_option_buttons(flow)},
                },
            )
        )
        logger.info(
            "ask_question.shown",
            engine=ENGINE,
            request_id=request_id,
            questions=len(questions),
            options=len(questions[0]["options"]),
        )
        settled = False
        try:
            await self._wait(held, stream)
            if held.decision is None and not held.gone:
                # Deadline. A tap writing its answer right now wins.
                if expire(request_id):
                    settled = True
                else:
                    with anyio.move_on_after(_LATE_TAP_GRACE_S):
                        await held.decided.wait()
            settled = True
            if held.decision is not None:
                self._decision_log("deny", "answer", step)
                logger.info(
                    "ask_question.answered", engine=ENGINE, request_id=request_id
                )
                await self._emit(
                    self.factory.action_completed(
                        action_id=action_id,
                        kind="warning",
                        title=_answer_title(held.decision),
                        ok=True,
                        detail={"request_id": request_id},
                    )
                )
                return {"decision": "deny", "reason": answer_reason(held.decision)}
            if held.gone:
                withdraw(request_id)
                logger.info(
                    "ask_question.withdrawn", engine=ENGINE, request_id=request_id
                )
                await self._emit(
                    self.factory.action_completed(
                        action_id=action_id,
                        kind="warning",
                        title="❓ question withdrawn — agy stopped waiting",
                        ok=False,
                        detail={"request_id": request_id},
                    )
                )
                return None
            self._decision_log("deny", "timeout", step)
            await self._emit(
                self.factory.action_completed(
                    action_id=action_id,
                    kind="warning",
                    title="⏱️ No answer — agy carried on",
                    ok=True,
                    detail={"request_id": request_id},
                )
            )
            return {"decision": "deny", "reason": TIMEOUT_TEXT}
        finally:
            self._held.pop(request_id, None)
            _PENDING_ASK_REQUESTS.pop(request_id, None)
            leftover = _ASK_QUESTION_FLOWS.pop(request_id, None)
            if leftover is not None and held.decision is not None:
                # A typed answer leaves the option flow behind: a late tap
                # on its buttons then reads "Already answered" (#698).
                _record_answered_ask_flow(request_id, leftover.channel_id)
            if not settled:
                # Cancelled (the run ended under it): no tap can land now.
                withdraw(request_id)

    async def _wait(self, held: _Held, stream: Any) -> None:
        """Until a decision, the deadline, or the hook's connection closing
        (agy's own hook timeout, or agy gone: ``registry.withdraw``)."""

        async def watch_eof() -> None:
            with contextlib.suppress(
                anyio.EndOfStream,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
                OSError,
            ):
                await stream.receive(1)
            if not held.decided.is_set():
                held.gone = True
                held.decided.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(watch_eof)
            with anyio.move_on_after(self.wait_s):
                await held.decided.wait()
            tg.cancel_scope.cancel()


# Indirection so tests that speak the hook protocol from the test process
# itself can stand in for agy's hook.
_LINEAGE_CHECK: Callable[[GateRun, Any, str, dict], str | None] = (
    GateRun._lineage_problem
)


# ── registry responder (#960) ───────────────────────────────────────────────


class _GateResponder:
    """Delivers a Telegram answer to the held hook of an agy run."""

    engine = ENGINE
    callback_prefix = CALLBACK_PREFIX

    def is_alive(self, session_key: str) -> bool:
        run = _RUNS.get(session_key)
        return run is not None and not run.closed

    async def respond(
        self, request_id: str, session_key: str, decision: Decision
    ) -> bool:
        run = _RUNS.get(session_key)
        if run is None:
            return False
        return run.resolve(request_id, decision)


RESPONDER = _GateResponder()


# ── steer backend (#981; ``live_sessions.steer_backend``) ───────────────────


class _AgySteer:
    """Steer into a running agy turn through ``PreInvocation.injectSteps``.

    Not a live-session backend: agy keeps no warm process (R2-1). A steer
    succeeds while a gated run for that conversation is in flight and agy
    hasn't sent its result; it is injected before agy's next model call."""

    engine = ENGINE
    display_name = "Antigravity"
    log_ns = ENGINE

    @property
    def supports_steer(self) -> bool:
        return gate_plugin.installed_ready()

    def get(self, session_id: str) -> GateRun | None:
        run = _BY_CONVERSATION.get(session_id)
        if run is None or run.closing:
            return None
        return run

    async def steer(self, session_id: str, text: str, **kwargs: Any) -> str | None:
        run = _BY_CONVERSATION.get(session_id)
        if run is None:
            return None
        command_uuid = kwargs.get("command_uuid") or secrets.token_hex(8)
        if not run.queue_steer(text, str(command_uuid)):
            return "window_closed"
        return "steered"


STEER_BACKEND = _AgySteer()


def active_run(conversation_id: str) -> GateRun | None:
    return _BY_CONVERSATION.get(conversation_id)


def _reset_for_tests() -> None:
    _RUNS.clear()
    _BY_CONVERSATION.clear()
    _SWEPT.clear()
