from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, OpenAIError

from untether.telegram.api_models import (
    Chat,
    ChatMember,
    File,
    ForumTopic,
    Message,
    Update,
    User,
)
from untether.telegram.client import BotClient
from untether.telegram.types import TelegramIncomingMessage, TelegramVoice
from untether.telegram.voice import (
    VOICE_TRANSCRIPTION_CONNECTION_HINT,
    VOICE_TRANSCRIPTION_DISABLED_HINT,
    transcribe_voice,
)

_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")


class _Bot(BotClient):
    def __init__(self, *, file_info: File | None, audio: bytes | None) -> None:
        self._file_info = file_info
        self._audio = audio

    async def close(self) -> None:
        return None

    async def get_updates(
        self,
        offset: int | None,
        timeout_s: int = 50,
        allowed_updates: list[str] | None = None,
    ) -> list[Update] | None:
        _ = offset, timeout_s, allowed_updates
        return []

    async def get_file(self, file_id: str) -> File | None:
        _ = file_id
        return self._file_info

    async def download_file(self, file_path: str) -> bytes | None:
        _ = file_path
        return self._audio

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        message_thread_id: int | None = None,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        *,
        replace_message_id: int | None = None,
    ) -> Message | None:
        _ = (
            chat_id,
            text,
            reply_to_message_id,
            disable_notification,
            message_thread_id,
            entities,
            parse_mode,
            reply_markup,
            replace_message_id,
        )
        raise AssertionError("send_message should not be called")

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool | None = False,
        caption: str | None = None,
    ) -> Message | None:
        _ = (
            chat_id,
            filename,
            content,
            reply_to_message_id,
            message_thread_id,
            disable_notification,
            caption,
        )
        raise AssertionError("send_document should not be called")

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        *,
        wait: bool = True,
    ) -> Message | None:
        _ = (
            chat_id,
            message_id,
            text,
            entities,
            parse_mode,
            reply_markup,
            wait,
        )
        raise AssertionError("edit_message_text should not be called")

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        _ = chat_id, message_id
        raise AssertionError("delete_message should not be called")

    async def set_my_commands(
        self,
        commands: list[dict],
        *,
        scope: dict | None = None,
        language_code: str | None = None,
    ) -> bool:
        _ = commands, scope, language_code
        raise AssertionError("set_my_commands should not be called")

    async def get_me(self) -> User | None:
        raise AssertionError("get_me should not be called")

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool | None = None,
    ) -> bool:
        _ = callback_query_id, text, show_alert
        raise AssertionError("answer_callback_query should not be called")

    async def get_chat(self, chat_id: int) -> Chat | None:
        _ = chat_id
        raise AssertionError("get_chat should not be called")

    async def get_chat_member(self, chat_id: int, user_id: int) -> ChatMember | None:
        _ = chat_id, user_id
        raise AssertionError("get_chat_member should not be called")

    async def create_forum_topic(self, chat_id: int, name: str) -> ForumTopic | None:
        _ = chat_id, name
        raise AssertionError("create_forum_topic should not be called")

    async def edit_forum_topic(
        self, chat_id: int, message_thread_id: int, name: str
    ) -> bool:
        _ = chat_id, message_thread_id, name
        raise AssertionError("edit_forum_topic should not be called")


def _voice_message(*, file_size: int = 123) -> TelegramIncomingMessage:
    voice = TelegramVoice(
        file_id="voice-id",
        mime_type="audio/ogg",
        file_size=file_size,
        duration=1,
        raw={},
    )
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=1,
        message_id=1,
        text="",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
        voice=voice,
        raw={},
    )


class _Transcriber:
    def __init__(self, *, result: str | None = None, error: Exception | None = None):
        self.calls: list[tuple[str, bytes]] = []
        self.languages: list[str | None] = []
        self._result = result
        self._error = error

    async def transcribe(
        self, *, model: str, audio_bytes: bytes, language: str | None = None
    ) -> str:
        self.calls.append((model, audio_bytes))
        self.languages.append(language)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@pytest.mark.anyio
async def test_transcribe_voice_disabled_replies_with_hint() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="should-not-run")
    result = await transcribe_voice(
        bot=_Bot(file_info=None, audio=None),
        msg=_voice_message(),
        enabled=False,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == VOICE_TRANSCRIPTION_DISABLED_HINT
    assert transcriber.calls == []


@pytest.mark.anyio
async def test_transcribe_voice_handles_missing_file() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    bot = _Bot(file_info=None, audio=None)
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(),
        enabled=True,
        model="whisper-1",
        reply=reply,
    )

    assert result is None
    assert replies[-1] == "failed to fetch voice file."


@pytest.mark.anyio
async def test_transcribe_voice_handles_missing_download() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=None)
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(),
        enabled=True,
        model="whisper-1",
        reply=reply,
    )

    assert result is None
    assert replies[-1] == "failed to download voice file."


@pytest.mark.anyio
async def test_transcribe_voice_rejects_large_voice_without_downloading() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    class _NoFetchBot(_Bot):
        async def get_file(self, file_id: str) -> File | None:  # type: ignore[override]
            _ = file_id
            raise AssertionError("get_file should not be called")

        async def download_file(self, file_path: str) -> bytes | None:  # type: ignore[override]
            _ = file_path
            raise AssertionError("download_file should not be called")

    bot = _NoFetchBot(file_info=None, audio=None)
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=10_000),
        enabled=True,
        model="whisper-1",
        max_bytes=100,
        reply=reply,
    )

    assert result is None
    assert replies[-1] == "voice message is too large to transcribe."


