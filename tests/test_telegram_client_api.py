import httpx
import pytest

from untether.telegram.api_models import User
from untether.telegram.client_api import (
    HttpBotClient,
    TelegramRetryAfter,
    classify_benign_rejection,
    retry_after_from_payload,
)


def _response() -> httpx.Response:
    request = httpx.Request("POST", "https://example.com")
    return httpx.Response(200, request=request)


def test_retry_after_from_payload() -> None:
    assert retry_after_from_payload({}) is None
    assert retry_after_from_payload({"parameters": {"retry_after": 2}}) == 2.0


def test_parse_envelope_invalid_payload() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    assert (
        client._parse_telegram_envelope(
            method="sendMessage",
            resp=_response(),
            payload="nope",
        )
        is None
    )


def test_parse_envelope_rate_limited() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    payload = {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
    with pytest.raises(TelegramRetryAfter) as exc:
        client._parse_telegram_envelope(
            method="sendMessage",
            resp=_response(),
            payload=payload,
        )
    assert exc.value.retry_after == 1.0


def test_parse_envelope_api_error() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    payload = {"ok": False, "error_code": 400, "description": "boom"}
    assert (
        client._parse_telegram_envelope(
            method="sendMessage",
            resp=_response(),
            payload=payload,
        )
        is None
    )


def test_parse_envelope_ok() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    payload = {"ok": True, "result": {"message_id": 1}}
    assert client._parse_telegram_envelope(
        method="sendMessage",
        resp=_response(),
        payload=payload,
    ) == {"message_id": 1}


@pytest.mark.anyio
async def test_client_methods_build_params_and_decode() -> None:
    payloads = {
        "getUpdates": [{"update_id": 1}],
        "getFile": {"file_path": "path"},
        "sendMessage": {"message_id": 1, "chat": {"id": 1, "type": "private"}},
        "sendDocument": {"message_id": 2, "chat": {"id": 1, "type": "private"}},
        "editMessageText": {"message_id": 3, "chat": {"id": 1, "type": "private"}},
        "deleteMessage": True,
        "setMyCommands": True,
        "getMe": {"id": 7},
        "answerCallbackQuery": True,
        "getChat": {"id": 5, "type": "private"},
        "getChatMember": {"status": "member"},
        "createForumTopic": {"message_thread_id": 11},
        "editForumTopic": True,
    }

    class _StubClient(HttpBotClient):
        def __init__(self) -> None:
            super().__init__("token", http_client=httpx.AsyncClient())
            self.calls: list[
                tuple[str, dict | None, dict | None, dict | None, float | None]
            ] = []

        async def _request(
            self,
            method: str,
            *,
            json: dict | None = None,
            data: dict | None = None,
            files: dict | None = None,
            request_timeout: float | None = None,
        ) -> object | None:
            self.calls.append((method, json, data, files, request_timeout))
            return payloads.get(method)

    client = _StubClient()

    updates = await client.get_updates(offset=10, allowed_updates=["message"])
    assert updates and updates[0].update_id == 1

    assert await client.get_file("file") is not None

    msg = await client.send_message(
        1,
        "hi",
        reply_to_message_id=2,
        disable_notification=True,
        message_thread_id=3,
        entities=[{"type": "bold", "offset": 0, "length": 2}],
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": []},
    )
    assert msg and msg.message_id == 1

    doc = await client.send_document(
        1,
        "file.txt",
        b"data",
        reply_to_message_id=2,
        message_thread_id=3,
        disable_notification=True,
        caption="doc",
    )
    assert doc and doc.message_id == 2

    edit = await client.edit_message_text(
        1,
        2,
        "edit",
        entities=[{"type": "italic", "offset": 0, "length": 4}],
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": []},
    )
    assert edit and edit.message_id == 3

    assert await client.delete_message(1, 2) is True
    assert await client.set_my_commands(
        [{"command": "ping", "description": "pong"}],
        scope={"type": "chat"},
        language_code="en",
    )
    assert await client.answer_callback_query("cb", text="ok", show_alert=True) is True
    assert await client.get_chat(1) is not None
    assert await client.get_chat_member(1, 2) is not None
    assert await client.create_forum_topic(1, "topic") is not None
    assert await client.edit_forum_topic(1, 2, "topic") is True

    await client.close()

    send_call = next(call for call in client.calls if call[0] == "sendMessage")
    assert send_call[1]["disable_notification"] is True
    assert send_call[1]["reply_to_message_id"] == 2
    assert send_call[1]["message_thread_id"] == 3
    assert send_call[1]["entities"]
    assert send_call[1]["parse_mode"] == "Markdown"
    assert send_call[1]["link_preview_options"] == {"is_disabled": True}
    assert send_call[1]["reply_markup"]

    doc_call = next(call for call in client.calls if call[0] == "sendDocument")
    assert doc_call[2]["caption"] == "doc"
    assert doc_call[3]["document"][0] == "file.txt"

    edit_call = next(call for call in client.calls if call[0] == "editMessageText")
    assert edit_call[1]["link_preview_options"] == {"is_disabled": True}


def test_default_timeout_is_30s() -> None:
    client = HttpBotClient("token")
    # httpx stores timeout as httpx.Timeout; default pool/connect/read/write = 30
    assert client._http_client.timeout.read == 30


@pytest.mark.anyio
async def test_get_updates_passes_per_request_timeout() -> None:
    payloads = {"getUpdates": [{"update_id": 1}]}

    class _Stub(HttpBotClient):
        def __init__(self) -> None:
            super().__init__("token", http_client=httpx.AsyncClient())
            self.last_timeout: float | None = None

        async def _request(
            self,
            method: str,
            *,
            json: dict | None = None,
            data: dict | None = None,
            files: dict | None = None,
            request_timeout: float | None = None,
        ) -> object | None:
            self.last_timeout = request_timeout
            return payloads.get(method)

    client = _Stub()
    await client.get_updates(offset=None, timeout_s=50)
    assert client.last_timeout == 70  # timeout_s + 20
    await client.close()


@pytest.mark.anyio
async def test_send_message_uses_default_timeout() -> None:
    payloads = {"sendMessage": {"message_id": 1, "chat": {"id": 1, "type": "private"}}}

    class _Stub(HttpBotClient):
        def __init__(self) -> None:
            super().__init__("token", http_client=httpx.AsyncClient())
            self.last_timeout: float | None = None

        async def _request(
            self,
            method: str,
            *,
            json: dict | None = None,
            data: dict | None = None,
            files: dict | None = None,
            request_timeout: float | None = None,
        ) -> object | None:
            self.last_timeout = request_timeout
            return payloads.get(method)

    client = _Stub()
    await client.send_message(1, "hi")
    assert client.last_timeout is None  # uses client default, no per-request override
    await client.close()


@pytest.mark.anyio
async def test_decode_result_invalid_payload_returns_none() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    assert client._decode_result(method="getMe", payload=["bad"], model=User) is None
    await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bogus_path",
    [
        "https://evil.example.com/exfil",  # absolute URL
        "//evil.example.com/x",  # scheme-relative
        "../../../etc/passwd",  # traversal
        "documents/../../secret",  # embedded traversal
        "/absolute/unix/path",  # absolute path
    ],
)
async def test_download_file_rejects_path_with_scheme_or_traversal(
    bogus_path: str,
) -> None:
    """#204: a spoofed/tampered getFile response that returns a file_path
    containing a URL scheme or '..' must not cause download_file to issue a
    request to an arbitrary host."""

    class _NoNetworkClient(HttpBotClient):
        def __init__(self) -> None:
            super().__init__("token", http_client=httpx.AsyncClient())
            self.got_called = False

        async def _request(self, *args, **kwargs):  # pragma: no cover
            self.got_called = True
            raise AssertionError("network request must not be attempted")

    client = _NoNetworkClient()
    try:
        result = await client.download_file(bogus_path)
        assert result is None
        assert client.got_called is False
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# #598 — failure reasons recorded for later correlation
# ---------------------------------------------------------------------------


