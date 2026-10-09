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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging import get_logger
from .json_state import atomic_write_json

logger = get_logger(__name__)

SEEN_CONFIG_FILENAME = "antigravity_seen_config.json"
NOTICES_FILENAME = "antigravity_notices.json"
_MAX_PROJECTS = 500
_MAX_CHATS = 5000


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
