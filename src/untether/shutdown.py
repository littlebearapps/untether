"""Graceful shutdown state for drain-then-restart."""

from __future__ import annotations

import threading

from .logging import get_logger

logger = get_logger(__name__)

# Module-level shutdown state. Thread-safe via threading.Event (signal handlers
# run on the main thread; the anyio loop may be on another).
_shutdown_requested = threading.Event()
_shutdown_lock = threading.Lock()
_shutdown_origin_chat_id: int | None = None

# Default drain timeout — how long to wait for in-progress runs to finish
# before the process exits. Matches systemd's default TimeoutStopSec.
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
    ``None``. The drain loop uses it to confirm the precise self-restart case
    (#559); ``active_runs == 1`` remains the operative trigger for the SIGTERM
    self-restart incident.
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


def select_drain_timeout(active_runs: int) -> float:
    """#559: pick the drain timeout. A sole active run is (almost certainly) the
    self-restart deadlock — it can't self-complete, so use the short timeout
    instead of the full DRAIN_TIMEOUT_S. Two or more runs get the full grace."""
    return SELF_RESTART_DRAIN_TIMEOUT_S if active_runs == 1 else DRAIN_TIMEOUT_S


def get_shutdown_origin_chat_id() -> int | None:
    """The chat that initiated the shutdown, or None (signal / unknown)."""
    return _shutdown_origin_chat_id


def reset_shutdown() -> None:
    """Reset shutdown state. Only for testing."""
    global _shutdown_origin_chat_id
    with _shutdown_lock:
        _shutdown_origin_chat_id = None
        _shutdown_requested.clear()
