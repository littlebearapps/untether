#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown-it-py", "sulguk", "typer"]
# ///
from __future__ import annotations

import sys
from typing import Optional

import typer

from .bridge_common import TelegramClient, RouteStore, config_get, load_telegram_config


def run(
    chat_id: Optional[int] = typer.Option(
        None,
        "--chat-id",
        help="Telegram chat id (defaults to chat_id in ~/.codex/telegram.toml).",
    ),
    tmux_target: str = typer.Option(
        ...,
        "--tmux-target",
        help='tmux target, e.g. "codex1:0.0" or "codex1"',
    ),
    db: Optional[str] = typer.Option(
        None,
        "--db",
        help="Path to the routing database.",
    ),
    reply_to: Optional[int] = typer.Option(
        None,
        "--reply-to",
        help="Optional Telegram message_id to reply to.",
    ),
    text: Optional[str] = typer.Option(
        None,
        "--text",
        help="Message text. If omitted, read stdin.",
    ),
) -> None:
    config = load_telegram_config()
    default_chat_id = config_get(config, "chat_id")
    if isinstance(default_chat_id, str):
        default_chat_id = int(default_chat_id) if default_chat_id.strip() else None
    elif not isinstance(default_chat_id, int):
        default_chat_id = None
    if chat_id is None:
        chat_id = default_chat_id
    if chat_id is None:
        raise typer.BadParameter(
            "chat_id is required (pass --chat-id or set chat_id in ~/.codex/telegram.toml)."
        )
    if db is None:
        db = config_get(config, "bridge_db") or "./bridge_routes.sqlite3"

    token = config_get(config, "bot_token") or ""
    bot = TelegramClient(token)
    store = RouteStore(db)

    if text is None:
        text = sys.stdin.read()

    sent = bot.send_message_markdown_chunked(
        chat_id=chat_id,
        text=text,
        reply_to_message_id=reply_to,
    )

    # Store mapping for every chunk so user can reply to any chunk.
    for m in sent:
        store.link(chat_id, m["message_id"], "tmux", tmux_target, meta={})


def main() -> None:
    typer.run(run)


if __name__ == "__main__":
    main()
