from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import anyio
import msgspec

from ..utils.json_state import atomic_write_json


class _Logger(Protocol):
    def warning(self, event: str, **fields: Any) -> None: ...


class _VersionedState(Protocol):
    version: int


class JsonStateStore[T: _VersionedState]:
    def __init__(
        self,
        path: Path,
        *,
        version: int,
        state_type: type[T],
        state_factory: Callable[[], T],
        log_prefix: str,
        logger: _Logger,
    ) -> None:
        self._path = path
        self._lock = anyio.Lock()
        self._loaded = False
        self._mtime_ns: int | None = None
        self._state_type = state_type
        self._state_factory = state_factory
        self._version = version
        self._log_prefix = log_prefix
        self._logger = logger
        self._state = state_factory()
        # #1381: called with (path, backup) after a quarantine, so the
        # transport can tell the admins their saved settings were reset.
        self.on_quarantine: Callable[[Path, Path], None] | None = None

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _reload_locked_if_needed(self) -> None:
        current = self._stat_mtime_ns()
        if self._loaded and current == self._mtime_ns:
            return
        self._load_locked()

    def _load_locked(self) -> None:
        mtime_ns = self._stat_mtime_ns()
        if mtime_ns is None:
            self._loaded = True
            self._mtime_ns = None
            self._state = self._state_factory()
            return
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            # #1277: a failed read (EIO, EMFILE, ...) says nothing about the
            # file. Keep the last good state, retry on the next access and
            # refuse saves until then, so the file is never overwritten.
            self._loaded = False
            self._log_load_failed(exc)
            return
        try:
            payload = msgspec.json.decode(raw, type=self._state_type)
        except Exception as exc:  # noqa: BLE001
            self._log_load_failed(exc)
            self._quarantine_locked()
            return
        if payload.version != self._version:
            self._logger.warning(
                f"{self._log_prefix}.version_mismatch",
                path=str(self._path),
                version=payload.version,
                expected=self._version,
            )
            self._quarantine_locked()
            return
        self._loaded = True
        self._mtime_ns = mtime_ns
        self._state = payload

    def _log_load_failed(self, exc: Exception) -> None:
        self._logger.warning(
            f"{self._log_prefix}.load_failed",
            path=str(self._path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    def _quarantine_locked(self) -> None:
        """#1277: move an undecodable (or other-version) file aside before
        starting fresh, so its bytes survive for a manual restore."""
        backup = self._path.with_name(f"{self._path.name}.corrupt-{time.time_ns()}")
        try:
            os.replace(self._path, backup)
        except OSError as exc:
            # Can't move it: stay unloaded (saves refused) rather than
            # overwrite the only copy.
            self._loaded = False
            self._log_load_failed(exc)
            return
        self._logger.warning(
            f"{self._log_prefix}.quarantined", path=str(self._path), backup=str(backup)
        )
        self._loaded = True
        self._mtime_ns = None
        self._state = self._state_factory()
        if self.on_quarantine is not None:
            try:
                self.on_quarantine(self._path, backup)
            except Exception as exc:  # noqa: BLE001 — a notice must not break reads
                self._logger.warning(
                    f"{self._log_prefix}.quarantine_notice_failed", error=str(exc)
                )

    def _save_locked(self) -> None:
        if not self._loaded:
            # #1277: the last read failed, so in-memory state may be stale or
            # empty; writing it would wipe the file.
            self._logger.warning(
                f"{self._log_prefix}.save_skipped", path=str(self._path)
            )
            return
        payload = msgspec.to_builtins(self._state)
        atomic_write_json(self._path, payload)
        self._mtime_ns = self._stat_mtime_ns()
