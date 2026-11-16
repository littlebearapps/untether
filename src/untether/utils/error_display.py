"""User-facing error sanitisation.

Exceptions from third-party libraries (OpenAI client, aiohttp, OS layer, etc.)
often embed absolute paths, URLs, stack frames, or internal class names that
are useful for debugging but leak environmental detail to end users. Helpers
here produce short, display-safe strings for Telegram reply bodies and keep
the full exception available for structlog at ``logger.error`` level.

Used by voice transcription error paths (#200) and command-dispatch error
paths (#201), and ``redact_url_secrets`` by engine error cards (#1151).
Mirrors the path/URL regex approach already used by
``runner._sanitise_stderr`` (#191).
"""

from __future__ import annotations

import re

from ..logging import URL_USERINFO_RE

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


# #1151: an engine's own error text (OpenCode ``error`` event, Codex
# ``turn.failed`` / API-error JSON, a Claude error ``result``, Pi
# ``errorMessage``) can quote a URL — a model id the user set, a provider's
# callback or sign-in link. The URL is kept readable (scheme, host and path
# tell the user what failed) but its userinfo and every query / fragment
# parameter value are masked: the same markers the logs use (#841, #1065),
# applied unconditionally because a card is never the place to read a
# parameter's value. Any scheme, any case, like ``runner._URL_RE`` (#1033); a
# JSON-escaped ``&`` separator stays inside the URL so a parameter after
# it is masked too.
_ERROR_URL_RE = re.compile(
    r"[a-z][a-z0-9+.-]{1,20}://(?:[^\s\"'<>\\]|\\u0026)*", re.IGNORECASE
)
_PARAM_SEP_RE = re.compile(r"(&|\\u0026)")
# Sentence punctuation after a URL in prose is not part of it.
_URL_TRAILING_PUNCT = ".,;:!?)]}"
_REDACTED = "[REDACTED]"


def _redact_params(segment: str) -> str:
    pieces = _PARAM_SEP_RE.split(segment)
    for index in range(0, len(pieces), 2):
        name, sep, value = pieces[index].partition("=")
        # Only ``name=value``: a bare ``#section`` anchor or ``?flag`` is kept,
        # and so is a value that is already a marker.
        if sep and value and not value.startswith("[REDACTED"):
            pieces[index] = f"{name}={_REDACTED}"
    return "".join(pieces)


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    body = url.rstrip(_URL_TRAILING_PUNCT)
    tail = url[len(body) :]
    fragment: str | None = None
    hash_at = body.find("#")
    if hash_at >= 0:
        body, fragment = body[:hash_at], body[hash_at + 1 :]
    query_at = body.find("?")
    if query_at >= 0:
        body = f"{body[: query_at + 1]}{_redact_params(body[query_at + 1 :])}"
    if fragment is not None:
        body = f"{body}#{_redact_params(fragment)}"
    return f"{body}{tail}"


def redact_url_secrets(text: str) -> str:
    """Mask URL userinfo and query / fragment parameter values in *text*.

    Everything else is left exactly as it was: prose, paths, a URL with no
    query, and the ``[url]`` / ``[path]`` markers an earlier sanitiser
    (``runner._sanitise_stderr``, ``redact_agy_text``) already wrote — so the
    pass is idempotent and safe on text that went through either.
    """
    if "://" not in text:
        return text
    text = URL_USERINFO_RE.sub(f"://{_REDACTED}@", text)
    return _ERROR_URL_RE.sub(_redact_url, text)
