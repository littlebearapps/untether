import asyncio
import logging

import httpx
import pytest

from takopi.logging import RedactTokenFilter
from takopi.telegram_client import TelegramClient


def test_telegram_429_retry_after_calls_sleep() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "retry",
                    "parameters": {"retry_after": 3},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 1}},
            request=request,
        )

    transport = httpx.MockTransport(handler)

    async def run() -> dict:
        client = httpx.AsyncClient(transport=transport)
        try:
            tg = TelegramClient("123:abcDEF_ghij", client=client, sleep=fake_sleep)
            return await tg._post("sendMessage", {"chat_id": 1, "text": "hi"})
        finally:
            await client.aclose()

    result = asyncio.run(run())

    assert result == {"message_id": 1}
    assert sleeps == [3]
    assert len(calls) == 2


def test_no_token_in_logs_on_http_error(caplog: pytest.LogCaptureFixture) -> None:
    token = "123:abcDEF_ghij"
    redactor = RedactTokenFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(redactor)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops", request=request)

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        client = httpx.AsyncClient(transport=transport)
        try:
            tg = TelegramClient(token, client=client)
            await tg._post("getUpdates", {"timeout": 1})
        finally:
            await client.aclose()

    caplog.set_level(logging.ERROR)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())

    root_logger.removeFilter(redactor)

    assert token not in caplog.text
    assert "bot[REDACTED]" in caplog.text
