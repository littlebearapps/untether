"""#929: a standalone, pushed approval message for a control request that has
no visible keyboard anywhere.

A background agent can raise a ``can_use_tool`` request after the parent's
turn has ended (the live session is idle). The run's progress message is then
final and no follow-up turn is open, so the request's Approve / Deny keyboard
had nowhere to render and the agent waited silently. The run-level
``ProgressEdits`` owns one :class:`OrphanApprovalSurface` that:

- **sends** one message per such request, carrying the request's own keyboard
  (event path, sub-second) or a synthetic Approve / Deny built from the request
  id (rescue path, after a short grace, for anything the event path missed);
- **re-posts** it with a push after ``first_s`` (10 min) and then every
  ``repeat_s`` (30 min), replacing the previous copy so exactly one is visible;
- **retires** it (delete, else edit to "✅ No longer waiting.") once the request
  is answered or withdrawn, or the run ends. It never auto-denies: the CLI has
  no permission deadline, and the session's absolute cap stays the only
  backstop.

All transport I/O is serialised by one lock (an in-flight send racing a retire
would otherwise orphan a keyboard), and every entry is re-checked after each
await. Writes go through the transport (outbox) only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import anyio

from .background_status import escape_markdown
from .logging import get_logger
from .markdown import HARD_BREAK
from .transport import MessageRef, RenderedMessage, SendOptions, Transport

logger = get_logger(__name__)

__all__ = [
    "OrphanApprovalSurface",
    "render_surface_markdown",
    "synth_keyboard",
]

_RETIRED_TEXT = "✅ No longer waiting."


def synth_keyboard(request_id: str) -> list[list[dict[str, str]]]:
    """Approve / Deny for a request with no tracked keyboard. Valid for tool
    requests, outline holds and ``da:`` synthetic requests alike (the
    ``claude_control`` handler resolves by request id)."""
    return [
        [
            {
                "text": "✅ Approve",
                "callback_data": f"claude_control:approve:{request_id}",
            },
            {"text": "❌ Deny", "callback_data": f"claude_control:deny:{request_id}"},
        ]
    ]


def render_surface_markdown(
    title: str,
    *,
    label: str | None,
    reason: str | None,
    agent: bool,
    mins: int | None = None,
) -> str:
    """The surface's markdown body. ``title`` is the action title (already
    markdown — it may carry a fenced diff preview); ``label`` and ``reason``
    are plain text and are escaped."""
    who = "a background agent" if agent else "Claude Code"
    if mins is None:
        header = (
            "🔐 **A background agent needs your approval**"
            if agent
            else "🔐 **Claude Code needs your approval**"
        )
    else:
        header = (
            f"⏳ **Still waiting for your approval ({mins} min)** — {who} is "
            "paused until you answer."
        )
    head_lines = [header]
    if label:
        head_lines.append(f"🤖 {escape_markdown(label)}")
    blocks = [HARD_BREAK.join(head_lines)]
    if title.strip():
        blocks.append(title.strip())
    tail_lines: list[str] = []
    if reason:
        tail_lines.append(f"🪝 {escape_markdown(reason)}")
    if mins is None:
        tail_lines.append(
            "The agent is paused until you answer."
            if agent
            else "Claude Code is paused until you answer."
        )
    if tail_lines:
        blocks.append(HARD_BREAK.join(tail_lines))
    return "\n\n".join(blocks)


@dataclass(slots=True)
class _Entry:
    request_id: str
    action_id: str | None
    title: str
    buttons: list[list[dict[str, Any]]]
    tool_name: str | None
    agent_id: str | None
    reason: str | None
    source: str
    queued_at: float
    ref: MessageRef | None = None
    first_sent_at: float | None = None
    sent_at: float = 0.0
    reposts: int = 0
    needs_send: bool = True
    retire_reason: str | None = None
    warned_send_failed: bool = False


def _callbacks_of(buttons: list[list[dict[str, Any]]]) -> set[str]:
    found: set[str] = set()
    for row in buttons:
        if not isinstance(row, list):
            continue
        for button in row:
            data = button.get("callback_data") if isinstance(button, dict) else None
            if isinstance(data, str):
                found.add(data)
    return found


def _keyboard_rows(action: Any) -> list[list[dict[str, Any]]] | None:
    detail = getattr(action, "detail", None) or {}
    keyboard = detail.get("inline_keyboard")
    rows = keyboard.get("buttons") if isinstance(keyboard, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    return rows


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class OrphanApprovalSurface:
    """One run's standalone approval messages (#929). See the module doc."""

    def __init__(
        self,
        *,
        transport: Transport,
        channel_id: Any,
        thread_id: Any,
        clock: Callable[[], float],
        label_for: Callable[[str | None], str | None],
        anchor_for: Callable[[str | None], MessageRef | None],
        first_s: float = 600.0,
        repeat_s: float = 1800.0,
    ) -> None:
        self._transport = transport
        self._channel_id = channel_id
        self._thread_id = thread_id
        self._clock = clock
        self._label_for = label_for
        self._anchor_for = anchor_for
        self.first_s = first_s
        self.repeat_s = repeat_s
        self._entries: dict[str, _Entry] = {}
        self._lock = anyio.Lock()
        self._retired_actions: list[tuple[str, str]] = []
        self._closed = False
        self.sent_total = 0

    # ── state queries (sync, no I/O) ────────────────────────────────────

    @property
    def has_work(self) -> bool:
        return any(
            e.retire_reason is not None or e.needs_send for e in self._entries.values()
        )

    @property
    def surfaced_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.ref is not None)

    def request_ids(self) -> frozenset[str]:
        """Requests this surface owns (queued or visible)."""
        return frozenset(
            rid for rid, e in self._entries.items() if e.retire_reason is None
        )

    def callbacks(self) -> frozenset[str]:
        """callback_data of the buttons currently visible on surface messages
        (feeds the #684 visible-buttons probe)."""
        found: set[str] = set()
        for entry in self._entries.values():
            if entry.ref is not None and entry.retire_reason is None:
                found |= _callbacks_of(entry.buttons)
        return frozenset(found)

    def drain_retired_actions(self) -> list[tuple[str, str]]:
        """``(action_id, reason)`` of retired entries since the last call, so
        the owner can complete the stale tracked action."""
        out, self._retired_actions = self._retired_actions, []
        return out

    # ── intake (sync, no I/O) ───────────────────────────────────────────

    def offer(self, action: Any) -> bool:
        """Event path: queue a keyboard action that has no visible host."""
        if self._closed:
            return False
        rows = _keyboard_rows(action)
        detail = getattr(action, "detail", None) or {}
        request_id = _opt_str(detail.get("request_id"))
        if rows is None or request_id is None or request_id in self._entries:
            return False
        self._entries[request_id] = _Entry(
            request_id=request_id,
            action_id=_opt_str(str(getattr(action, "id", "") or "")),
            title=str(getattr(action, "title", "") or ""),
            buttons=rows,
            tool_name=_opt_str(detail.get("tool_name")),
            agent_id=_opt_str(detail.get("agent_id")),
            reason=_opt_str(detail.get("decision_reason")),
            source="event",
            queued_at=self._clock(),
        )
        return True

    def rescue(self, snap: Any, action: Any = None) -> bool:
        """Backstop: queue a registered request nobody can see. Uses the
        tracked action's keyboard when there is one, else Approve / Deny."""
        if self._closed:
            return False
        request_id = _opt_str(getattr(snap, "request_id", None))
        if request_id is None or request_id in self._entries:
            return False
        if getattr(snap, "answerable_by_text", False) or not getattr(
            snap, "writer_ok", True
        ):
            return False
        rows = _keyboard_rows(action) if action is not None else None
        detail = (getattr(action, "detail", None) or {}) if action is not None else {}
        tool_name = _opt_str(getattr(snap, "tool_name", None)) or _opt_str(
            detail.get("tool_name")
        )
        if action is not None and str(getattr(action, "title", "") or "").strip():
            title = str(action.title)
        elif getattr(snap, "kind", "") in ("outline_hold", "synthetic"):
            title = "Plan approval"
        else:
            # Security review: without a tracked action, still say what
            # Approve allows — the same key parameters as the in-turn title
            # (already markdown code spans).
            details = _opt_str(getattr(snap, "input_details", None))
            title = "Permission Request" + (
                f" - tool: {escape_markdown(tool_name)}" if tool_name else ""
            )
            if details:
                title += f" {details}"
        self._entries[request_id] = _Entry(
            request_id=request_id,
            action_id=_opt_str(str(getattr(action, "id", "") or ""))
            if action is not None
            else None,
            title=title,
            buttons=rows if rows is not None else synth_keyboard(request_id),
            tool_name=tool_name,
            agent_id=_opt_str(detail.get("agent_id")),
            reason=_opt_str(detail.get("decision_reason")),
            source="rescue",
            queued_at=self._clock(),
        )
        return True

    def note_completed(self, action_id: str) -> None:
        """The request's action completed (answered, withdrawn by the CLI,
        expired, reconciled): retire on the next flush."""
        for entry in self._entries.values():
            if entry.action_id == action_id and entry.retire_reason is None:
                entry.retire_reason = "completed"

    # ── I/O (serialised) ────────────────────────────────────────────────

    async def flush(self) -> None:
        """Send queued surfaces and retire completed ones."""
        async with self._lock:
            await self._process()

    async def sync(self, live_request_ids: Iterable[str]) -> None:
        """Heartbeat: retire requests no longer registered (``resolved``),
        retry failed sends, and re-post due surfaces."""
        live = set(live_request_ids)
        async with self._lock:
            for entry in self._entries.values():
                if entry.request_id not in live and entry.retire_reason is None:
                    entry.retire_reason = "resolved"
            await self._process()
            now = self._clock()
            for entry in list(self._entries.values()):
                if entry.ref is None or entry.retire_reason is not None:
                    continue
                due = self.first_s if entry.reposts == 0 else self.repeat_s
                if now - entry.sent_at >= due:
                    await self._send(entry, repost=True)

    async def aclose(self, reason: str) -> None:
        """Run end: retire every surface; accept no new ones."""
        self._closed = True
        async with self._lock:
            for entry in self._entries.values():
                if entry.retire_reason is None:
                    entry.retire_reason = reason
            await self._process()

    async def _process(self) -> None:
        for entry in list(self._entries.values()):
            if entry.retire_reason is not None:
                await self._retire(entry)
        for entry in list(self._entries.values()):
            if entry.needs_send and entry.retire_reason is None:
                await self._send(entry, repost=False)

    async def _send_once(
        self, message: RenderedMessage, *, reply_to: MessageRef | None, replace: Any
    ) -> MessageRef | None:
        try:
            return await self._transport.send(
                channel_id=self._channel_id,
                message=message,
                options=SendOptions(
                    reply_to=reply_to,
                    notify=True,
                    replace=replace,
                    thread_id=self._thread_id,
                ),
            )
        except Exception:  # noqa: BLE001 - retried on the next tick
            logger.debug("approval_surface.send_error", exc_info=True)
            return None

    async def _send(self, entry: _Entry, *, repost: bool) -> None:
        # Local import: telegram.render pulls in the Telegram package, which
        # imports the bridge (same pattern as ProgressEdits._send_outline).
        from .telegram.render import render_markdown

        now = self._clock()
        mins = int((now - (entry.first_sent_at or now)) // 60) if repost else None
        label = None
        try:
            label = self._label_for(entry.agent_id)
        except Exception:  # noqa: BLE001 - cosmetic
            logger.debug("approval_surface.label_failed", exc_info=True)
        md = render_surface_markdown(
            entry.title,
            label=label,
            reason=entry.reason,
            agent=entry.agent_id is not None,
            mins=mins,
        )
        text, entities = render_markdown(md)
        message = RenderedMessage(
            text=text,
            extra={
                "entities": entities,
                "reply_markup": {"inline_keyboard": entry.buttons},
            },
        )
        anchor = None
        try:
            anchor = self._anchor_for(entry.agent_id)
        except Exception:  # noqa: BLE001 - fall back to no anchor
            logger.debug("approval_surface.anchor_failed", exc_info=True)
        previous = entry.ref
        ref = await self._send_once(message, reply_to=anchor, replace=previous)
        if ref is None and anchor is not None:
            # Review amendment 6: no ``allow_sending_without_reply`` — a
            # deleted anchor fails the send, so retry once unanchored.
            ref = await self._send_once(message, reply_to=None, replace=previous)
        if ref is None:
            if not entry.warned_send_failed:
                entry.warned_send_failed = True
                logger.warning(
                    "approval_surface.send_failed",
                    channel_id=self._channel_id,
                    request_id=entry.request_id,
                    repost=repost,
                )
            # Keep the previous copy (and the queued state) for the next tick.
            return
        entry.ref = ref
        entry.needs_send = False
        entry.sent_at = now
        if entry.first_sent_at is None:
            entry.first_sent_at = now
        if repost:
            entry.reposts += 1
        self.sent_total += 1
        logger.info(
            "approval_surface.sent",
            channel_id=self._channel_id,
            request_id=entry.request_id,
            action_id=entry.action_id,
            tool_name=entry.tool_name,
            agent_id=entry.agent_id,
            source=entry.source,
            reposts=entry.reposts,
            replaced=previous is not None,
            message_id=ref.message_id,
        )
        # Re-check after the await: a completion may have landed meanwhile.
        if entry.retire_reason is not None or (
            self._entries.get(entry.request_id) is not entry
        ):
            await self._retire(entry)

    async def _retire(self, entry: _Entry) -> None:
        reason = entry.retire_reason or "resolved"
        self._entries.pop(entry.request_id, None)
        if entry.action_id is not None:
            self._retired_actions.append((entry.action_id, reason))
        ref = entry.ref
        entry.ref = None
        if ref is None:
            logger.debug(
                "approval_surface.dropped_unsent",
                request_id=entry.request_id,
                reason=reason,
            )
            return
        deleted = False
        try:
            deleted = bool(await self._transport.delete(ref=ref))
        except Exception:  # noqa: BLE001
            deleted = False
        if not deleted:
            try:
                await self._transport.edit(
                    ref=ref, message=RenderedMessage(text=_RETIRED_TEXT)
                )
            except Exception:  # noqa: BLE001 - a user-deleted copy fails both
                logger.debug("approval_surface.retire_edit_failed", exc_info=True)
        logger.info(
            "approval_surface.retired",
            channel_id=self._channel_id,
            request_id=entry.request_id,
            reason=reason,
            deleted=deleted,
            age_s=round(self._clock() - (entry.first_sent_at or entry.queued_at), 1),
        )
