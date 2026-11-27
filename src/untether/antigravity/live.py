"""Antigravity live sessions (#977, phase 977-3): the warm-``agy`` registry.

A warm run (``[antigravity] live_sessions``, Workspace or Full access chat
runs, 977-2) keeps ``agy`` alive after its result with stdin open. This
module is what ``live_sessions._AgyLive`` reads: one :class:`WarmEntry` per
warm process, from spawn (``AntigravityRunner._send_payload``) until the
process has exited (``run_impl``'s ``finally``), so a run resuming the
conversation never meets a process that still holds it.

Follow-ups are written only between turns (``inject_when_idle``): a line
written mid-turn is queued by agy itself (probe P3), and stdin EOF after it
may still let agy run it on the config it loaded at spawn. "Between turns"
also waits for the turn's planted-config re-scan (``warm_checking``), and
each follow-up re-runs that scan first: a changed config closes the process
(``config_changed``) and the message resumes through a fresh, checked spawn.

Lifecycle (one task per warm process, ``lifecycle``): idle for
``[watchdog] post_result_limbo_grace`` → ``idle_no_tasks``;
``[watchdog] live_session_max_s`` from spawn → ``abs_cap``, deferred to the
turn boundary for at most ``ABS_CAP_TURN_GRACE_S`` (#887); a close the
process doesn't honour within ``EXIT_GRACE_S`` gets SIGTERM. agy has no
turn-only interrupt (P7) and holds its result for background work, so there
is no background hold and nothing is ever quarantined.

This module imports no runner: the runner hands itself in at ``register``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from ..logging import get_logger
from ..utils.antigravity_argv import native_compact_prompt

logger = get_logger(__name__)

# Timing knobs (module attributes so tests can shrink them).
POLL_S = 1.0
EXIT_GRACE_S = 5.0
ABS_CAP_TURN_GRACE_S = 1800.0  # #887, as Claude's and Codex's
# A follow-up whose turn never starts stops counting as pending after this
# (Codex's ``FOLLOWUP_TURN_TIMEOUT_S``); its uuid stays queued as a tombstone
# so a late turn still takes the right reply anchor.
FOLLOWUP_TURN_TIMEOUT_S = 120.0
INJECT_POLL_S = 0.2
HANDOFF_POLL_S = 0.25


@dataclass(slots=True, eq=False)
class WarmEntry:
    runner: Any  # AntigravityRunner
    state: Any  # AntigravityStreamState
    spawn_options: Any = None
    spawn_cwd: Path | None = None
    # ``[antigravity] permission_mode`` at spawn: a chat with no override
    # follows it, so its hot-reload is an options change too (review H2).
    toml_mode: Any = None
    spawned_at: float = field(default_factory=time.monotonic)
    idle_grace_s: float = 60.0
    max_s: float = 14400.0
    idle_since: float | None = None
    abs_cap_deferred: bool = False
    closed_at: float | None = None
    escalated: bool = False
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    listeners: list[Any] = field(default_factory=list)

    @property
    def session_id(self) -> str | None:
        return self.state.session_id

    @property
    def closing(self) -> bool:
        """``LiveSessionView``: no follow-up goes in any more."""
        return self.state.warm_closing is not None


_ENTRIES: list[WarmEntry] = []


def _live_settings() -> tuple[float, float]:
    """``post_result_limbo_grace`` and ``live_session_max_s`` (D5: shared
    with Claude and Codex), read per spawn."""
    grace, cap = 60.0, 14400.0
    try:
        from ..settings import load_settings_if_exists

        loaded = load_settings_if_exists()
        if loaded is not None:
            watchdog = loaded[0].watchdog
            grace = float(watchdog.post_result_limbo_grace)
            cap = float(watchdog.live_session_max_s)
    except Exception:  # noqa: BLE001 — settings errors must not block a run
        logger.debug("antigravity.live_session.settings_load_failed", exc_info=True)
    return grace, cap


def _engine_key(key: str, default: Any) -> Any:
    """One ``[antigravity]`` key, read per call (hot-reloads); *default*
    only when the config can't be read."""
    try:
        from ..settings import load_settings_if_exists

        loaded = load_settings_if_exists()
        if loaded is None:
            return None  # no config file: every key unset
        settings, path = loaded
        return settings.engine_config("antigravity", config_path=path).get(key)
    except Exception:  # noqa: BLE001
        return default


