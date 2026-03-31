"""Tests for /threads command (AMP thread management)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

import pytest

from untether.commands import CommandContext
from untether.telegram.commands.threads import (
    _THREAD_REGISTRY,
    ThreadsCommand,
    _format_thread_detail,
    _format_thread_list,
    _register_thread,
    _resolve_thread,
)
from untether.transport import MessageRef, RenderedMessage


@dataclass
class FakeSent:
    message: RenderedMessage | str
    reply_to: MessageRef | None = None
    notify: bool = True


@dataclass
class FakeExecutor:
    sent: list[FakeSent] = field(default_factory=list)

    async def send(
        self,
        message: RenderedMessage | str,
        *,
        reply_to: MessageRef | None = None,
        notify: bool = True,
    ) -> MessageRef | None:
        self.sent.append(FakeSent(message=message, reply_to=reply_to, notify=notify))
        return None

    async def edit(self, ref, message):
        return None

    async def run_one(self, request, *, mode="emit"):
        return None

    async def run_many(self, requests, *, mode="emit", parallel=False):
        return []


@dataclass
class FakeRuntime:
    def plugin_config(self, _id: str) -> dict[str, Any]:
        return {}


def _make_ctx(args_text: str = "", command: str = "threads") -> CommandContext:
    return CommandContext(
        command=command,
        text=f"/{command} {args_text}".strip(),
        args_text=args_text,
        args=tuple(args_text.split()) if args_text else (),
        message=MessageRef(channel_id=123, message_id=1, thread_id=None, sender_id=1),
        reply_to=None,
        reply_text=None,
        config_path=None,
        plugin_config={},
        runtime=FakeRuntime(),  # type: ignore[arg-type]
        executor=FakeExecutor(),  # type: ignore[arg-type]
    )


# --- Registry tests ---


def test_register_and_resolve_thread() -> None:
    _THREAD_REGISTRY.clear()
    tid = _register_thread("T-abc-123")
    assert _resolve_thread(tid) == "T-abc-123"


def test_register_same_thread_returns_same_id() -> None:
    _THREAD_REGISTRY.clear()
    tid1 = _register_thread("T-def-456")
    tid2 = _register_thread("T-def-456")
    assert tid1 == tid2


def test_resolve_unknown_returns_none() -> None:
    assert _resolve_thread(99999) is None


# --- Formatting tests ---


def test_format_thread_list_empty() -> None:
    text, buttons = _format_thread_list([])
    assert "No AMP threads" in text
    assert buttons == []


def test_format_thread_list_with_threads() -> None:
    _THREAD_REGISTRY.clear()
    threads = [
        {"id": "T-aaa-111", "title": "Fix bug"},
        {"id": "T-bbb-222", "title": "Add feature"},
    ]
    text, buttons = _format_thread_list(threads)
    assert "AMP threads" in text
    assert len(buttons) == 2
    assert "threads:v:" in buttons[0][0]["callback_data"]
    assert "threads:v:" in buttons[1][0]["callback_data"]


def test_format_thread_list_truncates_long_titles() -> None:
    _THREAD_REGISTRY.clear()
    threads = [{"id": "T-ccc-333", "title": "A" * 50}]
    _text, buttons = _format_thread_list(threads)
    button_text = buttons[0][0]["text"]
    assert len(button_text) < 60  # title truncated to 40 + suffix


def test_format_thread_detail() -> None:
    _THREAD_REGISTRY.clear()
    text, buttons = _format_thread_detail("T-xyz-789", {"title": "My thread"})
    assert "My thread" in text
    assert "T-xyz-789" in text
    # Should have Resume, Archive, and Back buttons
    assert len(buttons) == 2
    assert any("Resume" in b["text"] for row in buttons for b in row)
    assert any("Archive" in b["text"] for row in buttons for b in row)
    assert any("Back" in b["text"] for row in buttons for b in row)


def test_format_thread_detail_with_metadata() -> None:
    _THREAD_REGISTRY.clear()
    text, _buttons = _format_thread_detail(
        "T-xyz-789",
        {
            "title": "Thread",
            "created_at": "2026-01-01",
            "num_turns": 5,
        },
    )
    assert "2026-01-01" in text
    assert "Turns: 5" in text


# --- Command handler tests ---


@pytest.fixture(autouse=True)
def _fake_amp_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure shutil.which("amp") returns a path so CLI-check passes."""
    import untether.telegram.commands.threads as threads_mod

    _real_which = shutil.which

    def _patched_which(name: str, *a: Any, **kw: Any) -> str | None:
        if name == "amp":
            return "/usr/local/bin/amp"
        return _real_which(name, *a, **kw)

    monkeypatch.setattr(threads_mod.shutil, "which", _patched_which)


@pytest.mark.anyio
async def test_threads_no_amp_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """When amp CLI is not found, return helpful error."""
    import untether.telegram.commands.threads as threads_mod

    monkeypatch.setattr(threads_mod.shutil, "which", lambda _name, **kw: None)

    cmd = ThreadsCommand()
    ctx = _make_ctx()
    result = await cmd.handle(ctx)
    assert result is not None
    assert "not found" in result.text.lower()


@pytest.mark.anyio
async def test_threads_usage_with_unknown_subcommand() -> None:
    """Unknown subcommand shows usage."""
    cmd = ThreadsCommand()
    ctx = _make_ctx("foobar")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "Usage" in result.text


@pytest.mark.anyio
async def test_threads_view_invalid_id() -> None:
    cmd = ThreadsCommand()
    ctx = _make_ctx("v:notanumber")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "Invalid" in result.text


@pytest.mark.anyio
async def test_threads_view_expired_id() -> None:
    _THREAD_REGISTRY.clear()
    cmd = ThreadsCommand()
    ctx = _make_ctx("v:999")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "expired" in result.text.lower()


@pytest.mark.anyio
async def test_threads_resume_returns_continue_command() -> None:
    _THREAD_REGISTRY.clear()
    tid = _register_thread("T-resume-test-123")
    cmd = ThreadsCommand()
    ctx = _make_ctx(f"r:{tid}")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "amp threads continue" in result.text
    assert "T-resume-test-123" in result.text


@pytest.mark.anyio
async def test_threads_archive_invalid_id() -> None:
    cmd = ThreadsCommand()
    ctx = _make_ctx("a:bad")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "Invalid" in result.text


@pytest.mark.anyio
async def test_threads_archive_expired_id() -> None:
    _THREAD_REGISTRY.clear()
    cmd = ThreadsCommand()
    ctx = _make_ctx("a:888")
    result = await cmd.handle(ctx)
    assert result is not None
    assert "expired" in result.text.lower()


def test_callback_data_within_64_bytes() -> None:
    """Verify callback_data fits Telegram's 64-byte limit."""
    _THREAD_REGISTRY.clear()
    threads = [{"id": f"T-{'a' * 36}", "title": "test"}]
    _text, buttons = _format_thread_list(threads)
    for row in buttons:
        for btn in row:
            data = btn.get("callback_data", "")
            assert len(data.encode("utf-8")) <= 64, f"callback_data too long: {data}"
