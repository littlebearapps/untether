"""Tests for error hint matching."""

from __future__ import annotations

from untether.error_hints import get_error_hint


class TestGetErrorHint:
    def test_codex_token_refresh(self):
        msg = "Your access token could not be refreshed because your refresh token was already used."
        hint = get_error_hint(msg)
        assert hint is not None
        assert "codex login" in hint

    def test_codex_sign_in_again(self):
        hint = get_error_hint("Please log out and sign in again.")
        assert hint is not None
        assert "codex login" in hint

    def test_anthropic_api_key(self):
        hint = get_error_hint("Error: ANTHROPIC_API_KEY is not set")
        assert hint is not None
        assert "ANTHROPIC_API_KEY" in hint

    def test_rate_limit(self):
        hint = get_error_hint("429 rate limit exceeded")
        assert hint is not None
        assert "retry" in hint.lower()

    def test_session_not_found(self):
        hint = get_error_hint("Session not found for the given ID")
        assert hint is not None
        assert "--session" in hint

    def test_connection_refused(self):
        hint = get_error_hint("Connection refused on port 8080")
        assert hint is not None
        assert "running" in hint.lower()

    def test_unknown_error_returns_none(self):
        assert get_error_hint("Something completely unexpected happened") is None

    def test_empty_string(self):
        assert get_error_hint("") is None

    def test_case_insensitive(self):
        hint = get_error_hint("RATE LIMIT exceeded")
        assert hint is not None
