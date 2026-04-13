"""Tests for the webhook HTTP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from untether.transport import MessageRef
from untether.triggers.dispatcher import TriggerDispatcher
from untether.triggers.server import build_webhook_app
from untether.triggers.settings import TriggersSettings, parse_trigger_config


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
            task_group=FakeTaskGroup(),  # type: ignore[arg-type]
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
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "gh",
                    "path": "/hooks/gh",
                    "auth": "none",
                    "event_filter": "push",
                    "prompt_template": "{{action}}",
                }
            ],
        }
    )
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
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "gh",
                    "path": "/hooks/gh",
                    "auth": "none",
                    "event_filter": "push",
                    "prompt_template": "{{action}}",
                }
            ],
        }
    )
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
async def test_dispatch_errors_dont_fail_http_response(caplog):
    """After #281 fix, dispatch is fire-and-forget; errors log but don't surface as 500.

    The previous behavior (HTTP 500 on dispatch exception) was a side effect of the
    awaited-dispatch bug that caused rate limiter ineffectiveness. Now the HTTP
    response is immediate (202) and dispatch exceptions are logged.
    """
    import asyncio

    settings = _make_settings()

    class ExplodingDispatcher:
        async def dispatch_webhook(self, wh, prompt):
            raise RuntimeError("boom")

    app = build_webhook_app(settings, ExplodingDispatcher())  # type: ignore[arg-type]

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
            json={"text": "hello"},
        )
        assert resp.status == 202
        # Give the background task a chance to run and log.
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_multipart_file_upload_saves_file(tmp_path):
    """Regression #280: multipart uploads must succeed and write file to disk."""
    dest = tmp_path / "uploads"
    dest.mkdir()
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "mp",
                    "path": "/hooks/mp",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "file_write",
                    "accept_multipart": True,
                    "file_destination": str(dest / "{{file.filename}}"),
                    "file_path": str(dest / "fallback.bin"),
                    "notify_on_success": True,
                }
            ],
        }
    )
    transport = FakeTransport()
    dispatcher, _, _ = _make_dispatcher(transport=transport)
    app = build_webhook_app(settings, dispatcher)

    # Build a minimal multipart body by hand (exercises the raw-body path).
    boundary = "X-UNTETHER-TEST"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
        f"Hello from multipart\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    headers = {
        "Authorization": "Bearer tok_123",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post("/hooks/mp", headers=headers, data=body)
        assert resp.status == 202, await resp.text()

    saved = dest / "hello.txt"
    assert saved.exists(), f"expected file at {saved}"
    assert saved.read_bytes() == b"Hello from multipart"


@pytest.mark.anyio
async def test_multipart_with_form_fields_and_file(tmp_path):
    """Regression #280: multipart with non-file form fields must also parse."""
    dest = tmp_path / "uploads"
    dest.mkdir()
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "mp",
                    "path": "/hooks/mp",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "file_write",
                    "accept_multipart": True,
                    "file_destination": str(dest / "{{file.filename}}"),
                    "file_path": str(dest / "fallback.bin"),
                }
            ],
        }
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)

    boundary = "X-UNTETHER-TEST"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="metadata"\r\n\r\n'
        f"batch-42\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="data.json"\r\n'
        f"Content-Type: application/json\r\n\r\n"
        f'{{"k":"v"}}\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    headers = {
        "Authorization": "Bearer tok_123",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post("/hooks/mp", headers=headers, data=body)
        assert resp.status == 202, await resp.text()

    saved = dest / "data.json"
    assert saved.exists()
    assert saved.read_bytes() == b'{"k":"v"}'


