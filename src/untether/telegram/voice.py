from __future__ import annotations

import io
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from openai import APIConnectionError, AsyncOpenAI, OpenAIError

from ..logging import get_logger
from ..triggers.ssrf import SSRFError, validate_url_with_dns
from ..utils.error_display import user_safe_error
from .client import BotClient
from .types import TelegramIncomingMessage

logger = get_logger(__name__)

__all__ = ["transcribe_voice"]

VOICE_TRANSCRIPTION_DISABLED_HINT = (
    "voice transcription is disabled. enable it in config:\n"
    "```toml\n"
    "[transports.telegram]\n"
    "voice_transcription = true\n"
    "```"
)

# Shown when the transcription request fails at the transport level
# (APIConnectionError / APITimeoutError) — almost always a transient network
# or provider-edge blip rather than a config/auth problem. Give the user an
# actionable next step instead of the opaque "Connection error." string.
VOICE_TRANSCRIPTION_CONNECTION_HINT = (
    "couldn't reach the transcription service — transient network issue. "
    "please resend the voice note, or type your message instead."
)

# The OpenAI SDK retries connection errors twice by default; widen the window
# so a brief blip self-heals before it ever reaches the user.
_VOICE_MAX_RETRIES = 4


class VoiceTranscriber(Protocol):
    async def transcribe(
        self, *, model: str, audio_bytes: bytes, language: str | None = None
    ) -> str: ...


class OpenAIVoiceTranscriber:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key

    async def transcribe(
        self, *, model: str, audio_bytes: bytes, language: str | None = None
    ) -> str:
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice.ogg"
        # #638: only include `language` when configured — omitting the kwarg
        # entirely preserves the API's auto-detect for unset configs (passing
        # None would serialise a null the endpoint may reject).
        extra: dict[str, str] = {}
        if language is not None:
            extra["language"] = language
        async with AsyncOpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=120,
            max_retries=_VOICE_MAX_RETRIES,
        ) as client:
            response = await client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                **extra,
            )
        return response.text


async def transcribe_voice(
    *,
    bot: BotClient,
    msg: TelegramIncomingMessage,
    enabled: bool,
    model: str,
    max_bytes: int | None = None,
    reply: Callable[..., Awaitable[None]],
    transcriber: VoiceTranscriber | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    url_allowlist: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
    language: str | None = None,
) -> str | None:
    voice = msg.voice
    if voice is None:
        return msg.text
    if not enabled:
        await reply(text=VOICE_TRANSCRIPTION_DISABLED_HINT)
        return None
    if (
        max_bytes is not None
        and voice.file_size is not None
        and voice.file_size > max_bytes
    ):
        await reply(text="voice message is too large to transcribe.")
        return None
    file_info = await bot.get_file(voice.file_id)
    if file_info is None:
        logger.warning(
            "voice.file_info.failed",
            file_id=voice.file_id,
            file_size=voice.file_size,
        )
        await reply(text="failed to fetch voice file.")
        return None
    audio_bytes = await bot.download_file(file_info.file_path)
    if audio_bytes is None:
        logger.warning(
            "voice.download.failed",
            file_id=voice.file_id,
            file_size=voice.file_size,
            file_path=file_info.file_path,
        )
        await reply(text="failed to download voice file.")
        return None
    if max_bytes is not None and len(audio_bytes) > max_bytes:
        await reply(text="voice message is too large to transcribe.")
        return None
    # #381: SSRF-validate a custom base_url before any outbound call. This is
    # the authoritative chokepoint — every transcription path (incl. values that
    # arrived via hot-reload) passes through here. base_url=None means the SDK
    # uses public api.openai.com, which needs no validation.
    if base_url is not None:
        try:
            await validate_url_with_dns(base_url, allowlist=url_allowlist)
        except SSRFError as exc:
            logger.error(
                "voice.base_url.ssrf_blocked",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            await reply(text="voice transcription endpoint is not permitted.")
            return None
    if transcriber is None:
        transcriber = OpenAIVoiceTranscriber(base_url=base_url, api_key=api_key)
    try:
        text = await transcriber.transcribe(
            model=model, audio_bytes=audio_bytes, language=language
        )
        logger.debug(
            "voice.transcribe.success",
            model=model,
            language=language,
            audio_size=len(audio_bytes),
        )
        return text
    except OpenAIError as exc:
        # #594: include the resolved endpoint and the underlying cause.
        # APIConnectionError's str() is a bare "Connection error." — the
        # actual failure (DNS, TLS, or e.g. httpx's LocalProtocolError for
        # an illegal Authorization header built from a malformed api_key)
        # lives in __cause__, and without the endpoint the log can't even
        # say which service was unreachable.
        logger.error(
            "openai.transcribe.error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            cause=repr(exc.__cause__) if exc.__cause__ is not None else None,
            endpoint=base_url or "openai-default",
            file_id=voice.file_id,
            file_size=voice.file_size,
        )
        # #584: a transport-level failure (APIConnectionError, and its subclass
        # APITimeoutError) that survived the SDK's built-in retries is almost
        # always a transient network / provider-edge blip, not a config/auth
        # problem. Reply with an actionable hint instead of the opaque
        # "Connection error." string the user would otherwise see.
        if isinstance(exc, APIConnectionError):
            await reply(text=VOICE_TRANSCRIPTION_CONNECTION_HINT)
            return None
        # #200: don't leak URLs / absolute paths / internal class names back
        # to the Telegram user. Full detail is in the structlog record above.
        await reply(text=user_safe_error(exc, fallback="voice transcription failed"))
        return None
    except TimeoutError as exc:
        # Must precede the OSError branch below: TimeoutError is a subclass of
        # OSError, so listing it afterwards would make this handler dead code.
        logger.error(
            "voice.transcribe.timeout",
            error=str(exc),
            endpoint=base_url or "openai-default",
            file_id=voice.file_id,
            file_size=voice.file_size,
        )
        await reply(text="voice transcription timed out")
        return None
    except (RuntimeError, OSError, ValueError) as exc:
        logger.error(
            "voice.transcribe.error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            endpoint=base_url or "openai-default",
            file_id=voice.file_id,
            file_size=voice.file_size,
        )
        await reply(text=user_safe_error(exc, fallback="voice transcription failed"))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "voice.transcribe.unexpected",
            error=str(exc),
            error_type=exc.__class__.__name__,
            endpoint=base_url or "openai-default",
            file_id=voice.file_id,
            file_size=voice.file_size,
        )
        await reply(text=user_safe_error(exc, fallback="voice transcription failed"))
        return None
