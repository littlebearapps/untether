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
        self.prompts: list[str | None] = []
        self._result = result
        self._error = error

    async def transcribe(
        self,
        *,
        model: str,
        audio_bytes: bytes,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        self.calls.append((model, audio_bytes))
        self.languages.append(language)
        self.prompts.append(prompt)
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
async def test_transcribe_voice_passes_vocabulary_prompt() -> None:
    """#691: a configured voice_transcription_prompt is forwarded to the
    transcriber so the decoder is biased toward domain proper nouns
    ('trollo' → Trello)."""
    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = _Transcriber(result="Update the Trello cards")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    result = await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
        prompt="Trello, Untether, Claude Code",
    )

    assert result == "Update the Trello cards"
    assert transcriber.prompts == ["Trello, Untether, Claude Code"]
    # No prompt configured → None forwarded → the SDK kwarg is omitted.
    transcriber2 = _Transcriber(result="ok")
    bot2 = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    await transcribe_voice(
        bot=bot2,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber2,
    )
    assert transcriber2.prompts == [None]


def test_resolve_transcription_prompt_unset_uses_shipped_default() -> None:
    """#703: #691 was inert on every fleet host because no TOML set the key.
    Unset must now resolve to the shipped vocabulary."""
    from untether.telegram.voice import (
        DEFAULT_VOICE_TRANSCRIPTION_PROMPT,
        resolve_transcription_prompt,
    )

    assert resolve_transcription_prompt(None) == DEFAULT_VOICE_TRANSCRIPTION_PROMPT
    # Product-generic only — no deployment-specific nouns in a PyPI wheel.
    for term in ("lba-1", "nsd", "channelo", "Trello"):
        assert term not in DEFAULT_VOICE_TRANSCRIPTION_PROMPT


def _default_prompt_terms() -> list[str]:
    from untether.telegram.voice import DEFAULT_VOICE_TRANSCRIPTION_PROMPT

    # Exact elements: a substring check can't tell "Claude" from "Claude Code".
    return DEFAULT_VOICE_TRANSCRIPTION_PROMPT.split(", ")


def test_default_voice_prompt_includes_bare_claude() -> None:
    """#789 regression guard: rc14 had "Claude" only inside "Claude Code" and
    "CLAUDE.md" (tokenised C|LAU|DE), so nsd still heard "Clawde". V1 order
    (Claude first) per the rc15 plan; the recorded-clip A/B (R15-11) is owed."""
    terms = _default_prompt_terms()
    assert "Claude" in terms
    assert terms[0] == "Claude"


def test_default_voice_prompt_keeps_referent_terms() -> None:
    """Engine names and the agent context files carry a spoken instruction's
    referent ("run it on Codex", "update CLAUDE.md")."""
    assert {
        "Claude Code",
        "CLAUDE.md",
        "AGENTS.md",
        "Codex",
        "OpenCode",
        "Untether",
    } <= set(_default_prompt_terms())


def test_default_voice_prompt_drops_deprecated_and_out_of_scope_engines() -> None:
    from untether.telegram.voice import DEFAULT_VOICE_TRANSCRIPTION_PROMPT

    terms = _default_prompt_terms()
    for term in ("Gemini", "Amp", "Pi"):
        assert term not in terms
    # Substring check too, except "Pi" (it's inside "PyPI").
    for term in ("Gemini", "Amp"):
        assert term not in DEFAULT_VOICE_TRANSCRIPTION_PROMPT


def test_default_voice_prompt_excludes_non_canonical_filename() -> None:
    """Mixed-case "Claude.md" would bias towards a filename no repo uses."""
    from untether.telegram.voice import DEFAULT_VOICE_TRANSCRIPTION_PROMPT

    assert "Claude.md" not in _default_prompt_terms()
    assert "Claude.md" not in DEFAULT_VOICE_TRANSCRIPTION_PROMPT


