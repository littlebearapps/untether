"""Command backend for exporting the last session transcript."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ...commands import (
    CommandAttachment,
    CommandBackend,
    CommandContext,
    CommandResult,
)
from ...logging import get_logger
from ...markdown import backtick_fence, inline_code
from ...session_costs import token_counts
from ...transport import ChannelId

logger = get_logger(__name__)

# Store recent completed events for export
# Keyed by (channel_id, session_id) -> (last_activity_ts, events_list, usage_dict)
# #417: the timestamp is refreshed on every event/usage record, so "latest"
# means most recently *active* and trimming is LRU by activity.
_SessionKey = tuple[ChannelId, str]
_SESSION_HISTORY: dict[_SessionKey, tuple[float, list[dict], dict | None]] = {}
_MAX_SESSIONS = 20


def record_session_event(
    session_id: str, event: dict, *, channel_id: ChannelId = 0
) -> None:
    """Record an event for later export."""
    key: _SessionKey = (channel_id, session_id)
    entry = _SESSION_HISTORY.get(key)
    if entry is None:
        logger.debug("export.session.new", session_id=session_id, channel_id=channel_id)
        _SESSION_HISTORY[key] = (time.time(), [event], None)
    else:
        _ts, events, usage = entry
        events.append(event)
        _SESSION_HISTORY[key] = (time.time(), events, usage)
    # Trim old sessions
    if len(_SESSION_HISTORY) > _MAX_SESSIONS:
        oldest_key = min(_SESSION_HISTORY, key=lambda k: _SESSION_HISTORY[k][0])
        _SESSION_HISTORY.pop(oldest_key, None)
        logger.info("export.session.trimmed", total_sessions=len(_SESSION_HISTORY))


def record_session_usage(
    session_id: str, usage: dict, *, channel_id: ChannelId = 0
) -> None:
    """Record final usage data for a session."""
    key: _SessionKey = (channel_id, session_id)
    entry = _SESSION_HISTORY.get(key)
    if entry is not None:
        _ts, events, _ = entry
        _SESSION_HISTORY[key] = (time.time(), events, usage)


@dataclass(frozen=True, slots=True)
class ExportSession:
    session_id: str
    engine: str | None
    usage: dict | None
    events: list[dict]
    ts: float


def _session_engine(events: list[dict]) -> str | None:
    for evt in events:
        if evt.get("type") == "started":
            engine = evt.get("engine")
            return engine if isinstance(engine, str) else None
    return None


def latest_session_for_chat(
    channel_id: ChannelId, *, engine: str | None = None
) -> ExportSession | None:
    """The chat's most recently active recorded session (#417), optionally
    only among sessions whose first ``started`` event names ``engine``."""
    best: ExportSession | None = None
    for (chat, session_id), (ts, events, usage) in _SESSION_HISTORY.items():
        if chat != channel_id:
            continue
        sess_engine = _session_engine(events)
        if engine is not None and sess_engine != engine:
            continue
        if best is None or ts > best.ts:
            best = ExportSession(
                session_id=session_id,
                engine=sess_engine,
                usage=usage,
                events=events,
                ts=ts,
            )
    return best


def _command_line(symbol: str, command: str) -> str:
    """#871/#418: a command as Markdown that its own backticks can't break.

    One line → a code span; a multi-line command (heredoc) → a fenced block
    inside the list item, so the export keeps it verbatim."""
    if "\n" not in command.strip():
        return f"- {symbol} {inline_code(command)}"
    fence = backtick_fence(command, minimum=3)
    body = "\n".join(f"  {ln}" if ln else "" for ln in command.strip("\n").splitlines())
    return f"- {symbol}\n\n  {fence}\n{body}\n  {fence}\n"


# #418: headings for a live session's later turns (``TurnEvent.reason``).
_TURN_REASON_LABELS: dict[str, str] = {
    "followup": "follow-up",
    "task_finished": "background task finished",
    "scheduled_wakeup": "scheduled wake-up",
    "monitor_event": "monitor event",
    "hook_rewake": "hook wake-up",
}


def _format_export_markdown(
    session_id: str,
    events: list[dict],
    usage: dict | None,
) -> str:
    """Format session events as a Markdown transcript."""
    lines: list[str] = []
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# Session Export: {session_id}")
    lines.append(f"Exported: {now}\n")

    if usage:
        cost = usage.get("total_cost_usd")
        turns = usage.get("num_turns")
        duration_ms = usage.get("duration_ms")
        # #417: flat (Codex) or nested (Claude/OpenCode) token counts.
        counts = token_counts(usage) or {}
        input_tokens = counts.get("input_tokens")
        output_tokens = counts.get("output_tokens")
        parts: list[str] = []
        if cost is not None:
            parts.append(f"${cost:.4f}")
        if turns:
            parts.append(f"{turns} turns")
        if duration_ms:
            secs = duration_ms / 1000
            parts.append(f"{secs:.1f}s")
        if cost is None and input_tokens is not None:
            token_parts = [f"{input_tokens} in"]
            if output_tokens is not None:
                token_parts.append(f"{output_tokens} out")
            parts.append(" / ".join(token_parts) + " tokens")
        if parts:
            # The transcript spans every run of the session but the recorded
            # usage is the latest run's (rc15 integration finding), except
            # for engines that report a running thread total (Codex).
            from ...runner_bridge import _TOKEN_LEDGER_SCOPES

            engine = _session_engine(events)
            thread_total = _TOKEN_LEDGER_SCOPES.get(engine or "") == (
                "thread_cumulative"
            )
            parts.append("thread total" if thread_total else "last run")
            lines.append(f"**Usage:** {' · '.join(parts)}\n")

    lines.append("---\n")

    started_rendered = False
    # #928: where each live turn's heading starts (a dropped turn rewinds).
    turn_heading_at: dict[object, int] = {}
    for evt in events:
        evt_type = evt.get("type", "unknown")
        if evt_type == "started":
            if started_rendered:
                continue
            started_rendered = True
            engine = evt.get("engine", "unknown")
            title = evt.get("title", "")
            lines.append(f"## Session Started ({engine})")
            if title:
                lines.append(f"Model: {title}\n")
        elif evt_type == "action":
            phase = evt.get("phase", "")
            action = evt.get("action", {})
            kind = action.get("kind", "")
            title = action.get("title", "")
            ok = evt.get("ok")
            if phase == "started":
                symbol = "▸"
            elif phase == "completed":
                symbol = "✓" if ok else "✗"
            else:
                symbol = "↻"
            if kind == "command":
                lines.append(_command_line(symbol, title))
            elif kind == "file_change":
                lines.append(f"- {symbol} 📝 {title}")
            elif kind == "tool":
                lines.append(f"- {symbol} 🔧 {title}")
            elif kind == "note":
                # Skip thinking blocks for brevity
                continue
            elif kind == "warning":
                # #987: a title that already leads with ⚠️ (Codex warnings)
                # isn't given a second one.
                icon = "" if title.startswith("⚠") else "⚠️ "
                lines.append(f"- {symbol} {icon}{title}")
            else:
                lines.append(f"- {symbol} {title}")
        elif evt_type == "turn":
            # #418: a later turn of a live session (follow-up / wake turn).
            turn = evt.get("turn")
            label = _TURN_REASON_LABELS.get(str(evt.get("reason")), "turn")
            turn_heading_at[turn] = len(lines)
            lines.append(f"\n## Turn {turn} ({label})")
        elif evt_type == "turn_dropped":
            # #928: the CLI's no-query result closed that turn — it was never
            # a turn, so drop its heading (and anything rendered under it).
            at = turn_heading_at.pop(evt.get("turn"), None)
            if at is not None:
                del lines[at:]
        elif evt_type == "completed":
            ok = evt.get("ok", False)
            answer = evt.get("answer", "")
            status = "✓ Completed" if ok else "✗ Failed"
            error = evt.get("error")
            lines.append(f"\n## {status}")
            if error:
                lines.append(f"Error: {error}\n")
            if answer:
                # #418: never truncated — the export file is the durable copy.
                lines.append(f"\n{answer}")

    return "\n".join(lines)


def _format_export_json(
    session_id: str,
    events: list[dict],
    usage: dict | None,
) -> str:
    """Format session events as JSON."""
    export = {
        "session_id": session_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "usage": usage,
        "events": events,
    }
    return json.dumps(export, indent=2, default=str)


# #418: the inline preview sent when the export file can't be attached.
_FALLBACK_PREVIEW_CHARS = 3000
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def _export_filename(engine: str | None, session_id: str, fmt: str) -> str:
    """``untether-export-<engine>-<sid>-<YYYYmmdd-HHMM>.<md|json>`` (UTC)."""
    safe_engine = _UNSAFE_FILENAME_RE.sub("_", engine or "")[:24] or "session"
    safe_sid = _UNSAFE_FILENAME_RE.sub("_", session_id)[:36] or "unknown"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return f"untether-export-{safe_engine}-{safe_sid}-{stamp}.{fmt}"


class ExportCommand:
    """Command backend for exporting the last session transcript."""

    id = "export"
    description = "Export last session as Markdown or JSON"

    async def handle(self, ctx: CommandContext) -> CommandResult | None:
        args = ctx.args_text.strip().lower()
        fmt = "json" if args == "json" else "md"

        # The chat's most recently active session (#417 D10).
        latest = latest_session_for_chat(ctx.message.channel_id)
        if latest is None:
            return CommandResult(
                text="No session history available to export.",
                notify=True,
            )
        session_id, events, usage = latest.session_id, latest.events, latest.usage

        if not events:
            return CommandResult(
                text="Session has no recorded events.",
                notify=True,
            )

        if fmt == "json":
            content = _format_export_json(session_id, events, usage)
        else:
            content = _format_export_markdown(session_id, events, usage)

        # #418: attach the full transcript as a document; the caption is a
        # short summary and the inline preview is only the upload fallback.
        engine = latest.engine
        label = "JSON" if fmt == "json" else "Markdown"
        caption = (
            f"📄 Session export — {engine or 'unknown engine'} · "
            f"{len(events)} events · {label}\nSession: {session_id}"
        )
        fallback = (
            f"📄 Session export ({len(events)} events, {fmt}) — couldn't attach "
            f"the file, showing the first {_FALLBACK_PREVIEW_CHARS:,} characters:"
            f"\n\n{content[:_FALLBACK_PREVIEW_CHARS]}"
        )
        return CommandResult(
            text=caption,
            notify=True,
            attachment=CommandAttachment(
                filename=_export_filename(engine, session_id, fmt),
                content=content.encode("utf-8"),
                fallback_text=fallback,
            ),
        )


BACKEND: CommandBackend = ExportCommand()