def test_598_api_error_recorded_and_popped() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    payload = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: message is not modified",
    }
    result = client._parse_telegram_envelope(
        method="editMessageText",
        resp=_response(),
        payload=payload,
        request_payload={"chat_id": 123, "message_id": 916, "text": "x"},
    )
    assert result is None
    reason = client.pop_last_api_error("editMessageText", 123, 916)
    assert reason == "Bad Request: message is not modified"
    # pop clears the entry
    assert client.pop_last_api_error("editMessageText", 123, 916) is None


def test_598_error_store_is_bounded() -> None:
    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    for i in range(80):
        client._record_api_error(
            "editMessageText", {"chat_id": 1, "message_id": i}, f"err {i}"
        )
    assert len(client._last_api_errors) <= 64
    # Oldest entries evicted, newest retained
    assert client.pop_last_api_error("editMessageText", 1, 0) is None
    assert client.pop_last_api_error("editMessageText", 1, 79) == "err 79"


# ---------------------------------------------------------------------------
# #746 — benign edit/delete 400s log at INFO, not ERROR
# ---------------------------------------------------------------------------

# Exact descriptions built by tdlib/telegram-bot-api (Client.cpp @ e3e9dd8).
_NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and "
    "reply markup are exactly the same as a current content and reply markup "
    "of the message"
)
_EDIT_GONE = "Bad Request: message to edit not found"
_NOT_EDITABLE = "Bad Request: message can't be edited"
_DELETE_GONE = "Bad Request: message to delete not found"
_NOT_DELETABLE = "Bad Request: message can't be deleted"
_NOT_DELETABLE_ALL = "Bad Request: message can't be deleted for everyone"