def test_default_voice_prompt_well_inside_whisper_window() -> None:
    """≤300 chars ≈ ≤130 Whisper tokens at ~2.3 chars/token, against the
    224-token prompt window (Whisper keeps only the last 224)."""
    from untether.telegram.voice import DEFAULT_VOICE_TRANSCRIPTION_PROMPT

    terms = _default_prompt_terms()
    assert len(DEFAULT_VOICE_TRANSCRIPTION_PROMPT) <= 300
    assert len(terms) == len(set(terms))
    for term in terms:
        assert term
        assert term == term.strip()
    assert not DEFAULT_VOICE_TRANSCRIPTION_PROMPT.rstrip().endswith(",")


def test_default_voice_prompt_documented_verbatim() -> None:
    """#789 D4: the transport reference quotes the default verbatim, so a
    constant change without a docs change fails here (the FAQ drifted once)."""
    from pathlib import Path

    from untether.telegram.voice import DEFAULT_VOICE_TRANSCRIPTION_PROMPT

    doc = Path(__file__).parents[1] / "docs/reference/transports/telegram.md"
    assert DEFAULT_VOICE_TRANSCRIPTION_PROMPT in doc.read_text(encoding="utf-8")


def test_resolve_transcription_prompt_empty_string_opts_out() -> None:
    """#703: explicit "" disables the bias entirely — same shape as
    `[preamble] text = ""`. This is why the setting is str|None, not
    NonEmptyStr|None."""
    from untether.telegram.voice import resolve_transcription_prompt

    assert resolve_transcription_prompt("") is None


def test_resolve_transcription_prompt_override_replaces_default() -> None:
    """An operator value REPLACES the default rather than merging — their
    token budget stays theirs to spend."""
    from untether.telegram.voice import resolve_transcription_prompt

    assert resolve_transcription_prompt("Trello, lba-1") == "Trello, lba-1"


@pytest.mark.anyio
async def test_transcribe_voice_default_prompt_reaches_transcriber() -> None:
    """#703 end-to-end at the transport boundary: an unconfigured deployment
    now sends the vocabulary bias instead of omitting the parameter."""
    from untether.telegram.voice import (
        DEFAULT_VOICE_TRANSCRIPTION_PROMPT,
        resolve_transcription_prompt,
    )

    async def reply(**kwargs) -> None:
        return None

    transcriber = _Transcriber(result="Deploy to TestPyPI")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    await transcribe_voice(
        bot=bot,
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
        prompt=resolve_transcription_prompt(None),
    )
    assert transcriber.prompts == [DEFAULT_VOICE_TRANSCRIPTION_PROMPT]


@pytest.mark.anyio
async def test_openai_transcriber_omits_unset_prompt_kwarg() -> None:
    """#691: some OpenAI-compatible endpoints 400 on unknown multipart
    fields — `prompt` must be absent from the request when unset, and
    present verbatim when configured (same contract as #638's language)."""
    from untether.telegram.voice import OpenAIVoiceTranscriber

    captured: list[dict] = []

    class _FakeTranscriptions:
        async def create(self, **kwargs):
            captured.append(kwargs)

            class _Resp:
                text = "ok"

            return _Resp()

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeClient:
        audio = _FakeAudio()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    transcriber = OpenAIVoiceTranscriber()
    import untether.telegram.voice as voice_mod

    orig = voice_mod.AsyncOpenAI
    voice_mod.AsyncOpenAI = lambda **kwargs: _FakeClient()  # type: ignore[assignment]
    try:
        await transcriber.transcribe(model="whisper-1", audio_bytes=b"x")
        await transcriber.transcribe(
            model="whisper-1",
            audio_bytes=b"x",
            language="en",
            prompt="Trello, Untether",
        )
    finally:
        voice_mod.AsyncOpenAI = orig  # type: ignore[assignment]

    assert "prompt" not in captured[0]
    assert "language" not in captured[0]
    assert captured[1]["prompt"] == "Trello, Untether"
    assert captured[1]["language"] == "en"


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
    # #679: the reply now names the host and the allowlist entry to add.
    assert "`127.0.0.1`" in replies[-1]
    assert "voice_transcription_url_allowlist" in replies[-1]
    assert '"127.0.0.0/8"' in replies[-1]
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


