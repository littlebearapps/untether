from __future__ import annotations

import io
import ipaddress
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlparse

from openai import APIConnectionError, AsyncOpenAI, OpenAIError

from ..logging import get_logger
from ..triggers.ssrf import (
    SSRFBlockedError,
    SSRFError,
    SSRFResolutionError,
    parse_networks,
    strip_url_userinfo,
    suggest_allowlist,
    validate_url_with_dns,
)
from ..utils.error_display import user_safe_error
from .client import BotClient
from .types import TelegramIncomingMessage

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_VOICE_TRANSCRIPTION_PROMPT",
    "VOICE_ENDPOINT_KEYS",
    "VoiceEndpointVerdict",
    "check_voice_endpoint",
    "classify_voice_endpoint",
    "format_voice_endpoint_refusal",
    "resolve_transcription_prompt",
    "transcribe_voice",
    "voice_endpoint_keys_changed",
]

# #703: #691 shipped `voice_transcription_prompt` correctly but NO host set it,
# so the vocabulary bias was inert everywhere and "trollo" kept arriving. A
# shipped default fixes the common case on upgrade with no per-host TOML edit.
#
# Deliberately PRODUCT-GENERIC: engine names, the tool's own nouns, and the
# release vocabulary every Untether user speaks. Deployment-specific terms
# (project names, hostnames, third-party tools) stay the operator's job — a
# fleet's own nouns don't belong in a PyPI wheel, and every extra term widens
# the hallucination surface on short or silent clips. These are also the words
# that carry the *referent* of a spoken instruction ("run it on Codex"), so
# they're the highest-value ones to protect. #789 adds the agent context files
# (CLAUDE.md, AGENTS.md) — every user's agents work with them, and "CLAUDE.md"
# was otherwise heard as "Claw.md".
DEFAULT_VOICE_TRANSCRIPTION_PROMPT = (
    "Untether, Telegram, Claude Code, Codex, OpenCode, Gemini, Amp, Pi, "
    "MCP, CLI, repo, changelog, PyPI, CLAUDE.md, AGENTS.md"
)


def resolve_transcription_prompt(configured: str | None) -> str | None:
    """#703: map the configured value onto the prompt actually sent.

    - ``None`` (key absent) → the shipped default
    - ``""`` (explicitly empty) → ``None``, i.e. omit the parameter entirely
    - anything else → itself, verbatim (an override REPLACES the default; it
      does not merge, so the operator's token budget stays theirs to spend)
    """
    if configured is None:
        return DEFAULT_VOICE_TRANSCRIPTION_PROMPT
    return configured or None


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

# #679: config key that opts a private/loopback voice endpoint in.
VOICE_ALLOWLIST_KEY = "voice_transcription_url_allowlist"

# #679: transport keys whose change can alter the endpoint verdict — a reload
# touching any of them re-runs the endpoint check; unrelated reloads don't.
VOICE_ENDPOINT_KEYS: frozenset[str] = frozenset(
    {
        "voice_transcription",
        "voice_transcription_base_url",
        VOICE_ALLOWLIST_KEY,
    }
)

# Only a plain hostname / IP literal is echoed into Telegram (inline code).
# Anything else (backticks, markdown, spaces) → "the configured host".
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")


@dataclass(frozen=True, slots=True)
class VoiceEndpointVerdict:
    """#679: the SSRF verdict for a voice ``base_url``.

    Carries only the parsed host and port — never userinfo or the path — so
    anything built from it (reply text, log fields) cannot leak credentials.
    """

    status: Literal["permitted", "blocked", "unresolvable", "invalid"]
    host: str | None
    port: int | None
    addresses: tuple[str, ...] = ()
    suggested: tuple[str, ...] = ()
    error: str | None = None
    error_type: str | None = None


def voice_endpoint_keys_changed(keys: Iterable[str]) -> bool:
    """#679: True when a hot-reload touched a key that affects the verdict."""
    return any(k in VOICE_ENDPOINT_KEYS for k in keys)


