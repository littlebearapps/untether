from __future__ import annotations

import contextlib
import os
import shutil
import signal
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anyio
from anyio.abc import Process

from ..logging import get_logger
from .proc_diag import find_descendants, pid_starttime

logger = get_logger(__name__)


def wrap_with_env_i(cmd: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Return ``cmd`` wrapped with ``env -i KEY=VAL ...`` so the resolved
    environment at exec time is exactly ``env`` — even if the child later
    reads ``/etc/environment``, sources rc files, or otherwise re-introduces
    host vars (#361).

    Locates ``env`` via ``shutil.which`` with a ``/usr/bin/env`` fallback.
    Caller should pass ``env=None`` to ``manage_subprocess`` when using this
    wrap, so subprocess.exec doesn't double-set the environment.

    Security trade-off: ``KEY=VALUE`` pairs sit in ``env``'s argv during the
    fork/exec window before ``env`` exec's into the wrapped program. After
    exec, ``/proc/<pid>/cmdline`` reports the *new* program's argv (verified:
    ``env -i FOO=bar sleep 5`` shows ``sleep 5`` post-exec), so the only
    exposure is a microsecond window on ``env``'s own PID. The secrets remain
    in the spawned program's ``/proc/<pid>/environ`` which is per-user
    permission-protected. We accept this over the alternative of relying
    solely on ``subprocess.spawn(env=…)`` because v0.35.2rc3 testing on
    ``@untether_dev_bot`` proved that an upstream rc-file source / wrapper
    script can re-introduce host vars after Python's ``execve`` honoured the
    env dict — ``env -i`` is the only mechanism that survives that path.
    """
    env_path = shutil.which("env") or "/usr/bin/env"
    return [env_path, "-i", *(f"{k}={v}" for k, v in env.items()), *cmd]


def redact_env_i_args(cmd: Sequence[str]) -> list[str]:
    """Return ``cmd`` with ``KEY=VALUE`` pairs after ``env -i`` redacted.

    Used by structured logs that want to record the spawned cmdline
    without leaking the API keys / tokens that ``wrap_with_env_i`` puts
    into argv (#361 follow-up). Detects ``[env_path, "-i", "K=V", "K=V",
    ..., program, args...]`` shape; for any element matching ``KEY=…``
    between ``-i`` and the first non-``KEY=…`` element, replaces the value
    with ``***``. Returns the input unchanged if the pattern doesn't match.
    """
    if len(cmd) < 2 or cmd[1] != "-i":
        return list(cmd)
    out: list[str] = [cmd[0], cmd[1]]
    in_env_block = True
    for arg in cmd[2:]:
        if in_env_block and "=" in arg and not arg.startswith(("-", "/")):
            key, _, _ = arg.partition("=")
            out.append(f"{key}=***")
        else:
            in_env_block = False
            out.append(arg)
    return out


async def wait_for_process(proc: Process, timeout: float) -> bool:  # noqa: ASYNC109
    with anyio.move_on_after(timeout) as scope:
        await proc.wait()
    return scope.cancel_called


def terminate_process(proc: Process) -> None:
    _signal_process(
        proc,
        signal.SIGTERM,
        fallback=proc.terminate,
        log_event="subprocess.terminate.failed",
    )


def kill_process(proc: Process) -> None:
    _signal_process(
        proc,
        signal.SIGKILL,
        fallback=proc.kill,
        log_event="subprocess.kill.failed",
    )


def _signal_process(
    proc: Process,
    sig: signal.Signals,
    *,
    fallback: Callable[[], None],
    log_event: str,
) -> None:
    if proc.returncode is not None:
        return

    # Snapshot descendants BEFORE signalling the parent (#275).  Once the
    # parent dies, /proc/<pid>/task/*/children entries disappear and any
    # grandchildren in separate process groups (e.g. vitest → workerd, which
    # node spawns with fresh sessions) become invisible to `killpg`.  On
    # non-Linux hosts or /proc read errors the snapshot is empty and
    # behaviour falls back to the legacy pgroup-only path.
    descendants: list[int] = []
    if os.name == "posix" and proc.pid is not None and sys.platform == "linux":
        try:
            descendants = find_descendants(proc.pid)
        except OSError:
            descendants = []

    used_posix = False
    if os.name == "posix" and proc.pid is not None:
        used_posix = True
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass  # Parent already gone; still deliver to captured descendants.
        except OSError as exc:
            logger.debug(
                log_event,
                error=str(exc),
                error_type=exc.__class__.__name__,
                pid=proc.pid,
            )
            used_posix = False  # Fall through to the anyio fallback.

    if not used_posix:
        with contextlib.suppress(ProcessLookupError):
            fallback()

    # Best-effort signal to orphan descendants in separate process groups.
    # Ignored when the snapshot is empty (non-Linux, /proc error, or parent
    # had no grandchildren).  #275.
    _signal_descendants(descendants, sig, log_event)


def _signal_descendants(pids: list[int], sig: signal.Signals, log_event: str) -> None:
    """Deliver *sig* to each captured descendant PID, best-effort.

    Swallows ``ProcessLookupError`` (already exited since the snapshot) and
    ``PermissionError`` (process reparented to a different user's systemd).
    Other ``OSError``s are logged at debug level and skipped.  #275.
    """
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue
        except OSError as exc:
            logger.debug(
                log_event,
                error=str(exc),
                error_type=exc.__class__.__name__,
                pid=pid,
            )


def signal_pid_group(pid: int, sig: signal.Signals) -> None:
    """Deliver *sig* to a bare pid's process group AND its captured
    descendants (#590).

    The pid-based twin of :func:`_signal_process` for callers that hold a
    pid rather than an anyio ``Process`` (the Claude post-result watchdog).
    Bare ``os.killpg`` misses grandchildren that re-parented into separate
    sessions/pgroups (e.g. MCP ``node`` → ``mcp-remote`` chains), so the
    descendant tree is snapshotted BEFORE signalling the group.
    """
    if os.name != "posix":
        return
    descendants: list[int] = []
    if sys.platform == "linux":
        try:
            descendants = find_descendants(pid)
        except OSError:
            descendants = []
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, sig)
    _signal_descendants(descendants, sig, "subprocess.group_signal.failed")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _pgid_members(pgid: int) -> list[int]:
    """Enumerate live PIDs whose process group is *pgid* (Linux /proc scan).

    Used by the post-exit orphan sweep for logging and per-PID delivery.
    Best-effort: unreadable or vanished entries are skipped; non-Linux
    returns an empty list (the killpg-based sweep still works there).
    """
    if sys.platform != "linux":
        return []
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read()
            # Field 5 (pgrp) sits after the comm field, which can contain
            # spaces/parens — parse from the LAST ')'.
            after_comm = stat[stat.rindex(b")") + 2 :].split()
            if int(after_comm[1]) == pgid:
                members.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return members


async def reap_orphaned_group(
    pgid: int,
    *,
    extra_pids: Sequence[int] = (),
    extra_pid_starttimes: Mapping[int, int] | None = None,
    grace_s: float = 2.0,
) -> list[int]:
    """#590: sweep process-group survivors after the group leader exited.

    The Claude CLI leaks MCP ``node`` children on clean (rc=0) exits —
    observed fleet-wide as 1 orphan per run accumulating until the daily
    systemd SIGKILL sweep, and the dominant memory pressure behind the nsd
    OOM kills (#589). Nothing previously killed them: ``manage_subprocess``
    only signalled while the *leader* was still alive.

    Delivers SIGTERM to the group (plus any *extra_pids* captured while the
    leader was alive — pgroup escapees), waits up to *grace_s* for a clean
    exit, then SIGKILLs survivors. Returns the PIDs targeted (best-effort,
    for the ``subprocess.orphans_reaped`` log). No-ops instantly when the
    group is already empty.

    ``extra_pid_starttimes`` (#590 hardening): a ``{pid: /proc starttime}``
    map recorded when each *extra_pid* was captured. Before signalling an
    extra PID we re-read its start time and skip it on mismatch — so a
    captured descendant that exited and had its PID recycled by an unrelated
    process is never killed. The process-group kill is unaffected (escapees
    are the only PID-signalled targets).
    """
    if os.name != "posix":
        return []

    def _identity_ok(p: int) -> bool:
        if not extra_pid_starttimes:
            return True  # legacy / best-effort: no identity recorded
        recorded = extra_pid_starttimes.get(p)
        if recorded is None:
            return True  # this pid was captured without a starttime
        current = pid_starttime(p)
        # Unreadable now (raced away / non-Linux) → skip rather than risk a
        # wrong kill; a genuinely-alive orphan would still expose its stat.
        return current is not None and current == recorded

    live_extras = [
        p for p in extra_pids if p != pgid and _pid_alive(p) and _identity_ok(p)
    ]

    def _group_alive() -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    if not _group_alive() and not live_extras:
        return []

    victims: set[int] = set(_pgid_members(pgid))
    victims.update(live_extras)

    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    _signal_descendants(live_extras, signal.SIGTERM, "subprocess.orphan_sweep.failed")

    deadline_polls = max(1, int(grace_s / 0.1))
    for _ in range(deadline_polls):
        if not _group_alive() and not any(_pid_alive(p) for p in live_extras):
            break
        await anyio.sleep(0.1)

    survivors = [p for p in victims if _pid_alive(p) and _identity_ok(p)]
    if _group_alive() or survivors:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
        _signal_descendants(survivors, signal.SIGKILL, "subprocess.orphan_sweep.failed")

    reaped = sorted(victims)
    if reaped:
        logger.info(
            "subprocess.orphans_reaped",
            pgid=pgid,
            pids=reaped,
            extra_pids=sorted(live_extras),
        )
    return reaped


# #589: how many engine subprocesses are alive right now, process-wide.
#
# The pre-spawn RAM guard (#350) is per-spawn and count-blind: N chats can each
# individually pass the free-RAM check and then collectively exhaust the box.
# That is what happened on nsd — the OOM killer struck untether.service 5x in
# one evening and took two live Claude runs with it (rc=-9), each session
# holding 10-17 MCP node children.
#
# Counted here rather than from `TelegramLoopState.running_tasks` because this
# is the single spawn point shared by every engine runner, and because what
# actually consumes memory is a live subprocess, not a queued task. Plain int
# rather than a lock: anyio is single-threaded per event loop and both mutation
# sites are synchronous, so there is no interleaving to guard against.
_LIVE_ENGINE_SUBPROCESSES = 0


def _incr_live_engine_subprocesses(delta: int) -> None:
    global _LIVE_ENGINE_SUBPROCESSES
    _LIVE_ENGINE_SUBPROCESSES = max(0, _LIVE_ENGINE_SUBPROCESSES + delta)


def live_engine_subprocess_count() -> int:
    """Number of engine subprocesses currently alive in this process (#589)."""
    return _LIVE_ENGINE_SUBPROCESSES


@asynccontextmanager
async def manage_subprocess(
    cmd: Sequence[str],
    *,
    reap_orphans: bool = True,
    orphan_pid_snapshot: Sequence[int] | None = None,
    orphan_pid_starttimes: Mapping[int, int] | None = None,
    **kwargs: Any,
) -> AsyncIterator[Process]:
    """Ensure subprocesses receive SIGTERM, then SIGKILL after a 10s timeout.

    ``reap_orphans`` (#590, ``[watchdog] reap_orphans``): after the leader
    has exited — including clean rc=0 — sweep surviving process-group
    members and any PIDs the caller captured into *orphan_pid_snapshot*
    while the leader was alive (a mutable list works: it is read only at
    teardown time). *orphan_pid_starttimes* carries each captured PID's
    ``/proc`` start time so the sweep can reject a recycled PID before
    signalling (see ``reap_orphaned_group``).
    """
    if os.name == "posix":
        kwargs.setdefault("start_new_session", True)
    proc = await anyio.open_process(cmd, **kwargs)
    _incr_live_engine_subprocesses(1)
    try:
        yield proc
    finally:
        _incr_live_engine_subprocesses(-1)
        if proc.returncode is None:
            with anyio.CancelScope(shield=True):
                terminate_process(proc)
                timed_out = await wait_for_process(proc, timeout=10.0)
                if timed_out:
                    kill_process(proc)
                    await proc.wait()
        # #590: the leader is dead — reap group survivors (leaked MCP
        # children etc.) before releasing the transport.
        if reap_orphans:
            with anyio.CancelScope(shield=True):
                try:
                    await reap_orphaned_group(
                        proc.pid,
                        extra_pids=tuple(orphan_pid_snapshot or ()),
                        extra_pid_starttimes=(
                            dict(orphan_pid_starttimes)
                            if orphan_pid_starttimes
                            else None
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("subprocess.orphan_sweep_failed", exc_info=True)
        # #599: release the asyncio subprocess transport explicitly, on every
        # exit path including clean rc=0. Without this the transport is only
        # reached by GC at interpreter exit — after the event loop closed —
        # so BaseSubprocessTransport.__del__ raises "RuntimeError: Event loop
        # is closed" and the transport's pipes/FDs live from run completion
        # until process shutdown.
        with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
            await proc.aclose()
