from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from textwrap import indent
from typing import Any, Optional

ELLIPSIS = "…"
STATUS_RUNNING = "▸"
STATUS_DONE = "✓"
HEADER_SEP = " · "
HARD_BREAK = "  \n"

MAX_CMD_LEN = 40
MAX_REASON_LEN = 80
MAX_QUERY_LEN = 60
MAX_PATH_LEN = 40
MAX_PROGRESS_CHARS = 300


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, max_len: int) -> str:
    text = _one_line(text)
    if len(text) <= max_len:
        return text
    if max_len <= len(ELLIPSIS):
        return text[:max_len]
    return text[: max_len - len(ELLIPSIS)] + ELLIPSIS


def _inline_code(text: str) -> str:
    return f"`{text}`"


def _format_elapsed(elapsed_s: float) -> str:
    total = max(0, int(elapsed_s))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _format_header(elapsed_s: float, turn: Optional[int], label: str) -> str:
    elapsed = _format_elapsed(elapsed_s)
    if turn is not None:
        return f"{label}{HEADER_SEP}{elapsed}{HEADER_SEP}turn {turn}"
    return f"{label}{HEADER_SEP}{elapsed}"


def _format_reasoning(text: str) -> str:
    if not text:
        return ""
    return f"_{_truncate(text, MAX_REASON_LEN)}_"


def _format_command(command: str) -> str:
    command = _truncate(command, MAX_CMD_LEN)
    if not command:
        command = "(empty)"
    return _inline_code(command)


def _format_query(query: str) -> str:
    return _truncate(query, MAX_QUERY_LEN)


def _format_paths(paths: list[str]) -> str:
    rendered = []
    for path in paths:
        rendered.append(_inline_code(_truncate(path, MAX_PATH_LEN)))
    return ", ".join(rendered)


def _format_file_change(changes: list[dict[str, Any]]) -> str:
    paths = [change.get("path") for change in changes if change.get("path")]
    if not paths:
        total = len(changes)
        return "updated files" if total == 0 else f"updated {total} files"
    if len(paths) <= 3:
        return f"updated {_format_paths(paths)}"
    return f"updated {len(paths)} files"


def _format_tool_call(server: str, tool: str) -> str:
    name = ".".join(part for part in (server, tool) if part)
    return name or "tool"

def _is_command_log_line(line: str) -> bool:
    return f"{STATUS_DONE} ran:" in line


def _extract_numeric_id(item_id: Optional[object], fallback: Optional[int] = None) -> Optional[int]:
    if isinstance(item_id, int):
        return item_id
    if isinstance(item_id, str):
        match = re.search(r"(?:item_)?(\d+)", item_id)
        if match:
            return int(match.group(1))
    return fallback


def _with_id(item_id: Optional[int], line: str) -> str:
    if item_id is not None:
        return f"[{item_id}] {line}"
    return f"[?] {line}"


def _truncate_output(text: str, max_lines: int = 20, max_chars: int = 4000) -> str:
    if len(text) > max_chars:
        text = text[-max_chars:]
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = ["..."] + lines[-max_lines:]
    return "\n".join(lines)


def _format_item_action_line(etype: str, item_id: Optional[int], item: dict[str, Any]) -> str | None:
    itype = item["type"]
    if itype == "command_execution":
        command = _format_command(item["command"])
        if etype == "item.started":
            return _with_id(item_id, f"{STATUS_RUNNING} running: {command}")
        if etype == "item.completed":
            exit_code = item["exit_code"]
            exit_part = f" (exit {exit_code})" if exit_code is not None else ""
            return _with_id(item_id, f"{STATUS_DONE} ran: {command}{exit_part}")
        return None

    if itype == "mcp_tool_call":
        name = _format_tool_call(item["server"], item["tool"])
        if etype == "item.started":
            return _with_id(item_id, f"{STATUS_RUNNING} tool: {name}")
        if etype == "item.completed":
            return _with_id(item_id, f"{STATUS_DONE} tool: {name}")
        return None

    return None


def _format_item_completed_line(item_id: Optional[int], item: dict[str, Any]) -> str | None:
    itype = item["type"]
    if itype == "web_search":
        query = _format_query(item["query"])
        return _with_id(item_id, f"{STATUS_DONE} searched: {query}")
    if itype == "file_change":
        return _with_id(item_id, f"{STATUS_DONE} {_format_file_change(item['changes'])}")
    if itype == "error":
        warning = _truncate(item["message"], 120)
        return _with_id(item_id, f"{STATUS_DONE} warning: {warning}")
    return None


@dataclass
class ExecRenderState:
    recent_actions: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    current_action: Optional[str] = None
    current_action_id: Optional[int] = None
    pending_reasoning: Optional[str] = None
    current_reasoning: Optional[str] = None
    last_turn: Optional[int] = None


def _record_item(state: ExecRenderState, item: dict[str, Any]) -> None:
    numeric_id = _extract_numeric_id(item["id"])
    if numeric_id is not None:
        state.last_turn = numeric_id