class _BoomError(Exception):
    """An Exception subclass none of the specific handlers catch."""


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "event"),
    [
        (OpenAIError("nope"), "openai.transcribe.error"),
        (TimeoutError("slow"), "voice.transcribe.timeout"),
        (RuntimeError("bad"), "voice.transcribe.error"),
        (_BoomError("odd"), "voice.transcribe.unexpected"),
    ],
)
async def test_841_endpoint_userinfo_masked_at_all_four_sites(
    monkeypatch: pytest.MonkeyPatch, error: Exception, event: str
) -> None:
    """#841: a base_url with userinfo credentials (and a signed query) must
    never reach the log verbatim from any of the four failure branches."""
    from structlog.testing import capture_logs

    import untether.telegram.voice as voice_mod

    async def _permitted(*args, **kwargs):
        return voice_mod.VoiceEndpointVerdict(
            status="permitted", host="stt.example.com", port=443
        )

    monkeypatch.setattr(voice_mod, "classify_voice_endpoint", _permitted)

    async def reply(**kwargs) -> None:
        pass

    transcriber = _Transcriber(error=error)
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")

    with capture_logs() as logs:
        result = await transcribe_voice(
            bot=bot,
            msg=_voice_message(file_size=2),
            enabled=True,
            model="whisper-1",
            reply=reply,
            transcriber=transcriber,
            base_url="https://r17user:r17pw@stt.example.com/v1?sig=abc",
        )

    assert result is None
    rec = next(r for r in logs if r["event"] == event)
    assert rec["endpoint"] == "https://***@stt.example.com/v1"
    blob = str(logs)
    assert "r17pw" not in blob
    assert "r17user" not in blob
    assert "sig=abc" not in blob


# ---------------------------------------------------------------------------
# #679: actionable SSRF refusal + startup/reload endpoint check
# ---------------------------------------------------------------------------


def _gai(*ips: str) -> list[tuple]:
    import socket

    out: list[tuple] = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 8000, 0, 0) if family == socket.AF_INET6 else (ip, 8000)
        out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return out


async def _run_voice(
    *,
    base_url: str,
    allowlist: tuple[str, ...] = (),
    transcriber: _Transcriber | None = None,
) -> tuple[str | None, list[str], _Transcriber]:
    from untether.triggers.ssrf import parse_networks

    replies: list[str] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs["text"])

    transcriber = transcriber or _Transcriber(result="should-not-run")
    result = await transcribe_voice(
        bot=_Bot(file_info=File(file_path="voice.ogg"), audio=b"ok"),
        msg=_voice_message(file_size=2),
        enabled=True,
        model="whisper-1",
        reply=reply,
        transcriber=transcriber,
        base_url=base_url,
        url_allowlist=parse_networks(list(allowlist)),
    )
    return result, replies, transcriber


@pytest.mark.anyio
async def test_679_localhost_reply_names_host_key_and_suggestion() -> None:
    from unittest.mock import patch

    with patch("socket.getaddrinfo", return_value=_gai("127.0.0.1", "::1")):
        result, replies, transcriber = await _run_voice(
            base_url="http://localhost:8000/v1"
        )
    assert result is None
    assert transcriber.calls == []
    assert len(replies) == 1
    text = replies[0]
    assert "`localhost`" in text
    assert 'voice_transcription_url_allowlist = ["127.0.0.0/8"]' in text
    assert "hot-reloads" in text
    assert "8000" not in text
    assert "/v1" not in text


@pytest.mark.anyio
async def test_679_blocked_log_fields() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("127.0.0.1", "::1")),
    ):
        await _run_voice(base_url="http://localhost:8000/v1")
    events = [e for e in logs if e["event"] == "voice.base_url.ssrf_blocked"]
    assert len(events) == 1
    ev = events[0]
    assert ev["log_level"] == "error"
    assert ev["host"] == "localhost"
    assert ev["port"] == 8000
    assert ev["reason"] == "blocked_address"
    assert ev["blocked_addresses"] == ["127.0.0.1", "::1"]
    assert ev["allowlist_key"] == "voice_transcription_url_allowlist"
    assert ev["suggested_allowlist"] == ["127.0.0.0/8"]
    assert ev["error_type"] == "SSRFBlockedError"
    assert "127.0.0.0/8" in ev["hint"]


