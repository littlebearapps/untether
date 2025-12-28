#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown-it-py", "sulguk", "typer"]
# ///
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Tuple

import typer

from .bridge_common import (
    TelegramClient,
    RouteStore,
    TELEGRAM_HARD_LIMIT,
    config_get,
    load_telegram_config,
    render_markdown,
    resolve_chat_ids,
)

# -------------------- Codex runner --------------------


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _one_line(text: Optional[str]) -> str:
    if text is None:
        return "None"
    return text.replace("\r", "\\r").replace("\n", "\\n")


TELEGRAM_TEXT_LIMIT = TELEGRAM_HARD_LIMIT


def _clamp_tg_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...(truncated)"


def _summarize_item(item: Dict[str, Any]) -> str:
    item_type = item.get("type")
    if isinstance(item_type, str):
        if item_type == "agent_message" and isinstance(item.get("text"), str):
            snippet = item["text"].strip().replace("\n", " ")
            if len(snippet) > 140:
                snippet = snippet[:140] + "..."
            return f"agent_message: {snippet}"
        name = item.get("name") or item.get("tool_name") or item.get("id")
        if isinstance(name, str):
            return f"{item_type}: {name}"
        return item_type
    return "item.completed"


class ProgressEditor:
    def __init__(
        self,
        bot: TelegramClient,
        chat_id: int,
        message_id: int,
        edit_every_s: float,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.edit_every_s = edit_every_s

        self._lock = threading.Lock()
        self._pending: Optional[str] = None
        self._last_sent: Optional[str] = None
        self._last_edit_at = 0.0

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set(self, text: str) -> None:
        text = _clamp_tg_text(text)
        with self._lock:
            self._pending = text

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _edit(self, text: str) -> None:
        try:
            self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
        except Exception as e:
            log(
                "[progress] edit failed "
                f"chat_id={self.chat_id} message_id={self.message_id}: {e}"
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            to_send: Optional[str] = None
            now = time.monotonic()
            with self._lock:
                if self._pending is not None and (now - self._last_edit_at) >= self.edit_every_s:
                    if self._pending != self._last_sent:
                        to_send = self._pending
                        self._last_sent = self._pending
                        self._last_edit_at = now
                    self._pending = None

            if to_send is not None:
                self._edit(to_send)

            self._stop.wait(0.2)


def _typing_loop(bot: TelegramClient, chat_id: int, stop_evt: threading.Event) -> None:
    while not stop_evt.is_set():
        try:
            bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as e:
            log(f"[typing] send_chat_action failed chat_id={chat_id}: {e}")
        stop_evt.wait(4.0)


class CodexExecRunner:
    """
    Runs Codex in non-interactive mode:
      - new:    codex exec --json ... -
      - resume: codex exec --json ... resume <SESSION_ID> -
    """

    def __init__(self, codex_cmd: str, workspace: Optional[str], extra_args: list[str]) -> None:
        self.codex_cmd = codex_cmd
        self.workspace = workspace
        self.extra_args = extra_args

        # per-session locks to prevent concurrent resumes to same session_id
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def run(
        self,
        prompt: str,
        session_id: Optional[str],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[str, str]:
        """
        Returns (session_id, final_agent_message_text)
        """
        log(f"[codex] start run session_id={session_id!r} workspace={self.workspace!r}")
        args = [self.codex_cmd, "exec", "--json"]
        args.extend(self.extra_args)
        if self.workspace:
            args.extend(["--cd", self.workspace])

        # Always pipe prompt via stdin ("-") to avoid quoting issues.
        if session_id:
            args.extend(["resume", session_id, "-"])
        else:
            args.append("-")

        # read both stdout+stderr without deadlock
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdin and proc.stdout and proc.stderr

        # send prompt then close stdin
        proc.stdin.write(prompt)
        proc.stdin.close()

        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            for line in proc.stderr:
                log(f"[codex][stderr] {line.rstrip()}")
                stderr_lines.append(line)

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        found_session: Optional[str] = session_id
        last_agent_text: Optional[str] = None

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            log(f"[codex][event] {line}")
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if on_event is not None:
                try:
                    on_event(evt)
                except Exception as e:
                    log(f"[codex][on_event] callback error: {e}")

            # From Codex JSONL event stream
            if evt.get("type") == "thread.started":
                found_session = evt.get("thread_id") or found_session

            if evt.get("type") == "item.completed":
                item = evt.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    last_agent_text = item["text"]

        rc = proc.wait()
        t.join(timeout=2.0)

        if rc != 0:
            tail = "".join(stderr_lines[-200:])
            raise RuntimeError(f"codex exec failed (rc={rc}). stderr tail:\n{tail}")

        if not found_session:
            raise RuntimeError("codex exec finished but no session_id/thread_id was captured")

        log(f"[codex] done run session_id={found_session!r}")
        return found_session, (last_agent_text or "(No agent_message captured from JSON stream.)")

    def run_serialized(
        self,
        prompt: str,
        session_id: Optional[str],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[str, str]:
        """
        If resuming, serialize per-session.
        """
        if not session_id:
            return self.run(prompt, session_id=None, on_event=on_event)
        lock = self._lock_for(session_id)
        with lock:
            return self.run(prompt, session_id=session_id, on_event=on_event)


# -------------------- Telegram loop --------------------


def run(
    progress_edit_every_s: float = typer.Option(
        2.5,
        "--progress-edit-every",
        help="Minimum seconds between progress message edits.",
        min=0.1,
    ),
    progress_silent: bool = typer.Option(
        True,
        "--progress-silent/--no-progress-silent",
        help="Send the progress message without sound/vibration.",
    ),
    final_notify: bool = typer.Option(
        True,
        "--final-notify/--no-final-notify",
        help="Send the final response as a new message (not an edit).",
    ),
    ignore_backlog: bool = typer.Option(
        True,
        "--ignore-backlog/--process-backlog",
        help="Skip pending Telegram updates that arrived before startup.",
    ),
) -> None:
    config = load_telegram_config()
    token = config_get(config, "bot_token") or ""
    db_path = config_get(config, "bridge_db") or "./bridge_routes.sqlite3"
    chat_ids = resolve_chat_ids(config)
    allowed = chat_ids
    startup_ids = chat_ids
    startup_msg = config_get(config, "startup_message") or "✅ exec_bridge started (codex exec)."
    startup_pwd = os.getcwd()
    startup_msg = f"{startup_msg}\nPWD: {startup_pwd}"

    codex_cmd = config_get(config, "codex_cmd") or "codex"
    workspace = config_get(config, "codex_workspace")
    raw_exec_args = config_get(config, "codex_exec_args") or ""
    if isinstance(raw_exec_args, list):
        extra_args = [str(v) for v in raw_exec_args]
    else:
        extra_args = shlex.split(str(raw_exec_args))  # e.g. "--full-auto --search"

    def _has_notify_override(args: list[str]) -> bool:
        for i, arg in enumerate(args):
            if arg in ("-c", "--config"):
                if i + 1 >= len(args):
                    continue
                key = args[i + 1].split("=", 1)[0].strip()
                if key == "notify" or key.endswith(".notify"):
                    return True
            elif arg.startswith(("--config=", "-c=")):
                key = arg.split("=", 1)[1].split("=", 1)[0].strip()
                if key == "notify" or key.endswith(".notify"):
                    return True
        return False

    # Default: disable notify hook for exec-bridge runs to avoid duplicate messages.
    if not _has_notify_override(extra_args):
        extra_args.extend(["-c", "notify=[]"])

    bot = TelegramClient(token)
    store = RouteStore(db_path)
    runner = CodexExecRunner(codex_cmd=codex_cmd, workspace=workspace, extra_args=extra_args)

    max_workers = config_get(config, "max_workers")
    if isinstance(max_workers, str):
        max_workers = int(max_workers) if max_workers.strip() else None
    elif not isinstance(max_workers, int):
        max_workers = None
    pool = ThreadPoolExecutor(max_workers=max_workers or 4)
    offset: Optional[int] = None
    ignore_backlog = bool(ignore_backlog)

    log(f"[startup] pwd={startup_pwd}")
    log("Option1 bridge running (codex exec). Long-polling Telegram...")
    if startup_ids:
        for chat_id in startup_ids:
            try:
                bot.send_message(chat_id=chat_id, text=startup_msg)
                log(f"[startup] sent startup message to chat_id={chat_id}")
            except Exception as e:
                log(f"[startup] failed to send startup message to chat_id={chat_id}: {e}")
    else:
        log("[startup] no chat_id configured; skipping startup message")

    def handle(chat_id: int, user_msg_id: int, text: str, resume_session: Optional[str]) -> None:
        log(
            "[handle] start "
            f"chat_id={chat_id} user_msg_id={user_msg_id} resume_session={resume_session!r}"
        )
        edit_every_s = progress_edit_every_s
        silent_progress = progress_silent
        loud_final = final_notify

        typing_stop = threading.Event()
        typing_thread = threading.Thread(
            target=_typing_loop,
            args=(bot, chat_id, typing_stop),
            daemon=True,
        )
        typing_thread.start()

        progress_id: Optional[int] = None
        progress: Optional[ProgressEditor] = None
        try:
            progress_msg = bot.send_message(
                chat_id=chat_id,
                text="Working...",
                reply_to_message_id=user_msg_id,
                disable_notification=silent_progress,
            )
            progress_id = int(progress_msg["message_id"])
        except Exception as e:
            log(f"[handle] failed to send progress message chat_id={chat_id}: {e}")

        if progress_id is not None:
            progress = ProgressEditor(bot, chat_id, progress_id, edit_every_s)

        started_at = time.monotonic()
        session_box: dict[str, Optional[str]] = {"id": resume_session}

        def on_event(evt: Dict[str, Any]) -> None:
            event_type = evt.get("type")
            if event_type == "thread.started":
                thread_id = evt.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    session_box["id"] = thread_id
                    if progress_id is not None:
                        store.link(
                            chat_id,
                            progress_id,
                            "exec",
                            thread_id,
                            meta={"workspace": workspace},
                        )
            elif event_type == "item.completed":
                item = evt.get("item") or {}
                elapsed = int(time.monotonic() - started_at)
                line = _summarize_item(item) if isinstance(item, dict) else "item.completed"
                session_id = session_box["id"]
                header = f"Working... ({elapsed}s)"
                if session_id:
                    header += f"\nSession: {session_id}"
                msg = f"{header}\n\n{line}"
                if progress is not None:
                    progress.set(msg)

        def _stop_background() -> None:
            typing_stop.set()
            typing_thread.join(timeout=1.0)
            if progress is not None:
                progress.stop()

        try:
            session_id, answer = runner.run_serialized(text, resume_session, on_event=on_event)
        except Exception as e:
            _stop_background()
            err = _clamp_tg_text(f"Error:\n{e}")
            route_id = session_box["id"] or resume_session or "unknown"
            if progress_id is not None and len(err) <= TELEGRAM_TEXT_LIMIT:
                try:
                    bot.edit_message_text(chat_id=chat_id, message_id=progress_id, text=err)
                    store.link(chat_id, progress_id, "exec", route_id, meta={"error": True})
                    log(
                        "[handle] error "
                        f"chat_id={chat_id} user_msg_id={user_msg_id} "
                        f"resume_session={resume_session!r} err={e}"
                    )
                    return
                except Exception as ee:
                    log(f"[handle] failed to edit progress into error: {ee}")

            sent_msgs = bot.send_message_markdown_chunked(
                chat_id=chat_id,
                text=err,
                reply_to_message_id=user_msg_id,
            )
            for m in sent_msgs:
                store.link(chat_id, m["message_id"], "exec", route_id, meta={"error": True})
            log(
                "[handle] error "
                f"chat_id={chat_id} user_msg_id={user_msg_id} resume_session={resume_session!r} err={e}"
            )
            return

        _stop_background()

        answer = answer or "(No agent_message captured from JSON stream.)"
        final_text, final_entities = render_markdown(answer)
        can_edit_final = progress_id is not None and len(final_text) <= TELEGRAM_TEXT_LIMIT

        if loud_final or not can_edit_final:
            if progress_id is not None:
                try:
                    bot.delete_message(chat_id=chat_id, message_id=progress_id)
                except Exception as e:
                    log(f"[handle] delete progress failed chat_id={chat_id} message_id={progress_id}: {e}")

            sent_msgs = bot.send_message_markdown_chunked(
                chat_id=chat_id,
                text=answer,
                reply_to_message_id=user_msg_id,
            )
            for m in sent_msgs:
                store.link(chat_id, m["message_id"], "exec", session_id, meta={"workspace": workspace})
        else:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_id,
                text=final_text,
                entities=final_entities or None,
            )
            store.link(chat_id, progress_id, "exec", session_id, meta={"workspace": workspace})

        log(
            "[handle] done "
            f"chat_id={chat_id} user_msg_id={user_msg_id} session_id={session_id!r}"
        )

    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout_s=50, allowed_updates=["message"])
        except Exception as e:
            log(f"[telegram] get_updates error: {e}")
            time.sleep(2.0)
            continue

        if ignore_backlog:
            if updates:
                offset = updates[-1]["update_id"] + 1
                log(f"[startup] drained {len(updates)} pending update(s)")
                continue
            ignore_backlog = False

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = msg.get("chat", {}).get("id")
            from_bot = msg.get("from", {}).get("is_bot")
            msg_text = msg.get("text")
            reply_to = (msg.get("reply_to_message") or {}).get("message_id")
            log(
                "[telegram] received "
                f"update_id={upd.get('update_id')} chat_id={chat_id} "
                f"from_bot={from_bot} has_text={msg_text is not None} "
                f"reply_to={reply_to} text={_one_line(msg_text)}"
            )
            if "text" not in msg:
                log(
                    "[telegram] ignoring non-text message "
                    f"chat_id={chat_id} update_id={upd.get('update_id')}"
                )
                continue

            if allowed is not None and int(chat_id) not in allowed:
                log(
                    "[telegram] rejected by ACL "
                    f"chat_id={chat_id} allowed={sorted(allowed)}"
                )
                continue

            if msg.get("from", {}).get("is_bot"):
                log(
                    "[telegram] ignoring bot message "
                    f"chat_id={chat_id} update_id={upd.get('update_id')}"
                )
                continue

            text = msg["text"]
            user_msg_id = msg["message_id"]
            log(
                "[telegram] accepted message "
                f"chat_id={chat_id} user_msg_id={user_msg_id} text={_one_line(text)}"
            )

            # If user replied to a bot message, route to that session
            resume_session: Optional[str] = None
            r = msg.get("reply_to_message")
            if r and "message_id" in r:
                route = store.resolve(chat_id, r["message_id"])
                if route and route.route_type == "exec":
                    resume_session = route.route_id
                    log(
                        "[telegram] resolved reply route "
                        f"chat_id={chat_id} bot_message_id={r['message_id']} session_id={resume_session!r}"
                    )
                else:
                    log(
                        "[telegram] reply has no exec route "
                        f"chat_id={chat_id} bot_message_id={r['message_id']}"
                    )

            pool.submit(handle, chat_id, user_msg_id, text, resume_session)


def main() -> None:
    typer.run(run)


if __name__ == "__main__":
    main()
