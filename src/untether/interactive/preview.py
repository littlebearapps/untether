"""Diff / command previews for approval cards (#960).

Moved verbatim out of ``runners/claude.py`` (CC4). Takes Claude's tool names
and input shapes (``Edit`` / ``Write`` / ``Bash``); another engine maps its
own tools onto those shapes before calling it.
"""

from __future__ import annotations

from typing import Any

from ..markdown import backtick_fence, inline_code


def format_diff_preview(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a compact diff preview for Edit/Write tool approval messages."""
    max_preview_lines = 8
    max_line_len = 60

    def _truncate(text: str, max_len: int) -> str:
        if len(text) > max_len:
            return text[: max_len - 1] + "…"
        return text

    def _fenced(lines: list[str], info: str = "diff") -> str:
        # The approval text is rendered as Markdown: bare ``+ x`` lines
        # became ``- x`` list items (an added line shown as removed) and the
        # lines ran together. A fenced block keeps them verbatim; the fence
        # outruns any backtick run in the content so it can't close early.
        body = "\n".join(lines)
        fence = backtick_fence(body, minimum=3)
        return f"{fence}{info}\n{body}\n{fence}"

    if tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        if not old_string and not new_string:
            return ""
        lines: list[str] = []
        if file_path:
            from ..utils.paths import relativize_path

            lines.append(f"📝 {relativize_path(file_path)}")
        old_lines = old_string.splitlines()
        new_lines = new_string.splitlines()
        # Show removed/added lines
        half = max_preview_lines // 2
        lines.extend(f"- {_truncate(line, max_line_len)}" for line in old_lines[:half])
        if len(old_lines) > half:
            lines.append(f"  …({len(old_lines) - half} more removed)")
        lines.extend(f"+ {_truncate(line, max_line_len)}" for line in new_lines[:half])
        if len(new_lines) > half:
            lines.append(f"  …({len(new_lines) - half} more added)")
        return _fenced(lines)

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        if not content:
            return ""
        lines = []
        if file_path:
            from ..utils.paths import relativize_path

            lines.append(f"📝 {relativize_path(file_path)}")
        content_lines = content.splitlines()
        line_count = len(content_lines)
        for line in content_lines[:max_preview_lines]:
            lines.append(f"+ {_truncate(line, max_line_len)}")
        if line_count > max_preview_lines:
            lines.append(f"  …({line_count - max_preview_lines} more lines)")
        return _fenced(lines)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not command:
            return ""
        shown = _truncate(command.strip("\n"), 200)
        # #871: the command the user approves must survive Markdown: a
        # single line becomes a backtick-safe code span; a multi-line
        # command (heredoc, script) keeps its lines in a fenced block.
        if len(shown.splitlines()) > 1:
            return f"$\n{_fenced(shown.splitlines(), info='sh')}"
        return f"$ {inline_code(shown)}"

    return ""
