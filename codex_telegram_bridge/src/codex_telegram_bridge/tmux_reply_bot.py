#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown-it-py", "sulguk", "typer"]
# ///
from __future__ import annotations

import subprocess
import time
from typing import Optional

import typer

from .bridge_common import (
    TelegramClient,
    RouteStore,
    config_get,
    load_telegram_config,
    resolve_chat_ids,
)


def tmux_send_text(target: str, text: str, press_enter: bool = True) -> None:
    """
    Send text to tmux target pane/session.
    If your Telegram messages include newlines, we replace them with literal '\n'
    to avoid accidentally submitting early.
    """
    safe = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    subprocess.check_call(["tmux", "send-keys", "-t", target, "-l", safe])
    if press_enter:
        subprocess.check_call(["tmux", "send-keys", "-t", target, "Enter"])


def run(
    ignore_backlog: bool = typer.Option(
        True,
        "--ignore-backlog/--process-backlog",
        help="Skip pending Telegram updates that arrived before startup.",
    ),
) -> None:
    config = load_telegram_config()
    token = config_get(config, "bot_token") or ""
    db_path = config_get(config, "bridge_db") or "./bridge_routes.sqlite3"
    allowed = resolve_chat_ids(config)

    bot = TelegramClient(token)
    store = RouteStore(db_path)

    offset: Optional[int] = None
    ignore_backlog = bool(ignore_backlog)
    print("Option3 reply bot running (tmux injector). Long-polling Telegram...")

    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout_s=50, allowed_updates=["message"])
        except Exception as e:
            print(f"[telegram] get_updates error: {e}")
            time.sleep(2.0)
            continue

        if ignore_backlog:
            if updates:
                offset = updates[-1]["update_id"] + 1
                print(f"[startup] drained {len(updates)} pending update(s)")
                continue
            ignore_backlog = False

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if "text" not in msg:
                continue

            chat_id = msg["chat"]["id"]
            if allowed is not None and int(chat_id) not in allowed:
                continue
            if msg.get("from", {}).get("is_bot"):
                continue

            text = msg["text"]
            user_msg_id = msg["message_id"]

            r = msg.get("reply_to_message")
            if not (r and "message_id" in r):
                # In tmux mode we only route replies (no reply => ignore or treat as new session)
                bot.send_message(
                    chat_id=chat_id,
                    text="Reply to a Codex message (from the bot) so I know which tmux session to send this to.",
                    reply_to_message_id=user_msg_id,
                )
                continue

            route = store.resolve(chat_id, r["message_id"])
            if not route or route.route_type != "tmux":
                bot.send_message(
                    chat_id=chat_id,
                    text="I don't know which tmux session this reply belongs to (no routing record found).",
                    reply_to_message_id=user_msg_id,
                )
                continue

            tmux_target = route.route_id
            try:
                tmux_send_text(tmux_target, text, press_enter=True)
                bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Sent to tmux target: {tmux_target}",
                    reply_to_message_id=user_msg_id,
                )
            except Exception as e:
                bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Failed to send to tmux ({tmux_target}): {e}",
                    reply_to_message_id=user_msg_id,
                )


def main() -> None:
    typer.run(run)


if __name__ == "__main__":
    main()
