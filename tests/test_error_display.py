"""Tests for utils/error_display.py (C.1 of #195-#204 bundle).

Exercises user_safe_error() across the exception shapes encountered by
voice.py (#200), dispatch.py (#201), and auth.py (#199).
"""

from __future__ import annotations

from untether.utils.error_display import user_safe_error


def test_strips_absolute_paths() -> None:
    msg = "failed to open /home/nathan/secrets/token.txt"
    assert "/home/nathan" not in user_safe_error(msg, fallback="x")
    assert "[path]" in user_safe_error(msg, fallback="x")


def test_strips_urls() -> None:
    msg = "request to https://api.openai.com/v1/audio/transcriptions failed"
    assert "https://" not in user_safe_error(msg, fallback="x")
    assert "[url]" in user_safe_error(msg, fallback="x")


def test_strips_multiple_paths_and_urls() -> None:
    msg = (
        "AuthenticationError: POST https://api.openai.com/v1/chat "
        "couldn't read /home/user/.openai/key — check /etc/openai.conf"
    )
    out = user_safe_error(msg, fallback="x")
    assert "https://" not in out
    assert "/home/user" not in out
    assert "/etc/openai" not in out


def test_empty_message_returns_fallback() -> None:
    assert user_safe_error("", fallback="operation failed") == "operation failed"
    assert user_safe_error("   ", fallback="oops") == "oops"


def test_caps_length() -> None:
    long = "x" * 500
    out = user_safe_error(long, fallback="f", max_chars=50)
    assert len(out) == 50
    assert out.endswith("…")


def test_accepts_exception_instance() -> None:
    exc = RuntimeError("connection to https://example.com timed out")
    out = user_safe_error(exc, fallback="timeout")
    assert "https://" not in out
    assert "[url]" in out


def test_no_class_name_leak() -> None:
    """str(exc) is used — not repr(exc) — so class name doesn't appear."""

    class _InternalErrorName(Exception):
        pass

    out = user_safe_error(_InternalErrorName("something broke"), fallback="x")
    assert "_InternalErrorName" not in out
    assert "something broke" in out


def test_bare_openai_auth_error_scrubbed() -> None:
    """Realistic OpenAI-client exception shape."""
    msg = (
        "Error code: 401 - {'error': {'message': 'Incorrect API key provided: sk-XXX', "
        "'type': 'invalid_request_error', 'param': null, 'code': 'invalid_api_key'}}"
    )
    out = user_safe_error(msg, fallback="auth failed")
    # No URLs or paths in this message — should pass through trimmed.
    assert "Incorrect API key" in out
    assert len(out) <= 200


def test_strips_long_sk_secret() -> None:
    """sk- prefixed keys with realistic length should be redacted."""
    msg = "Auth failed for sk-proj-abcdef0123456789ZYXWvutsrqpo"
    out = user_safe_error(msg, fallback="x")
    assert "sk-proj-abcdef" not in out
    assert "[secret]" in out


def test_strips_bearer_token() -> None:
    msg = "request rejected: Authorization: Bearer ey0.aBcDeFgHiJkLmNoP"
    out = user_safe_error(msg, fallback="x")
    assert "ey0.aBcDeFgHiJkLmNoP" not in out
    assert "[secret]" in out


def test_strips_inline_token_assignment() -> None:
    msg = "config error: api_key=ABCDEF123456 is invalid"
    out = user_safe_error(msg, fallback="x")
    assert "ABCDEF123456" not in out
    assert "[secret]" in out
