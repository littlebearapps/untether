from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from markdown_it import MarkdownIt
from sulguk import transform_html

TELEGRAM_HARD_LIMIT = 4096
DEFAULT_CHUNK_LEN = 3500  # leave room for formatting / safety
TELEGRAM_CONFIG_PATH = Path.home() / ".codex" / "telegram.toml"


def _now_unix() -> int:
    return int(time.time())


def _load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_telegram_config(path: Optional[str] = None) -> Dict[str, Any]:
    cfg_path = Path(path) if path else TELEGRAM_CONFIG_PATH
    return _load_toml(cfg_path)


def config_get(config: Dict[str, Any], key: str) -> Any:
    if key in config:
        return config[key]
    nested = config.get("telegram")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return None


def render_markdown(md: str) -> Tuple[str, List[Dict[str, Any]]]:
    html = MarkdownIt("commonmark", {"html": False}).render(md or "")
    rendered = transform_html(html)

    text = re.sub("(?m)^(\\s*)\u2022", r"\1-", rendered.text)

    # FIX: Telegram requires MessageEntity.language (if present) to be a String.
    entities: List[Dict[str, Any]] = []
    for e in rendered.entities:
        d = dict(e)
        if "language" in d and not isinstance(d["language"], str):
            d.pop("language", None)
        entities.append(d)
    return text, entities


