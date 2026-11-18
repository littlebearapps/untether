"""#1207: finals whose send certainly failed, kept until Telegram takes them.

A final's ``sendMessage`` that failed before it was sent, or with an HTTP 5xx
(D1: a rare duplicate after a 5xx beats a lost answer), is stored here with
the chunks still owed. It is sent again once polling works (``kick`` is called
after every successful ``getUpdates``) and at the next start, as a reply to
the original message and replacing the run's progress card. A send whose
outcome is unknown (a timeout) or that Telegram refused (4xx) is never stored:
it may already be in the chat, or would fail the same way again.

Entries live in ``pending_finals.json`` beside the config, so they survive a
restart. A corrupt file loads as empty with a WARNING.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from ..logging import get_logger
from ..transport import RenderedMessage, message_kind
from ..utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_FILENAME = "pending_finals.json"
MAX_ATTEMPTS = 5
MAX_AGE_S = 24 * 3600.0
# Least time between two redelivery passes (each failed pass is an attempt).
RETRY_SPACING_S = 30.0


def resolve_pending_finals_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)


def _pop(bot: Any, name: str, chat_id: int) -> bool:
    pop = getattr(bot, name, None)
    return bool(pop(chat_id)) if callable(pop) else False


class PendingFinals:
    def __init__(self, *, wall_clock: Callable[[], float] = time.time) -> None:
        self._wall_clock = wall_clock
        self._path: Path | None = None
        self._entries: dict[str, dict[str, Any]] = {}
        self._kicked = False

    def __len__(self) -> int:
        return len(self._entries)

    def attach(self, path: Path) -> None:
        """Persist to ``path``; entries left by a previous run are due now."""
        self._path = path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "pending_finals.load_failed", path=str(path), exc_info=True
                )
                data = None
            if isinstance(data, dict):
                for key, entry in data.items():
                    if isinstance(entry, dict) and entry.get("chunks"):
                        self._entries.setdefault(key, entry)
        self.kick()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            atomic_write_json(self._path, self._entries)
        except Exception:  # noqa: BLE001 — memory still holds the entries
            logger.warning(
                "pending_finals.save_failed", path=str(self._path), exc_info=True
            )

    def add(
        self,
        *,
        chat_id: int,
        chunks: list[RenderedMessage],
        reply_to_message_id: int | None,
        thread_id: int | None,
        replace_message_id: int | None,
        notify: bool,
    ) -> None:
        now = self._wall_clock()
        key = f"{chat_id}:{reply_to_message_id}:{time.time_ns()}"
        self._entries[key] = {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_message_id,
            "thread_id": thread_id,
            "replace_message_id": replace_message_id,
            "notify": notify,
            # Buttons are dropped: they would act on a finished run.
            "chunks": [
                {
                    "text": chunk.text,
                    "entities": chunk.extra.get("entities"),
                    "parse_mode": chunk.extra.get("parse_mode"),
                }
                for chunk in chunks
            ],
            "created_ts": now,
            "attempts": 0,
        }
        self._save()
        logger.warning(
            "final.pending_saved",
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            chunks=len(chunks),
        )

    def kick(self) -> None:
        """Telegram answered: try the pending finals on the next pass."""
        if self._entries:
            self._kicked = True

    async def run(self, bot: Any) -> None:
        """Background worker: one redelivery pass per kick, spaced out."""
        while True:
            if self._kicked:
                self._kicked = False
                try:
                    await self.redeliver(bot)
                except Exception:
                    logger.exception("pending_finals.redeliver_failed")
            await anyio.sleep(RETRY_SPACING_S)

    async def redeliver(self, bot: Any) -> None:
        for key in sorted(
            self._entries, key=lambda k: self._entries[k].get("created_ts", 0)
        ):
            await self._redeliver_one(bot, key, self._entries[key])

    async def _redeliver_one(self, bot: Any, key: str, entry: dict[str, Any]) -> None:
        chat_id = int(entry["chat_id"])
        age_s = self._wall_clock() - float(entry.get("created_ts", 0))
        if age_s >= MAX_AGE_S:
            await self._drop(bot, key, entry, event="final.redelivery_abandoned")
            return
        chunks: list[dict[str, Any]] = entry["chunks"]
        while chunks:
            chunk = chunks[0]
            with message_kind("final"):
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk["text"],
                    entities=chunk.get("entities"),
                    parse_mode=chunk.get("parse_mode"),
                    reply_to_message_id=entry.get("reply_to_message_id"),
                    message_thread_id=entry.get("thread_id"),
                    replace_message_id=entry.get("replace_message_id"),
                    disable_notification=not entry.get("notify", True),
                )
            if sent is not None:
                chunks.pop(0)
                entry["replace_message_id"] = None  # the card went with chunk 1
                self._save()
                continue
            if _pop(bot, "pop_resendable_send", chat_id):
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                if entry["attempts"] >= MAX_ATTEMPTS:
                    await self._drop(
                        bot, key, entry, event="final.redelivery_abandoned"
                    )
                else:
                    self._save()
                    logger.warning(
                        "final.redelivery_failed",
                        chat_id=chat_id,
                        attempts=entry["attempts"],
                    )
                return
            # Unknown (it may be in the chat now) or refused: never resend.
            unknown = _pop(bot, "pop_unconfirmed_send", chat_id)
            await self._drop(
                bot, key, entry, event="final.redelivery_dropped", unknown=unknown
            )
            return
        del self._entries[key]
        self._save()
        logger.info(
            "final.redelivered",
            chat_id=chat_id,
            age_s=round(age_s, 1),
            attempts=entry.get("attempts", 0),
        )

    async def _drop(
        self,
        bot: Any,
        key: str,
        entry: dict[str, Any],
        *,
        event: str,
        unknown: bool = False,
    ) -> None:
        from ..runner_bridge import _FINAL_FAILED_NOTICE, _FINAL_UNCONFIRMED_NOTICE

        del self._entries[key]
        self._save()
        log = logger.error if event == "final.redelivery_abandoned" else logger.warning
        log(
            event,
            chat_id=entry.get("chat_id"),
            attempts=entry.get("attempts", 0),
            unknown=unknown,
        )
        card = entry.get("replace_message_id")
        if card is None:
            return
        # The card may still say "working": finish it with the notice.
        with message_kind("progress"):
            await bot.edit_message_text(
                chat_id=int(entry["chat_id"]),
                message_id=int(card),
                text=_FINAL_UNCONFIRMED_NOTICE if unknown else _FINAL_FAILED_NOTICE,
                reply_markup={"inline_keyboard": []},
            )
