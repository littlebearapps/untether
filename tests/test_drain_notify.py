"""Tests for per-chat drain notifications during graceful shutdown."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from untether.runner_bridge import RunningTask
from untether.telegram.loop import _notify_drain_start, _notify_drain_timeout
from untether.transport import MessageRef, RenderedMessage, SendOptions


@dataclass
class FakeTransport:
    send_calls: list[dict] = field(default_factory=list)
    _fail_channels: set[int] = field(default_factory=set)

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        if int(channel_id) in self._fail_channels:
            raise RuntimeError("transport error")
        ref = MessageRef(channel_id=channel_id, message_id=1)
        self.send_calls.append({"channel_id": channel_id, "text": message.text})
        return ref


def _make_tasks(*channel_ids: int) -> dict[MessageRef, RunningTask]:
    """Create running_tasks dict with the given channel IDs."""
    tasks: dict[MessageRef, RunningTask] = {}
    for i, cid in enumerate(channel_ids):
        ref = MessageRef(channel_id=cid, message_id=i + 100)
        tasks[ref] = RunningTask()
    return tasks


class TestNotifyDrainStart:
    @pytest.mark.anyio
    async def test_sends_to_each_active_chat(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111, 222, 333)

        await _notify_drain_start(transport, tasks)

        channel_ids = [c["channel_id"] for c in transport.send_calls]
        assert sorted(channel_ids) == [111, 222, 333]

    @pytest.mark.anyio
    async def test_deduplicates_same_channel(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111, 111, 222)

        await _notify_drain_start(transport, tasks)

        channel_ids = [c["channel_id"] for c in transport.send_calls]
        assert sorted(channel_ids) == [111, 222]

    @pytest.mark.anyio
    async def test_message_text_contains_restarting(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111)

        await _notify_drain_start(transport, tasks)

        assert len(transport.send_calls) == 1
        text = transport.send_calls[0]["text"]
        assert "Restarting" in text
        assert "waiting" in text.lower()

    @pytest.mark.anyio
    async def test_transport_error_swallowed(self) -> None:
        transport = FakeTransport(_fail_channels={111})
        tasks = _make_tasks(111, 222)

        # Should not raise despite transport error on channel 111
        await _notify_drain_start(transport, tasks)

        # Channel 222 should still be notified
        channel_ids = [c["channel_id"] for c in transport.send_calls]
        assert channel_ids == [222]

    @pytest.mark.anyio
    async def test_empty_running_tasks(self) -> None:
        transport = FakeTransport()
        await _notify_drain_start(transport, {})
        assert transport.send_calls == []


class TestNotifyDrainTimeout:
    @pytest.mark.anyio
    async def test_sends_to_each_remaining_chat(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111, 222)

        await _notify_drain_timeout(transport, tasks, remaining=2)

        channel_ids = [c["channel_id"] for c in transport.send_calls]
        assert sorted(channel_ids) == [111, 222]

    @pytest.mark.anyio
    async def test_deduplicates_same_channel(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111, 111, 111)

        await _notify_drain_timeout(transport, tasks, remaining=3)

        assert len(transport.send_calls) == 1
        assert transport.send_calls[0]["channel_id"] == 111

    @pytest.mark.anyio
    async def test_message_contains_remaining_count(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111)

        await _notify_drain_timeout(transport, tasks, remaining=3)

        text = transport.send_calls[0]["text"]
        assert "3 run(s) interrupted" in text

    @pytest.mark.anyio
    async def test_message_contains_resume_hint(self) -> None:
        transport = FakeTransport()
        tasks = _make_tasks(111)

        await _notify_drain_timeout(transport, tasks, remaining=1)

        text = transport.send_calls[0]["text"]
        assert "session is saved" in text
        assert "/claude" in text

    @pytest.mark.anyio
    async def test_transport_error_swallowed(self) -> None:
        transport = FakeTransport(_fail_channels={222})
        tasks = _make_tasks(111, 222)

        await _notify_drain_timeout(transport, tasks, remaining=2)

        channel_ids = [c["channel_id"] for c in transport.send_calls]
        assert channel_ids == [111]

    @pytest.mark.anyio
    async def test_empty_running_tasks(self) -> None:
        transport = FakeTransport()
        await _notify_drain_timeout(transport, {}, remaining=0)
        assert transport.send_calls == []
