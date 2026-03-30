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
    (
        "authentication_error",
        "API key is invalid or expired."
        " Check your API key configuration and try again.",
    ),
    (
        "invalid_api_key",
        "API key is invalid or expired."
        " Check your API key configuration and try again.",
    ),
    (
        "api_key_invalid",
        "API key is invalid or expired."
        " Check your API key configuration and try again.",
    ),
    (
        "invalid x-api-key",
        "API key is invalid or expired."
        " Check your API key configuration and try again.",
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
    # --- Model errors ---
    (
        "model_not_found",
        "Model not available. Check the model name in /config"
        " \N{EM DASH} it may not be available for your account or region.",
    ),
    (
        "invalid_model",
        "Model not available. Check the model name in /config"
        " \N{EM DASH} it may not be available for your account or region.",
    ),
    (
        "model not available",
        "Model not available. Check the model name in /config"
        " \N{EM DASH} it may not be available for your account or region.",
    ),
    (
        "does not exist",
        "The requested resource was not found."
        " Check your model or configuration, then try again.",
    ),
    # --- Context length ---
    (
        "context_length_exceeded",
        "Session context is too long. Start a fresh session with /new.",
    ),
    (
        "max_tokens",
        "Token limit exceeded. Start a fresh session with /new.",
    ),
    (
        "context window",
        "Session context is too long. Start a fresh session with /new.",
    ),
    (
        "too many tokens",
        "Token limit exceeded. Start a fresh session with /new.",
    ),
    # --- Content safety ---
    (
        "content_filter",
        "Request blocked by content safety filter. Try rephrasing your prompt.",
    ),
    (
        "harm_category",
        "Request blocked by content safety filter. Try rephrasing your prompt.",
    ),
    (
        "prompt_blocked",
        "Request blocked by content safety filter. Try rephrasing your prompt.",
    ),
    (
        "safety_block",
        "Request blocked by content safety filter. Try rephrasing your prompt.",
    ),
    # --- Invalid request ---
    (
        "invalid_request_error",
        "Invalid API request. Try updating the engine CLI to the latest version.",
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
    (
        "certificate verify failed",
        "SSL certificate verification failed."
        " Check your network, proxy, or certificate configuration.",
    ),
    (
        "ssl handshake",
        "SSL/TLS handshake failed."
        " Check your network, proxy, or certificate configuration.",
    ),
    # --- CLI / filesystem errors ---
    (
        "command not found",
        "Engine CLI not found. Check that it is installed and in your PATH.",
    ),
    (
        "enoent",
        "Engine CLI not found. Check that it is installed and in your PATH.",
    ),
    (
        "no space left",
        "Disk full \N{EM DASH} free up space and try again.",
    ),
    (
        "permission denied",
        "Permission denied \N{EM DASH} check file and directory permissions.",
    ),
    (
        "read-only file system",
        "File system is read-only \N{EM DASH} check mount and permissions.",
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
        "The session could not be loaded \N{EM DASH} Claude Code may have"
        " archived or expired it. Send /new to start a fresh session.",
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
    # --- Engine-specific errors ---
    (
        "require paid credits",
        "AMP execute mode requires paid credits."
        " Add credits at ampcode.com/pay, then try again.",
    ),
    (
        "amp login",
        "Run `amp login` to authenticate with Sourcegraph.",
    ),
    (
        "gemini result status:",
        "Gemini returned an unexpected result. Try a fresh session with /new.",
    ),
    # --- Account errors ---
    (
        "account_suspended",
        "Your account has been suspended. Check your provider's dashboard for details.",
    ),
    (
        "account_disabled",
        "Your account has been disabled. Check your provider's dashboard for details.",
    ),
    # --- Proxy / timeout errors ---
    (
        "407 proxy",
        "Proxy authentication required. Check your proxy configuration.",
    ),
    (
        "deadline exceeded",
        "Request timed out \N{EM DASH} this is usually transient. Try again.",
    ),
    (
        "timeout exceeded",
        "Request timed out \N{EM DASH} this is usually transient. Try again.",
    ),
    # --- Generic exit code errors (signal deaths not caught above) ---
    (
        "rc=137",
        "The process was forcefully terminated (out of memory)."
        " Your session is saved \N{EM DASH} try resuming by sending a new message.",
    ),
    (
        "rc=143",
        "The process was terminated by a signal (SIGTERM)."
        " Your session is saved \N{EM DASH} try resuming by sending a new message.",
    ),
    (
        "rc=-9",
        "The process was forcefully terminated (out of memory)."
        " Your session is saved \N{EM DASH} try resuming by sending a new message.",
    ),
    (
        "rc=-15",
        "The process was terminated by a signal (SIGTERM)."
        " Your session is saved \N{EM DASH} try resuming by sending a new message.",
    ),
]


def get_error_hint(error_message: str) -> str | None:
    """Return an actionable hint for a known error pattern, or None."""
    lower = error_message.lower()
    for pattern, hint in _HINT_PATTERNS:
        if pattern in lower:
            return hint
    return None