def chunk_text(text: str, limit: int = DEFAULT_CHUNK_LEN) -> List[str]:
    """
    Telegram hard limit is 4096 chars. Chunk at newlines when possible.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]

    out: List[str] = []
    buf: List[str] = []
    size = 0

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            # flush current buffer
            if buf:
                out.append("".join(buf))
                buf, size = [], 0
            # hard-split this long line
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue

        if size + len(line) > limit:
            out.append("".join(buf))
            buf, size = [line], len(line)
        else:
            buf.append(line)
            size += len(line)

    if buf:
        out.append("".join(buf))
    return out


def _chunk_text_with_indices(text: str, limit: int) -> List[Tuple[str, int, int]]:
    text = text or ""
    if len(text) <= limit:
        return [(text, 0, len(text))]

    out: List[Tuple[str, int, int]] = []
    buf: List[str] = []
    size = 0
    buf_start = 0
    pos = 0

    for line in text.splitlines(keepends=True):
        line_len = len(line)
        line_start = pos
        line_end = pos + line_len

        if line_len > limit:
            if buf:
                out.append(("".join(buf), buf_start, line_start))
                buf, size = [], 0
            for i in range(0, line_len, limit):
                part = line[i : i + limit]
                out.append((part, line_start + i, line_start + i + len(part)))
            pos = line_end
            buf_start = pos
            continue

        if size + line_len > limit:
            out.append(("".join(buf), buf_start, line_start))
            buf = [line]
            size = line_len
            buf_start = line_start
        else:
            if not buf:
                buf_start = line_start
            buf.append(line)
            size += line_len

        pos = line_end

    if buf:
        out.append(("".join(buf), buf_start, pos))
    return out


def _slice_entities(entities: List[Dict[str, Any]], start: int, end: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ent in entities:
        try:
            ent_start = int(ent.get("offset", 0))
            ent_len = int(ent.get("length", 0))
        except (TypeError, ValueError):
            continue
        if ent_len <= 0:
            continue
        ent_end = ent_start + ent_len
        if ent_end <= start or ent_start >= end:
            continue
        new_start = max(ent_start, start)
        new_end = min(ent_end, end)
        new_len = new_end - new_start
        if new_len <= 0:
            continue
        new_ent = dict(ent)
        new_ent["offset"] = new_start - start
        new_ent["length"] = new_len
        out.append(new_ent)
    return out


class TelegramClient:
    """
    Minimal Telegram Bot API client using standard library (no requests dependency).
    """

    def __init__(self, token: str, timeout_s: int = 120) -> None:
        if not token:
            raise ValueError("Telegram token is empty")
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout_s = timeout_s

    def _call(self, method: str, params: Dict[str, Any]) -> Any:
        url = f"{self._base}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTPError {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram URLError: {e}") from e

        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload["result"]

    def get_updates(
        self,
        offset: Optional[int],
        timeout_s: int = 50,
        allowed_updates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout_s}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        return self._call("getUpdates", params)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        disable_notification: Optional[bool] = False,
        entities: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if len(text) > TELEGRAM_HARD_LIMIT:
            raise ValueError("send_message received too-long text; chunk it first")
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if disable_notification is not None:
            params["disable_notification"] = disable_notification
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if entities is not None:
            params["entities"] = entities
        return self._call("sendMessage", params)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if len(text) > TELEGRAM_HARD_LIMIT:
            raise ValueError("edit_message_text received too-long text")
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if entities is not None:
            params["entities"] = entities
        return self._call("editMessageText", params)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        res = self._call("deleteMessage", params)
        return bool(res)

    def send_message_chunked(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
        chunk_len: int = DEFAULT_CHUNK_LEN,
    ) -> List[Dict[str, Any]]:
        sent: List[Dict[str, Any]] = []
        chunks = chunk_text(text, limit=chunk_len)
        for i, c in enumerate(chunks):
            msg = self.send_message(
                chat_id=chat_id,
                text=c,
                reply_to_message_id=(reply_to_message_id if i == 0 else None),
                disable_notification=disable_notification,
            )
            sent.append(msg)
        return sent

    def send_message_markdown_chunked(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
        chunk_len: int = DEFAULT_CHUNK_LEN,
    ) -> List[Dict[str, Any]]:
        sent: List[Dict[str, Any]] = []
        rendered_text, entities = render_markdown(text)
        chunks = _chunk_text_with_indices(rendered_text, limit=chunk_len)
        for i, (chunk, start, end) in enumerate(chunks):
            chunk_entities = _slice_entities(entities, start, end) if entities else None
            msg = self.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=(reply_to_message_id if i == 0 else None),
                disable_notification=disable_notification,
                entities=chunk_entities,
            )
            sent.append(msg)
        return sent

    def send_chat_action(self, chat_id: int, action: str = "typing") -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "action": action,
        }
        return self._call("sendChatAction", params)


@dataclass(frozen=True)
class Route:
    route_type: str  # "exec" | "mcp" | "tmux"
    route_id: str    # session_id / conversationId / tmux target
    meta: Dict[str, Any]


class RouteStore:
    """
    Stores mapping: (chat_id, bot_message_id) -> route
    so Telegram replies can be routed.
    """

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routes (
              chat_id INTEGER NOT NULL,
              bot_message_id INTEGER NOT NULL,
              route_type TEXT NOT NULL,
              route_id TEXT NOT NULL,
              meta_json TEXT,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (chat_id, bot_message_id)
            );
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_routes_route_id ON routes(route_id);"
        )
        self._conn.commit()

    def link(
        self,
        chat_id: int,
        bot_message_id: int,
        route_type: str,
        route_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO routes(chat_id, bot_message_id, route_type, route_id, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (chat_id, bot_message_id, route_type, route_id, meta_json, _now_unix()),
        )
        self._conn.commit()

    def resolve(self, chat_id: int, bot_message_id: int) -> Optional[Route]:
        cur = self._conn.execute(
            """
            SELECT route_type, route_id, meta_json
            FROM routes
            WHERE chat_id = ? AND bot_message_id = ?
            """,
            (chat_id, bot_message_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        route_type, route_id, meta_json = row
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except json.JSONDecodeError:
            meta = {}
        return Route(route_type=route_type, route_id=route_id, meta=meta)

    def close(self) -> None:
        self._conn.close()


def parse_allowed_chat_ids(value: str) -> Optional[set[int]]:
    """
    Parse a comma-separated chat id string like "123,456".
    """
    v = (value or "").strip()
    if not v:
        return None
    out: set[int] = set()
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


def parse_chat_id_list(value: Any) -> Optional[set[int]]:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_allowed_chat_ids(value)
    if isinstance(value, int):
        return {value}
    if isinstance(value, (list, tuple, set)):
        out: set[int] = set()
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                if not item.strip():
                    continue
                out.add(int(item))
            else:
                out.add(int(item))
        return out or None
    return None


def resolve_chat_ids(config: Dict[str, Any]) -> Optional[set[int]]:
    chat_ids = parse_chat_id_list(config_get(config, "chat_id"))
    if chat_ids is None:
        chat_ids = parse_chat_id_list(config_get(config, "allowed_chat_ids"))
    if chat_ids is None:
        chat_ids = parse_chat_id_list(config_get(config, "startup_chat_ids"))
    return chat_ids
