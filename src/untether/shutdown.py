"""Graceful shutdown state for drain-then-restart."""

from __future__ import annotations

import os
import threading

from .logging import get_logger

logger = get_logger(__name__)

# Module-level shutdown state. Thread-safe via threading.Event (signal handlers
# run on the main thread; the anyio loop may be on another).
_shutdown_requested = threading.Event()
_shutdown_lock = threading.Lock()
_shutdown_origin_chat_id: int | None = None

# Default drain timeout — how long to wait for in-progress runs to finish
# before the process exits. The unit ships TimeoutStopSec=150, leaving margin
# for the drain-timeout notification + outbox flush after this expires.
DRAIN_TIMEOUT_S: float = 120.0

# #559: shorter drain used when the sole active run is the session that
# triggered the restart (self-restart pattern, #547). Waiting the full
# DRAIN_TIMEOUT_S there is dead time — the lone run can't self-complete (it's
# blocked on the synchronous `systemctl restart` it just issued), so the long
# wait only delays a clean exit and risks dropping the final outbox message.
SELF_RESTART_DRAIN_TIMEOUT_S: float = 10.0


def request_shutdown(origin_chat_id: int | None = None) -> None:
    """Signal a graceful shutdown.

    Safe to call from signal handlers (uses threading.Event, not anyio).

    ``origin_chat_id`` records the chat that initiated the shutdown when known
    (the ``/restart`` command path). SIGTERM/SIGINT carry no chat, so they pass
    ``None``. The drain loop uses it as one of the two self-restart evidence
    sources (#559/#690); for the SIGTERM self-restart incident the evidence is
    the systemctl/launchctl descendant scan (``scan_self_restart_evidence``).
    """
    global _shutdown_origin_chat_id
    if _shutdown_requested.is_set():
        return
    with _shutdown_lock:
        if _shutdown_requested.is_set():
            return
        _shutdown_origin_chat_id = origin_chat_id
        _shutdown_requested.set()
    logger.info("shutdown.requested", origin_chat_id=origin_chat_id)


def is_shutting_down() -> bool:
    """Check whether a graceful shutdown has been requested."""
    return _shutdown_requested.is_set()


def select_drain_timeout(active_runs: int, *, self_restart: bool) -> float:
    """#559/#690: pick the drain timeout.

    The 10s fast path applies only when the sole active run demonstrably
    initiated the shutdown (``self_restart`` — origin-chat match or a
    systemctl/launchctl descendant in the run's process tree). ``active_runs
    == 1`` alone is cardinality, not causality: an external restart (fleet
    rollout, systemctl, reboot — origin None) landing on a lone healthy run
    must get the full grace, or the rollout destroys in-flight work (#690).
    """
    return (
        SELF_RESTART_DRAIN_TIMEOUT_S
        if active_runs == 1 and self_restart
        else DRAIN_TIMEOUT_S
    )


def get_shutdown_origin_chat_id() -> int | None:
    """The chat that initiated the shutdown, or None (signal / unknown)."""
    return _shutdown_origin_chat_id


# #690: strict verb sets for the self-restart evidence scan. `systemctl kill`
# and `status` return immediately — only verbs that WAIT on the unit stopping
# (and therefore deadlock against our own drain, #547) count as evidence.
_SELF_RESTART_SYSTEMCTL_VERBS = frozenset({"restart", "stop"})
_SELF_RESTART_LAUNCHCTL_VERBS = frozenset({"kickstart", "stop", "bootout"})


def is_self_restart_argv(argv: list[str]) -> bool:
    """#690: does this argv invoke a blocking restart/stop of an untether unit?

    Matches parsed argv tokens (executable basename + exact verb + unit
    name), never substrings — ``bash -c 'echo systemctl restart untether'``
    and ``systemctl status untether`` must not count.
    """
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base == "systemctl":
        args = [a for a in argv[1:] if not a.startswith("-")]
        if len(args) < 2 or args[0] not in _SELF_RESTART_SYSTEMCTL_VERBS:
            return False
        return any(
            token == "untether" or token.startswith(("untether.", "untether-"))
            for token in args[1:]
        )
    if base == "launchctl":
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args or args[0] not in _SELF_RESTART_LAUNCHCTL_VERBS:
            return False
        return any("untether" in token for token in args[1:])
    return False


def scan_self_restart_evidence(pid: int | None) -> str | None:
    """#690: walk a run's process tree for the systemctl/launchctl invocation
    that IS the #547 self-restart deadlock (the run is blocked on it, so it
    is still present at drain time). Returns an evidence label or None.

    Fail-closed: no PID, dead process, non-Linux cmdline reads, or any scan
    error all return None → the drain keeps the full grace. A wrong None
    costs 110s of restart latency; a wrong label destroys a healthy run.
    """
    if pid is None:
        return None
    try:
        from .utils.proc_diag import find_descendants, read_cmdline_argv

        for candidate in (pid, *find_descendants(pid)):
            argv = read_cmdline_argv(candidate)
            if argv and is_self_restart_argv(argv):
                return f"cmdline:{os.path.basename(argv[0])}"
    except Exception:  # noqa: BLE001 — evidence scan must never break drain
        logger.debug("shutdown.self_restart_scan_failed", exc_info=True)
    return None


def reset_shutdown() -> None:
    """Reset shutdown state. Only for testing."""
    global _shutdown_origin_chat_id
    with _shutdown_lock:
        _shutdown_origin_chat_id = None
        _shutdown_requested.clear()
