from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .model import Action, ActionEvent, StartedEvent, UntetherEvent
from .progress import ProgressState
from .transport import RenderedMessage
from .utils.paths import relativize_path

STATUS = {"running": "▸", "update": "↻", "done": "✓", "fail": "✗"}
HEADER_SEP = " · "
HARD_BREAK = "  \n"

MAX_PROGRESS_CMD_LEN = 300
MAX_FILE_CHANGES_INLINE = 3


@dataclass(frozen=True, slots=True)
class MarkdownParts:
    header: str
    body: str | None = None
    footer: str | None = None


def assemble_markdown_parts(parts: MarkdownParts) -> str:
    return "\n\n".join(
        chunk for chunk in (parts.header, parts.body, parts.footer) if chunk
    )


def format_changed_file_path(path: str, *, base_dir: Path | None = None) -> str:
    return f"`{relativize_path(path, base_dir=base_dir)}`"


def format_elapsed(elapsed_s: float) -> str:
    total = max(0, int(elapsed_s))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_header(
    elapsed_s: float, item: int | None, *, label: str, engine: str
) -> str:
    elapsed = format_elapsed(elapsed_s)
    parts = [label, engine]
    parts.append(elapsed)
    if item is not None:
        parts.append(f"step {item}")
    return HEADER_SEP.join(parts)


def shorten(text: str, width: int | None) -> str:
    if width is None:
        return text
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return textwrap.shorten(text, width=width, placeholder="…")


def action_status(action: Action, *, completed: bool, ok: bool | None = None) -> str:
    if not completed:
        return STATUS["running"]
    if ok is not None:
        return STATUS["done"] if ok else STATUS["fail"]
    detail = action.detail or {}
    exit_code = detail.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return STATUS["fail"]
    return STATUS["done"]


def action_suffix(action: Action) -> str:
    detail = action.detail or {}
    exit_code = detail.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return f" (exit {exit_code})"
    return ""


def format_file_change_title(action: Action, *, command_width: int | None) -> str:
    title = str(action.title or "")
    detail = action.detail or {}

    changes = detail.get("changes")
    if isinstance(changes, list) and changes:
        rendered: list[str] = []
        for raw in changes:
            path: str | None
            kind: str | None
            if isinstance(raw, dict):
                path = raw.get("path")
                kind = raw.get("kind")
            else:
                path = getattr(raw, "path", None)
                kind = getattr(raw, "kind", None)
            if not isinstance(path, str) or not path:
                continue
            verb = kind if isinstance(kind, str) and kind else "update"
            rendered.append(f"{verb} {format_changed_file_path(path)}")

        if rendered:
            if len(rendered) > MAX_FILE_CHANGES_INLINE:
                remaining = len(rendered) - MAX_FILE_CHANGES_INLINE
                rendered = rendered[:MAX_FILE_CHANGES_INLINE] + [f"…({remaining} more)"]
            inline = shorten(", ".join(rendered), command_width)
            return f"files: {inline}"

    fallback = title
    relativized = relativize_path(fallback)
    was_relativized = relativized != fallback
    if was_relativized:
        fallback = relativized
    if (
        fallback
        and not (fallback.startswith("`") and fallback.endswith("`"))
        and (was_relativized or os.sep in fallback or "/" in fallback)
    ):
        fallback = f"`{fallback}`"
    return f"files: {shorten(fallback, command_width)}"


def format_action_title(action: Action, *, command_width: int | None) -> str:
    title = str(action.title or "")
    kind = action.kind
    if kind == "command":
        title = shorten(title, command_width)
        return f"`{title}`"
    if kind == "tool":
        title = shorten(title, command_width)
        return f"tool: {title}"
    if kind == "web_search":
        title = shorten(title, command_width)
        return f"searched: {title}"
    if kind == "subagent":
        title = shorten(title, command_width)
        return f"subagent: {title}"
    if kind == "file_change":
        return format_file_change_title(action, command_width=command_width)
    if kind in {"note", "warning"}:
        # Multi-line titles (e.g. plan outlines) are intentionally long;
        # don't truncate them — the body trim (3500 chars) handles overflow.
        if "\n" in title:
            return title
        return shorten(title, command_width)
    return shorten(title, command_width)


def format_action_line(
    action: Action,
    phase: str,
    ok: bool | None,
    *,
    command_width: int | None,
) -> str:
    if phase != "completed":
        status = STATUS["update"] if phase == "updated" else STATUS["running"]
        return f"{status} {format_action_title(action, command_width=command_width)}"
    status = action_status(action, completed=True, ok=ok)
    suffix = action_suffix(action)
    return (
        f"{status} {format_action_title(action, command_width=command_width)}{suffix}"
    )


_VERBOSE_DETAIL_WIDTH = 120