def _assert_no_secret(logs: list[dict], replies: list[str]) -> None:
    for text in replies:
        assert "s3cret" not in text
    for entry in logs:
        for value in entry.values():
            assert "s3cret" not in str(value)


@pytest.mark.anyio
async def test_679_userinfo_never_echoed_blocked_path() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("127.0.0.1")),
    ):
        _, replies, _ = await _run_voice(
            base_url="http://user:s3cret@localhost:8000/v1"
        )
    assert replies
    assert any(e["event"] == "voice.base_url.ssrf_blocked" for e in logs)
    _assert_no_secret(logs, replies)


@pytest.mark.anyio
async def test_679_userinfo_never_echoed_permitted_path() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("127.0.0.1")),
    ):
        result, replies, transcriber = await _run_voice(
            base_url="http://user:s3cret@localhost:8000/v1",
            allowlist=("127.0.0.0/8",),
            transcriber=_Transcriber(result="hello"),
        )
    assert result == "hello"
    assert transcriber.calls
    assert any(e["event"] == "ssrf.validated" for e in logs)
    _assert_no_secret(logs, replies)


@pytest.mark.anyio
@pytest.mark.parametrize("allowlist", [(), ("127.0.0.0/8",)])
async def test_679_userinfo_never_echoed_startup_check(
    allowlist: tuple[str, ...],
) -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.telegram.voice import check_voice_endpoint

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("127.0.0.1")),
    ):
        verdict = await check_voice_endpoint(
            enabled=True,
            base_url="http://user:s3cret@localhost:8000/v1",
            allowlist_entries=allowlist,
            phase="startup",
        )
    assert verdict is not None
    assert verdict.status == ("permitted" if allowlist else "blocked")
    _assert_no_secret(logs, [])


@pytest.mark.anyio
async def test_679_metadata_host_no_allowlist_suggestion() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("169.254.169.254")),
    ):
        _, replies, _ = await _run_voice(base_url="http://meta.internal/latest")
    assert "reserved address" in replies[0]
    assert "voice_transcription_url_allowlist =" not in replies[0]
    ev = next(e for e in logs if e["event"] == "voice.base_url.ssrf_blocked")
    assert ev["suggested_allowlist"] == []


@pytest.mark.anyio
async def test_679_dns_failure_reply_and_log() -> None:
    import socket
    from unittest.mock import patch

    from structlog.testing import capture_logs

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")),
    ):
        _, replies, _ = await _run_voice(base_url="http://whisper.invalid:8000/v1")
    assert "could not be resolved" in replies[0]
    assert "voice_transcription_url_allowlist" not in replies[0]
    ev = next(e for e in logs if e["event"] == "voice.base_url.ssrf_blocked")
    assert ev["reason"] == "dns_failed"


@pytest.mark.anyio
async def test_679_tailnet_private_suggests_exact_ip() -> None:
    from unittest.mock import patch

    with patch("socket.getaddrinfo", return_value=_gai("100.101.102.103")):
        _, replies, _ = await _run_voice(base_url="http://whisper.tailnet.ts.net/v1")
    assert '["100.101.102.103"]' in replies[0]
    assert "`whisper.tailnet.ts.net`" in replies[0]


def test_679_hostile_hostname_not_rendered() -> None:
    from untether.telegram.voice import (
        VoiceEndpointVerdict,
        format_voice_endpoint_refusal,
    )

    for host in ("evil`host", "a*b", None):
        text = format_voice_endpoint_refusal(
            VoiceEndpointVerdict(
                status="blocked",
                host=host,
                port=80,
                addresses=("127.0.0.1",),
                suggested=("127.0.0.0/8",),
            )
        )
        assert "the configured host" in text
        if host:
            assert host not in text