def switch_on() -> bool:
    """``[antigravity] live_sessions``, read per call (hot-reloads; a run
    already warm keeps its process until it closes)."""
    return _engine_key("live_sessions", False) is True


def _toml_mode() -> Any:
    # Unreadable → a sentinel never equal to a spawn-time value: fail closed.
    return _engine_key("permission_mode", object())


# -- registry -----------------------------------------------------------------


def register(runner: Any, state: Any, *, options: Any, cwd: Path | None) -> WarmEntry:
    grace, cap = _live_settings()
    entry = WarmEntry(
        runner=runner,
        state=state,
        spawn_options=options,
        spawn_cwd=cwd,
        toml_mode=_toml_mode(),
        idle_grace_s=grace,
        max_s=cap,
    )
    _ENTRIES.append(entry)
    return entry


def entry_for_state(state: Any) -> WarmEntry | None:
    return next((e for e in _ENTRIES if e.state is state), None)


def entry(session_id: str) -> WarmEntry | None:
    """The process holding ``session_id``; an open one wins over one that is
    closing (the session lock keeps it to one in practice)."""
    found = [e for e in _ENTRIES if session_id and e.session_id == session_id]
    return next((e for e in found if not e.closing), found[0] if found else None)


def accepting(e: WarmEntry | None) -> bool:
    proc = None if e is None else e.state.warm_proc
    return (
        e is not None
        and not e.closing
        and proc is not None
        and proc.returncode is None
        and e.state.warm_turns > 0
    )


def line_pending(state: Any, now: float | None = None) -> bool:
    """A written follow-up agy hasn't started yet (bounded; tombstones don't
    count)."""
    if len(state.warm_uuids) <= state.warm_expired:
        return False
    waited = (time.monotonic() if now is None else now) - state.warm_written_at
    if waited < FOLLOWUP_TURN_TIMEOUT_S:
        return True
    state.warm_expired = len(state.warm_uuids)
    logger.warning(
        "antigravity.live_session.followup_turn_timeout",
        session_id=state.session_id,
        waited_s=round(waited, 1),
    )
    return False


def is_idle(state: Any, now: float | None = None) -> bool:
    """Between turns: the first turn answered, none open, its re-scan done,
    no written line waiting for its turn."""
    return (
        state.warm_turns > 0
        and not state.warm_turn_open
        and not state.warm_checking
        and not line_pending(state, now)
    )


def idle_count() -> int:
    return sum(1 for e in _ENTRIES if accepting(e) and is_idle(e.state))