@pytest.mark.parametrize(
    ("method", "description", "expected"),
    [
        ("editMessageText", _NOT_MODIFIED, "not_modified"),
        ("editMessageText", _EDIT_GONE, "target_gone"),
        ("editMessageText", _NOT_EDITABLE, "not_editable"),
        ("editMessageReplyMarkup", _EDIT_GONE, "target_gone"),
        ("deleteMessage", _DELETE_GONE, "target_gone"),
        ("deleteMessage", _NOT_DELETABLE, "not_deletable"),
        ("deleteMessage", _NOT_DELETABLE_ALL, "not_deletable"),
        ("editMessageText", "Bad Request: Message To Edit Not Found", "target_gone"),
    ],
)
def test_746_classify_benign_rejection_table(
    method: str, description: str, expected: str
) -> None:
    assert classify_benign_rejection(method, description, 916) == expected


@pytest.mark.parametrize(
    ("method", "description"),
    [
        ("sendMessage", _EDIT_GONE),
        (
            "editMessageText",
            'Bad Request: can\'t parse entities: Unsupported start tag "br" '
            "at byte offset 5",
        ),
        ("editMessageText", "Bad Request: message not found"),
        ("deleteMessage", "Forbidden: bot was blocked by the user"),
        ("editMessageText", "Bad Request: MESSAGE_ID_INVALID"),
        ("editMessageText", None),
        ("editMessageText", ""),
        ("editMessageText", 400),
    ],
)
def test_746_classify_negative_cases(method: str, description: object) -> None:
    assert classify_benign_rejection(method, description, 916) is None


@pytest.mark.parametrize("message_id", [0, -1, None, "916", True, 1.5])
def test_746_classify_requires_positive_message_id(message_id: object) -> None:
    assert classify_benign_rejection("editMessageText", _EDIT_GONE, message_id) is None
    assert classify_benign_rejection("editMessageText", _EDIT_GONE, 1) == "target_gone"


def _api_400(
    description: str | None = None,
    *,
    status: int = 400,
    text: str | None = None,
    clock=None,
) -> tuple[HttpBotClient, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text, request=request)
        return httpx.Response(
            status,
            json={"ok": False, "error_code": status, "description": description},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs = {} if clock is None else {"clock": clock}
    return HttpBotClient("123:abcDEF_ghij", http_client=http, **kwargs), http


def _events(logs: list[dict], name: str) -> list[dict]:
    return [r for r in logs if r.get("event") == name]


@pytest.mark.anyio
async def test_746_http_400_non_positive_message_id_stays_error() -> None:
    from structlog.testing import capture_logs

    api, http = _api_400(_EDIT_GONE)
    try:
        with capture_logs() as logs:
            result = await api.edit_message_text(chat_id=123, message_id=0, text="x")
    finally:
        await http.aclose()
    assert result is None
    errors = _events(logs, "telegram.http_error")
    assert len(errors) == 1
    assert errors[0]["log_level"] == "error"
    assert errors[0]["message_id"] == 0
    assert not _events(logs, "telegram.benign_rejection")
    assert api._benign_hits == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("description", "reason_class"),
    [
        (_NOT_MODIFIED, "not_modified"),
        (_EDIT_GONE, "target_gone"),
        (_NOT_EDITABLE, "not_editable"),
    ],
)
async def test_746_http_400_benign_edit_logs_info_not_error(
    description: str, reason_class: str
) -> None:
    from structlog.testing import capture_logs

    api, http = _api_400(description)
    try:
        with capture_logs() as logs:
            result = await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await http.aclose()
    assert result is None
    assert not [r for r in logs if r.get("log_level") == "error"]
    recs = _events(logs, "telegram.benign_rejection")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["log_level"] == "info"
    assert rec["method"] == "editMessageText"
    assert rec["status"] == 400
    assert rec["message_id"] == 916
    assert rec["chat_id"] == 123
    assert rec["reason_class"] == reason_class
    assert rec["description"] == description
    assert "url" not in rec


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("description", "reason_class"),
    [
        (_DELETE_GONE, "target_gone"),
        (_NOT_DELETABLE, "not_deletable"),
        (_NOT_DELETABLE_ALL, "not_deletable"),
    ],
)
async def test_746_http_400_benign_delete_logs_info_not_error(
    description: str, reason_class: str
) -> None:
    from structlog.testing import capture_logs

    api, http = _api_400(description)
    try:
        with capture_logs() as logs:
            result = await api.delete_message(chat_id=123, message_id=917)
    finally:
        await http.aclose()
    assert result is False
    assert not [r for r in logs if r.get("log_level") == "error"]
    recs = _events(logs, "telegram.benign_rejection")
    assert len(recs) == 1
    assert recs[0]["log_level"] == "info"
    assert recs[0]["method"] == "deleteMessage"
    assert recs[0]["reason_class"] == reason_class
    assert recs[0]["message_id"] == 917


