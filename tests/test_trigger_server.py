"""Tests for the webhook HTTP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from untether.transport import MessageRef
from untether.triggers.dispatcher import TriggerDispatcher
from untether.triggers.server import (
    _unauthenticated_on_public_bind,
    build_webhook_app,
    is_loopback_host,
    run_webhook_server,
)
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


@pytest.mark.anyio
async def test_bind_failure_degrades_gracefully(monkeypatch):
    """#320: an OSError on TCPSite.start() must not propagate.

    Port conflicts (another process on the configured port) previously crashed
    the bot via an unhandled OSError propagating through the anyio task group.
    Now the webhook server logs a structured `triggers.server.bind_failed` and
    returns, so polling/commands/crons stay up.
    """
    from aiohttp.web import TCPSite

    from untether.triggers import server as server_module

    async def _raise_addr_in_use(self):  # type: ignore[no-untyped-def]
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(TCPSite, "start", _raise_addr_in_use)

    # Intercept the module-level structlog logger directly — more robust than
    # stdout/stderr capture, which depends on how structlog was configured by
    # earlier tests in the session.
    recorded: list[tuple[str, dict]] = []

    class _RecordingLogger:
        def info(self, event, **fields):
            recorded.append((event, fields))

        def error(self, event, **fields):
            recorded.append((event, fields))

    monkeypatch.setattr(server_module, "logger", _RecordingLogger())

    settings = _make_settings()
    dispatcher, _, _ = _make_dispatcher()

    # Must return normally — no exception propagates.
    await run_webhook_server(settings, dispatcher)

    events = [name for name, _ in recorded]
    assert "triggers.server.bind_failed" in events, (
        f"expected triggers.server.bind_failed in {events!r}"
    )
    fields = next(f for name, f in recorded if name == "triggers.server.bind_failed")
    assert fields["port"] == settings.server.port
    assert "ss -tlnp" in fields["hint"]
    assert "untether.toml" in fields["fix"]


# ── #382: startup policy — unauthenticated webhooks on non-loopback bind ──


def _unauth_settings(**overrides) -> TriggersSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "server": {"host": "0.0.0.0", "port": 9876},
        "webhooks": [
            {
                "id": "open",
                "path": "/hooks/open",
                "auth": "none",
                "prompt_template": "Event: {{text}}",
            }
        ],
    }
    base.update(overrides)
    return parse_trigger_config(base)


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("localhost", True),
        ("LocalHost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.50", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_unauthenticated_on_public_bind_flags_open_webhook():
    settings = _unauth_settings()
    assert _unauthenticated_on_public_bind(settings) == ["open"]


def test_unauthenticated_on_public_bind_optin_allows():
    settings = _unauth_settings(allow_unauthenticated_webhooks=True)
    assert _unauthenticated_on_public_bind(settings) == []


def test_unauthenticated_on_loopback_bind_allows():
    settings = _unauth_settings(server={"host": "127.0.0.1", "port": 9876})
    assert _unauthenticated_on_public_bind(settings) == []


def test_unauthenticated_on_public_bind_authed_is_safe():
    settings = _make_settings(server={"host": "0.0.0.0", "port": 9876})
    assert _unauthenticated_on_public_bind(settings) == []


@pytest.mark.anyio
async def test_run_webhook_server_refuses_unauthenticated_public_bind():
    """The server must NOT bind when an auth=none webhook is exposed on a
    non-loopback host without the opt-in — it returns after logging."""
    from structlog.testing import capture_logs

    settings = _unauth_settings()
    dispatcher, _, _ = _make_dispatcher()
    with capture_logs() as logs:
        # Refused path returns immediately; a successful bind would block on
        # sleep_forever, so completing the await proves the refusal.
        await run_webhook_server(settings, dispatcher)
    refused = [
        r for r in logs if r["event"] == "triggers.server.refused_unauthenticated"
    ]
    assert refused, "expected refusal log"
    assert refused[0]["webhook_ids"] == ["open"]


# ── #906: auth hardening ────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token",
    ["t\u00f6k_123", "\u2603", "tok_123\u00a0"],
)
async def test_906_non_ascii_bearer_returns_401_not_500(token):
    settings = _make_settings()
    dispatcher, transport, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/test",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "hello"},
        )
        assert resp.status == 401
    assert transport.sent == []


@pytest.mark.anyio
async def test_906_non_ascii_hmac_signature_returns_401_not_500():
    settings = _make_settings(
        webhooks=[
            {
                "id": "gh",
                "path": "/hooks/gh",
                "auth": "hmac-sha256",
                "secret": "s3cret",
                "prompt_template": "x",
            }
        ]
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/gh",
            headers={"X-Hub-Signature-256": "sha256=d\u00e9adbeef"},
            json={},
        )
        assert resp.status == 401


def _two_hook_settings(rate_limit: int) -> TriggersSettings:
    return parse_trigger_config(
        {
            "enabled": True,
            "server": {"rate_limit": rate_limit},
            "webhooks": [
                {
                    "id": hook,
                    "path": f"/hooks/{hook}",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "notify_only",
                    "message_template": "x",
                }
                for hook in ("a", "b")
            ],
        }
    )


@pytest.mark.anyio
async def test_906_failed_auth_is_rate_limited(monkeypatch):
    """A flood of bad credentials used to get an unlimited stream of 401s:
    the limiter ran only after auth passed. (A sender Untether can tell apart
    by address — the loopback case is covered further down.)"""
    _remote_from_test_header(monkeypatch)
    dispatcher, transport, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    bad = {"Authorization": "Bearer wrong", "X-Test-Remote": "203.0.113.9"}
    good = {"Authorization": "Bearer tok_123", "X-Test-Remote": "203.0.113.9"}
    async with TestClient(TestServer(app)) as cl:
        statuses = [
            (await cl.post("/hooks/a", headers=bad, json={})).status for _ in range(8)
        ]
        assert statuses[:3] == [401, 401, 401]
        assert set(statuses[3:]) == {429}

        # Fail closed: while locked out, even the right secret is refused,
        # so guessing can't continue past the limit.
        assert (await cl.post("/hooks/a", headers=good, json={})).status == 429

        # The lockout is per webhook — another webhook still works.
        assert (await cl.post("/hooks/b", headers=good, json={})).status == 202
        import asyncio

        await asyncio.sleep(0)
    assert len(transport.sent) == 1


@pytest.mark.anyio
async def test_906_failed_auth_does_not_spend_the_request_budget():
    """Bad credentials below the lockout threshold must not eat the tokens
    that legitimate deliveries use."""
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    bad = {"Authorization": "Bearer wrong"}
    good = {"Authorization": "Bearer tok_123"}
    async with TestClient(TestServer(app)) as cl:
        for _ in range(2):
            assert (await cl.post("/hooks/a", headers=bad, json={})).status == 401
        statuses = [
            (await cl.post("/hooks/a", headers=good, json={})).status for _ in range(3)
        ]
        assert statuses == [202, 202, 202]


@pytest.mark.anyio
async def test_906_global_refusal_does_not_spend_the_webhook_token(monkeypatch):
    """``allow(webhook) or not allow(global)`` spent a per-webhook token even
    when the global bucket then answered 429."""
    import untether.triggers.server as server_mod

    limiters: list[Any] = []

    class _SpyLimiter(server_mod.TokenBucketLimiter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            limiters.append(self)

    monkeypatch.setattr(server_mod, "TokenBucketLimiter", _SpyLimiter)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(2), dispatcher)
    good = {"Authorization": "Bearer tok_123"}
    async with TestClient(TestServer(app)) as cl:
        # Two deliveries to "a" drain the global bucket.
        for _ in range(2):
            assert (await cl.post("/hooks/a", headers=good, json={})).status == 202
        for _ in range(3):
            assert (await cl.post("/hooks/b", headers=good, json={})).status == 429

    # Two limiters since the #906 follow-up (requests, and failed auth keyed
    # by client address); this is the one that counts accepted requests.
    limiter = next(lim for lim in limiters if lim._max_keys is None)
    # "b" was refused by the global bucket every time, so its own bucket is
    # untouched (never created, or still full).
    tokens, _ = limiter._buckets.get("b", (2.0, 0.0))
    assert tokens >= 2.0


@pytest.mark.anyio
async def test_906_multipart_destination_uses_the_files_deny_list(tmp_path):
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
                    "file_path": str(tmp_path / "raw.bin"),
                    "accept_multipart": True,
                    "file_destination": str(tmp_path / "{{file.filename}}"),
                }
            ],
        }
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    import aiohttp

    form = aiohttp.FormData()
    form.add_field("file", b"KEY", filename="server.key")
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/mp", headers={"Authorization": "Bearer tok_123"}, data=form
        )
        assert resp.status == 400
    assert not (tmp_path / "server.key").exists()


# ── #906 follow-up: lockout scoped to the client address, no check/spend race ──

_BAD = {"Authorization": "Bearer wrong"}
_GOOD = {"Authorization": "Bearer tok_123"}


def _remote_from_test_header(monkeypatch) -> None:
    """Every test client connects from 127.0.0.1; let a request name its own
    peer address so tests can play several senders. The named address goes
    through the real `_client_address`, so loopback stays loopback."""
    from types import SimpleNamespace

    import untether.triggers.server as server_mod

    real = server_mod._client_address
    monkeypatch.setattr(
        server_mod,
        "_client_address",
        lambda request: real(
            SimpleNamespace(remote=request.headers.get("X-Test-Remote", "127.0.0.1"))
        ),
    )


def _count_credential_checks(monkeypatch) -> list[int]:
    import untether.triggers.server as server_mod

    checks = [0]
    real_verify = server_mod.verify_auth

    def _counting_verify(*args, **kwargs):
        checks[0] += 1
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(server_mod, "verify_auth", _counting_verify)
    return checks


def _spy_limiters(monkeypatch) -> list[Any]:
    import untether.triggers.server as server_mod

    limiters: list[Any] = []

    class _SpyLimiter(server_mod.TokenBucketLimiter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            limiters.append(self)

    monkeypatch.setattr(server_mod, "TokenBucketLimiter", _SpyLimiter)
    return limiters


@pytest.mark.anyio
async def test_906_bad_auth_flood_locks_out_only_the_offending_address(monkeypatch):
    """A per-webhook failed-auth bucket let anyone who could reach the port
    keep every legitimate sender on 429 with `rate_limit` bad requests a
    minute."""
    import asyncio

    _remote_from_test_header(monkeypatch)
    dispatcher, transport, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    attacker = {"X-Test-Remote": "203.0.113.9"}
    sender = {"X-Test-Remote": "198.51.100.7"}
    async with TestClient(TestServer(app)) as cl:
        statuses = [
            (await cl.post("/hooks/a", headers=_BAD | attacker, json={})).status
            for _ in range(8)
        ]
        assert statuses == [401, 401, 401, 429, 429, 429, 429, 429]

        # The legitimate sender, on another address, is not locked out.
        resp = await cl.post("/hooks/a", headers=_GOOD | sender, json={})
        assert resp.status == 202

        # The offending address stays locked out even with the right secret,
        # so it can't keep guessing past the limit.
        resp = await cl.post("/hooks/a", headers=_GOOD | attacker, json={})
        assert resp.status == 429
        await asyncio.sleep(0)
    assert len(transport.sent) == 1


@pytest.mark.anyio
async def test_906_concurrent_bad_auth_cannot_exceed_the_cap(monkeypatch):
    """The bucket was checked, then the body awaited, then the token spent —
    so every request of a parallel burst passed the check before any spent."""
    import asyncio

    _remote_from_test_header(monkeypatch)
    checks = _count_credential_checks(monkeypatch)
    bad = _BAD | {"X-Test-Remote": "203.0.113.9"}
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    release = asyncio.Event()

    async def _slow_body():
        # Headers arrive first, so every handler is past the early check and
        # parked on the body read before any of them reaches auth.
        yield b"{"
        await release.wait()
        yield b"}"

    async def _release_later():
        await asyncio.sleep(0.3)
        release.set()

    async with TestClient(TestServer(app)) as cl:
        *resps, _ = await asyncio.gather(
            *[cl.post("/hooks/a", headers=bad, data=_slow_body()) for _ in range(10)],
            _release_later(),
        )
    statuses = sorted(r.status for r in resps)
    assert checks[0] == 3, f"{checks[0]} credential checks for a cap of 3"
    assert statuses == [401] * 3 + [429] * 7


@pytest.mark.anyio
async def test_906_failed_auth_buckets_stay_bounded(monkeypatch):
    """Bad auth from many addresses must not grow the limiter without bound.
    Once the table is full, an address it can't track gets no failed attempts
    (429) — but its correct credential is still accepted: filling the table
    must not be a way to lock everyone else out."""
    import untether.triggers.server as server_mod

    _remote_from_test_header(monkeypatch)
    monkeypatch.setattr(server_mod, "_AUTH_FAILED_MAX_KEYS", 5)
    limiters = _spy_limiters(monkeypatch)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    async with TestClient(TestServer(app)) as cl:
        statuses = [
            (
                await cl.post(
                    "/hooks/a",
                    headers=_BAD | {"X-Test-Remote": f"203.0.113.{i}"},
                    json={},
                )
            ).status
            for i in range(40)
        ]
        assert statuses[:5] == [401] * 5
        assert set(statuses[5:]) == {429}
        auth_limiter = next(lim for lim in limiters if lim._max_keys is not None)
        assert len(auth_limiter._buckets) == 5

        # With no room to count it, an address the limiter isn't tracking has
        # no budget for failures, but the right secret is not refused.
        newcomer = {"X-Test-Remote": "198.51.100.7"}
        resp = await cl.post("/hooks/b", headers=_BAD | newcomer, json={})
        assert resp.status == 429
        resp = await cl.post("/hooks/b", headers=_GOOD | newcomer, json={})
        assert resp.status == 202
        assert len(auth_limiter._buckets) == 5

        # An address that emptied its own bucket is still locked out.
        for _ in range(2):
            await cl.post(
                "/hooks/a", headers=_BAD | {"X-Test-Remote": "203.0.113.0"}, json={}
            )
        resp = await cl.post(
            "/hooks/a", headers=_GOOD | {"X-Test-Remote": "203.0.113.0"}, json={}
        )
        assert resp.status == 429

        # Once the tracked buckets have refilled they are dropped, and the
        # newcomer gets a budget of its own again.
        import time

        long_ago = time.monotonic() - 3600.0
        for key, (tokens, _) in list(auth_limiter._buckets.items()):
            auth_limiter._buckets[key] = (tokens, long_ago)
        auth_limiter._next_prune = 0.0
        resp = await cl.post("/hooks/b", headers=_GOOD | newcomer, json={})
        assert resp.status == 202
        # A request that passes auth leaves no failed-auth bucket behind.
        assert auth_limiter._buckets == {}


@pytest.mark.anyio
async def test_906_full_failed_auth_table_does_not_refuse_auth_none(monkeypatch):
    """A webhook with `auth = "none"` can't fail auth, so the failed-auth
    limiter — full or not — has no say over it."""
    import untether.triggers.server as server_mod

    _remote_from_test_header(monkeypatch)
    monkeypatch.setattr(server_mod, "_AUTH_FAILED_MAX_KEYS", 1)
    settings = parse_trigger_config(
        {
            "enabled": True,
            "webhooks": [
                {
                    "id": "locked",
                    "path": "/hooks/locked",
                    "auth": "bearer",
                    "secret": "tok_123",
                    "action": "notify_only",
                    "message_template": "x",
                },
                {
                    "id": "open",
                    "path": "/hooks/open",
                    "auth": "none",
                    "action": "notify_only",
                    "message_template": "x",
                },
            ],
        }
    )
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)
    async with TestClient(TestServer(app)) as cl:
        resp = await cl.post(
            "/hooks/locked", headers=_BAD | {"X-Test-Remote": "203.0.113.9"}, json={}
        )
        assert resp.status == 401
        other = {"X-Test-Remote": "198.51.100.7"}
        assert (await cl.post("/hooks/locked", headers=other, json={})).status == 429
        assert (await cl.post("/hooks/open", headers=other, json={})).status == 202


def _auth_failed_warnings(logs) -> list[dict]:
    return [
        r
        for r in logs
        if r["event"] == "triggers.webhook.auth_failed" and r["log_level"] == "warning"
    ]


@pytest.mark.anyio
async def test_906_auth_failed_warnings_are_capped_per_webhook(monkeypatch):
    """Every failed attempt logged a warning with no cap for callers on many
    addresses; a flood must not be able to write a warning per request."""
    from structlog.testing import capture_logs

    _remote_from_test_header(monkeypatch)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(5), dispatcher)
    async with TestClient(TestServer(app)) as cl:
        with capture_logs() as logs:
            for i in range(40):
                await cl.post(
                    "/hooks/a",
                    headers=_BAD | {"X-Test-Remote": f"203.0.113.{i}"},
                    json={},
                )
    warnings = _auth_failed_warnings(logs)
    # `rate_limit` warnings a minute for the webhook, however many addresses.
    assert len(warnings) == 5
    assert warnings[0]["remote"] == "203.0.113.0"


@pytest.mark.anyio
async def test_906_paced_guessing_keeps_warning(monkeypatch):
    """Warning only for an address's first failure and its lockout let a
    caller who paced guesses at the refill rate (never full, never empty)
    guess indefinitely with nothing above debug in the log."""
    from structlog.testing import capture_logs

    _remote_from_test_header(monkeypatch)
    limiters = _spy_limiters(monkeypatch)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(5), dispatcher)
    attacker = _BAD | {"X-Test-Remote": "203.0.113.9"}
    async with TestClient(TestServer(app)) as cl:
        with capture_logs() as logs:
            # Two quick guesses take the bucket off full; from then on one
            # refill interval (60 / 5 s) passes between guesses, so it is
            # never full again and never empty.
            for i in range(8):
                assert (
                    await cl.post("/hooks/a", headers=attacker, json={})
                ).status == 401
                if i == 0:
                    continue
                for lim in limiters:
                    for key, (tokens, last) in list(lim._buckets.items()):
                        lim._buckets[key] = (tokens, last - 12.0)
    warnings = _auth_failed_warnings(logs)
    assert len(warnings) == 8
    assert {w["remote"] for w in warnings} == {"203.0.113.9"}


# ── #906 second follow-up: a loopback peer (reverse proxy) is never locked out ──


@pytest.mark.anyio
async def test_906_loopback_bad_auth_flood_does_not_lock_out_the_right_secret(
    monkeypatch,
):
    """Behind a reverse proxy on the same host (the default deployment) every
    request has the proxy's loopback address, so a per-address lockout let
    anyone 429 legitimate senders with `rate_limit` bad requests a minute."""
    import asyncio

    checks = _count_credential_checks(monkeypatch)
    dispatcher, transport, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    async with TestClient(TestServer(app)) as cl:
        statuses = [
            (await cl.post("/hooks/a", headers=_BAD, json={})).status for _ in range(8)
        ]
        # Failures are still budgeted: past the budget they are 429, not 401.
        assert statuses == [401, 401, 401, 429, 429, 429, 429, 429]

        resp = await cl.post("/hooks/a", headers=_GOOD, json={})
        assert resp.status == 202
        await asyncio.sleep(0)
    assert len(transport.sent) == 1
    # One credential check per request, never more.
    assert checks[0] == 9


@pytest.mark.parametrize(
    "remote", ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"]
)
@pytest.mark.anyio
async def test_906_every_loopback_form_is_exempt_from_lockout(monkeypatch, remote):
    _remote_from_test_header(monkeypatch)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(2), dispatcher)
    peer = {"X-Test-Remote": remote}
    async with TestClient(TestServer(app)) as cl:
        for _ in range(5):
            await cl.post("/hooks/a", headers=_BAD | peer, json={})
        assert (await cl.post("/hooks/a", headers=_GOOD | peer, json={})).status == 202


@pytest.mark.anyio
async def test_906_unknown_peer_is_exempt_from_lockout(monkeypatch):
    """With no peer address every such caller would share one bucket."""
    _remote_from_test_header(monkeypatch)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(2), dispatcher)
    peer = {"X-Test-Remote": ""}
    async with TestClient(TestServer(app)) as cl:
        for _ in range(5):
            await cl.post("/hooks/a", headers=_BAD | peer, json={})
        assert (await cl.post("/hooks/a", headers=_GOOD | peer, json={})).status == 202


@pytest.mark.anyio
async def test_906_loopback_concurrent_bad_auth_past_the_budget(monkeypatch):
    """A parallel burst from a loopback peer: the budget's worth of 401s, 429
    for the rest, and one credential check per request (the check and the
    spend have no await between them)."""
    import asyncio

    checks = _count_credential_checks(monkeypatch)
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    release = asyncio.Event()

    async def _slow_body():
        yield b"{"
        await release.wait()
        yield b"}"

    async def _release_later():
        await asyncio.sleep(0.3)
        release.set()

    async with TestClient(TestServer(app)) as cl:
        *resps, _ = await asyncio.gather(
            *[cl.post("/hooks/a", headers=_BAD, data=_slow_body()) for _ in range(10)],
            _release_later(),
        )
    assert sorted(r.status for r in resps) == [401] * 3 + [429] * 7
    assert checks[0] == 10


@pytest.mark.anyio
async def test_906_loopback_over_budget_failures_are_logged_quietly():
    from structlog.testing import capture_logs

    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(3), dispatcher)
    async with TestClient(TestServer(app)) as cl:
        with capture_logs() as logs:
            for _ in range(12):
                await cl.post("/hooks/a", headers=_BAD, json={})
    assert len(_auth_failed_warnings(logs)) == 3
    assert [r for r in logs if r["log_level"] == "error"] == []


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("203.0.113.9", "203.0.113.9"),
        # A dual-stack socket reports IPv4 peers as IPv4-mapped IPv6.
        ("::ffff:203.0.113.9", "203.0.113.9"),
        # One IPv6 subscriber holds a whole /64; rotating inside it must not
        # buy a fresh bucket each time.
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("fe80::1%eth0", "fe80::/64"),
        # Loopback in every form is one shared peer (a reverse proxy or a
        # local process), which is never locked out.
        ("::1", "loopback"),
        ("127.0.0.1", "loopback"),
        ("127.8.9.10", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        # NAT64 (RFC 6052 well-known prefix): the IPv4 sender is in the low
        # 32 bits, so a /64 would put every IPv4 sender in one bucket.
        ("64:ff9b::cb00:7109", "64:ff9b::cb00:7109"),
        ("64:ff9b::c633:6407", "64:ff9b::c633:6407"),
        (None, "unknown"),
        ("", "unknown"),
        ("not-an-address", "not-an-address"),
    ],
)
def test_906_client_address(remote, expected):
    from types import SimpleNamespace

    from untether.triggers.server import _client_address

    assert _client_address(SimpleNamespace(remote=remote)) == expected


@pytest.mark.anyio
async def test_906_client_address_ignores_forwarded_headers():
    """The address is the TCP peer. A forwarded header is caller-supplied
    and would let anyone pick their own bucket."""
    dispatcher, _, _ = _make_dispatcher()
    app = build_webhook_app(_two_hook_settings(2), dispatcher)
    async with TestClient(TestServer(app)) as cl:
        statuses = [
            (
                await cl.post(
                    "/hooks/a",
                    headers=_BAD
                    | {
                        "X-Forwarded-For": f"203.0.113.{i}",
                        "X-Real-IP": f"203.0.113.{i}",
                        "Forwarded": f"for=203.0.113.{i}",
                    },
                    json={},
                )
            ).status
            for i in range(5)
        ]
    assert statuses == [401, 401, 429, 429, 429]


@pytest.mark.anyio
async def test_906_chunked_oversized_body_is_413_not_500():
    """A body with no Content-Length skips the size check; aiohttp then
    raises from `request.read()`, which the catch-all turned into a 500 and an
    error-level traceback — one per request, from a caller with no
    credentials."""
    from structlog.testing import capture_logs

    settings = _make_settings(server={"max_body_bytes": 1024})
    dispatcher, transport, _ = _make_dispatcher()
    app = build_webhook_app(settings, dispatcher)

    async def _body():
        for _ in range(8):
            yield b"x" * 1024

    async with TestClient(TestServer(app)) as cl:
        with capture_logs() as logs:
            resp = await cl.post("/hooks/test", headers=_BAD, data=_body())
        assert resp.status == 413
    assert [r for r in logs if r["log_level"] in ("warning", "error")] == []
    assert transport.sent == []