async def notify(e: WarmEntry, kind: str, payload: dict[str, Any]) -> None:
    for callback in list(e.listeners):
        try:
            result = callback(kind, payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001 — a listener must never break a run
            logger.warning(
                "antigravity.live_session.listener_failed",
                session_id=e.session_id,
                kind=kind,
                exc_info=True,
            )


# -- turn boundaries (called by the runner) -----------------------------------


async def turn_ended(state: Any) -> None:
    """After a warm turn's result and its re-scan: steers the gate queued
    for that turn never ran — say so per turn (977-2 only logged them at the
    next turn's start), then the session counts as idle."""
    gate = state.gate
    uuids = gate.drain_unrun() if gate is not None else []
    e = entry_for_state(state)
    if uuids and e is not None:
        await notify(e, "not_run", {"command_uuids": uuids})
    state.warm_checking = False


async def ended(state: Any, *, died: bool) -> None:
    """The warm process has exited: drop its entry, then one ``closed``
    notice for an Untether close, or the #1001 ``died`` notice for an idle
    exit nobody asked for (agy never exits on its own with stdin open)."""
    e = entry_for_state(state)
    if e is None:
        return
    with contextlib.suppress(ValueError):
        _ENTRIES.remove(e)
    reason = state.warm_closing
    if reason is not None:
        logger.info(
            "antigravity.live_session.closed",
            session_id=e.session_id,
            reason=reason,
            turns=state.warm_turns,
            quarantined=False,
        )
        await notify(e, "closed", {"reason": reason, "quarantined": False, "tasks": []})
        return
    from ..shutdown import is_shutting_down

    if not died or is_shutting_down():
        return
    from ..runners.claude import _exit_cause, _exit_signal, _signal_name

    rc = getattr(state.warm_proc, "returncode", None)
    sig = _exit_signal(rc)
    logger.warning(
        "antigravity.live_session.died_unexpectedly",
        session_id=e.session_id,
        rc=rc,
        signal_name=_signal_name(sig),
        exit_cause=_exit_cause(rc),
    )
    await notify(
        e,
        "died",
        {
            "rc": rc,
            "signal": sig,
            "signal_name": _signal_name(sig),
            "exit_cause": _exit_cause(rc),
            "tasks": [],
            "task_ids": [],
            "quarantined": False,
        },
    )


# -- the backend's operations -------------------------------------------------


async def close(
    session_id: str,
    reason: str,
    *,
    notice: bool = False,
    only_if_idle: bool = False,
) -> bool:
    """stdin EOF: agy exits rc 0 (P6) and the run ends. Idempotent; False
    when there is nothing (left) to close, or ``only_if_idle`` and a turn,
    its re-scan or a written line is in flight (re-checked under the lock a
    follow-up is written under)."""
    e = entry(session_id)
    if e is None:
        return False
    async with e.lock:
        if e.closing:
            return False
        busy = not is_idle(e.state)
        if only_if_idle and busy:
            return False
        e.closed_at = time.monotonic()
        await e.runner.close_warm(e.state, reason)
    if notice:
        payload: dict[str, Any] = {"reason": reason, "tasks": []}
        if reason == "abs_cap":
            payload["mid_turn"] = busy
            payload["max_s"] = e.max_s
        await notify(e, "closing", payload)
    return True


async def _config_changed(e: WarmEntry) -> bool:
    """R3 before a follow-up: agy loaded hooks, plugins and agents at spawn,
    so a planted-config change since then (or a re-check that can't rule
    one out) must never take a follow-up (fail closed)."""
    pre = e.state.precheck
    if pre is None or pre.scan is None:
        return False
    rows: list[Any] | None = None
    with anyio.move_on_after(_scan_timeout()):
        rows = await anyio.to_thread.run_sync(
            e.runner._config_change_rows, pre, abandon_on_cancel=True
        )
    if rows is None or rows:
        logger.warning(
            "antigravity.live_session.config_changed",
            session_id=e.session_id,
            reason="unverifiable" if rows is None else "changed",
        )
        return True
    return False


def _scan_timeout() -> float:
    from ..utils import antigravity_scan

    return antigravity_scan.SCAN_TIMEOUT_S


async def inject_when_idle(
    session_id: str,
    text: str,
    *,
    command_uuid: str,
    poll_s: float | None = None,
    cancel: Any = None,
    recheck: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Queue semantics: wait until the warm session is between turns, then
    write ``text`` as one more ``user`` line (``send_followup``: stdin only,
    never argv). False: the caller keeps the message for the resume path.
    agy's own ``/compact`` can't run in a process started with
    ``--disable-slash-commands`` (§5.4), and a changed config can't take a
    follow-up: both close the idle session so the message resumes one-shot.
    ``recheck`` (the caller's chat-options comparison) and the TOML mode run
    again under the lock, just before the write: the wait can span a whole
    turn, and a switch to Ask me or Plan first meanwhile must not run in this
    process (review H1/H2)."""
    close_reason = "error"
    compact = native_compact_prompt(text) is not None
    while True:
        if cancel is not None and cancel.cancelled:
            return False
        e = entry(session_id)
        if e is None or not accepting(e):
            return False
        if is_idle(e.state):
            async with e.lock:
                if not accepting(e):
                    return False
                if is_idle(e.state):
                    if compact:
                        close_reason = "compact"
                        break
                    if await _config_changed(e):
                        close_reason = "config_changed"
                        break
                    if _toml_mode() != e.toml_mode or (
                        recheck is not None and not await recheck()
                    ):
                        close_reason = "options_changed"
                        break
                    if cancel is not None and not cancel.claim():
                        return False  # #935: cancelled from its queued card
                    outcome = await e.runner.send_followup(
                        e.state, text, command_uuid=command_uuid
                    )
                    if outcome == "written":
                        e.idle_since = None
                        return True
                    break
        await anyio.sleep(INJECT_POLL_S if poll_s is None else poll_s)
    await close(session_id, close_reason, only_if_idle=True)
    return False


async def close_idle_for_continue(cwd: Path | None) -> int:
    """#1275 (and the fork guard, R1): close the idle warm sessions spawned
    in ``cwd`` before a ``/continue`` there resolves its conversation."""
    closed = 0
    for e in [*_ENTRIES]:  # a close's exit can drop entries meanwhile
        if e.spawn_cwd == cwd and e.session_id:
            closed += await close(e.session_id, "continue", only_if_idle=True)
    return closed


async def close_steer_window(session_id: str, reason: str) -> bool:
    """#775: no steer lands in a run being cancelled. The gate's hook reads
    the window synchronously, so nothing is injected after this returns."""
    from . import gate_server

    run = gate_server.active_run(session_id)
    if run is None:
        return False
    run.shut_steer()
    logger.info(
        "antigravity.live_session.steer_window_closed",
        session_id=session_id,
        reason=reason,
    )
    return True


async def wait_for_handoff(
    session_id: str, timeout_s: float, *, poll_s: float | None = None
) -> str:
    """#633: ``free`` (no warm process holds the conversation), ``exited``
    (one did and is gone) or ``timed_out``."""
    if entry(session_id) is None:
        return "free"
    started = time.monotonic()
    with anyio.move_on_after(max(0.0, timeout_s)):
        while True:
            if entry(session_id) is None:
                break
            await anyio.sleep(HANDOFF_POLL_S if poll_s is None else poll_s)
    outcome = "timed_out" if entry(session_id) is not None else "exited"
    logger.info(
        "session.handoff_wait_done",
        session_id=session_id,
        outcome=outcome,
        elapsed_s=round(time.monotonic() - started, 1),
    )
    return outcome


# -- lifecycle ------------------------------------------------------------------


async def lifecycle(state: Any) -> None:
    """One warm process's checks, on their own clock (stdout traffic can't
    postpone them). Ends with the reader's task group."""
    while True:
        await anyio.sleep(POLL_S)
        e = entry_for_state(state)
        if e is None:
            return
        await _tick(e)


async def _tick(e: WarmEntry) -> None:
    now = time.monotonic()
    state = e.state
    if e.closing:
        proc = state.warm_proc
        if e.closed_at is None:
            # A close straight from the runner (``close_warm`` after a
            # turn's re-scan) sets no deadline: start it now (review M1).
            e.closed_at = now
        since = e.closed_at
        if (
            not e.escalated
            and proc is not None
            and proc.returncode is None
            and now - since > EXIT_GRACE_S
        ):
            # A close agy didn't honour (or a turn it is finishing past the
            # session limit): end it.
            e.escalated = True
            logger.warning(
                "antigravity.live_session.close_escalated",
                session_id=e.session_id,
                reason=state.warm_closing,
            )
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
        return
    if e.lock.locked():
        return  # a follow-up is being written
    idle = is_idle(state, now)
    over = now - e.spawned_at - e.max_s
    if e.max_s > 0 and over >= 0 and e.session_id:
        if idle or over >= ABS_CAP_TURN_GRACE_S:
            await close(e.session_id, "abs_cap", notice=not idle, only_if_idle=idle)
            return
        if not e.abs_cap_deferred:
            e.abs_cap_deferred = True
            logger.info(
                "antigravity.live_session.abs_cap_deferred",
                session_id=e.session_id,
                cap_s=e.max_s,
                grace_s=ABS_CAP_TURN_GRACE_S,
            )
    if not idle:
        e.idle_since = None
        return
    if e.idle_since is None:
        e.idle_since = now
    elif now - e.idle_since >= e.idle_grace_s and e.session_id:
        await close(e.session_id, "idle_no_tasks", only_if_idle=True)