def test_679_invalid_verdict_keeps_generic_reply() -> None:
    from untether.telegram.voice import (
        VoiceEndpointVerdict,
        format_voice_endpoint_refusal,
    )

    text = format_voice_endpoint_refusal(
        VoiceEndpointVerdict(status="invalid", host=None, port=None)
    )
    assert text == "voice transcription endpoint is not permitted."


@pytest.mark.anyio
async def test_679_allowlisted_localhost_transcribes() -> None:
    from unittest.mock import patch

    with patch("socket.getaddrinfo", return_value=_gai("127.0.0.1", "::1")):
        result, replies, transcriber = await _run_voice(
            base_url="http://localhost:8000/v1",
            allowlist=("127.0.0.0/8",),
            transcriber=_Transcriber(result="transcribed"),
        )
    assert result == "transcribed"
    assert replies == []
    assert transcriber.calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("enabled", "base_url"),
    [(False, "http://localhost:8000/v1"), (True, None)],
)
async def test_679_check_skips_when_disabled_or_unset(
    enabled: bool, base_url: str | None
) -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.telegram.voice import check_voice_endpoint

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")),
    ):
        verdict = await check_voice_endpoint(
            enabled=enabled,
            base_url=base_url,
            allowlist_entries=(),
            phase="startup",
        )
    assert verdict is None
    assert not [e for e in logs if e["event"].startswith("voice.base_url.")]


@pytest.mark.anyio
async def test_679_check_warns_for_localhost_startup() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.telegram.voice import check_voice_endpoint

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("127.0.0.1", "::1")),
    ):
        verdict = await check_voice_endpoint(
            enabled=True,
            base_url="http://localhost:8000/v1",
            allowlist_entries=(),
            phase="startup",
        )
    assert verdict is not None and verdict.status == "blocked"
    warns = [e for e in logs if e["event"] == "voice.base_url.not_permitted"]
    assert len(warns) == 1
    ev = warns[0]
    assert ev["log_level"] == "warning"
    assert ev["phase"] == "startup"
    assert ev["host"] == "localhost"
    assert ev["port"] == 8000
    assert ev["allowlist_key"] == "voice_transcription_url_allowlist"
    assert ev["suggested_allowlist"] == ["127.0.0.0/8"]


@pytest.mark.anyio
async def test_679_check_permitted_logs_info() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.telegram.voice import check_voice_endpoint

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("93.184.216.34")),
    ):
        verdict = await check_voice_endpoint(
            enabled=True,
            base_url="https://api.groq.com/openai/v1",
            allowlist_entries=(),
            phase="reload",
        )
    assert verdict is not None and verdict.status == "permitted"
    permitted = [e for e in logs if e["event"] == "voice.base_url.permitted"]
    assert len(permitted) == 1
    assert permitted[0]["log_level"] == "info"
    assert permitted[0]["phase"] == "reload"
    assert permitted[0]["host"] == "api.groq.com"
    assert not [e for e in logs if e["log_level"] == "warning"]


@pytest.mark.anyio
async def test_679_check_allowlisted_localhost_permitted() -> None:
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.telegram.voice import check_voice_endpoint

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", return_value=_gai("127.0.0.1", "::1")),
    ):
        verdict = await check_voice_endpoint(
            enabled=True,
            base_url="http://localhost:8000/v1",
            allowlist_entries=("127.0.0.0/8",),
            phase="reload",
        )
    assert verdict is not None and verdict.status == "permitted"
    assert [e for e in logs if e["event"] == "voice.base_url.permitted"]
    assert not [e for e in logs if e["event"] == "voice.base_url.not_permitted"]


@pytest.mark.anyio
async def test_679_check_dns_failure_logs_check_failed() -> None:
    import socket
    from unittest.mock import patch

    from structlog.testing import capture_logs

    from untether.telegram.voice import check_voice_endpoint

    with (
        capture_logs() as logs,
        patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")),
    ):
        verdict = await check_voice_endpoint(
            enabled=True,
            base_url="http://whisper.invalid/v1",
            allowlist_entries=(),
            phase="startup",
        )
    assert verdict is not None and verdict.status == "unresolvable"
    ev = next(e for e in logs if e["event"] == "voice.base_url.check_failed")
    assert ev["reason"] == "dns_failed"
    assert ev["log_level"] == "warning"


