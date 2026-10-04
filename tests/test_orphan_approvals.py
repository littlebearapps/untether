"""#929: standalone approval surface for a control request with no visible
keyboard (a background agent asking while the live session is idle)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
import structlog.testing

from untether.model import Action
from untether.orphan_approvals import (
    OrphanApprovalSurface,
    render_surface_markdown,
    synth_keyboard,
)
from untether.runners.claude import ControlRequestSnapshot
from untether.transport import MessageRef, RenderedMessage, SendOptions

pytestmark = pytest.mark.anyio

_BUTTONS = [
    [
        {"text": "✅ Approve", "callback_data": "claude_control:approve:r-1"},
        {"text": "❌ Deny", "callback_data": "claude_control:deny:r-1"},
    ]
]


class FakeTransport:
    def __init__(self) -> None:
        self._next_id = 100
        self.send_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self.delete_calls: list[MessageRef] = []
        self.send_results: list[bool] = []  # False → return None for that send
        self.delete_ok = True
        self.send_gate: anyio.Event | None = None

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.send_calls.append(
            {"channel_id": channel_id, "message": message, "options": options}
        )
        if self.send_results and not self.send_results.pop(0):
            self.send_calls[-1]["ref"] = None
            return None
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.send_calls[-1]["ref"] = ref
        return ref

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef:
        self.edit_calls.append({"ref": ref, "message": message})
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        self.delete_calls.append(ref)
        return self.delete_ok

    async def close(self) -> None:
        return None


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


_ANCHOR = MessageRef(channel_id=123, message_id=7)


def _surface(
    transport: FakeTransport | None = None,
    clock: _Clock | None = None,
    *,
    label: str | None = "Plan the #927 fix",
    anchor: MessageRef | None = _ANCHOR,
) -> tuple[OrphanApprovalSurface, FakeTransport, _Clock]:
    transport = transport or FakeTransport()
    clock = clock or _Clock()
    surface = OrphanApprovalSurface(
        transport=transport,
        channel_id=123,
        thread_id=None,
        clock=clock,
        label_for=lambda agent_id: label if agent_id else None,
        anchor_for=lambda agent_id: anchor,
    )
    return surface, transport, clock


def _action(
    request_id: str = "r-1",
    *,
    action_id: str = "claude.control.1",
    agent_id: str | None = "a1c1",
    reason: str | None = "R20 test hook: confirm this command",
    buttons: list[list[dict[str, str]]] | None = None,
    title: str = "Permission Request [CanUseTool] - tool: Bash (command=`echo hi`)",
) -> Action:
    detail: dict[str, Any] = {
        "request_id": request_id,
        "request_type": "CanUseTool",
        "tool_name": "Bash",
        "inline_keyboard": {"buttons": buttons if buttons is not None else _BUTTONS},
    }
    if agent_id:
        detail["agent_id"] = agent_id
        detail["decision_reason_type"] = "hook"
    if reason:
        detail["decision_reason"] = reason
    return Action(id=action_id, kind="warning", title=title, detail=detail)


def _snap(
    request_id: str = "r-9",
    *,
    kind: str = "tool",
    age_s: float = 40.0,
    answerable_by_text: bool = False,
    writer_ok: bool = True,
    tool_name: str = "Bash",
) -> ControlRequestSnapshot:
    return ControlRequestSnapshot(
        request_id=request_id,
        session_id="sess-929",
        age_s=age_s,
        tool_name=tool_name,
        kind=kind,
        answerable_by_text=answerable_by_text,
        writer_ok=writer_ok,
    )


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == name]


# ── rendering ────────────────────────────────────────────────────────────


def test_render_background_agent_copy() -> None:
    md = render_surface_markdown(
        "Permission Request [CanUseTool] - tool: Bash",
        label="Plan the fix",
        reason="confirm this command",
        agent=True,
    )
    assert "🔐 **A background agent needs your approval**" in md
    assert "🤖 Plan the fix" in md
    assert "Permission Request [CanUseTool] - tool: Bash" in md
    assert "🪝 confirm this command" in md
    assert "paused until you answer" in md


def test_render_generic_without_agent() -> None:
    md = render_surface_markdown(
        "Permission Request", label=None, reason=None, agent=False
    )
    assert "Claude Code needs your approval" in md
    assert "background agent" not in md
    assert "🤖" not in md and "🪝" not in md


def test_render_repost_header_minutes() -> None:
    md = render_surface_markdown(
        "Permission Request", label=None, reason=None, agent=True, mins=10
    )
    assert md.startswith("⏳ **Still waiting for your approval (10 min)**")
    assert "a background agent is paused until you answer" in md


def test_render_escapes_reason_markdown() -> None:
    md = render_surface_markdown(
        "T", label="a_b*c", reason="run `rm` *now* [x](y)", agent=True
    )
    assert "\\`rm\\`" in md
    assert "\\*now\\*" in md
    assert "\\[x\\]\\(y\\)" in md
    assert "a\\_b\\*c" in md


def test_synth_keyboard_approve_deny() -> None:
    rows = synth_keyboard("r-5")
    data = [b["callback_data"] for row in rows for b in row]
    assert data == ["claude_control:approve:r-5", "claude_control:deny:r-5"]


# ── event path ───────────────────────────────────────────────────────────


async def test_offer_flush_sends_pushed_keyboard_message() -> None:
    surface, transport, _ = _surface()
    assert surface.offer(_action()) is True
    assert surface.has_work
    with structlog.testing.capture_logs() as logs:
        await surface.flush()
    (call,) = transport.send_calls
    opts = call["options"]
    assert opts.notify is True
    assert opts.reply_to == _ANCHOR
    assert opts.thread_id is None
    assert call["message"].extra["reply_markup"] == {"inline_keyboard": _BUTTONS}
    assert "A background agent needs your approval" in call["message"].text
    assert "🤖 Plan the #927 fix" in call["message"].text
    (sent,) = _events(logs, "approval_surface.sent")
    assert sent["source"] == "event" and sent["request_id"] == "r-1"
    assert sent["agent_id"] == "a1c1" and sent["tool_name"] == "Bash"
    assert sent["reposts"] == 0 and sent["replaced"] is False
    assert surface.surfaced_count == 1 and surface.sent_total == 1
    assert not surface.has_work


async def test_offer_ignored_without_keyboard_or_duplicate() -> None:
    surface, _, _ = _surface()
    no_kb = Action(id="x", kind="warning", title="t", detail={"request_id": "r-2"})
    assert surface.offer(no_kb) is False
    no_rid = Action(
        id="y",
        kind="warning",
        title="t",
        detail={"inline_keyboard": {"buttons": _BUTTONS}},
    )
    assert surface.offer(no_rid) is False
    assert surface.offer(_action()) is True
    assert surface.offer(_action()) is False
    assert surface.request_ids() == frozenset({"r-1"})


async def test_send_failure_keeps_entry_for_retry() -> None:
    surface, transport, _ = _surface(anchor=None)
    transport.send_results = [False, False, True]
    surface.offer(_action())
    with structlog.testing.capture_logs() as logs:
        await surface.flush()
        assert surface.has_work and surface.surfaced_count == 0
        await surface.flush()
        await surface.flush()
    assert len(_events(logs, "approval_surface.send_failed")) == 1
    assert surface.surfaced_count == 1
    assert not surface.has_work


async def test_send_retries_without_reply_to() -> None:
    """Review amendment 6: a deleted anchor fails ``reply_to``; retry once
    without it before logging ``send_failed``."""
    surface, transport, _ = _surface()
    transport.send_results = [False, True]
    surface.offer(_action())
    with structlog.testing.capture_logs() as logs:
        await surface.flush()
    first, second = transport.send_calls
    assert first["options"].reply_to == _ANCHOR
    assert second["options"].reply_to is None
    assert surface.surfaced_count == 1
    assert _events(logs, "approval_surface.send_failed") == []


# ── sync: retire / re-post ───────────────────────────────────────────────


async def test_sync_retires_missing_request() -> None:
    surface, transport, clock = _surface()
    surface.offer(_action())
    await surface.flush()
    ref = transport.send_calls[0]["ref"]
    clock.now += 42
    with structlog.testing.capture_logs() as logs:
        await surface.sync(set())
    assert transport.delete_calls == [ref]
    (retired,) = _events(logs, "approval_surface.retired")
    assert retired["reason"] == "resolved" and retired["deleted"] is True
    assert retired["age_s"] == 42.0
    assert surface.request_ids() == frozenset()
    assert surface.drain_retired_actions() == [("claude.control.1", "resolved")]
    assert surface.drain_retired_actions() == []


async def test_sync_keeps_live_request() -> None:
    surface, transport, _ = _surface()
    surface.offer(_action())
    await surface.flush()
    await surface.sync({"r-1"})
    assert transport.delete_calls == []
    assert surface.surfaced_count == 1


async def test_sync_delete_failure_edits_fallback() -> None:
    surface, transport, _ = _surface()
    transport.delete_ok = False
    surface.offer(_action())
    await surface.flush()
    ref = transport.send_calls[0]["ref"]
    with structlog.testing.capture_logs() as logs:
        await surface.sync(set())
    assert [c["message"].text for c in transport.edit_calls if c["ref"] == ref] == [
        "✅ No longer waiting."
    ]
    (retired,) = _events(logs, "approval_surface.retired")
    assert retired["deleted"] is False


async def test_sync_reposts_at_600_then_1800_with_replace() -> None:
    surface, transport, clock = _surface()
    surface.offer(_action())
    await surface.flush()
    start = clock.now
    sent_at = [0.0]
    for t in range(30, 3001, 30):
        clock.now = start + t
        before = len(transport.send_calls)
        await surface.sync({"r-1"})
        if len(transport.send_calls) > before:
            sent_at.append(float(t))
    assert sent_at == [0.0, 600.0, 2400.0]
    first, second, third = transport.send_calls
    assert second["options"].replace == first["ref"]
    assert third["options"].replace == second["ref"]
    assert "Still waiting for your approval (10 min)" in second["message"].text
    assert "Still waiting for your approval (40 min)" in third["message"].text
    assert second["options"].notify is True
    assert surface.surfaced_count == 1
    assert set(surface.callbacks()) == {
        "claude_control:approve:r-1",
        "claude_control:deny:r-1",
    }


async def test_repost_send_failure_keeps_old_ref() -> None:
    surface, transport, clock = _surface(anchor=None)
    surface.offer(_action())
    await surface.flush()
    first_ref = transport.send_calls[0]["ref"]
    transport.send_results = [False]
    clock.now += 601
    await surface.sync({"r-1"})
    assert len(transport.send_calls) == 2
    # Old copy still the visible one; the retire later deletes it.
    await surface.sync(set())
    assert transport.delete_calls == [first_ref]


async def test_note_completed_retires() -> None:
    surface, transport, _ = _surface()
    surface.offer(_action())
    await surface.flush()
    ref = transport.send_calls[0]["ref"]
    surface.note_completed("claude.control.1")
    surface.note_completed("unrelated")
    assert surface.has_work
    with structlog.testing.capture_logs() as logs:
        await surface.flush()
    assert transport.delete_calls == [ref]
    (retired,) = _events(logs, "approval_surface.retired")
    assert retired["reason"] == "completed"
    assert surface.drain_retired_actions() == [("claude.control.1", "completed")]


async def test_aclose_retires_all() -> None:
    surface, transport, _ = _surface()
    surface.offer(_action("r-1", action_id="c.1"))
    surface.offer(_action("r-2", action_id="c.2"))
    await surface.flush()
    surface.offer(_action("r-3", action_id="c.3"))  # queued, never sent
    with structlog.testing.capture_logs() as logs:
        await surface.aclose("run_end")
    assert len(transport.delete_calls) == 2
    reasons = {e["reason"] for e in _events(logs, "approval_surface.retired")}
    assert reasons == {"run_end"}
    assert surface.request_ids() == frozenset()
    assert len(transport.send_calls) == 2  # r-3 never sent after close


async def test_completed_before_first_send_never_sends() -> None:
    surface, transport, _ = _surface()
    surface.offer(_action())
    surface.note_completed("claude.control.1")
    await surface.flush()
    assert transport.send_calls == []
    assert surface.request_ids() == frozenset()


# ── rescue path ──────────────────────────────────────────────────────────


async def test_rescue_builds_synthetic_approve_deny() -> None:
    surface, transport, _ = _surface()
    assert surface.rescue(_snap("r-9")) is True
    with structlog.testing.capture_logs() as logs:
        await surface.flush()
    (call,) = transport.send_calls
    rows = call["message"].extra["reply_markup"]["inline_keyboard"]
    data = [b["callback_data"] for row in rows for b in row]
    assert data == ["claude_control:approve:r-9", "claude_control:deny:r-9"]
    assert "Claude Code needs your approval" in call["message"].text
    assert "Bash" in call["message"].text
    (sent,) = _events(logs, "approval_surface.sent")
    assert sent["source"] == "rescue"


def test_rescue_da_callback_within_64_bytes() -> None:
    rid = "da:" + "0" * 36
    for row in synth_keyboard(rid):
        for button in row:
            assert len(button["callback_data"].encode()) <= 64


def test_rescue_skips_answerable_by_text_and_no_writer() -> None:
    surface, _, _ = _surface()
    assert surface.rescue(_snap(kind="ask", answerable_by_text=True)) is False
    assert surface.rescue(_snap(writer_ok=False)) is False
    assert surface.request_ids() == frozenset()


async def test_rescue_prefers_tracked_keyboard() -> None:
    surface, transport, _ = _surface()
    tracked = _action(
        "r-9",
        buttons=[[{"text": "Option A", "callback_data": "aq:r-9:0"}]],
    )
    assert surface.rescue(_snap("r-9"), action=tracked) is True
    await surface.flush()
    rows = transport.send_calls[0]["message"].extra["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "Option A", "callback_data": "aq:r-9:0"}]]


async def test_rescue_does_not_duplicate_offer() -> None:
    surface, _, _ = _surface()
    surface.offer(_action("r-9"))
    assert surface.rescue(_snap("r-9")) is False


async def test_callbacks_and_request_ids_reflect_live_entries() -> None:
    surface, _, _ = _surface()
    surface.offer(_action())
    assert surface.request_ids() == frozenset({"r-1"})
    assert surface.callbacks() == frozenset()  # nothing visible yet
    await surface.flush()
    assert "claude_control:approve:r-1" in surface.callbacks()
    await surface.sync(set())
    assert surface.callbacks() == frozenset()
    assert surface.request_ids() == frozenset()


# ── concurrency (review amendment 3) ─────────────────────────────────────


async def test_flush_and_sync_concurrently_leave_one_message() -> None:
    """A retire racing an in-flight send must not orphan the keyboard."""
    surface, transport, _ = _surface()
    transport.send_gate = anyio.Event()
    surface.offer(_action())
    async with anyio.create_task_group() as tg:
        tg.start_soon(surface.flush)
        await anyio.sleep(0.01)
        tg.start_soon(surface.sync, set())
        await anyio.sleep(0.01)
        transport.send_gate.set()
    refs = [c["ref"] for c in transport.send_calls if c["ref"] is not None]
    assert len(refs) == 1
    assert transport.delete_calls == refs  # the sent copy was retired
    assert surface.request_ids() == frozenset()


async def test_completion_during_send_retires_the_new_copy() -> None:
    surface, transport, _ = _surface()
    transport.send_gate = anyio.Event()
    surface.offer(_action())
    async with anyio.create_task_group() as tg:
        tg.start_soon(surface.flush)
        await anyio.sleep(0.01)
        surface.note_completed("claude.control.1")
        transport.send_gate.set()
    (call,) = transport.send_calls
    assert transport.delete_calls == [call["ref"]]
    assert surface.request_ids() == frozenset()
