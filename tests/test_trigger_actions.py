"""Tests for non-agent webhook actions (file_write, http_forward, notify_only)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from untether.triggers.actions import (
    _MAX_FILE_BYTES,
    _MAX_PATH_DEPTH,
    _deny_reason,
    _resolve_file_path,
    execute_file_write,
    execute_http_forward,
    execute_notify_message,
)
from untether.triggers.settings import WebhookConfig


def _make_webhook(**overrides) -> WebhookConfig:
    """Build a WebhookConfig with sensible defaults for testing."""
    defaults = {
        "id": "test",
        "path": "/hooks/test",
        "auth": "none",
        "action": "file_write",
        "file_path": "/tmp/test-output.json",
    }
    defaults.update(overrides)
    return WebhookConfig(**defaults)


# ---------------------------------------------------------------------------
# _resolve_file_path
# ---------------------------------------------------------------------------


class TestResolveFilePath:
    def test_absolute_path(self) -> None:
        result = _resolve_file_path("/tmp/data/output.json")
        assert result is not None
        assert result == Path("/tmp/data/output.json").resolve()

    def test_tilde_expansion(self) -> None:
        result = _resolve_file_path("~/data/output.json")
        assert result is not None
        assert str(result).startswith("/home") or str(result).startswith("/root")

    def test_traversal_rejected(self) -> None:
        result = _resolve_file_path("../../../etc/passwd")
        assert result is None

    def test_traversal_in_middle_rejected(self) -> None:
        result = _resolve_file_path("/tmp/data/../../etc/passwd")
        assert result is None


# ---------------------------------------------------------------------------
# _deny_reason
# ---------------------------------------------------------------------------


class TestDenyReason:
    def test_git_denied(self) -> None:
        assert _deny_reason(Path(".git/config")) is not None

    def test_env_denied(self) -> None:
        assert _deny_reason(Path(".env")) is not None

    def test_pem_denied(self) -> None:
        assert _deny_reason(Path("certs/server.pem")) is not None

    def test_ssh_denied(self) -> None:
        assert _deny_reason(Path("home/.ssh/id_rsa")) is not None

    def test_normal_path_allowed(self) -> None:
        assert _deny_reason(Path("data/output.json")) is None

    def test_nested_data_allowed(self) -> None:
        assert _deny_reason(Path("incoming/batch-2026-04-12.json")) is None


# ---------------------------------------------------------------------------
# execute_file_write
# ---------------------------------------------------------------------------


class TestExecuteFileWrite:
    @pytest.mark.anyio
    async def test_successful_write(self, tmp_path: Path) -> None:
        target = tmp_path / "output.json"
        wh = _make_webhook(file_path=str(target))
        ok, msg = await execute_file_write(wh, {}, b'{"data": "test"}')
        assert ok is True
        assert "written to" in msg
        assert target.read_bytes() == b'{"data": "test"}'

    @pytest.mark.anyio
    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "output.json"
        wh = _make_webhook(file_path=str(target))
        ok, msg = await execute_file_write(wh, {}, b"hello")
        assert ok is True
        assert target.exists()

    @pytest.mark.anyio
    async def test_path_traversal_rejected(self) -> None:
        wh = _make_webhook(file_path="../../../etc/passwd")
        ok, msg = await execute_file_write(wh, {}, b"evil")
        assert ok is False
        assert "path traversal" in msg

    @pytest.mark.anyio
    async def test_deny_glob_git_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / ".git" / "config"
        wh = _make_webhook(file_path=str(target))
        ok, msg = await execute_file_write(wh, {}, b"evil")
        assert ok is False
        assert "deny glob" in msg

    @pytest.mark.anyio
    async def test_deny_glob_env_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        wh = _make_webhook(file_path=str(target))
        ok, msg = await execute_file_write(wh, {}, b"SECRET=evil")
        assert ok is False
        assert "deny glob" in msg

    @pytest.mark.anyio
    async def test_size_limit_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "huge.bin"
        wh = _make_webhook(file_path=str(target))
        payload = b"x" * (_MAX_FILE_BYTES + 1)
        ok, msg = await execute_file_write(wh, {}, payload)
        assert ok is False
        assert "too large" in msg

    @pytest.mark.anyio
    async def test_path_depth_limit_rejected(self, tmp_path: Path) -> None:
        deep = str(
            tmp_path / "/".join(f"d{i}" for i in range(_MAX_PATH_DEPTH + 5)) / "f.json"
        )
        wh = _make_webhook(file_path=deep)
        ok, msg = await execute_file_write(wh, {}, b"data")
        assert ok is False
        assert "too deep" in msg

    @pytest.mark.anyio
    async def test_on_conflict_error(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.json"
        target.write_text("old data")
        wh = _make_webhook(file_path=str(target), on_conflict="error")
        ok, msg = await execute_file_write(wh, {}, b"new data")
        assert ok is False
        assert "already exists" in msg
        assert target.read_text() == "old data"

    @pytest.mark.anyio
    async def test_on_conflict_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.json"
        target.write_text("old data")
        wh = _make_webhook(file_path=str(target), on_conflict="overwrite")
        ok, msg = await execute_file_write(wh, {}, b"new data")
        assert ok is True
        assert target.read_bytes() == b"new data"

    @pytest.mark.anyio
    async def test_on_conflict_append_timestamp(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.json"
        target.write_text("old data")
        wh = _make_webhook(file_path=str(target), on_conflict="append_timestamp")
        ok, msg = await execute_file_write(wh, {}, b"new data")
        assert ok is True
        # Original file should be unchanged.
        assert target.read_text() == "old data"
        # A timestamped file should exist.
        timestamped = list(tmp_path.glob("existing_*.json"))
        assert len(timestamped) == 1
        assert timestamped[0].read_bytes() == b"new data"

    @pytest.mark.anyio
    async def test_template_substitution_in_path(self, tmp_path: Path) -> None:
        template_path = str(tmp_path / "batch-{{batch_id}}.json")
        wh = _make_webhook(file_path=template_path)
        payload = {"batch_id": "2026-04-12"}
        ok, msg = await execute_file_write(wh, payload, b"batch data")
        assert ok is True
        assert (tmp_path / "batch-2026-04-12.json").exists()

    @pytest.mark.anyio
    async def test_atomic_write(self, tmp_path: Path) -> None:
        """Verify no partial files on success."""
        target = tmp_path / "atomic.json"
        wh = _make_webhook(file_path=str(target))
        ok, _ = await execute_file_write(wh, {}, b"complete data")
        assert ok is True
        # No temp files left behind.
        temp_files = list(tmp_path.glob(".untether-trigger-*"))
        assert len(temp_files) == 0


# ---------------------------------------------------------------------------
# execute_http_forward
# ---------------------------------------------------------------------------


class TestExecuteHttpForward:
    @pytest.mark.anyio
    async def test_successful_forward(self) -> None:
        wh = _make_webhook(
            action="http_forward",
            file_path=None,
            forward_url="https://api.example.com/events",
        )
        mock_resp = httpx.Response(
            200, request=httpx.Request("POST", "https://api.example.com/events")
        )
        with (
            patch(
                "untether.triggers.actions.validate_url_with_dns",
                new_callable=AsyncMock,
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            ok, msg = await execute_http_forward(wh, {}, b'{"event": "test"}')

        assert ok is True
        assert "forwarded" in msg

    @pytest.mark.anyio
    async def test_ssrf_blocked(self) -> None:
        wh = _make_webhook(
            action="http_forward",
            file_path=None,
            forward_url="http://127.0.0.1:8080/internal",
        )
        from untether.triggers.ssrf import SSRFError

        with patch(
            "untether.triggers.actions.validate_url_with_dns",
            new_callable=AsyncMock,
            side_effect=SSRFError("Blocked: private range"),
        ):
            ok, msg = await execute_http_forward(wh, {}, b"{}")

        assert ok is False
        assert "blocked" in msg.lower()

    @pytest.mark.anyio
    async def test_4xx_no_retry(self) -> None:
        wh = _make_webhook(
            action="http_forward",
            file_path=None,
            forward_url="https://api.example.com/events",
        )
        mock_resp = httpx.Response(
            403, request=httpx.Request("POST", "https://api.example.com/events")
        )
        with (
            patch(
                "untether.triggers.actions.validate_url_with_dns",
                new_callable=AsyncMock,
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            ok, msg = await execute_http_forward(wh, {}, b"{}")

        assert ok is False
        assert "403" in msg
        # Should only be called once (no retry on 4xx).
        mock_client.request.assert_called_once()

    @pytest.mark.anyio
    async def test_header_injection_rejected(self) -> None:
        wh = _make_webhook(
            action="http_forward",
            file_path=None,
            forward_url="https://api.example.com/events",
            forward_headers={"X-Custom": "value\r\nInjected: header"},
        )
        with patch(
            "untether.triggers.actions.validate_url_with_dns", new_callable=AsyncMock
        ):
            ok, msg = await execute_http_forward(wh, {}, b"{}")

        assert ok is False
        assert "control characters" in msg

    @pytest.mark.anyio
    async def test_template_substitution_in_url(self) -> None:
        wh = _make_webhook(
            action="http_forward",
            file_path=None,
            forward_url="https://api.example.com/{{service}}/events",
        )
        mock_resp = httpx.Response(
            200, request=httpx.Request("POST", "https://api.example.com/sentry/events")
        )
        with (
            patch(
                "untether.triggers.actions.validate_url_with_dns",
                new_callable=AsyncMock,
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            ok, msg = await execute_http_forward(wh, {"service": "sentry"}, b"{}")

        assert ok is True


# ---------------------------------------------------------------------------
# execute_notify_message
# ---------------------------------------------------------------------------


class TestExecuteNotifyMessage:
    def test_simple_template(self) -> None:
        wh = _make_webhook(
            action="notify_only",
            file_path=None,
            message_template="Alert: {{event}} at {{time}}",
        )
        result = execute_notify_message(wh, {"event": "deploy", "time": "14:30"})
        assert result == "Alert: deploy at 14:30"

    def test_missing_field_renders_empty(self) -> None:
        wh = _make_webhook(
            action="notify_only",
            file_path=None,
            message_template="Status: {{missing_field}}",
        )
        result = execute_notify_message(wh, {})
        assert result == "Status: "

    def test_no_untrusted_prefix(self) -> None:
        wh = _make_webhook(
            action="notify_only",
            file_path=None,
            message_template="Hello {{name}}",
        )
        result = execute_notify_message(wh, {"name": "World"})
        assert not result.startswith("#--")
        assert result == "Hello World"