@pytest.mark.anyio
async def test_746_http_400_reason_recorded_as_description() -> None:
    api, http = _api_400(_EDIT_GONE)
    try:
        await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await http.aclose()
    assert api.pop_last_api_error("editMessageText", 123, 916) == _EDIT_GONE
    assert api.pop_last_api_error("editMessageText", 123, 916) is None


@pytest.mark.anyio
async def test_746_http_400_non_benign_stays_error() -> None:
    from structlog.testing import capture_logs

    desc = 'Bad Request: can\'t parse entities: Unsupported start tag "br"'
    api, http = _api_400(desc)
    try:
        with capture_logs() as logs:
            await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await http.aclose()
    errors = _events(logs, "telegram.http_error")
    assert len(errors) == 1
    assert errors[0]["log_level"] == "error"
    assert errors[0]["status"] == 400
    assert errors[0]["message_id"] == 916
    # #823: the chat is logged too (message_ids are per chat)
    assert errors[0]["chat_id"] == 123
    assert not _events(logs, "telegram.benign_rejection")
    # D4: the readable description is recorded for the #598 reason
    assert api.pop_last_api_error("editMessageText", 123, 916) == desc


@pytest.mark.anyio
async def test_746_benign_string_on_other_method_stays_error() -> None:
    from structlog.testing import capture_logs

    api, http = _api_400(_EDIT_GONE)
    try:
        with capture_logs() as logs:
            await api.send_message(chat_id=123, text="x")
    finally:
        await http.aclose()
    errors = _events(logs, "telegram.http_error")
    assert len(errors) == 1 and errors[0]["log_level"] == "error"
    assert not _events(logs, "telegram.benign_rejection")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "description", "text", "reason"),
    [
        (
            403,
            "Forbidden: bot was blocked by the user",
            None,
            "Forbidden: bot was blocked by the user",
        ),
        (500, None, "oops", "http 500: oops"),
    ],
)
async def test_746_non_400_status_stays_error(
    status: int, description: str | None, text: str | None, reason: str
) -> None:
    from structlog.testing import capture_logs

    api, http = _api_400(description, status=status, text=text)
    try:
        with capture_logs() as logs:
            await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await http.aclose()
    errors = _events(logs, "telegram.http_error")
    assert len(errors) == 1 and errors[0]["log_level"] == "error"
    assert errors[0]["status"] == status
    assert errors[0]["chat_id"] == 123
    assert not _events(logs, "telegram.benign_rejection")
    assert api.pop_last_api_error("editMessageText", 123, 916) == reason


@pytest.mark.anyio
async def test_746_http_400_unparseable_body_stays_error() -> None:
    from structlog.testing import capture_logs

    api, http = _api_400(text="nope")
    try:
        with capture_logs() as logs:
            await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await http.aclose()
    errors = _events(logs, "telegram.http_error")
    assert len(errors) == 1 and errors[0]["log_level"] == "error"
    assert api.pop_last_api_error("editMessageText", 123, 916) == "http 400: nope"