def format_verbose_detail(action: Action) -> str | None:
    """Extract a compact detail line from action.detail for verbose mode.

    Returns a single line like ``"→ src/settings.py (4821 chars)"`` or None
    if no meaningful detail is available.
    """
    detail = action.detail or {}
    name = detail.get("name", "")
    inp = detail.get("input") or detail.get("arguments") or detail.get("args") or {}
    if not isinstance(inp, dict):
        inp = {}

    # Bash/command: show command text
    if action.kind == "command":
        cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
        if cmd:
            return shorten(cmd, 200)
        return None

    # Read: show file path + result size
    if name in ("Read", "read"):
        path = inp.get("file_path", "")
        if path:
            result_len = detail.get("result_len")
            suffix = f" ({result_len} chars)" if result_len else ""
            return f"→ {relativize_path(path)}{suffix}"
        return None

    # Edit: show file path + brief old text
    if name in ("Edit", "edit"):
        path = inp.get("file_path", "")
        if path:
            old = shorten(str(inp.get("old_string", "")), 40)
            return (
                f"→ {relativize_path(path)} `{old}`→…"
                if old
                else f"→ {relativize_path(path)}"
            )
        return None

    # Write: show file path
    if name in ("Write", "write"):
        path = inp.get("file_path", "")
        if path:
            return f"→ {relativize_path(path)}"
        return None

    # Grep/Glob: show pattern
    if name in ("Grep", "grep", "Glob", "glob"):
        pattern = inp.get("pattern", "")
        if pattern:
            return f"→ `{shorten(pattern, 60)}`"
        return None

    # Task/subagent: show description
    if name in ("Task",):
        desc = inp.get("description", "")
        if desc:
            return f"→ {shorten(desc, 80)}"
        return None

    # WebSearch: show query
    if name in ("WebSearch",):
        query = inp.get("query", "")
        if query:
            return f'→ "{shorten(query, 80)}"'
        return None

    # MCP tools: show server:tool
    server = detail.get("server", "")
    tool = detail.get("tool", name)
    if server:
        return f"→ {server}:{tool}"

    # Fallback: show first short string arg
    for v in inp.values():
        if isinstance(v, str) and v and len(v) < 200:
            return f"→ {shorten(v, 80)}"
    return None


def render_event_cli(event: UntetherEvent) -> list[str]:
    match event:
        case StartedEvent(engine=engine):
            return [str(engine)]
        case ActionEvent() as action_event:
            action = action_event.action
            if action.kind == "turn":
                return []
            return [
                format_action_line(
                    action_event.action,
                    action_event.phase,
                    action_event.ok,
                    command_width=MAX_PROGRESS_CMD_LEN,
                )
            ]
        case _:
            return []


def _short_model_name(model: str) -> str:
    """Shorten a Claude model ID to its family name.

    ``'claude-sonnet-4-5-20250929'`` → ``'sonnet'``
    """
    for family in ("opus", "sonnet", "haiku"):
        if family in model.lower():
            return family
    return model.split("-202")[0] if "-202" in model else model


def format_meta_line(meta: dict[str, Any]) -> str | None:
    """Format model + permission mode into a compact footer line."""
    parts: list[str] = []
    model = meta.get("model")
    if isinstance(model, str) and model:
        parts.append(_short_model_name(model))
    perm = meta.get("permissionMode")
    if isinstance(perm, str) and perm:
        parts.append(perm)
    return ("\N{LABEL} " + HEADER_SEP.join(parts)) if parts else None


class MarkdownFormatter:
    def __init__(
        self,
        *,
        max_actions: int = 5,
        command_width: int | None = MAX_PROGRESS_CMD_LEN,
        verbosity: Literal["compact", "verbose"] = "compact",
    ) -> None:
        self.max_actions = max(0, int(max_actions))
        self.command_width = command_width
        self.verbosity = verbosity

    def render_progress_parts(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> MarkdownParts:
        step = state.action_count or None
        header = format_header(
            elapsed_s,
            step,
            label=label,
            engine=state.engine,
        )
        body = self._assemble_body(self._format_actions(state))
        return MarkdownParts(
            header=header, body=body, footer=self._format_footer(state)
        )

    def render_final_parts(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> MarkdownParts:
        step = state.action_count or None
        header = format_header(
            elapsed_s,
            step,
            label=status,
            engine=state.engine,
        )
        answer = (answer or "").strip()
        body = answer if answer else None
        return MarkdownParts(
            header=header, body=body, footer=self._format_footer(state)
        )

    def _format_footer(self, state: ProgressState) -> str | None:
        lines: list[str] = []
        if state.context_line:
            lines.append(state.context_line)
        if state.meta_line:
            lines.append(state.meta_line)
        if state.resume_line:
            lines.append(state.resume_line)
        if not lines:
            return None
        return HARD_BREAK.join(lines)

    def _format_actions(self, state: ProgressState) -> list[str]:
        actions = list(state.actions)
        actions = [] if self.max_actions == 0 else actions[-self.max_actions :]
        lines: list[str] = []
        for action_state in actions:
            line = format_action_line(
                action_state.action,
                action_state.display_phase,
                action_state.ok,
                command_width=self.command_width,
            )
            lines.append(line)
            if self.verbosity == "verbose":
                detail_line = format_verbose_detail(action_state.action)
                if detail_line:
                    lines.append(f"  {shorten(detail_line, _VERBOSE_DETAIL_WIDTH)}")
        return lines

    @staticmethod
    def _assemble_body(lines: list[str]) -> str | None:
        if not lines:
            return None
        return HARD_BREAK.join(lines)


class MarkdownPresenter:
    def __init__(self, *, formatter: MarkdownFormatter | None = None) -> None:
        self._formatter = formatter or MarkdownFormatter()

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label
        )
        return RenderedMessage(text=assemble_markdown_parts(parts))

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )
        return RenderedMessage(text=assemble_markdown_parts(parts))
