"""Tests for the /at delayed-run command and at_scheduler (#288)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import pytest

from untether.commands import CommandContext
from untether.telegram import at_scheduler
from untether.telegram.commands.at import AtCommand, _format_delay, _parse_args
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio


# ── Parse tests ─────────────────────────────────────────────────────────


class TestParse:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("60s test", (60, "test")),
            ("2m hello world", (120, "hello world")),
            ("1h do a thing", (3600, "do a thing")),
            ("30m multi\nline\nprompt", (1800, "multi\nline\nprompt")),
            ("   5m   extra space   ", (300, "extra space")),
            ("90s single seconds", (90, "single seconds")),
            ("24h max", (86400, "max")),
        ],
    )
    def test_parse_valid(self, text, expected):
        assert _parse_args(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "30m",  # no prompt
            "30m   ",  # whitespace-only prompt
            "1d hello",  # days unit not supported
            "x10s hello",  # letter before number
            "59s hello",  # below minimum
            "25h hello",  # above maximum (86400s = 24h, 25h = 90000s)
            "0s hello",  # zero
            "hello world",  # no duration
            "10 hello",  # missing unit
        ],
    )
    def test_parse_invalid(self, text):
        assert _parse_args(text) is None

    def test_parse_unit_case_insensitive(self):
        assert _parse_args("30M hello") == (1800, "hello")
        assert _parse_args("2H go") == (7200, "go")


# ── _format_delay tests ──────────────────────────────────────────────────


class TestFormatDelay:
    @pytest.mark.parametrize(
        "delay_s,expected",
        [
            (30, "30s"),
            (60, "1m"),
            (90, "1m 30s"),
            (600, "10m"),
            (3600, "1h"),
            (3660, "1h 1m"),
            (5400, "1h 30m"),
        ],
    )
    def test_format(self, delay_s, expected):
        assert _format_delay(delay_s) == expected


# ── Scheduler fakes ──────────────────────────────────────────────────────


@dataclass
class FakeTransport:
    sent: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.sent = []

    async def send(self, *, channel_id, message, options=None, **_):
        self.sent.append((channel_id, message.text, options))
        return MessageRef(channel_id=channel_id, message_id=9999)

    async def edit(self, *, ref, message, **_):
        return ref

    async def delete(self, ref):
        return None


class RunJobRecorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)


# ── AtCommand.handle tests ──────────────────────────────────────────────


def _make_ctx(args_text: str, chat_id: int = 12345) -> CommandContext:
    message = MessageRef(channel_id=chat_id, message_id=1)
    return CommandContext(
        command="at",
        text=f"/at {args_text}",
        args_text=args_text,
        args=tuple(args_text.split()),
        message=message,
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config={},
        runtime=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
    )


class TestAtCommand:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        """Each test starts with a clean scheduler state."""
        at_scheduler.uninstall()
        yield
        at_scheduler.uninstall()

    async def test_usage_when_empty(self):
        result = await AtCommand().handle(_make_ctx(""))
        assert result is not None
        assert "Usage: /at" in result.text

    async def test_scheduler_not_installed(self):
        result = await AtCommand().handle(_make_ctx("60s test"))
        assert result is not None
        assert "not installed" in result.text

    async def test_invalid_format_reply(self):
        # Install so parsing actually runs all the way through.
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, _fake_run_job, FakeTransport(), 999)
            try:
                result = await AtCommand().handle(_make_ctx("xyz prompt"))
                assert result is not None
                assert "\u274c" in result.text
                assert "Usage" in result.text
            finally:
                tg.cancel_scope.cancel()

    async def test_schedule_successful(self):
        run_recorder = RunJobRecorder()
        transport = FakeTransport()
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, run_recorder, transport, 12345)
            try:
                result = await AtCommand().handle(_make_ctx("60s test prompt"))
                assert result is not None
                assert "Scheduled" in result.text
                assert "1m" in result.text
                assert "Cancel with /cancel" in result.text
                # One pending delay should be tracked.
                pending = at_scheduler.pending_for_chat(12345)
                assert len(pending) == 1
                assert pending[0].prompt == "test prompt"
            finally:
                tg.cancel_scope.cancel()


# ── Scheduler: schedule / cancel / drain ────────────────────────────────


class TestAtScheduler:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        at_scheduler.uninstall()
        yield
        at_scheduler.uninstall()

    async def test_schedule_rejects_below_min(self):
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, _fake_run_job, FakeTransport(), 1)
            try:
                with pytest.raises(at_scheduler.AtSchedulerError):
                    at_scheduler.schedule_delayed_run(1, None, 30, "x")
            finally:
                tg.cancel_scope.cancel()

    async def test_schedule_rejects_above_max(self):
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, _fake_run_job, FakeTransport(), 1)
            try:
                with pytest.raises(at_scheduler.AtSchedulerError):
                    at_scheduler.schedule_delayed_run(
                        1, None, at_scheduler.MAX_DELAY_SECONDS + 1, "x"
                    )
            finally:
                tg.cancel_scope.cancel()

    async def test_schedule_respects_per_chat_cap(self):
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, _fake_run_job, FakeTransport(), 1)
            try:
                for _ in range(at_scheduler.PER_CHAT_LIMIT):
                    at_scheduler.schedule_delayed_run(1, None, 60, "x")
                with pytest.raises(at_scheduler.AtSchedulerError):
                    at_scheduler.schedule_delayed_run(1, None, 60, "over cap")
            finally:
                tg.cancel_scope.cancel()

    async def test_cancel_pending_for_chat(self):
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, _fake_run_job, FakeTransport(), 1)
            try:
                at_scheduler.schedule_delayed_run(111, None, 60, "a")
                at_scheduler.schedule_delayed_run(111, None, 60, "b")
                at_scheduler.schedule_delayed_run(222, None, 60, "c")
                assert at_scheduler.active_count() == 3
                cancelled = at_scheduler.cancel_pending_for_chat(111)
                assert cancelled == 2
                assert at_scheduler.active_count() == 1
                assert at_scheduler.pending_for_chat(222)[0].prompt == "c"
            finally:
                tg.cancel_scope.cancel()

    async def test_uninstall_clears_pending(self):
        async with anyio.create_task_group() as tg:
            at_scheduler.install(tg, _fake_run_job, FakeTransport(), 1)
            at_scheduler.schedule_delayed_run(1, None, 60, "x")
            assert at_scheduler.active_count() == 1
            tg.cancel_scope.cancel()
        at_scheduler.uninstall()
        assert at_scheduler.active_count() == 0


async def _fake_run_job(*args, **kwargs):
    """Drop-in replacement for run_job — does nothing."""
    return