def _set_current_action(state: ExecRenderState, item_id: Optional[int], line: str) -> bool:
    changed = False
    if state.current_action != line or state.current_action_id != item_id:
        state.current_action = line
        state.current_action_id = item_id
        if state.pending_reasoning:
            state.current_reasoning = state.pending_reasoning
            state.pending_reasoning = None
        changed = True
    return changed


def _complete_action(state: ExecRenderState, item_id: Optional[int], line: str) -> bool:
    changed = False
    if state.current_reasoning:
        if not state.recent_actions or state.recent_actions[-1] != state.current_reasoning:
            state.recent_actions.append(state.current_reasoning)
            changed = True
    if line:
        state.recent_actions.append(line)
        changed = True
    if item_id and state.current_action_id == item_id:
        state.current_action = None
        state.current_action_id = None
        state.current_reasoning = None
        changed = True
    if not item_id and state.current_action_id is None:
        state.current_reasoning = None
    return changed


def render_event_cli(
    event: dict[str, Any],
    state: ExecRenderState,
    *,
    show_output: bool = False,
) -> list[str]:
    etype = event["type"]
    lines: list[str] = []

    if etype == "thread.started":
        return ["thread started"]

    if etype == "turn.started":
        return ["turn started"]

    if etype == "turn.completed":
        return ["turn completed"]

    if etype == "turn.failed":
        error = event["error"]["message"]
        return [f"turn failed: {error}"]

    if etype == "error":
        return [f"stream error: {event['message']}"]

    if etype in {"item.started", "item.updated", "item.completed"}:
        item = event["item"]
        _record_item(state, item)

        itype = item["type"]
        item_num = _extract_numeric_id(item["id"], state.last_turn)

        if itype == "agent_message" and etype == "item.completed":
            lines.append("assistant:")
            lines.extend(indent(item["text"], "  ").splitlines())

        else:
            action_line = _format_item_action_line(etype, item_num, item)
            if action_line is not None:
                lines.append(action_line)
                if show_output and itype == "command_execution" and etype == "item.completed":
                    output = _truncate_output(item["aggregated_output"])
                    if output:
                        lines.extend(indent(output, "  ").splitlines())
            elif etype == "item.completed":
                completed_line = _format_item_completed_line(item_num, item)
                if completed_line is not None:
                    lines.append(completed_line)

    return lines


class ExecProgressRenderer:
    def __init__(self, max_actions: int = 5, max_chars: int = MAX_PROGRESS_CHARS) -> None:
        self.max_actions = max_actions
        self.state = ExecRenderState(recent_actions=deque(maxlen=max_actions))
        self.max_chars = max_chars

    def note_event(self, event: dict[str, Any]) -> bool:
        etype = event["type"]
        changed = False

        if etype in {"thread.started", "turn.started"}:
            return True

        if etype in {"item.started", "item.updated", "item.completed"}:
            item = event["item"]
            _record_item(self.state, item)
            itype = item["type"]
            item_id = _extract_numeric_id(item["id"], self.state.last_turn)

            if itype == "reasoning":
                reasoning = _format_reasoning(item["text"])
                if reasoning:
                    reasoning_line = _with_id(item_id, reasoning)
                    if self.state.current_action and not self.state.current_reasoning:
                        self.state.current_reasoning = reasoning_line
                        changed = True
                    else:
                        self.state.pending_reasoning = reasoning_line
                return changed

            action_line = _format_item_action_line(etype, item_id, item)
            if action_line is not None:
                if etype == "item.started":
                    return _set_current_action(self.state, item_id, action_line) or changed
                if etype == "item.completed":
                    return _complete_action(self.state, item_id, action_line) or changed
                return changed

            if etype == "item.completed":
                completed_line = _format_item_completed_line(item_id, item)
                if completed_line is not None:
                    return _complete_action(self.state, item_id, completed_line) or changed

        return changed

    def render_progress(self, elapsed_s: float) -> str:
        header = _format_header(elapsed_s, self.state.last_turn, label="working")
        current_reasoning = self.state.current_reasoning
        current_action = self.state.current_action
        lines = list(self.state.recent_actions)
        if current_reasoning and current_action:
            lines.append(current_reasoning)
        if current_action:
            lines.append(current_action)

        message = self._assemble(header, lines)
        if len(message) <= self.max_chars:
            return message
        return header

    def render_final(self, elapsed_s: float, answer: str, status: str = "done") -> str:
        header = _format_header(elapsed_s, self.state.last_turn, label=status)
        lines = list(self.state.recent_actions)
        if status == "done":
            lines = [line for line in lines if not _is_command_log_line(line)]
        body = self._assemble(header, lines)
        answer = (answer or "").strip()
        if answer:
            body = body + "\n\n" + answer
        return body

    @staticmethod
    def _assemble(header: str, lines: list[str]) -> str:
        if not lines:
            return header
        return header + "\n\n" + HARD_BREAK.join(lines)
