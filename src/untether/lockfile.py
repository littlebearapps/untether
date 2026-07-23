from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LockInfo:
    pid: int | None
    token_fingerprint: str | None


class LockError(RuntimeError):
    def __init__(
        self,
        *,
        path: Path,
        state: str,
    ) -> None:
        self.path = path
        self.state = state
        super().__init__(_format_lock_message(path, state))


@dataclass(slots=True)
class LockHandle:
    """Holds an exclusive ``flock(2)`` on the lock file for the process lifetime.

    The kernel releases the lock automatically when the holding process exits
    (#459: this eliminates the PID-reuse race that the old ``os.kill(pid, 0)``
    liveness check suffered — a reused PID could make a stale lock look valid
    forever). ``fd`` is kept open until :meth:`release` (or process death);
    it MUST stay non-inheritable so the lock can't leak into a spawned engine
    subprocess that outlives us — ``os.open`` returns a non-inheritable fd by
    default (PEP 446), so do NOT ``os.set_inheritable(fd, True)``.
    """

    path: Path
    fd: int

    def release(self) -> None:
        # Release the advisory lock and close the descriptor. The file itself
        # is intentionally NOT unlinked: removing it would let another process
        # create+lock a fresh file at the same path while a third still holds
        # an flock on the now-orphaned inode (the classic open-then-flock race).
        # A leftover lock file with no live flock is harmless — the next
        # acquire just re-locks it.
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError as exc:
            logger.warning(
                "lock.release.failed",
                path=str(self.path),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(self.fd)

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def token_fingerprint(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:10]


def lock_path_for_config(config_path: Path) -> Path:
    return config_path.with_suffix(".lock")


def acquire_lock(
    *, config_path: Path, token_fingerprint: str | None = None
) -> LockHandle:
    cfg_path = config_path.expanduser().resolve()
    lock_path = lock_path_for_config(cfg_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT so the file appears on first run; O_RDWR so we can stamp
        # diagnostics. The fd is non-inheritable by default (PEP 446) — keep
        # it that way (see LockHandle docstring).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise LockError(path=lock_path, state=str(exc)) from exc

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another live process holds the lock. The kernel guarantees this is a
        # real, running holder — no PID inference needed.
        os.close(fd)
        raise LockError(path=lock_path, state="running") from None
    except OSError as exc:
        os.close(fd)
        raise LockError(path=lock_path, state=str(exc)) from exc

    # We hold the lock. Stamp pid + fingerprint into the file purely for human
    # debugging (`cat untether.lock`); they're no longer used for liveness.
    try:
        payload = json.dumps(
            {"pid": os.getpid(), "token_fingerprint": token_fingerprint},
            indent=2,
            sort_keys=True,
        )
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (payload + "\n").encode("utf-8"))
    except OSError as exc:
        # Stamping is best-effort; the lock itself is already held. Log and
        # keep going rather than dropping the lock over a diagnostics write.
        logger.warning(
            "lock.stamp.failed",
            path=str(lock_path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    return LockHandle(path=lock_path, fd=fd)


def _read_lock_info(path: Path) -> LockInfo | None:
    """Read the diagnostic pid/fingerprint stamp. No longer used for locking
    decisions (flock handles liveness) — retained for tooling/tests."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    token_hint = data.get("token_fingerprint")
    if not isinstance(token_hint, str):
        token_hint = None
    return LockInfo(
        pid=pid,
        token_fingerprint=token_hint,
    )


def _format_lock_message(path: Path, state: str) -> str:
    if state != "running":
        return f"error: lock failed: {state}"
    header = "error: already running"
    display_path = _display_lock_path(path)
    # flock auto-releases when the holder dies, so manual `rm` is no longer the
    # recovery path — surface the lock file only for diagnostics.
    lines = [
        header,
        f"another untether process holds the lock on {display_path}",
        "(the lock auto-releases when that process exits)",
    ]
    return "\n".join(lines)


def _display_lock_path(path: Path) -> str:
    home = Path.home()
    try:
        resolved = path.expanduser().resolve()
        rel = resolved.relative_to(home)
        return f"~/{rel}"
    except (ValueError, OSError):
        return str(path)
