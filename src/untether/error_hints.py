"""Actionable hints for common engine error messages."""

from __future__ import annotations

# (pattern_substring, hint_text) — first match wins.
# Order: auth → subscription/billing → overload/server → rate limits
# → session → network → signals → execution.
_HINT_PATTERNS: list[tuple[str, str]] = [
    # --- Authentication ---
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
        "openai_api_key",
        "Check that OPENAI_API_KEY is set in your environment.",
    ),
    (
        "google_api_key",
        "Check that your Google API key is set in your environment.",
    ),
    # --- Subscription / billing limits ---
    (
        "out of extra usage",
        "Subscription usage limit reached. Your session is saved"
        " \N{EM DASH} wait for the reset window shown above, then resume.",
    ),
    (
        "hit your limit",
        "Subscription usage limit reached. Your session is saved"
        " \N{EM DASH} wait for the reset window shown above, then resume.",
    ),
    (
        "insufficient_quota",
        "OpenAI billing quota exceeded. Check your billing dashboard"
        " at platform.openai.com and add credits, then resume.",
    ),
    (
        "exceeded your current quota",
        "OpenAI billing quota exceeded. Check your billing dashboard"
        " at platform.openai.com and add credits, then resume.",
    ),
    (
        "billing_hard_limit_reached",
        "OpenAI billing hard limit reached. Increase your spend limit"
        " at platform.openai.com, then resume.",
    ),
    (
        "resource_exhausted",
        "Google API quota exhausted. Check your quota at"
        " console.cloud.google.com, then resume.",
    ),
    # --- API overload / server errors ---
    (
        "overloaded_error",
        "Anthropic API is overloaded. This is temporary"
        " \N{EM DASH} your session is saved. Try again in a few minutes.",
    ),
    (
        "server is overloaded",
        "The API server is overloaded. This is temporary"
        " \N{EM DASH} your session is saved. Try again in a few minutes.",
    ),
    (
        "internal_server_error",
        "The API returned an internal server error. This is usually temporary"
        " \N{EM DASH} your session is saved. Try again shortly.",
    ),
    (
        "bad gateway",
        "The API returned a bad gateway error (502). This is usually temporary"
        " \N{EM DASH} your session is saved. Try again shortly.",
    ),
    (
        "service unavailable",
        "The API is temporarily unavailable (503). Your session is saved"
        " \N{EM DASH} try again in a few minutes.",
    ),
    (
        "gateway timeout",
        "The API gateway timed out (504). This is usually temporary"
        " \N{EM DASH} your session is saved. Try again shortly.",
    ),
    # --- Rate limits ---
    (
        "rate limit",
        "Rate limited \N{EM DASH} the engine will retry automatically.",
    ),
    (
        "too many requests",
        "Rate limited \N{EM DASH} the engine will retry automatically.",
    ),
    # --- Session errors ---
    (
        "session not found",
        "Try a fresh session without --session flag.",
    ),
    # --- Network / connection errors ---
    (
        "connection refused",
        "Check that the target service is running.",
    ),
    (
        "connecttimeout",
        "Connection timed out before reaching the API."
        " Check your network, then try again.",
    ),
    (
        "readtimeout",
        "Connection timed out \N{EM DASH} this is usually transient. Try again.",
    ),
    (
        "name or service not known",
        "DNS resolution failed \N{EM DASH} check your network connection.",
    ),
    (
        "network is unreachable",
        "Network is unreachable \N{EM DASH} check your internet connection.",
    ),
    # --- Signal errors ---
    (
        "sigterm",
        "Untether was restarted. Your session is saved"
        " \N{EM DASH} resume by sending a new message.",
    ),
    (
        "sigkill",
        "The process was forcefully terminated (timeout or out of memory)."
        " Your session is saved \N{EM DASH} try resuming by sending a new message.",
    ),
    (
        "sigabrt",
        "The process aborted unexpectedly. Try starting a fresh session with /new.",
    ),
    # --- Execution errors ---
    (
        "error_during_execution",
        "The session failed to load \N{EM DASH} it may have been"
        " corrupted during a restart. Send /new to start a fresh session.",
    ),
    # --- Process / session errors ---
    (
        "finished without a result event",
        "The engine exited before producing a final answer."
        " This can happen after a crash or timeout."
        " Your session is saved \N{EM DASH} try sending a new message to resume.",
    ),
    (
        "finished but no session_id",
        "The engine exited before establishing a session."
        " This usually means it crashed during startup."
        " Check that the engine CLI is installed and working, then try again.",
    ),
]


def get_error_hint(error_message: str) -> str | None:
    """Return an actionable hint for a known error pattern, or None."""
    lower = error_message.lower()
    for pattern, hint in _HINT_PATTERNS:
        if pattern in lower:
            return hint
    return None
