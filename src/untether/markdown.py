from __future__ import annotations

import os
import re
import textwrap
import unicodedata
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


def format_changed_file_path(
    path: str, *, base_dir: Path | None = None, width: int | None = None
) -> str:
    return inline_code(_shorten_path(relativize_path(path, base_dir=base_dir), width))


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
    elapsed_s: float,
    item: int | None,
    *,
    label: str,
    engine: str,
    context_pct: int | None = None,
) -> str:
    """``label · engine · elapsed[ · step N][ · N% ctx]``.

    #819: ``context_pct`` (Claude's context-window use) is appended last, with
    no emoji, and omitted when unknown.
    """
    elapsed = format_elapsed(elapsed_s)
    parts = [label, engine]
    parts.append(elapsed)
    if item is not None:
        parts.append(f"step {item}")
    if context_pct is not None:
        parts.append(f"{context_pct}% ctx")
    return HEADER_SEP.join(parts)


def shorten(text: str, width: int | None) -> str:
    if width is None:
        return text
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return textwrap.shorten(text, width=width, placeholder="…")


_BACKTICK_RUN_RE = re.compile(r"`+")


def backtick_fence(text: str, *, minimum: int = 1) -> str:
    """A backtick fence longer than any backtick run in *text* (#855, #871)."""
    longest = max((len(m) for m in _BACKTICK_RUN_RE.findall(text)), default=0)
    return "`" * max(minimum, longest + 1)


def inline_code(text: str, width: int | None = None) -> str:
    """A CommonMark code span that backticks inside *text* can't close (#871).

    Code spans can't hold line breaks (and backslash escapes don't work in
    them), so lines are joined with spaces, the text is shortened BEFORE
    fencing (a cut can never land inside the fence), and the fence outruns
    every inner backtick run. A lead/trail backtick gets one padding space,
    which CommonMark strips again. Backtick-free text renders as before:
    ``inline_code("git status") == "`git status`"``.
    """
    text = shorten(" ".join(text.splitlines()).strip(), width)
    if not text:
        return ""
    fence = backtick_fence(text)
    pad = " " if text[0] == "`" or text[-1] == "`" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _shorten_path(path: str, width: int | None) -> str:
    """Keep a path's tail (the file name) when it must be cut."""
    if width is None or len(path) <= width:
        return path
    if width <= 1:
        return "…"
    return "…" + path[-(width - 1) :]


_EMOJI_PRESENTATION = "\ufe0f"