def test_746_envelope_path_unchanged() -> None:
    """The HTTP-200 ``ok:false`` envelope path is out of scope (§4.3)."""
    from structlog.testing import capture_logs

    client = HttpBotClient("token", http_client=httpx.AsyncClient())
    with capture_logs() as logs:
        result = client._parse_telegram_envelope(
            method="editMessageText",
            resp=_response(),
            payload={"ok": False, "error_code": 400, "description": _EDIT_GONE},
            request_payload={"chat_id": 123, "message_id": 916, "text": "x"},
        )
    assert result is None
    errors = _events(logs, "telegram.api_error")
    assert len(errors) == 1 and errors[0]["log_level"] == "error"
    assert not _events(logs, "telegram.benign_rejection")
    assert client.pop_last_api_error("editMessageText", 123, 916) == _EDIT_GONE


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.anyio
async def test_746_benign_burst_warns_once_per_window() -> None:
    from structlog.testing import capture_logs

    clock = _FakeClock()
    api, http = _api_400(_EDIT_GONE, clock=clock)
    try:
        with capture_logs() as logs:
            for t in range(4):
                clock.now = float(t)
                await api.edit_message_text(chat_id=123, message_id=916, text="x")
            assert not _events(logs, "telegram.benign_rejection.burst")
            clock.now = 4.0
            await api.edit_message_text(chat_id=123, message_id=916, text="x")
            bursts = _events(logs, "telegram.benign_rejection.burst")
            assert len(bursts) == 1
            b = bursts[0]
            assert b["log_level"] == "warning"
            assert b["method"] == "editMessageText"
            assert b["reason_class"] == "target_gone"
            assert b["count"] == 5
            assert b["distinct_messages"] == 1
            assert b["message_ids"] == [916]
            assert b["chat_ids"] == [123]
            for t in range(5, 10):
                clock.now = float(t)
                await api.edit_message_text(chat_id=123, message_id=916, text="x")
            assert len(_events(logs, "telegram.benign_rejection.burst")) == 1
            for i in range(5):
                clock.now = 70.0 + i
                await api.edit_message_text(chat_id=123, message_id=916, text="x")
            assert len(_events(logs, "telegram.benign_rejection.burst")) == 2
    finally:
        await http.aclose()
    assert len(_events(logs, "telegram.benign_rejection")) == 15
    assert not [r for r in logs if r.get("log_level") == "error"]


@pytest.mark.anyio
async def test_746_benign_burst_window_expires() -> None:
    from structlog.testing import capture_logs

    clock = _FakeClock()
    api, http = _api_400(_EDIT_GONE, clock=clock)
    try:
        with capture_logs() as logs:
            for _ in range(4):
                await api.edit_message_text(chat_id=123, message_id=916, text="x")
            clock.now = 61.0
            await api.edit_message_text(chat_id=123, message_id=916, text="x")
    finally:
        await http.aclose()
    assert not _events(logs, "telegram.benign_rejection.burst")


@pytest.mark.anyio
async def test_746_benign_burst_keys_are_independent() -> None:
    from structlog.testing import capture_logs

    clock = _FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        desc = (
            _NOT_DELETABLE
            if request.url.path.endswith("deleteMessage")
            else (_EDIT_GONE)
        )
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": desc},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = HttpBotClient("123:abcDEF_ghij", http_client=http, clock=clock)
    try:
        with capture_logs() as logs:
            for i in range(4):
                clock.now = float(i)
                await api.edit_message_text(chat_id=123, message_id=916, text="x")
                await api.delete_message(chat_id=123, message_id=917)
            assert not _events(logs, "telegram.benign_rejection.burst")
        # A fresh window: 5 target_gone edits to 5 different ids (wrong-id shape)
        api2 = HttpBotClient("123:abcDEF_ghij", http_client=http, clock=clock)
        with capture_logs() as logs:
            for i in range(5):
                await api2.edit_message_text(chat_id=123, message_id=1000 + i, text="x")
    finally:
        await http.aclose()
    bursts = _events(logs, "telegram.benign_rejection.burst")
    assert len(bursts) == 1
    assert bursts[0]["distinct_messages"] == 5
    assert bursts[0]["message_ids"] == [1000, 1001, 1002, 1003, 1004]


# --- rc15 integration finding: dead connections and per-call timeouts --------


