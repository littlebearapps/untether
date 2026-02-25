"""Tests for the trigger dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest

from untether.context import RunContext
from untether.transport import MessageRef, RenderedMessage, SendOptions
from untether.triggers.dispatcher import TriggerDispatcher
from untether.triggers.settings import CronConfig, WebhookConfig


@dataclass
class FakeTransport:
    """Minimal transport stub for dispatcher tests."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.sent.append(
            {"channel_id": channel_id, "text": message.text, "options": options}
        )
        return ref

    async def edit(self, *, ref, message, wait=True):
        return ref

    async def delete(self, *, ref):
        return True

    async def close(self):
        pass


@dataclass
class RunJobCapture:
    """Capture run_job calls."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        chat_id: int,
        user_msg_id: int,
        text: str,
        resume_token,
        context,
        thread_id=None,
        chat_session_key=None,
        reply_ref=None,
        on_thread_known=None,
        engine_override=None,
        progress_ref=None,
    ) -> None:
        self.calls.append(
            {
                "chat_id": chat_id,
                "user_msg_id": user_msg_id,
                "text": text,
                "resume_token": resume_token,
                "context": context,
                "engine_override": engine_override,
            }
        )


def _make_webhook(**kwargs: Any) -> WebhookConfig:
    defaults = {
        "id": "test-wh",
        "path": "/hooks/test",
        "auth": "none",
        "prompt_template": "Hello",
    }
    defaults.update(kwargs)
    return WebhookConfig(**defaults)


def _make_cron(**kwargs: Any) -> CronConfig:
    defaults = {
        "id": "test-cron",
        "schedule": "* * * * *",
        "prompt": "Run daily check",
    }
    defaults.update(kwargs)
    return CronConfig(**defaults)


@pytest.mark.anyio
async def test_webhook_dispatch_sends_notification():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=tg,
        )
        await dispatcher.dispatch_webhook(_make_webhook(), "Test prompt")
        # Let the task group run the fire-and-forget job.
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    # Should have sent a notification message.
    assert len(transport.sent) >= 1
    assert "webhook:test-wh" in transport.sent[0]["text"]


@pytest.mark.anyio
async def test_webhook_dispatch_calls_run_job():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=tg,
        )
        await dispatcher.dispatch_webhook(_make_webhook(), "Test prompt")
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert len(run_job.calls) == 1
    call = run_job.calls[0]
    assert call["chat_id"] == 100
    assert call["text"] == "Test prompt"
    assert call["resume_token"] is None


@pytest.mark.anyio
async def test_webhook_uses_custom_chat_id():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=tg,
        )
        wh = _make_webhook(chat_id=-999)
        await dispatcher.dispatch_webhook(wh, "Hello")
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert run_job.calls[0]["chat_id"] == -999


@pytest.mark.anyio
async def test_webhook_with_project_creates_context():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=tg,
        )
        wh = _make_webhook(project="myapp")
        await dispatcher.dispatch_webhook(wh, "Deploy")
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    ctx = run_job.calls[0]["context"]
    assert isinstance(ctx, RunContext)
    assert ctx.project == "myapp"


@pytest.mark.anyio
async def test_webhook_with_engine_override():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=tg,
        )
        wh = _make_webhook(engine="claude")
        await dispatcher.dispatch_webhook(wh, "Review")
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert run_job.calls[0]["engine_override"] == "claude"


@pytest.mark.anyio
async def test_cron_dispatch_sends_notification():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=200,
            task_group=tg,
        )
        await dispatcher.dispatch_cron(_make_cron())
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert len(transport.sent) >= 1
    assert "cron:test-cron" in transport.sent[0]["text"]


@pytest.mark.anyio
async def test_cron_dispatch_calls_run_job():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=200,
            task_group=tg,
        )
        cron = _make_cron(project="infra", engine="codex")
        await dispatcher.dispatch_cron(cron)
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    call = run_job.calls[0]
    assert call["chat_id"] == 200
    assert call["text"] == "Run daily check"
    assert call["context"].project == "infra"
    assert call["engine_override"] == "codex"


@pytest.mark.anyio
async def test_no_project_means_no_context():
    transport = FakeTransport()
    run_job = RunJobCapture()

    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=tg,
        )
        await dispatcher.dispatch_webhook(_make_webhook(), "Plain")
        await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert run_job.calls[0]["context"] is None
