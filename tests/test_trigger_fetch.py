"""Tests for cron data-fetch triggers (#279)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from untether.triggers.fetch import (
    _parse_response,
    build_fetch_prompt,
    execute_fetch,
)
from untether.triggers.settings import CronFetchConfig


def _make_fetch(**overrides) -> CronFetchConfig:
    defaults = {"type": "http_get", "url": "https://api.example.com/data"}
    defaults.update(overrides)
    return CronFetchConfig(**defaults)


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_json_parse(self) -> None:
        body = b'{"issues": [1, 2, 3]}'
        result = _parse_response(body, "json")
        assert result == {"issues": [1, 2, 3]}

    def test_json_invalid_falls_back_to_text(self) -> None:
        body = b"not json at all"
        result = _parse_response(body, "json")
        assert result == "not json at all"

    def test_text_mode(self) -> None:
        body = b"hello world"
        result = _parse_response(body, "text")
        assert result == "hello world"

    def test_lines_mode(self) -> None:
        body = b"line1\nline2\n\nline3\n"
        result = _parse_response(body, "lines")
        assert result == ["line1", "line2", "line3"]

    def test_lines_strips_empty(self) -> None:
        body = b"\n\n\n"
        result = _parse_response(body, "lines")
        assert result == []


# ---------------------------------------------------------------------------
# build_fetch_prompt
# ---------------------------------------------------------------------------


class TestBuildFetchPrompt:
    def test_static_prompt_appends_data(self) -> None:
        result = build_fetch_prompt("Review issues", None, {"count": 5}, "issues")
        assert "Review issues" in result
        assert "Fetched data (issues)" in result
        assert '"count": 5' in result

    def test_template_renders_with_data(self) -> None:
        result = build_fetch_prompt(
            None,
            "There are {{fetch_result}} open issues",
            "42",
            "fetch_result",
        )
        assert "There are 42 open issues" in result

    def test_untrusted_prefix_present(self) -> None:
        result = build_fetch_prompt("Test", None, "data", "result")
        assert result.startswith("#-- EXTERNAL FETCH DATA")

    def test_list_data_serialised_as_json(self) -> None:
        result = build_fetch_prompt("Review", None, ["a", "b", "c"], "items")
        assert '"a"' in result
        assert '"b"' in result


# ---------------------------------------------------------------------------
# execute_fetch — HTTP
# ---------------------------------------------------------------------------


class TestFetchHTTP:
    @pytest.mark.anyio
    async def test_http_get_success(self) -> None:
        fetch = _make_fetch(parse_as="json")
        mock_resp = httpx.Response(
            200,
            content=b'{"status": "ok"}',
            request=httpx.Request("GET", "https://api.example.com/data"),
        )
        with (
            patch(
                "untether.triggers.fetch.validate_url_with_dns",
                new_callable=AsyncMock,
            ),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            ok, err, data = await execute_fetch(fetch)

        assert ok is True
        assert err == ""
        assert data == {"status": "ok"}

    @pytest.mark.anyio
    async def test_http_get_ssrf_blocked(self) -> None:
        fetch = _make_fetch(url="http://127.0.0.1/internal")
        from untether.triggers.ssrf import SSRFError

        with patch(
            "untether.triggers.fetch.validate_url_with_dns",
            new_callable=AsyncMock,
            side_effect=SSRFError("blocked"),
        ):
            ok, err, data = await execute_fetch(fetch)

        assert ok is False
        assert "SSRF" in err
        assert data is None

    @pytest.mark.anyio
    async def test_http_get_4xx_error(self) -> None:
        fetch = _make_fetch()
        mock_resp = httpx.Response(
            404,
            request=httpx.Request("GET", "https://api.example.com/data"),
        )
        with (
            patch(
                "untether.triggers.fetch.validate_url_with_dns",
                new_callable=AsyncMock,
            ),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            ok, err, data = await execute_fetch(fetch)

        assert ok is False
        assert "404" in err

    @pytest.mark.anyio
    async def test_http_post(self) -> None:
        fetch = _make_fetch(type="http_post", body="query")
        mock_resp = httpx.Response(
            200,
            content=b"result",
            request=httpx.Request("POST", "https://api.example.com/data"),
        )
        with (
            patch(
                "untether.triggers.fetch.validate_url_with_dns",
                new_callable=AsyncMock,
            ),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            ok, err, data = await execute_fetch(fetch)

        assert ok is True
        assert data == "result"
        # Verify POST method was used.
        call_args = mock_client.request.call_args
        assert call_args[0][0] == "POST"


# ---------------------------------------------------------------------------
# execute_fetch — file_read
# ---------------------------------------------------------------------------


class TestFetchFileRead:
    @pytest.mark.anyio
    async def test_file_read_success(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        target.write_text('{"count": 42}')
        fetch = _make_fetch(
            type="file_read",
            url=None,
            file_path=str(target),
            parse_as="json",
        )
        ok, err, data = await execute_fetch(fetch)
        assert ok is True
        assert data == {"count": 42}

    @pytest.mark.anyio
    async def test_file_read_not_found(self, tmp_path: Path) -> None:
        fetch = _make_fetch(
            type="file_read",
            url=None,
            file_path=str(tmp_path / "missing.txt"),
        )
        ok, err, data = await execute_fetch(fetch)
        assert ok is False
        assert "not found" in err

    @pytest.mark.anyio
    async def test_file_read_path_traversal(self) -> None:
        fetch = _make_fetch(
            type="file_read",
            url=None,
            file_path="../../../etc/passwd",
        )
        ok, err, data = await execute_fetch(fetch)
        assert ok is False
        assert "path traversal" in err

    @pytest.mark.anyio
    async def test_file_read_deny_glob(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        target.write_text("SECRET=value")
        fetch = _make_fetch(
            type="file_read",
            url=None,
            file_path=str(target),
        )
        ok, err, data = await execute_fetch(fetch)
        assert ok is False
        assert "deny glob" in err

    @pytest.mark.anyio
    async def test_file_read_lines_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "list.txt"
        target.write_text("item1\nitem2\nitem3\n")
        fetch = _make_fetch(
            type="file_read",
            url=None,
            file_path=str(target),
            parse_as="lines",
        )
        ok, err, data = await execute_fetch(fetch)
        assert ok is True
        assert data == ["item1", "item2", "item3"]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestCronFetchConfig:
    def test_http_get_requires_url(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="url is required"):
            CronFetchConfig(type="http_get")

    def test_file_read_requires_file_path(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="file_path is required"):
            CronFetchConfig(type="file_read")

    def test_http_get_valid(self) -> None:
        f = CronFetchConfig(type="http_get", url="https://api.example.com")
        assert f.type == "http_get"
        assert f.timeout_seconds == 15

    def test_file_read_valid(self) -> None:
        f = CronFetchConfig(type="file_read", file_path="/tmp/data.json")
        assert f.type == "file_read"
        assert f.parse_as == "text"

    def test_parse_as_options(self) -> None:
        for mode in ("json", "text", "lines"):
            f = CronFetchConfig(
                type="http_get", url="https://example.com", parse_as=mode
            )
            assert f.parse_as == mode

    def test_on_failure_options(self) -> None:
        for mode in ("abort", "run_with_error"):
            f = CronFetchConfig(
                type="http_get", url="https://example.com", on_failure=mode
            )
            assert f.on_failure == mode

    def test_default_store_as(self) -> None:
        f = CronFetchConfig(type="http_get", url="https://example.com")
        assert f.store_as == "fetch_result"