def _flaky_client(error: Exception, *, fail_times: int = 1):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if len(calls) <= fail_times:
            raise error
        return httpx.Response(200, json={"ok": True, "result": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpBotClient("token", http_client=http), calls


@pytest.mark.anyio
async def test_edit_retries_once_after_read_timeout() -> None:
    client, calls = _flaky_client(httpx.ReadTimeout("dead flow"))
    result = await client._request(
        "editMessageText", json={"chat_id": 1, "message_id": 2, "text": "x"}
    )
    assert result is True
    assert calls == ["editMessageText", "editMessageText"]


@pytest.mark.anyio
async def test_send_is_not_repeated_after_read_timeout() -> None:
    # It may have reached Telegram: a repeat could duplicate the message.
    client, calls = _flaky_client(httpx.ReadTimeout("dead flow"))
    result = await client._request("sendMessage", json={"chat_id": 1, "text": "x"})
    assert result is None
    assert calls == ["sendMessage"]


@pytest.mark.anyio
async def test_send_retries_when_the_request_never_left() -> None:
    client, calls = _flaky_client(httpx.ConnectError("refused"))
    result = await client._request("sendMessage", json={"chat_id": 1, "text": "x"})
    assert result is True
    assert calls == ["sendMessage", "sendMessage"]


@pytest.mark.anyio
async def test_retry_happens_only_once() -> None:
    client, calls = _flaky_client(httpx.ConnectError("refused"), fail_times=5)
    result = await client._request("sendMessage", json={"chat_id": 1, "text": "x"})
    assert result is None
    assert len(calls) == 2


@pytest.mark.anyio
async def test_get_updates_is_never_retried_here() -> None:
    client, calls = _flaky_client(httpx.ConnectError("refused"))
    assert await client._request("getUpdates", json={"timeout": 1}) is None
    assert calls == ["getUpdates"]


def test_owned_client_uses_short_message_timeouts() -> None:
    client = HttpBotClient("token", timeout_s=120)
    timeout = client._http_client.timeout
    assert timeout.read == 30.0
    assert timeout.connect == 10.0
    assert client._bulk_timeout_s == 120


# --- #823: chat_id on every unattributable error line ---


@pytest.mark.anyio
async def test_823_send_document_http_error_has_chat_id() -> None:
    from structlog.testing import capture_logs

    api, http = _api_400("Bad Request: file is too big")
    try:
        with capture_logs() as logs:
            result = await api.send_document(chat_id=456, filename="x.md", content=b"x")
    finally:
        await http.aclose()
    assert result is None
    errors = _events(logs, "telegram.http_error")
    assert len(errors) == 1
    # multipart (data=) payloads carry the chat too
    assert errors[0]["chat_id"] == 456
    assert errors[0]["message_id"] is None


@pytest.mark.anyio
async def test_823_network_error_has_chat_id() -> None:
    from structlog.testing import capture_logs

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = HttpBotClient("123:abcDEF_ghij", http_client=http)
    try:
        with capture_logs() as logs:
            result = await api.edit_message_text(chat_id=123, message_id=9, text="x")
    finally:
        await http.aclose()
    assert result is None
    errors = _events(logs, "telegram.network_error")
    assert len(errors) == 1
    assert errors[0]["chat_id"] == 123
    assert errors[0]["message_id"] == 9
    for retry in _events(logs, "telegram.network_retry"):
        assert retry["chat_id"] == 123
    assert "abcDEF" not in str(logs)


@pytest.mark.anyio
async def test_823_envelope_api_error_has_chat_id() -> None:
    from structlog.testing import capture_logs

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 400, "description": "x"},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = HttpBotClient("123:abcDEF_ghij", http_client=http)
    try:
        with capture_logs() as logs:
            result = await api.edit_message_text(chat_id=123, message_id=9, text="x")
    finally:
        await http.aclose()
    assert result is None
    errors = _events(logs, "telegram.api_error")
    assert len(errors) == 1
    assert errors[0]["chat_id"] == 123
    assert errors[0]["message_id"] == 9
    assert api.pop_last_api_error("editMessageText", 123, 9) == "x"


@pytest.mark.parametrize("payload", [None, "x", [1, 2], 5])
def test_823_payload_target_non_dict(payload: object) -> None:
    from untether.telegram.client_api import _payload_target

    assert _payload_target(payload) == (None, None)


def test_823_payload_target_dict() -> None:
    from untether.telegram.client_api import _payload_target

    assert _payload_target({"chat_id": 1, "message_id": 2}) == (1, 2)
    assert _payload_target({"offset": 3}) == (None, None)