@pytest.mark.anyio
async def test_679_check_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    import untether.telegram.voice as voice_mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(voice_mod, "classify_voice_endpoint", _boom)
    with capture_logs() as logs:
        verdict = await voice_mod.check_voice_endpoint(
            enabled=True,
            base_url="http://localhost:8000/v1",
            allowlist_entries=(),
            phase="startup",
        )
    assert verdict is None
    ev = next(e for e in logs if e["event"] == "voice.base_url.check_failed")
    assert ev["reason"] == "error"
    assert ev["error_type"] == "RuntimeError"


def test_679_voice_endpoint_keys_changed() -> None:
    from untether.telegram.voice import voice_endpoint_keys_changed

    for key in (
        "voice_transcription",
        "voice_transcription_base_url",
        "voice_transcription_url_allowlist",
    ):
        assert voice_endpoint_keys_changed([key])
    assert not voice_endpoint_keys_changed(
        ["show_resume_line", "voice_transcription_model", "voice_transcription_prompt"]
    )
    assert not voice_endpoint_keys_changed([])


# --- #789: deterministic fix-up of the known "Claude" mishears ---


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Thanks, Clawde. Yes, go ahead", "Thanks, Claude. Yes, go ahead"),
        ("thanks clawd", "thanks Claude"),
        ("open it in Clawed Code", "open it in Claude Code"),
        ("NSDMain Corde Code session", "NSDMain Claude Code session"),
        ("Clawde Code is running", "Claude Code is running"),
        ("including Clawde.md and AGENTS.md", "including CLAUDE.md and AGENTS.md"),
        ("update the Claw.md file", "update the CLAUDE.md file"),
        ("check clawed.md, then", "check CLAUDE.md, then"),
        # rc5 dev-bot voice note: "clode" (a non-word) for both forms.
        ("open the clode.md file", "open the CLAUDE.md file"),
        ("what clode code says", "what Claude Code says"),
        ("thanks Clode", "thanks Claude"),
        ("read clod.md first", "read CLAUDE.md first"),
        ("in Clod Code", "in Claude Code"),
    ],
)
def test_789_known_claude_mishears_corrected(heard: str, fixed: str) -> None:
    from untether.telegram.voice import correct_known_mishears

    text, count = correct_known_mishears(heard)
    assert text == fixed
    assert count >= 1


@pytest.mark.parametrize(
    "text",
    [
        # Real words and real names stay as heard.
        "the cat clawed at the door",
        "a claw machine and a corded drill",
        "Claude Code, CLAUDE.md and Claude are already right",
        "Cloud Code is a Google product",
        "a clod of earth",  # real word, not before .md / Code
        "clawdeck",  # inside another word
        "",
    ],
)
def test_789_correction_leaves_other_text_alone(text: str) -> None:
    from untether.telegram.voice import correct_known_mishears

    assert correct_known_mishears(text) == (text, 0)


@pytest.mark.anyio
async def test_789_transcribe_voice_returns_corrected_text() -> None:
    from structlog.testing import capture_logs

    async def reply(**kwargs) -> None:
        raise AssertionError(kwargs)

    transcriber = _Transcriber(result="Thanks, Clawde. Update Claw.md in Clawed Code")
    bot = _Bot(file_info=File(file_path="voice.ogg"), audio=b"ok")
    with capture_logs() as logs:
        result = await transcribe_voice(
            bot=bot,
            msg=_voice_message(file_size=2),
            enabled=True,
            model="whisper-1",
            reply=reply,
            transcriber=transcriber,
        )
    assert result == "Thanks, Claude. Update CLAUDE.md in Claude Code"
    fixed = [r for r in logs if r["event"] == "voice.transcript.corrected"]
    assert len(fixed) == 1 and fixed[0]["corrections"] == 3
    # The transcript itself is never logged.
    assert "Clawde" not in str(fixed)