def _host_and_port(base_url: str) -> tuple[str | None, int | None]:
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None, None
    return host, port


async def classify_voice_endpoint(
    base_url: str,
    allowlist: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
) -> VoiceEndpointVerdict:
    """#679: run the SSRF guard on *base_url* and map the outcome to a verdict.

    Both the per-voice-note chokepoint and the startup/reload check call this,
    so the two can never disagree. Validates a userinfo-stripped copy: the
    verdict depends only on the scheme and host, and the ``ssrf.*`` logs then
    never see credentials.
    """
    url = strip_url_userinfo(base_url)
    host, port = _host_and_port(url)
    try:
        await validate_url_with_dns(url, allowlist=allowlist)
    except SSRFBlockedError as exc:
        return VoiceEndpointVerdict(
            status="blocked",
            host=host or exc.hostname,
            port=port,
            addresses=exc.addresses,
            suggested=suggest_allowlist(exc.addresses),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
    except SSRFResolutionError as exc:
        return VoiceEndpointVerdict(
            status="unresolvable",
            host=host or exc.hostname,
            port=port,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
    except SSRFError as exc:
        return VoiceEndpointVerdict(
            status="invalid",
            host=host,
            port=port,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
    return VoiceEndpointVerdict(status="permitted", host=host, port=port)


def _display_host(host: str | None) -> str:
    if host and _SAFE_HOST_RE.match(host):
        return f"`{host}`"
    return "the configured host"


def _allowlist_toml(suggested: Sequence[str]) -> str:
    inner = ", ".join(f'"{entry}"' for entry in suggested)
    return f"{VOICE_ALLOWLIST_KEY} = [{inner}]"


def _endpoint_hint(verdict: VoiceEndpointVerdict) -> str:
    """One-line operator fix, shared by the runtime and startup logs."""
    if verdict.status == "blocked" and verdict.suggested:
        return (
            f"add {_allowlist_toml(verdict.suggested)} to [transports.telegram] "
            "(hot-reloads, no restart needed)"
        )
    if verdict.status == "blocked":
        return (
            "the host resolves to a reserved address that can't be allowlisted "
            "safely; point voice_transcription_base_url at a different host"
        )
    if verdict.status == "unresolvable":
        return "DNS lookup failed; check voice_transcription_base_url"
    return "check voice_transcription_base_url"


def format_voice_endpoint_refusal(verdict: VoiceEndpointVerdict) -> str:
    """#679: the actionable Telegram reply for a refused voice endpoint.

    Names the host (only if it is a plain hostname/IP) and, for a
    loopback/private address, the exact allowlist entry that opts it in. Never
    echoes the full URL, userinfo, path or port (#200 posture).
    """
    host = _display_host(verdict.host)
    if verdict.status == "blocked" and verdict.suggested:
        return (
            f"voice transcription endpoint {host} is blocked by the SSRF guard "
            "(it resolves to a loopback/private address).\n"
            "to allow it, add this to `[transports.telegram]` in untether.toml "
            "(hot-reloads, no restart needed):\n"
            "```toml\n"
            f"{_allowlist_toml(verdict.suggested)}\n"
            "```\n"
            "if you already have entries, add the new value to that list."
        )
    if verdict.status == "blocked":
        return (
            f"voice transcription endpoint {host} resolves to a reserved address "
            "(link-local / cloud metadata) and is blocked by the SSRF guard. "
            "point `voice_transcription_base_url` at a different host."
        )
    if verdict.status == "unresolvable":
        return (
            f"voice transcription endpoint {host} could not be resolved "
            "(DNS lookup failed). check `voice_transcription_base_url`."
        )
    return "voice transcription endpoint is not permitted."


_VERDICT_REASON = {
    "blocked": "blocked_address",
    "unresolvable": "dns_failed",
    "invalid": "invalid",
}


async def check_voice_endpoint(
    *,
    enabled: bool,
    base_url: str | None,
    allowlist_entries: Sequence[str],
    phase: Literal["startup", "reload"],
) -> VoiceEndpointVerdict | None:
    """#679: log whether the configured voice endpoint would be refused.

    Runs at startup and after a hot-reload that touched a voice endpoint key,
    so a blocked ``localhost``/tailnet endpoint shows up in the journal before
    the first voice note. Log-only (never a Telegram message) and it NEVER
    raises — it runs as a background task in the main task group.

    Note: the DNS lookup runs in a worker thread that is not abandon-on-cancel,
    so shutdown may wait up to the resolver timeout if DNS hangs.
    """
    if not enabled or base_url is None:
        return None
    try:
        verdict = await classify_voice_endpoint(
            base_url, allowlist=parse_networks(allowlist_entries)
        )
        if verdict.status == "permitted":
            logger.info("voice.base_url.permitted", phase=phase, host=verdict.host)
        elif verdict.status == "blocked":
            logger.warning(
                "voice.base_url.not_permitted",
                phase=phase,
                host=verdict.host,
                port=verdict.port,
                blocked_addresses=list(verdict.addresses),
                allowlist_key=VOICE_ALLOWLIST_KEY,
                suggested_allowlist=list(verdict.suggested),
                hint=_endpoint_hint(verdict),
            )
        else:
            logger.warning(
                "voice.base_url.check_failed",
                phase=phase,
                host=verdict.host,
                reason=_VERDICT_REASON[verdict.status],
                hint=_endpoint_hint(verdict),
            )
        return verdict
    except Exception as exc:  # noqa: BLE001 — advisory check, never break the loop
        logger.warning(
            "voice.base_url.check_failed",
            phase=phase,
            reason="error",
            error_type=exc.__class__.__name__,
        )
        return None


class VoiceTranscriber(Protocol):
    async def transcribe(
        self,
        *,
        model: str,
        audio_bytes: bytes,
        language: str | None = None,
        prompt: str | None = None,
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
        self,
        *,
        model: str,
        audio_bytes: bytes,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice.ogg"
        # #638: only include `language` when configured — omitting the kwarg
        # entirely preserves the API's auto-detect for unset configs (passing
        # None would serialise a null the endpoint may reject).
        # #691: same for `prompt` (vocabulary bias) — some OpenAI-compatible
        # endpoints 400 on unknown multipart fields rather than ignoring them.
        extra: dict[str, str] = {}
        if language is not None:
            extra["language"] = language
        if prompt is not None:
            extra["prompt"] = prompt
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
    prompt: str | None = None,
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
    # #679: the verdict carries the host/addresses so the reply and log can
    # name the host and the allowlist entry that opts it in.
    if base_url is not None:
        verdict = await classify_voice_endpoint(base_url, allowlist=url_allowlist)
        if verdict.status != "permitted":
            logger.error(
                "voice.base_url.ssrf_blocked",
                error=verdict.error,
                error_type=verdict.error_type,
                host=verdict.host,
                port=verdict.port,
                reason=_VERDICT_REASON[verdict.status],
                blocked_addresses=list(verdict.addresses),
                allowlist_key=VOICE_ALLOWLIST_KEY,
                suggested_allowlist=list(verdict.suggested),
                hint=_endpoint_hint(verdict),
            )
            await reply(text=format_voice_endpoint_refusal(verdict))
            return None
    if transcriber is None:
        transcriber = OpenAIVoiceTranscriber(base_url=base_url, api_key=api_key)
    try:
        text = await transcriber.transcribe(
            model=model, audio_bytes=audio_bytes, language=language, prompt=prompt
        )
        logger.debug(
            "voice.transcribe.success",
            model=model,
            language=language,
            # #691: never log the prompt text itself — operators may put
            # project/client names in it.
            prompt_configured=prompt is not None,
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
