"""Tests for the webhook HTTP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from untether.transport import MessageRef
from untether.triggers.dispatcher import TriggerDispatcher
from untether.triggers.settings import TriggersSettings, parse_trigger_config
from untether.triggers.server import build_webhook_app


@dataclass
class FakeTransport:
    sent: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1

    async def send(self, *, channel_id, message, options=None):
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.sent.append({"channel_id": channel_id, "text": message.text})
        return ref

    async def edit(self, *, ref, message, wait=True):
        return ref

    async def delete(self, *, ref):
        return True

    async def close(self):
        pass


@dataclass
class FakeTaskGroup:
    tasks: list = field(default_factory=list)

    def start_soon(self, fn, *args):
        self.tasks.append((fn, args))


@dataclass
class RunJobCapture:
    calls: list = field(default_factory=list)

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)


def _make_settings(**overrides) -> TriggersSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "webhooks": [
            {
                "id": "test",
                "path": "/hooks/test",
                "auth": "bearer",
                "secret": "tok_123",
                "prompt_template": "Event: {{text}}",
            }
        ],
    }
    base.update(overrides)
    return parse_trigger_config(base)


def _make_dispatcher(transport=None, run_job=None):
    transport = transport or FakeTransport()
    run_job = run_job or RunJobCapture()
    return (
        TriggerDispatcher(
            run_job=run_job,
            transport=transport,
            default_chat_id=100,
            task_group=FakeTaskGroup(),
        ),
        transport,
        run_job,
    )


@pytest.mark.anyio
async def test_health_endpoint():
    settings = _make_settings()
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["webhooks"] == 1


@pytest.mark.anyio
async def test_unknown_path_returns_404():
    settings = _make_settings()
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/nonexistent",
            headers={"Authorization": "Bearer tok_123"},
            json={"text": "hello"},
        )
        assert resp.status == 404


@pytest.mark.anyio
async def test_valid_webhook_returns_202():
    settings = _make_settings()
    transport = FakeTransport()
    dispatcher, _, _ = _make_dispatcher(transport=transport)
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
            json={"text": "hello"},
        )
        assert resp.status == 202
        assert len(transport.sent) == 1


@pytest.mark.anyio
async def test_auth_failure_returns_401():
    settings = _make_settings()
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer wrong"},
            json={"text": "hello"},
        )
        assert resp.status == 401


@pytest.mark.anyio
async def test_invalid_json_returns_400():
    settings = _make_settings()
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
            data=b"not json{{{",
        )
        assert resp.status == 400


@pytest.mark.anyio
async def test_empty_body_accepted():
    settings = _make_settings()
    transport = FakeTransport()
    dispatcher, _, _ = _make_dispatcher(transport=transport)
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
        )
        assert resp.status == 202


@pytest.mark.anyio
async def test_non_dict_json_wrapped():
    settings = _make_settings()
    transport = FakeTransport()
    dispatcher, _, _ = _make_dispatcher(transport=transport)
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
            data=b'"just a string"',
        )
        assert resp.status == 202


@pytest.mark.anyio
async def test_event_filter_skips_non_matching():
    settings = parse_trigger_config({
        "enabled": True,
        "webhooks": [{
            "id": "gh",
            "path": "/hooks/gh",
            "auth": "none",
            "event_filter": "push",
            "prompt_template": "{{action}}",
        }],
    })
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/gh",
            headers={"X-GitHub-Event": "issues"},
            json={"action": "opened"},
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "filtered"


@pytest.mark.anyio
async def test_event_filter_blocks_when_header_missing():
    """Security fix: missing event header must not bypass the filter."""
    settings = parse_trigger_config({
        "enabled": True,
        "webhooks": [{
            "id": "gh",
            "path": "/hooks/gh",
            "auth": "none",
            "event_filter": "push",
            "prompt_template": "{{action}}",
        }],
    })
    dispatcher, transport, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        # No X-GitHub-Event or X-Event-Type header at all
        resp = await cl.post(
            "/hooks/gh",
            json={"action": "opened"},
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "filtered"
        assert len(transport.sent) == 0


@pytest.mark.anyio
async def test_internal_error_returns_500():
    """Security fix: unhandled exceptions return generic 500, not details."""
    settings = _make_settings()

    class ExplodingDispatcher:
        async def dispatch_webhook(self, wh, prompt):
            raise RuntimeError("boom")

    app = build_webhook_app(settings, ExplodingDispatcher())

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
            json={"text": "hello"},
        )
        assert resp.status == 500
        text = await resp.text()
        assert text == "internal error"


@pytest.mark.anyio
async def test_event_filter_allows_matching():
    settings = parse_trigger_config({
        "enabled": True,
        "webhooks": [{
            "id": "gh",
            "path": "/hooks/gh",
            "auth": "none",
            "event_filter": "push",
            "prompt_template": "{{ref}}",
        }],
    })
    transport = FakeTransport()
    dispatcher, _, _ = _make_dispatcher(transport=transport)
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/gh",
            headers={"X-GitHub-Event": "push"},
            json={"ref": "refs/heads/main"},
        )
        assert resp.status == 202
