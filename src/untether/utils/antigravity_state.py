"""Small persisted state for the Antigravity runner (#558, REVIEW-2 B2/M14).

Both files live beside ``chat_prefs.json`` (the config file's directory):

- ``antigravity_seen_config.json`` — per project root, the digest of agy's
  executable workspace config an *attended* run last showed (first-sight ⚠️
  row). Unattended runs refuse while the current digest differs.
- ``antigravity_notices.json`` — chats that have seen the one-time Google
  sign-in (OAuth) ToS notice.

Without a config path (tests, ad-hoc runners) both stay in memory. A read or
write failure is logged and the store carries on in memory: losing it only
means a notice or warning is shown once more.

``antigravity_trust.json`` (#1032, :class:`TrustStore`) is different: it can
let Ask me run beside config that runs outside the gate, so it fails closed.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..logging import get_logger
from .json_state import atomic_write_json

logger = get_logger(__name__)

SEEN_CONFIG_FILENAME = "antigravity_seen_config.json"
NOTICES_FILENAME = "antigravity_notices.json"
TRUST_FILENAME = "antigravity_trust.json"
_MAX_PROJECTS = 500
_MAX_CHATS = 5000
_MAX_TRUST = 500


def _load(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "antigravity.state.load_failed",
            path=str(path),
            error_type=exc.__class__.__name__,
        )
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        atomic_write_json(path, payload)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "antigravity.state.save_failed",
            path=str(path),
            error_type=exc.__class__.__name__,
        )


class SeenConfigStore:
    """Project root → the agy-config digest an attended run last showed."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._loaded = False
        self._projects: dict[str, str] = {}

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = _load(self.path).get("projects")
        if isinstance(raw, dict):
            self._projects = {
                str(k): str(v) for k, v in raw.items() if isinstance(v, str)
            }

    def get(self, root: Path) -> str | None:
        self._ensure()
        return self._projects.get(str(root))

    def set(self, root: Path, digest: str) -> None:
        self._ensure()
        key = str(root)
        if self._projects.get(key) == digest:
            return
        self._projects.pop(key, None)
        self._projects[key] = digest
        while len(self._projects) > _MAX_PROJECTS:
            self._projects.pop(next(iter(self._projects)))
        _save(self.path, {"version": 1, "projects": self._projects})


class NoticeStore:
    """Chats that have already been shown the OAuth ToS notice."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._loaded = False
        self._chats: list[int] = []

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = _load(self.path).get("oauth_notice_chats")
        if isinstance(raw, list):
            self._chats = [c for c in raw if isinstance(c, int)]

    def seen(self, chat_id: int) -> bool:
        self._ensure()
        return chat_id in self._chats

    def mark(self, chat_id: int) -> None:
        self._ensure()
        if chat_id in self._chats:
            return
        self._chats.append(chat_id)
        del self._chats[:-_MAX_CHATS]
        _save(self.path, {"version": 1, "oauth_notice_chats": self._chats})


_FP_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """A chat admin accepted this agy config for Ask me (#1032)."""

    fp: str  # gate_trust.Fingerprint.digest of what the offer showed
    paths: tuple[str, ...]
    groups: tuple[str, ...]
    agy: str  # agy version
    at: float  # epoch seconds
    by: int | None  # Telegram sender id


def _strings(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return tuple(value)


def _trust_record(raw: Any) -> TrustRecord | None:
    """A stored record, or None when any field has the wrong type."""
    if not isinstance(raw, dict):
        return None
    fp, agy, at, by = raw.get("fp"), raw.get("agy"), raw.get("at"), raw.get("by")
    paths, groups = _strings(raw.get("paths")), _strings(raw.get("groups"))
    if not isinstance(fp, str) or not _FP_HEX.fullmatch(fp):
        return None
    if paths is None or groups is None or not isinstance(agy, str):
        return None
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    if by is not None and (isinstance(by, bool) or not isinstance(by, int)):
        return None
    return TrustRecord(fp=fp, paths=paths, groups=groups, agy=agy, at=at, by=by)


def _lstat_stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


class TrustStore:
    """``(chat id, project root realpath)`` → :class:`TrustRecord` (#1032).

    Fails closed, unlike the stores above: a missing, unreadable, corrupt,
    wrong-version or non-regular file (a symlink included) means **no
    records**, and a record with a wrong-typed field is dropped. ``set``
    returns False and changes nothing when the file can't be written, so a
    tap never reports "trusted" for something a restart would lose;
    ``revoke`` removes the record in memory even then. Re-read when the
    file changes (another process or a hand edit)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._records: dict[str, TrustRecord] = {}
        self._stamp: tuple[int, int, int] | None = None

    @staticmethod
    def key(chat_id: int, root: str | Path) -> str:
        return f"{chat_id}|{root}"

    def _failed(self, reason: str) -> None:
        self._records = {}
        self._stamp = None
        logger.warning(
            "antigravity.trust.load_failed", path=str(self.path), reason=reason
        )

    def _refresh(self) -> None:
        if self.path is None:
            return
        stamp = _lstat_stamp(self.path)
        if stamp is None:
            self._records, self._stamp = {}, None
            if os.path.lexists(self.path):
                self._failed("unstatable")
            return
        if stamp == self._stamp:
            return
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            self._failed(exc.__class__.__name__)  # a symlink: ELOOP
            return
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_ino != stamp[0]:
                self._failed("not_a_regular_file")
                return
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            self._failed(exc.__class__.__name__)
            return
        finally:
            os.close(fd)
        version = data.get("version") if isinstance(data, dict) else None
        raw = data.get("records") if type(version) is int and version == 1 else None
        if not isinstance(raw, dict):
            self._failed("wrong_shape")
            return
        records: dict[str, TrustRecord] = {}
        for key, value in raw.items():
            record = _trust_record(value)
            if record is None:
                logger.warning("antigravity.trust.record_dropped", key=str(key)[:200])
                continue
            records[str(key)] = record
        self._records, self._stamp = records, stamp

    def _write(self, records: dict[str, TrustRecord]) -> bool:
        if self.path is None:
            return True
        payload = {
            "version": 1,
            "records": {k: asdict(v) for k, v in records.items()},
        }
        try:
            atomic_write_json(self.path, payload)
            ok = True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "antigravity.trust.save_failed",
                path=str(self.path),
                error_type=exc.__class__.__name__,
            )
            ok = False
        # Memory is authoritative until somebody else changes the file.
        self._stamp = _lstat_stamp(self.path)
        return ok

    def get(self, chat_id: int, root: str | Path) -> TrustRecord | None:
        self._refresh()
        return self._records.get(self.key(chat_id, root))

    def set(self, chat_id: int, root: str | Path, record: TrustRecord) -> bool:
        self._refresh()
        key = self.key(chat_id, root)
        records = dict(self._records)
        records.pop(key, None)
        records[key] = record
        while len(records) > _MAX_TRUST:
            records.pop(next(iter(records)))
        if not self._write(records):
            return False
        self._records = records
        return True

    def revoke(self, chat_id: int, root: str | Path) -> bool:
        """True when a record was removed and the file saved."""
        self._refresh()
        key = self.key(chat_id, root)
        if key not in self._records:
            return False
        records = dict(self._records)
        del records[key]
        self._records = records
        return self._write(records)
