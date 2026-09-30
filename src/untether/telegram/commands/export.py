"""Command backend for exporting the last session transcript."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ...commands import CommandBackend, CommandContext, CommandResult
from ...logging import get_logger
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
            lines.append(f"**Usage:** {' · '.join(parts)}\n")

    lines.append("---\n")

    started_rendered = False
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
                lines.append(f"- {symbol} `{title}`")
            elif kind == "file_change":
                lines.append(f"- {symbol} 📝 {title}")
            elif kind == "tool":
                lines.append(f"- {symbol} 🔧 {title}")
            elif kind == "note":
                # Skip thinking blocks for brevity
                continue
            elif kind == "warning":
                lines.append(f"- {symbol} ⚠️ {title}")
            else:
                lines.append(f"- {symbol} {title}")
        elif evt_type == "completed":
            ok = evt.get("ok", False)
            answer = evt.get("answer", "")
            status = "✓ Completed" if ok else "✗ Failed"
            error = evt.get("error")
            lines.append(f"\n## {status}")
            if error:
                lines.append(f"Error: {error}\n")
            if answer:
                # Truncate very long answers
                if len(answer) > 2000:
                    answer = answer[:2000] + "\n\n…(truncated)"
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

        # Send the formatted text (Telegram supports up to 4096 chars)
        preview = content[:3000] if len(content) > 3000 else content
        return CommandResult(
            text=f"📄 Session export ({len(events)} events, {fmt}):\n\n{preview}",
            notify=True,
        )


BACKEND: CommandBackend = ExportCommand()
