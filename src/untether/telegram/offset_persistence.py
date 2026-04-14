"""Persist the last confirmed Telegram ``update_id`` across restarts.

On shutdown, the bot writes the most recently acknowledged ``update_id``
to a small JSON state file. On startup, it loads that value and resumes
polling from ``offset = saved + 1``. Telegram retains undelivered updates
for 24 hours, so this eliminates the window where a restart re-processes
(or drops) recent messages. See issue #287.

The file lives alongside ``active_progress.json`` in the Untether state
directory (sibling to the config file).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..logging import get_logger
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "last_update_id.json"

__all__ = [
    "STATE_FILENAME",
    "DebouncedOffsetWriter",
    "load_last_update_id",
    "resolve_offset_path",
    "save_last_update_id",
]


def resolve_offset_path(config_path: Path) -> Path:
    """Return the offset state file path (sibling to config file)."""
    return config_path.with_name(STATE_FILENAME)


def load_last_update_id(path: Path) -> int | None:
    """Load the saved ``update_id``, or ``None`` if missing/corrupt."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "offset_persistence.load_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("last_update_id")
    if isinstance(raw, int) and raw >= 0:
        return raw
    return None


def save_last_update_id(path: Path, update_id: int) -> None:
    """Persist ``update_id`` atomically. Swallows errors (logs at warning)."""
    try:
        atomic_write_json(path, {"last_update_id": int(update_id)})
    except (OSError, ValueError) as exc:
        logger.warning(
            "offset_persistence.save_failed",
            path=str(path),
            update_id=update_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )


class DebouncedOffsetWriter:
    """Debounce update_id writes to amortise the fsync cost over polling.

    Under long-polling, each ``getUpdates`` batch can advance the offset
    by dozens of updates in a fraction of a second. Writing every bump
    works but is wasteful. This writer coalesces pending bumps and only
    flushes to disk when either:

    - ``min_interval_s`` has elapsed since the last flush, or
    - ``max_pending`` un-flushed advances have accumulated.

    On shutdown, call :meth:`flush` to force a final write.

    The risk of the debounce window is bounded: Telegram resends undelivered
    updates for 24 hours, so at worst a crash causes up to ``min_interval_s``
    worth of updates to be re-processed (message handlers are idempotent).
    """

    __slots__ = (
        "_last_flush",
        "_max_pending",
        "_min_interval_s",
        "_path",
        "_pending_count",
        "_pending_offset",
    )

    def __init__(
        self,
        path: Path,
        *,
        min_interval_s: float = 5.0,
        max_pending: int = 100,
    ) -> None:
        self._path = path
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._max_pending = max(1, int(max_pending))
        self._pending_offset: int | None = None
        self._pending_count = 0
        # Start the clock at construction so the first note is debounced
        # properly instead of firing an immediate write.
        self._last_flush = time.monotonic()

    def note(self, update_id: int) -> None:
        """Record that ``update_id`` has been acknowledged.

        The stored offset is the ``update_id`` of the most recently
        confirmed update. Callers typically want to store ``upd.update_id``
        directly; when resuming, use ``offset = saved + 1``.
        """
        self._pending_offset = update_id
        self._pending_count += 1
        now = time.monotonic()
        should_flush = self._pending_count >= self._max_pending or (
            now - self._last_flush >= self._min_interval_s
        )
        if should_flush:
            self._write(now)

    def flush(self) -> None:
        """Force a write of the pending offset (safe no-op if none pending)."""
        if self._pending_offset is not None:
            self._write(time.monotonic())

    def _write(self, now: float) -> None:
        if self._pending_offset is None:
            return
        save_last_update_id(self._path, self._pending_offset)
        self._pending_count = 0
        self._last_flush = now