@pytest.mark.anyio
async def test_multipart_file_too_large_returns_413(tmp_path):
    """Regression #280: per-file size limit still enforced under the new path."""
    dest = tmp_path / "uploads"
    dest.mkdir()
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "mp",
                    "path": "/hooks/mp",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "file_write",
                    "accept_multipart": True,
                    "file_destination": str(dest / "{{file.filename}}"),
                    "file_path": str(dest / "fallback.bin"),
                    "max_file_size_bytes": 1024,
                }
            ],
        }
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)

    boundary = "X-UNTETHER-TEST"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n\r\n'
        + ("A" * 2000)
        + f"\r\n--{boundary}--\r\n"
    ).encode()
    headers = {
        "Authorization": "Bearer tok_123",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post("/hooks/mp", headers=headers, data=body)
        assert resp.status == 413, await resp.text()


@pytest.mark.anyio
async def test_multipart_unsafe_filename_sanitised(tmp_path):
    """Regression #280: traversal-style filenames must be neutralised."""
    dest = tmp_path / "uploads"
    dest.mkdir()
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "mp",
                    "path": "/hooks/mp",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "file_write",
                    "accept_multipart": True,
                    "file_destination": str(dest / "{{file.filename}}"),
                    "file_path": str(dest / "fallback.bin"),
                }
            ],
        }
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)

    boundary = "X-UNTETHER-TEST"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="../../etc/passwd"\r\n\r\n'
        f"evil\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    headers = {
        "Authorization": "Bearer tok_123",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post("/hooks/mp", headers=headers, data=body)
        assert resp.status == 202, await resp.text()

    # Must land inside the expected directory, not escape.
    assert (dest / "upload.bin").exists() or any(dest.glob("*"))
    # Ensure we did NOT write to /etc/passwd or anywhere above tmp_path.
    assert not (tmp_path.parent / "etc" / "passwd").exists()


@pytest.mark.anyio
async def test_multipart_auth_failure_returns_401(tmp_path):
    """Regression #280: auth still rejects wrong bearer on multipart."""
    dest = tmp_path / "uploads"
    dest.mkdir()
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "mp",
                    "path": "/hooks/mp",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "file_write",
                    "accept_multipart": True,
                    "file_destination": str(dest / "{{file.filename}}"),
                    "file_path": str(dest / "fallback.bin"),
                }
            ],
        }
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)

    boundary = "X-UNTETHER-TEST"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
        f"nope\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    headers = {
        "Authorization": "Bearer wrong",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post("/hooks/mp", headers=headers, data=body)
        assert resp.status == 401


@pytest.mark.anyio
async def test_rate_limit_returns_429_under_burst():
    """Regression #281: rate limiter must return 429 once bucket is drained."""
    import asyncio

    settings = parse_trigger_config(
        {
            "enabled": True,
            "server": {"rate_limit": 10},
            "webhooks": [
                {
                    "id": "burst",
                    "path": "/hooks/burst",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "notify_only",
                    "message_template": "x",
                }
            ],
        }
    )
    transport = FakeTransport()
    dispatcher, _, _ = _make_dispatcher(transport=transport)
    app = build_webhook_app(settings, dispatcher)

    async with TestClient(TestServer(app)) as cl:
        # Fire 30 requests concurrently — bucket starts at 10.
        resps = await asyncio.gather(
            *[
                cl.post(
                    "/hooks/burst",
                    headers={"Authorization": "Bearer tok_123"},
                    json={},
                )
                for _ in range(30)
            ]
        )
        statuses = [r.status for r in resps]
        accepted = sum(1 for s in statuses if s == 202)
        limited = sum(1 for s in statuses if s == 429)
        # With rate_limit=10 and no meaningful refill during the burst,
        # we expect at most ~10 accepted and the rest limited.
        assert accepted <= 15, f"too many accepted: {accepted} (statuses={statuses})"
        assert limited >= 15, f"too few 429s: {limited} (statuses={statuses})"


@pytest.mark.anyio
async def test_webhook_returns_202_before_dispatch_completes():
    """Regression #281: slow dispatch must not block HTTP 202 response."""
    import asyncio

    dispatch_started = asyncio.Event()
    dispatch_release = asyncio.Event()

    class SlowDispatcher:
        async def dispatch_webhook(self, wh, prompt):
            dispatch_started.set()
            # Block until the test releases us.
            await dispatch_release.wait()

    settings = _make_settings()
    app = build_webhook_app(settings, SlowDispatcher())  # type: ignore[arg-type]

    async with TestClient(TestServer(app)) as cl:
        # Start the request — it should return 202 without waiting for dispatch.
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": "Bearer tok_123"},
            json={"text": "hello"},
        )
        assert resp.status == 202
        # Dispatch should still be running (blocked on dispatch_release).
        assert dispatch_started.is_set()
        # Release it so the test can clean up.
        dispatch_release.set()


@pytest.mark.anyio
async def test_event_filter_allows_matching():
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "gh",
                    "path": "/hooks/gh",
                    "auth": "none",
                    "event_filter": "push",
                    "prompt_template": "{{ref}}",
                }
            ],
        }
    )
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
