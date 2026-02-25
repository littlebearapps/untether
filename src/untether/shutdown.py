"""Graceful shutdown state for drain-then-restart."""

from __future__ import annotations

import threading

from .logging import get_logger

logger = get_logger(__name__)

# Module-level shutdown state. Thread-safe via threading.Event (signal handlers
# run on the main thread; the anyio loop may be on another).
_shutdown_requested = threading.Event()

# Default drain timeout — how long to wait for in-progress runs to finish
# before the process exits. Matches systemd's default TimeoutStopSec.
DRAIN_TIMEOUT_S: float = 120.0


def request_shutdown() -> None:
    """Signal a graceful shutdown.

    Safe to call from signal handlers (uses threading.Event, not anyio).
    """
    if _shutdown_requested.is_set():
        return
    _shutdown_requested.set()
    logger.info("shutdown.requested")


def is_shutting_down() -> bool:
    """Check whether a graceful shutdown has been requested."""
    return _shutdown_requested.is_set()


def reset_shutdown() -> None:
    """Reset shutdown state. Only for testing."""
    _shutdown_requested.clear()
