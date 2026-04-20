"""User-facing error sanitisation.

Exceptions from third-party libraries (OpenAI client, aiohttp, OS layer, etc.)
often embed absolute paths, URLs, stack frames, or internal class names that
are useful for debugging but leak environmental detail to end users. Helpers
here produce short, display-safe strings for Telegram reply bodies and keep
the full exception available for structlog at ``logger.error`` level.

Used by voice transcription error paths (#200) and command-dispatch error
paths (#201). Mirrors the path/URL regex approach already used by
``runner._sanitise_stderr`` (#191).
"""

from __future__ import annotations

import re

# Match POSIX-style absolute paths with at least two components after the
# leading slash, e.g. ``/home/nathan/foo`` or ``/var/log/bar``. Matches are
# replaced with ``[path]``.
_ABS_PATH_RE = re.compile(r"(/[\w./-]{3,}/[\w.-]+)")

# Match URLs. Replaced with ``[url]``. Lifted from runner._URL_RE.
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

# Match credential-shaped tokens that may appear in third-party error messages
# (OpenAI/Anthropic/Stripe-style ``sk-…`` keys, ``Bearer …`` headers, and
# generic ``api_key=… / token: …`` style assignments). Replaced with
# ``[secret]``. Order matters — runs after URL/path stripping so the bearer
# regex doesn't swallow URL-encoded paths.
_SECRET_RE = re.compile(
    r"(?i)\b(?:"
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|token|password)"
    r"\s*[:=]\s*[^\s,'\"}\]]+"
    r")"
)

# Default character cap for user-facing error bodies. Telegram callback
# toasts are capped at ~200 chars by Bot API; we use the same budget for
# consistency across send() and answer_callback_query.
_DEFAULT_MAX_CHARS = 200


def user_safe_error(
    exc: BaseException | str,
    *,
    fallback: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Return a short, display-safe string describing *exc*.

    Strips absolute paths and URLs, caps length to *max_chars*, and falls
    back to *fallback* if the sanitised message is empty. Exception class
    names are not leaked — callers can log them separately via structlog's
    ``error_type`` field.
    """
    text = str(exc) if not isinstance(exc, str) else exc
    # URL regex first — the path regex would otherwise match the URL's own
    # path segment (``https://host/a/b`` → ``https:[path]``).
    text = _URL_RE.sub("[url]", text)
    text = _SECRET_RE.sub("[secret]", text)
    text = _ABS_PATH_RE.sub("[path]", text)
    text = text.strip()
    if not text:
        return fallback
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text
