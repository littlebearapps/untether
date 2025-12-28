#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown-it-py", "sulguk", "typer"]
# ///
from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from queue import Queue, Empty
from typing import Any, Dict, List, Optional, Tuple

import typer

from .bridge_common import (
    TelegramClient,
    RouteStore,
    config_get,
    load_telegram_config,
    resolve_chat_ids,
)

MCP_PROTOCOL_VERSION = "2025-06-18"


def _deep_find_agent_text(obj: Any) -> Optional[str]:
    """
    Heuristic: search nested dict/list for something like:
      {"type":"agent_message","text":"..."}
    """
    if isinstance(obj, dict):
        if obj.get("type") == "agent_message" and isinstance(obj.get("text"), str):
            return obj["text"]
        for v in obj.values():
            t = _deep_find_agent_text(v)
            if t is not None:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _deep_find_agent_text(it)
            if t is not None:
                return t
    return None


def _extract_text_from_tool_result(result: Any) -> str:
    """
    MCP tool results often look like: {"content":[{"type":"text","text":"..."}]}
    """
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "\n".join(parts).strip()


class MCPStdioClient:
    """
    Minimal MCP stdio JSON-RPC client:
      - spawns subprocess (codex mcp-server)
      - performs initialize + notifications/initialized
      - supports tools/list + tools/call
    """

    def __init__(self, cmd: List[str]) -> None:
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr
        self._inbox: "Queue[Dict[str, Any]]" = Queue()
        self._next_id = 1
        self._lock = threading.Lock()
        self._stderr_tail: List[str] = []

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

        self._initialize()

    def _read_stdout(self) -> None:
        for line in self._proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._inbox.put(msg)

    def _read_stderr(self) -> None:
        for line in self._proc.stderr:  # type: ignore[union-attr]
            self._stderr_tail.append(line)
            # keep last ~300 lines
            if len(self._stderr_tail) > 300:
                self._stderr_tail = self._stderr_tail[-300:]

    def _send(self, msg: Dict[str, Any]) -> None:
        raw = json.dumps(msg, ensure_ascii=False)
        self._proc.stdin.write(raw + "\n")  # type: ignore[union-attr]
        self._proc.stdin.flush()  # type: ignore[union-attr]

    def _request(self, method: str, params: Optional[Dict[str, Any]], timeout_s: int = 600) -> Any:
        """
        Send one request and wait for response.
        Sequential-only (guarded by self._lock).
        """
        with self._lock:
            req_id = self._next_id
            self._next_id += 1

            msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params

            self._send(msg)

            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"MCP request timed out: {method}")

                try:
                    incoming = self._inbox.get(timeout=min(1.0, remaining))
                except Empty:
                    # also check process
                    if self._proc.poll() is not None:
                        tail = "".join(self._stderr_tail)
                        raise RuntimeError(f"MCP server exited unexpectedly. stderr tail:\n{tail}")
                    continue

                if incoming.get("id") == req_id:
                    if "error" in incoming:
                        raise RuntimeError(f"MCP error response: {incoming['error']}")
                    return incoming.get("result")

                # ignore other messages here (we run sequential requests)
                # (notifications are handled by call_tool_collecting_events)

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _initialize(self) -> None:
        # MCP lifecycle: initialize then notifications/initialized.
        init_params = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "telegram-codex-bridge", "version": "0.1.0"},
        }
        self._request("initialize", init_params, timeout_s=30)
        # send initialized notification
        self._notify("notifications/initialized")
        return

    def tools_list(self) -> Any:
        return self._request("tools/list", {}, timeout_s=30)

    def call_tool_collecting_events(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout_s: int = 600,
    ) -> Tuple[Optional[str], str]:
        """
        Calls tools/call and collects notifications during the call.
        Returns (session_id, best_text_answer)
        """
        with self._lock:
            req_id = self._next_id
            self._next_id += 1

            msg = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            self._send(msg)

            deadline = time.monotonic() + timeout_s
            session_id: Optional[str] = None
            last_agent_text: Optional[str] = None
            final_result: Optional[Any] = None

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"tools/call timed out: {tool_name}")

                try:
                    incoming = self._inbox.get(timeout=min(1.0, remaining))
                except Empty:
                    if self._proc.poll() is not None:
                        tail = "".join(self._stderr_tail)
                        raise RuntimeError(f"MCP server exited unexpectedly. stderr tail:\n{tail}")
                    continue

                # Notifications (no id) can stream progress/events
                if "method" in incoming and "id" not in incoming:
                    if incoming.get("method") == "codex/event":
                        params = incoming.get("params") or {}
                        if isinstance(params, dict):
                            # Workaround: session_id can arrive in notification
                            sid = params.get("session_id")
                            if isinstance(sid, str) and sid:
                                session_id = sid

                            # Try to extract agent message text from event payload heuristically
                            t = _deep_find_agent_text(params)
                            if t:
                                last_agent_text = t
                    continue

                # Response for our request
                if incoming.get("id") == req_id:
                    if "error" in incoming:
                        raise RuntimeError(f"MCP tools/call error: {incoming['error']}")
                    final_result = incoming.get("result")
                    break

                # Ignore other responses (we do sequential calls)

            # Prefer last streamed agent message, else result.content text
            if last_agent_text:
                return session_id, last_agent_text

            text_from_result = _extract_text_from_tool_result(final_result)
            return session_id, (text_from_result or "(No agent_message found; tool result had no text.)")

    def close(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass


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

    # How to start Codex MCP server:
    # default: "codex mcp-server" (can also be "npx -y codex mcp-server")
    raw_mcp_cmd = config_get(config, "codex_mcp_cmd") or "codex mcp-server"
    if isinstance(raw_mcp_cmd, list):
        mcp_cmd = [str(v) for v in raw_mcp_cmd]
    else:
        mcp_cmd = shlex.split(str(raw_mcp_cmd))

    # Optional defaults for tool args (you can override as you like)
    default_cwd = config_get(config, "codex_workspace")
    default_sandbox = config_get(config, "codex_sandbox") or "workspace-write"
    default_approval = config_get(config, "codex_approval_policy") or "never"

    bot = TelegramClient(token)
    store = RouteStore(db_path)

    print(f"Starting MCP server: {mcp_cmd}")
    mcp = MCPStdioClient(mcp_cmd)

    # Optional: verify tools exist
    try:
        mcp.tools_list()
        # Not strictly required; but helpful for debugging
        print("tools/list ok")
    except Exception as e:
        print(f"tools/list failed: {e}")

    offset: Optional[int] = None
    ignore_backlog = bool(ignore_backlog)

    print("Option2 bridge running (codex mcp-server). Long-polling Telegram...")

    # Single worker queue so we never overlap tools/call
    work_q: "Queue[Tuple[int, int, str, Optional[str]]]" = Queue()

    def worker() -> None:
        while True:
            chat_id, user_msg_id, prompt, conversation_id = work_q.get()
            try:
                if conversation_id:
                    args = {"conversationId": conversation_id, "prompt": prompt}
                    sid, answer = mcp.call_tool_collecting_events("codex-reply", args, timeout_s=600)
                    # sid may be None on replies; keep conversation_id
                    sid = sid or conversation_id
                else:
                    args = {
                        "prompt": prompt,
                        "cwd": default_cwd,
                        "sandbox": default_sandbox,
                        "approval-policy": default_approval,
                    }
                    sid, answer = mcp.call_tool_collecting_events("codex", args, timeout_s=600)
                    if not sid:
                        # Worst-case fallback (still let user see output)
                        sid = "unknown-session"

                sent_msgs = bot.send_message_markdown_chunked(
                    chat_id=chat_id,
                    text=answer,
                    reply_to_message_id=user_msg_id,
                )
                for m in sent_msgs:
                    store.link(chat_id, m["message_id"], "mcp", sid, meta={"cwd": default_cwd})
            except Exception as e:
                err = f"❌ Error:\n{e}"
                sent_msgs = bot.send_message_markdown_chunked(
                    chat_id=chat_id,
                    text=err,
                    reply_to_message_id=user_msg_id,
                )
                for m in sent_msgs:
                    store.link(chat_id, m["message_id"], "mcp", conversation_id or "unknown", meta={"error": True})
            finally:
                work_q.task_done()

    threading.Thread(target=worker, daemon=True).start()

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

            prompt = msg["text"]
            user_msg_id = msg["message_id"]

            conversation_id: Optional[str] = None
            r = msg.get("reply_to_message")
            if r and "message_id" in r:
                route = store.resolve(chat_id, r["message_id"])
                if route and route.route_type == "mcp":
                    conversation_id = route.route_id

            work_q.put((chat_id, user_msg_id, prompt, conversation_id))


def main() -> None:
    typer.run(run)


if __name__ == "__main__":
    main()
