"""Actionable hints for common engine error messages."""

from __future__ import annotations

# (pattern_substring, hint_text) — first match wins
_HINT_PATTERNS: list[tuple[str, str]] = [
    (
        "access token could not be refreshed",
        "Run `codex login --device-auth` to re-authenticate.",
    ),
    (
        "log out and sign in again",
        "Run `codex login` to re-authenticate.",
    ),
    (
        "anthropic_api_key",
        "Check that ANTHROPIC_API_KEY is set in your environment.",
    ),
    (
        "rate limit",
        "Rate limited \N{EM DASH} the engine will retry automatically.",
    ),
    (
        "session not found",
        "Try a fresh session without --session flag.",
    ),
    (
        "connection refused",
        "Check that the target service is running.",
    ),
]


def get_error_hint(error_message: str) -> str | None:
    """Return an actionable hint for a known error pattern, or None."""
    lower = error_message.lower()
    for pattern, hint in _HINT_PATTERNS:
        if pattern in lower:
            return hint
    return None