def starts_with_pictograph(text: str) -> bool:
    """True when *text*'s first non-space character is a symbol/pictograph.

    That is: Unicode category ``So`` (warning sign, hooked arrow, check marks
    and, deliberately, box drawing such as tree lines), the pictograph blocks
    U+1F000-1FAFF and U+2600-27BF, or any character followed by U+FE0F (emoji
    presentation). The information source sign U+2139 is category ``Ll``, so
    only its trailing FE0F marks it; without FE0F it is not a pictograph.
    #868 uses this to drop the done glyph on emoji-led notes; #870 to spot
    status lines that must keep their own line.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    first = stripped[0]
    code = ord(first)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        return True
    if unicodedata.category(first) == "So":
        return True
    return stripped[1:2] == _EMOJI_PRESENTATION


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
        # #871: shorten each path before fencing, so no later cut lands
        # inside a code span.
        shown = min(len(changes), MAX_FILE_CHANGES_INLINE)
        path_width = (
            None if command_width is None else max(16, command_width // shown - 12)
        )
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
            rendered.append(
                f"{verb} {format_changed_file_path(path, width=path_width)}"
            )

        if rendered:
            if len(rendered) > MAX_FILE_CHANGES_INLINE:
                remaining = len(rendered) - MAX_FILE_CHANGES_INLINE
                rendered = rendered[:MAX_FILE_CHANGES_INLINE] + [f"…({remaining} more)"]
            return f"files: {', '.join(rendered)}"

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
        return f"files: {inline_code(_shorten_path(fallback, command_width))}"
    if fallback.startswith("`") and fallback.endswith("`"):
        return f"files: {fallback}"  # already a span: never cut inside it
    return f"files: {shorten(fallback, command_width)}"


_WEB_SEARCH_PREFIX: dict[str, str] = {
    "search": "searched: ",
    "open_page": "opened: ",
    "find_in_page": "find in page: ",
    "other": "",
}


def format_action_title(action: Action, *, command_width: int | None) -> str:
    title = str(action.title or "")
    kind = action.kind
    if kind == "command":
        # #871: a fence longer than any backtick in the command, so an inner
        # backtick can't end the span early and swallow the next lines.
        return inline_code(title, command_width)
    if kind == "tool":
        title = shorten(title, command_width)
        return f"tool: {title}"
    if kind == "web_search":
        title = shorten(title, command_width)
        # #419: Codex tags each web_search with its action type; Claude's
        # WebSearch has none and keeps the ``searched:`` prefix byte-for-byte.
        detail = action.detail if isinstance(action.detail, dict) else {}
        action_type = detail.get("action_type")
        prefix = _WEB_SEARCH_PREFIX.get(
            action_type if isinstance(action_type, str) else "search", "searched: "
        )
        return f"{prefix}{title}"
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


def format_duration(seconds: float | int) -> str:
    """Render a duration as ``Nm Ys`` (≥60s) or ``Ys``.

    Used by the #481 long-running-action tail to surface elapsed time
    on the progress message even when no JSONL events are arriving.
    Negative values render as ``0s`` (defensive — clock skew shouldn't
    break the renderer).
    """
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    minutes, secs = divmod(s, 60)
    return f"{minutes}m {secs:02d}s"


def format_countdown(seconds: float | int) -> str:
    """Render a remaining-time countdown — alias of format_duration.

    Kept distinct so call sites read clearly (countdown vs elapsed) and
    so future formatting differences (e.g. ``ETA 14:32``) can land in
    one place.
    """
    return format_duration(seconds)


def format_action_line(
    action: Action,
    phase: str,
    ok: bool | None,
    *,
    command_width: int | None,
    elapsed_seconds: float | None = None,
) -> str:
    """Render one action line for the progress message.

    #481: ``elapsed_seconds`` triggers the long-running tail. When the
    action is non-completed AND age > 60 s, append ``· <elapsed> · <key arg>``
    so a glancing user can answer "is it alive? what is it doing? for how
    long?" without waiting for the next JSONL event. The tail fires
    regardless of formatter verbosity — verbose mode keeps its existing
    ``→ <detail>`` second line below (slight redundancy is fine; verbose
    users opted in).
    """
    if phase != "completed":
        status = STATUS["update"] if phase == "updated" else STATUS["running"]
        line = f"{status} {format_action_title(action, command_width=command_width)}"
        if elapsed_seconds is not None and elapsed_seconds > 60:
            elapsed_str = format_duration(elapsed_seconds)
            detail = format_verbose_detail(action, width=_TAIL_DETAIL_WIDTH)
            if detail:
                # Strip the ``→ `` prefix so the tail reads as
                # ``▸ Bash · 3m 47s · npm run build`` rather than
                # ``▸ Bash · 3m 47s · → npm run build``.
                detail_clean = detail.lstrip("→ ").strip()
                tail = f" · {elapsed_str} · {_fit_detail(action, detail_clean, _TAIL_DETAIL_WIDTH)}"
            else:
                tail = f" · {elapsed_str}"
            # On the first line: a multi-line title (an approval's fenced
            # diff preview) would otherwise get the tail after its closing
            # fence, which then never closes (rc15 integration finding).
            first, sep, rest = line.partition("\n")
            line = f"{first}{tail}{sep}{rest}"
        return line
    status = action_status(action, completed=True, ok=ok)
    suffix = action_suffix(action)
    title = format_action_title(action, command_width=command_width)
    # #868: a successful note/warning that leads with its own emoji (⚠️ rate
    # limit, ⏳, 🛡️, 🗜️, ↪️) uses that emoji as its status — a ✓ in front
    # would make a warning read as a finished step. ✗ and ▸ are unchanged.
    if (
        action.kind in {"note", "warning"}
        and status == STATUS["done"]
        and starts_with_pictograph(title)
    ):
        return f"{title}{suffix}"
    return f"{status} {title}{suffix}"


_VERBOSE_DETAIL_WIDTH = 120
_TAIL_DETAIL_WIDTH = 80
# Verbose details that carry a code span, built to fit the caller's width so
# nobody shortens (and cuts) them afterwards (#871).
_FENCED_DETAIL_NAMES = frozenset({"Edit", "edit", "Grep", "grep", "Glob", "glob"})


def _fit_detail(action: Action, detail: str, width: int) -> str:
    """Fit a verbose detail into *width* without ever cutting a code span."""
    if action.kind == "command":
        return inline_code(detail, width)
    name = (action.detail or {}).get("name")
    if name in _FENCED_DETAIL_NAMES:
        return detail  # format_verbose_detail(width=...) already sized it
    return shorten(detail, width)


def format_verbose_detail(action: Action, *, width: int | None = None) -> str | None:
    """Extract a compact detail line from action.detail for verbose mode.

    Returns a single line like ``"→ src/settings.py (4821 chars)"`` or None
    if no meaningful detail is available. Commands are returned raw (callers
    fence them); Edit/Grep/Glob details carry a code span sized to *width*
    (default 120), so callers must not shorten them again (#871).
    """
    span_width = width if width is not None else _VERBOSE_DETAIL_WIDTH
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
            old = inline_code(str(inp.get("old_string", "")), 40)
            if not old:
                return f"→ {relativize_path(path)}"
            # "→ " + path + " " + span + "→…" must fit span_width.
            budget = max(8, span_width - len(old) - 5)
            return f"→ {_shorten_path(relativize_path(path), budget)} {old}→…"
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
            return f"→ {inline_code(str(pattern), min(60, max(8, span_width - 8)))}"
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

    # #481: BashOutput — Claude Code's mechanism for polling backgrounded
    # Bash shells. The previous tool_result_event populated
    # ``detail["result_preview"]`` with the recent stdout snapshot; render
    # the LAST line as the verbose detail so users see live polling output
    # (e.g. ``→ Deploy Production: in_progress``) instead of a generic
    # ``▸ BashOutput`` line for 10+ minutes.
    if name == "BashOutput":
        preview = detail.get("result_preview") or ""
        if isinstance(preview, str) and preview.strip():
            last = preview.rstrip().splitlines()[-1]
            if last:
                return f"→ {shorten(last, 120)}"
        bash_id = inp.get("bash_id", "")
        if isinstance(bash_id, str) and bash_id:
            return f"→ bash:{bash_id[-8:]}"
        return None

    # #481: KillShell — show which background bash is being terminated.
    if name == "KillShell":
        bash_id = inp.get("shell_id") or inp.get("bash_id") or ""
        if isinstance(bash_id, str) and bash_id:
            return f"→ kill bash:{bash_id[-8:]}"
        return None

    # #481: ScheduleWakeup — render countdown from heartbeat-mutated
    # ``detail['countdown_s']`` (set by ProgressEdits._heartbeat_tick), or
    # fall back to ``delaySeconds`` from input. Optional ``reason`` field
    # is shown in quotes when present.
    if name == "ScheduleWakeup":
        reason = inp.get("reason")
        countdown_s = detail.get("countdown_s")
        if countdown_s is None:
            delay = (
                inp.get("delaySeconds")
                or (inp.get("delay_ms") or 0) / 1000.0
                or (inp.get("timeout_ms") or 0) / 1000.0
            )
            if delay > 0:
                countdown_s = float(delay)
        if countdown_s is None or countdown_s < 0:
            return None
        timer = format_countdown(countdown_s)
        if isinstance(reason, str) and reason.strip():
            return f'→ fires in {timer} · "{shorten(reason, 60)}"'
        return f"→ fires in {timer}"

    # #481: Monitor — render countdown from heartbeat-mutated countdown_s.
    if name == "Monitor":
        countdown_s = detail.get("countdown_s")
        if isinstance(countdown_s, (int, float)) and countdown_s > 0:
            return f"→ monitoring · {format_countdown(countdown_s)} remaining"
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
            if action.kind in ("turn", "telemetry"):
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


# #688: the minor version is OPTIONAL and bounded to 1-2 digits. The Claude 5
# family ships major-only IDs (``claude-opus-5``), which the old mandatory
# ``(\d+)[.-](\d+)`` could not match at all — every Claude 5 model fell through
# to the lossy family fallback below and rendered as a bare ``opus``, silently
# dropping both the version and the ``[1m]`` context marker. The ``(?!\d)``
# guard is what keeps a trailing date from being read as a minor version:
# ``claude-opus-5-20260725`` → ``opus 5``, not ``opus 5.20260725``, while
# ``claude-opus-4-6-20260101`` still yields ``opus 4.6``.
_CLAUDE_MODEL_RE = re.compile(
    r"(opus|sonnet|haiku|fable)[- ](\d+)(?:[.-](\d{1,2})(?!\d))?[^\[]*(?:\[([^\]]+)\])?",
    re.IGNORECASE,
)

_CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")

_CONTEXT_SUFFIX_MAP: dict[str, str] = {"1m": "1M"}


def _short_model_name(model: str) -> str:
    """Shorten a Claude model ID to its family name with version.

    ``'claude-opus-4-6'`` → ``'opus 4.6'``
    ``'claude-opus-4-6[1m]'`` → ``'opus 4.6 (1M)'``
    ``'claude-sonnet-4-5-20250929'`` → ``'sonnet 4.5'``
    ``'claude-opus-5[1m]'`` → ``'opus 5 (1M)'`` (major-only Claude 5 IDs)
    ``'claude-fable-5'`` → ``'fable 5'``
    """
    m = _CLAUDE_MODEL_RE.search(model)
    if m:
        base = f"{m.group(1).lower()} {m.group(2)}"
        minor = m.group(3)
        if minor:
            base = f"{base}.{minor}"
        suffix = m.group(4)
        if suffix:
            label = _CONTEXT_SUFFIX_MAP.get(suffix.lower(), suffix.upper())
            return f"{base} ({label})"
        return base
    for family in _CLAUDE_FAMILIES:
        if family in model.lower():
            return family
    if model.lower().startswith("auto-"):
        model = model[5:]
    return model.split("-202")[0] if "-202" in model else model


def format_meta_line(meta: dict[str, Any]) -> str | None:
    """Format model + effort + permission mode (+ trigger source) as a footer line."""
    parts: list[str] = []
    model = meta.get("model")
    if isinstance(model, str) and model:
        parts.append(_short_model_name(model))
    effort = meta.get("effort")
    if isinstance(effort, str) and effort:
        parts.append(effort)
    perm = meta.get("permissionMode")
    if isinstance(perm, str) and perm:
        parts.append(perm)
    # rc4 (#271): show trigger provenance when set by the dispatcher.
    trigger = meta.get("trigger")
    if isinstance(trigger, str) and trigger:
        parts.append(trigger)
    # #333: show "✓ turn complete" hint on bidirectional Claude sessions
    # so the user knows the turn is done and the bot is waiting (rather
    # than processing). Set by translate_claude_event on result.
    complete = meta.get("complete")
    if isinstance(complete, str) and complete:
        parts.append(complete)
    return HEADER_SEP.join(parts) if parts else None


class MarkdownFormatter:
    def __init__(
        self,
        *,
        max_actions: int = 5,
        command_width: int | None = MAX_PROGRESS_CMD_LEN,
        verbosity: Literal["compact", "verbose"] = "compact",
        show_context_usage: bool = True,
    ) -> None:
        self.max_actions = max(0, int(max_actions))
        self.command_width = command_width
        self.verbosity = verbosity
        # #819: ``[progress] show_context_usage`` — read at every render, set
        # per run by ``refresh_from`` (hot reload).
        self.show_context_usage = show_context_usage

    def refresh_from(self, progress: Any) -> None:
        """Update mutable formatting knobs from a ``ProgressSettings`` snapshot (#269).

        Used by the runner bridge at the start of each run so edits to
        ``[progress].max_actions`` / ``[progress].verbosity`` in
        ``untether.toml`` apply on the next run without restarting the bot.
        Per-chat ``/verbose`` overrides still take precedence — they're
        rebuilt by ``runner_bridge._resolve_presenter`` from the refreshed
        defaults each call.
        """
        max_actions = getattr(progress, "max_actions", None)
        if isinstance(max_actions, int):
            self.max_actions = max(0, max_actions)
        verbosity = getattr(progress, "verbosity", None)
        if verbosity in ("compact", "verbose"):
            self.verbosity = verbosity
        show_context_usage = getattr(progress, "show_context_usage", None)
        if isinstance(show_context_usage, bool):
            self.show_context_usage = show_context_usage

    def _context_pct(self, state: ProgressState) -> int | None:
        return state.context_pct if self.show_context_usage else None

    def render_progress_parts(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
        now: float | None = None,
    ) -> MarkdownParts:
        step = state.action_count or None
        header = format_header(
            elapsed_s,
            step,
            label=label,
            engine=state.engine,
            context_pct=self._context_pct(state),
        )
        body = self._assemble_body(self._format_actions(state, now=now))
        if state.background:
            # #777: live background tasks, below the action lines.
            body = f"{body}\n\n{state.background}" if body else state.background
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
            context_pct=self._context_pct(state),
        )
        answer = (answer or "").strip()
        body = answer if answer else None
        return MarkdownParts(
            header=header, body=body, footer=self._format_footer(state)
        )

    def _format_footer(self, state: ProgressState) -> str | None:
        lines: list[str] = []
        # Combine context + meta into a single 🏷 info line with pipe separators
        info_parts: list[str] = []
        if state.context_line:
            info_parts.append(state.context_line)
        if state.meta_line:
            info_parts.append(state.meta_line)
        if info_parts:
            lines.append("\N{LABEL} " + " | ".join(info_parts))
        if state.resume_line:
            lines.append("")  # blank line for visual separation
            lines.append(f"\u21a9\ufe0f {state.resume_line}")
        if not lines:
            return None
        return HARD_BREAK.join(lines)

    def _format_actions(
        self, state: ProgressState, *, now: float | None = None
    ) -> list[str]:
        actions = list(state.actions)
        actions = [] if self.max_actions == 0 else actions[-self.max_actions :]
        lines: list[str] = []
        for action_state in actions:
            # #481: derive per-action elapsed when both ``now`` and
            # ``started_at`` are available. Tests that don't pass a clock
            # default to None → no tail (preserves the existing compact
            # output for fast actions and unit tests).
            elapsed_seconds: float | None = None
            if now is not None and action_state.started_at > 0:
                elapsed_seconds = max(0.0, now - action_state.started_at)
            line = format_action_line(
                action_state.action,
                action_state.display_phase,
                action_state.ok,
                command_width=self.command_width,
                elapsed_seconds=elapsed_seconds,
            )
            lines.append(line)
            if self.verbosity == "verbose":
                action = action_state.action
                detail_line = format_verbose_detail(action, width=_VERBOSE_DETAIL_WIDTH)
                if detail_line:
                    fitted = _fit_detail(action, detail_line, _VERBOSE_DETAIL_WIDTH - 2)
                    lines.append(f"  {fitted}")
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
        now: float | None = None,
    ) -> RenderedMessage:
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label, now=now
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
