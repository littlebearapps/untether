import httpx
import pytest

from untether.logging import setup_logging
from untether.telegram.client import TelegramClient, TelegramRetryAfter
from untether.telegram.client_api import HttpBotClient


@pytest.mark.anyio
async def test_telegram_429_no_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429,
            json={
                "ok": False,
                "description": "retry",
                "parameters": {"retry_after": 3},
            },
            request=request,
        )

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        api = HttpBotClient("123:abcDEF_ghij", http_client=client)
        with pytest.raises(TelegramRetryAfter) as exc:
            await api._post("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.aclose()

    assert exc.value.retry_after == 3
    assert len(calls) == 1


@pytest.mark.anyio
async def test_custom_bot_api_base_url() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "result": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        api = HttpBotClient(
            "123:abcDEF_ghij",
            base_url="http://127.0.0.1:8081/",
            http_client=client,
        )
        await api.get_updates(None, timeout_s=0)
    finally:
        await client.aclose()

    assert urls == ["http://127.0.0.1:8081/bot123:abcDEF_ghij/getUpdates"]


@pytest.mark.anyio
async def test_no_token_in_logs_on_http_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "123:abcDEF_ghij"
    setup_logging(debug=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops", request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        api = HttpBotClient(token, http_client=client)
        await api._post("getUpdates", {"timeout": 1})
    finally:
        await client.aclose()

    out = capsys.readouterr().out
    assert token not in out
    assert "bot[REDACTED]" in out


@pytest.mark.anyio
async def test_telegram_429_no_retry_post_form() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429,
            json={
                "ok": False,
                "description": "retry",
                "parameters": {"retry_after": 2},
            },
            request=request,
        )

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        api = HttpBotClient("123:abcDEF_ghij", http_client=client)
        with pytest.raises(TelegramRetryAfter) as exc:
            await api._post_form(
                "sendDocument",
                {"chat_id": 1},
                files={"document": ("note.txt", b"hi")},
            )
    finally:
        await client.aclose()

    assert exc.value.retry_after == 2
    assert len(calls) == 1


@pytest.mark.anyio
async def test_telegram_429_defaults_retry_after_on_bad_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="nope", request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        api = HttpBotClient("123:abcDEF_ghij", http_client=client)
        with pytest.raises(TelegramRetryAfter) as exc:
            await api._post("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.aclose()

    assert exc.value.retry_after == 5.0


@pytest.mark.anyio
async def test_telegram_ok_false_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 400, "description": "bad"},
            request=request,
        )

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        api = HttpBotClient("123:abcDEF_ghij", http_client=client)
        result = await api._post("getUpdates", {"timeout": 1})
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_telegram_invalid_payload_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"], request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        api = HttpBotClient("123:abcDEF_ghij", http_client=client)
        result = await api._post("getUpdates", {"timeout": 1})
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_telegram_decode_failure_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"username": "bot-only"}},
            request=request,
        )

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient("123:abcDEF_ghij", http_client=client)
        result = await tg.get_me()
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_telegram_download_file_retries_on_429() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={"ok": False, "parameters": {"retry_after": 3}},
                request=request,
            )
        return httpx.Response(200, content=b"ok", request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient("123:abcDEF_ghij", http_client=client, sleep=sleep)
        payload = await tg.download_file("path/to/file")
    finally:
        await client.aclose()

    assert payload == b"ok"
    assert sleeps == [3.0]
    assert len(calls) == 2


@pytest.mark.anyio
async def test_telegram_download_file_429_defaults_retry_after_on_bad_body() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="nope", request=request)
        return httpx.Response(200, content=b"ok", request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient("123:abcDEF_ghij", http_client=client, sleep=sleep)
        payload = await tg.download_file("path")
    finally:
        await client.aclose()

    assert payload == b"ok"
    assert sleeps == [5.0]
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# #746 — benign edit/delete 400s
# ---------------------------------------------------------------------------

_EDIT_GONE = "Bad Request: message to edit not found"
_NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and "
    "reply markup are exactly the same as a current content and reply markup "
    "of the message"
)


def _handler_400(description: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": description},
            request=request,
        )

    return handler


@pytest.mark.anyio
async def test_746_no_token_in_logs_on_benign_400(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "123:abcDEF_ghij"
    setup_logging(debug=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler_400(_EDIT_GONE)))
    try:
        api = HttpBotClient(token, http_client=client)
        for _ in range(5):  # the 5th also emits the burst WARNING
            await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await client.aclose()

    out = capsys.readouterr().out
    assert token not in out
    assert "abcDEF_ghij" not in out
    assert "telegram.benign_rejection" in out
    assert "telegram.benign_rejection.burst" in out


def _transport_over(description: str):
    from untether.telegram.bridge import TelegramTransport

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler_400(description)))
    tg = TelegramClient(
        "123:abcDEF_ghij",
        http_client=http,
        private_chat_rps=0.0,
        group_chat_rps=0.0,
    )
    return TelegramTransport(tg), tg, http


@pytest.mark.anyio
async def test_746_transport_edit_not_modified_no_error_end_to_end() -> None:
    from structlog.testing import capture_logs

    from untether.transport import MessageRef, RenderedMessage

    transport, tg, http = _transport_over(_NOT_MODIFIED)
    ref = MessageRef(channel_id=123, message_id=916)
    try:
        with capture_logs() as logs:
            result = await transport.edit(ref=ref, message=RenderedMessage(text="same"))
    finally:
        await tg.close()
        await http.aclose()

    assert result == ref
    assert any(r.get("event") == "transport.edit.noop" for r in logs)
    assert not [r for r in logs if r.get("log_level") == "error"]


@pytest.mark.anyio
async def test_746_transport_edit_target_gone_warns_with_description() -> None:
    from structlog.testing import capture_logs

    from untether.transport import MessageRef, RenderedMessage

    transport, tg, http = _transport_over(_EDIT_GONE)
    ref = MessageRef(channel_id=123, message_id=916)
    try:
        with capture_logs() as logs:
            result = await transport.edit(ref=ref, message=RenderedMessage(text="x"))
    finally:
        await tg.close()
        await http.aclose()

    assert result is None
    rec = next(r for r in logs if r.get("event") == "transport.edit.failed")
    assert rec["log_level"] == "warning"
    assert rec["error"] == _EDIT_GONE
    assert not [r for r in logs if r.get("log_level") == "error"]