@pytest.mark.anyio
async def test_transcribe_voice_rejects_large_download() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="should-not-run")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"x" * 200)
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=10),
        enabled=True,
        model="whisper-1",
        max_bytes=100,
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == "voice message is too large to transcribe."
    assert transcriber.calls == []


@pytest.mark.anyio
async def test_transcribe_voice_handles_transcriber_error() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(error=RuntimeError("boom"))
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == "boom"
    assert transcriber.calls


@pytest.mark.anyio
async def test_transcribe_voice_success() -> None:
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="transcribed")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result == "transcribed"
    assert replies == []
    assert transcriber.calls
    # No language configured → no hint forwarded (auto-detect preserved)
    assert transcriber.languages == [None]


@pytest.mark.anyio
async def test_transcribe_voice_passes_language_hint() -> None:
    """#638: a configured voice_transcription_language is forwarded to the
    transcriber so Whisper-family models stop guessing the language on short
    utterances ('Continue' → '계속')."""
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="Continue")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
        language="en",
    )

    assert result == "Continue"
    assert transcriber.languages == ["en"]


@pytest.mark.anyio
async def test_transcribe_voice_blocks_private_base_url() -> None:
    """#381: a base_url pointing at a private/reserved address is blocked at
    the chokepoint before any outbound call (transcriber never runs)."""
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="should-not-run")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
        base_url="http://127.0.0.1:8080/v1",
    )

    assert result is None
    assert replies[-1] == "voice transcription endpoint is not permitted."
    assert transcriber.calls == []


@pytest.mark.anyio
async def test_transcribe_voice_allows_allowlisted_base_url() -> None:
    """#381: an explicitly allowlisted private range is permitted."""
    from untether.triggers.ssrf import parse_networks

    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="transcribed")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
        base_url="http://10.0.0.5:9000/v1",
        url_allowlist=parse_networks(["10.0.0.0/8"]),
    )

    assert result == "transcribed"
    assert replies == []
    assert transcriber.calls


@pytest.mark.anyio
async def test_transcribe_voice_connection_error_replies_with_hint() -> None:
    # #584: a transport-level APIConnectionError should surface an actionable
    # transient-network hint, not the opaque "Connection error." string.
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(error=APIConnectionError(request=_REQUEST))
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == VOICE_TRANSCRIPTION_CONNECTION_HINT
    assert transcriber.calls


@pytest.mark.anyio
async def test_transcribe_voice_timeout_error_replies_with_hint() -> None:
    # #584: APITimeoutError is a subclass of APIConnectionError, so it should
    # take the same transient-network hint path.
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(error=APITimeoutError(_REQUEST))
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == VOICE_TRANSCRIPTION_CONNECTION_HINT
    assert transcriber.calls


@pytest.mark.anyio
async def test_transcribe_voice_non_connection_openai_error_sanitised() -> None:
    # A non-connection OpenAIError still goes through user_safe_error so we
    # don't regress the #200 sanitisation path.
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(error=OpenAIError("model not found"))
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == "model not found"
    assert transcriber.calls


@pytest.mark.anyio
async def test_transcribe_voice_stdlib_timeout_branch_reachable() -> None:
    # #584: TimeoutError is a subclass of OSError; the dedicated timeout
    # handler must precede the OSError branch to stay reachable.
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(error=TimeoutError("slow"))
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
    )

    assert result is None
    assert replies[-1] == "voice transcription timed out"
    assert transcriber.calls


@pytest.mark.anyio
async def test_594_transcribe_error_log_includes_endpoint_and_cause() -> None:
    """#594: the openai.transcribe.error log must carry the resolved
    endpoint and the underlying __cause__ — APIConnectionError's str() is a
    bare "Connection error." which made the channelo outage (illegal
    Authorization header from a malformed api_key) undiagnosable from
    logs."""
    from structlog.testing import capture_logs

    async def reply(**kwargs) -> None:
        pass

    err = APIConnectionError(request=_REQUEST)
    err.__cause__ = RuntimeError("Illegal header value b'Bearer sk-a\\nsk-b'")
    transcriber = _Transcriber(error=err)
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")

    with capture_logs() as logs:
        result = await transcribe_voice(
            bot=bot,
            msg=_voice_message(file_size=2),
            enabled=True,
            model="whisper-1",
            reply=reply,
            transcriber=transcriber,
            base_url="https://api.groq.com/openai/v1",
        )

    assert result is None
    rec = next(r for r in logs if r["event"] == "openai.transcribe.error")
    assert rec["endpoint"] == "https://api.groq.com/openai/v1"
    assert "Illegal header value" in (rec["cause"] or "")


@pytest.mark.anyio
async def test_594_transcribe_error_log_default_endpoint_marker() -> None:
    """#594: with no base_url configured the log says "openai-default"
    rather than omitting the field."""
    from structlog.testing import capture_logs

    async def reply(**kwargs) -> None:
        pass

    transcriber = _Transcriber(error=OpenAIError("nope"))
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")

    with capture_logs() as logs:
        await transcribe_voice(
            bot=bot,
            msg=_voice_message(file_size=2),
            enabled=True,
            model="whisper-1",
            reply=reply,
            transcriber=transcriber,
        )

    rec = next(r for r in logs if r["event"] == "openai.transcribe.error")
    assert rec["endpoint"] == "openai-default"
    assert rec["cause"] is None
