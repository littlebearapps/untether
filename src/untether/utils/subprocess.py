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
from .proc_diag import find_descendants

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


@asynccontextmanager
async def manage_subprocess(
    cmd: Sequence[str], **kwargs: Any
) -> AsyncIterator[Process]:
    """Ensure subprocesses receive SIGTERM, then SIGKILL after a 10s timeout."""
    if os.name == "posix":
        kwargs.setdefault("start_new_session", True)
    proc = await anyio.open_process(cmd, **kwargs)
    try:
        yield proc
    finally:
        if proc.returncode is None:
            with anyio.CancelScope(shield=True):
                terminate_process(proc)
                timed_out = await wait_for_process(proc, timeout=10.0)
                if timed_out:
                    kill_process(proc)
                    await proc.wait()
