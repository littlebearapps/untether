"""Tests for error hint matching."""

from __future__ import annotations

import json
from pathlib import Path

from untether.error_hints import _HINT_PATTERNS, get_error_hint

_FIXTURES = Path(__file__).parent / "fixtures"


def _hint_for(pattern: str) -> str:
    """The hint text registered for ``pattern`` (never a duplicated literal)."""
    return next(hint for pat, hint in _HINT_PATTERNS if pat == pattern)


def _generic_hint() -> str:
    return _hint_for("invalid_request_error")


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

    def test_openai_api_key(self):
        hint = get_error_hint("Error: OPENAI_API_KEY is not set")
        assert hint is not None
        assert "OPENAI_API_KEY" in hint

    def test_google_api_key(self):
        hint = get_error_hint("Error: GOOGLE_API_KEY is not set")
        assert hint is not None
        assert "Google API key" in hint

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

    def test_read_timeout(self):
        hint = get_error_hint("ReadTimeout")
        assert hint is not None
        assert "timed out" in hint.lower()

    def test_unknown_error_returns_none(self):
        assert get_error_hint("Something completely unexpected happened") is None

    def test_empty_string(self):
        assert get_error_hint("") is None

    def test_case_insensitive(self):
        hint = get_error_hint("RATE LIMIT exceeded")
        assert hint is not None

    def test_error_during_execution_resumed(self):
        msg = (
            "Claude Code run failed (error_during_execution)\n"
            "session: abcdef12 \N{MIDDLE DOT} resumed \N{MIDDLE DOT} turns: 0 \N{MIDDLE DOT} cost: $0.00"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "/new" in hint

    def test_error_during_execution_new_session(self):
        msg = (
            "Claude Code run failed (error_during_execution)\n"
            "session: abcdef12 \N{MIDDLE DOT} new \N{MIDDLE DOT} turns: 0"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "could not be loaded" in hint.lower()

    # --- Subscription / billing limits ---

    def test_out_of_extra_usage(self):
        msg = (
            "You're out of extra usage \N{MIDDLE DOT} resets 7am (UTC)\n"
            "session: 73fe83ea \N{MIDDLE DOT} resumed \N{MIDDLE DOT} turns: 61 "
            "\N{MIDDLE DOT} cost: $5.77 \N{MIDDLE DOT} api: 583473ms"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "subscription" in hint.lower()
        assert "session is saved" in hint.lower()

    def test_hit_your_limit(self):
        msg = (
            "You've hit your limit \N{MIDDLE DOT} resets 8am (UTC)\n"
            "session: d72e0aca \N{MIDDLE DOT} resumed \N{MIDDLE DOT} turns: 26"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "subscription" in hint.lower()
        assert "session is saved" in hint.lower()

    def test_insufficient_quota(self):
        hint = get_error_hint(
            "Error: insufficient_quota - You exceeded your current quota"
        )
        assert hint is not None
        assert "openai" in hint.lower()

    def test_exceeded_current_quota(self):
        hint = get_error_hint(
            "You exceeded your current quota, please check your plan and billing details"
        )
        assert hint is not None
        assert "openai" in hint.lower()

    def test_billing_hard_limit(self):
        hint = get_error_hint("billing_hard_limit_reached")
        assert hint is not None
        assert "spend limit" in hint.lower()

    def test_resource_exhausted(self):
        hint = get_error_hint(
            "RESOURCE_EXHAUSTED: Quota exceeded for aiplatform.googleapis.com"
        )
        assert hint is not None
        assert "google" in hint.lower()

    # --- API overload / server errors ---

    def test_overloaded_error(self):
        hint = get_error_hint(
            "overloaded_error: Anthropic's API is temporarily overloaded"
        )
        assert hint is not None
        assert "overloaded" in hint.lower()
        assert "session is saved" in hint.lower()

    def test_server_is_overloaded(self):
        hint = get_error_hint("Error: The server is overloaded, please try again later")
        assert hint is not None
        assert "temporary" in hint.lower()

    def test_internal_server_error(self):
        hint = get_error_hint("internal_server_error: An unexpected error occurred")
        assert hint is not None
        assert "internal server error" in hint.lower()

    def test_bad_gateway(self):
        hint = get_error_hint("502 Bad Gateway")
        assert hint is not None
        assert "bad gateway" in hint.lower()

    def test_service_unavailable(self):
        hint = get_error_hint("503 Service Unavailable")
        assert hint is not None
        assert "unavailable" in hint.lower()

    def test_gateway_timeout(self):
        hint = get_error_hint("504 Gateway Timeout")
        assert hint is not None
        assert "gateway timed out" in hint.lower()

    # --- Rate limits (extended) ---

    def test_too_many_requests(self):
        hint = get_error_hint("429 Too Many Requests")
        assert hint is not None
        assert "retry" in hint.lower()

    # --- Network errors (extended) ---

    def test_connect_timeout(self):
        hint = get_error_hint(
            "ConnectTimeout: timed out connecting to api.anthropic.com"
        )
        assert hint is not None
        assert "connection timed out" in hint.lower()

    def test_dns_failure(self):
        hint = get_error_hint("Name or service not known")
        assert hint is not None
        assert "dns" in hint.lower()

    def test_network_unreachable(self):
        hint = get_error_hint("Network is unreachable")
        assert hint is not None
        assert "internet" in hint.lower()

    # --- Signal errors ---

    def test_sigterm(self):
        hint = get_error_hint("gemini failed (rc=-15 (SIGTERM)).")
        assert hint is not None
        assert "restarted" in hint.lower()
        assert "session is saved" in hint.lower()

    def test_sigkill(self):
        hint = get_error_hint("claude failed (rc=-9 (SIGKILL)).")
        assert hint is not None
        assert "forcefully terminated" in hint.lower()
        assert "session is saved" in hint.lower()

    def test_sigabrt(self):
        hint = get_error_hint("codex exec failed (rc=-6 (SIGABRT)).")
        assert hint is not None
        assert "/new" in hint

    def test_signal_hints_are_engine_agnostic(self):
        """Signal hints should not hardcode a specific engine command."""
        for sig in ("sigterm", "sigkill", "sigabrt"):
            hint = get_error_hint(sig)
            assert hint is not None
            assert "/claude" not in hint, f"{sig} hint should not hardcode /claude"

    # --- Process / session errors ---

    def test_finished_without_result(self):
        hint = get_error_hint("amp finished without a result event")
        assert hint is not None
        assert "exited before producing" in hint.lower()
        assert "session is saved" in hint.lower()

    def test_finished_without_result_cross_engine(self):
        """Pattern matches all engine names."""
        for engine in ("claude code", "codex", "opencode", "pi", "gemini", "amp"):
            msg = f"{engine} finished without a result event"
            hint = get_error_hint(msg)
            assert hint is not None, f"no hint for: {msg}"

    def test_no_session_id(self):
        hint = get_error_hint("opencode finished but no session_id was captured")
        assert hint is not None
        assert "crashed during startup" in hint.lower()

    def test_no_session_id_cross_engine(self):
        """Pattern matches all engine names."""
        for engine in ("claude code", "codex", "gemini", "amp", "opencode"):
            msg = f"{engine} finished but no session_id was captured"
            hint = get_error_hint(msg)
            assert hint is not None, f"no hint for: {msg}"

    # --- Ordering: specific patterns match before generic ones ---

    def test_overloaded_does_not_match_rate_limit(self):
        """overloaded_error should get the overload hint, not rate limit."""
        hint = get_error_hint("overloaded_error")
        assert hint is not None
        assert "overloaded" in hint.lower()
        assert "retry automatically" not in hint.lower()

    def test_subscription_limit_does_not_match_rate_limit(self):
        """'hit your limit' should get subscription hint, not rate limit."""
        hint = get_error_hint("You've hit your limit \N{MIDDLE DOT} resets 10am (UTC)")
        assert hint is not None
        assert "subscription" in hint.lower()

    def test_insufficient_quota_matches_before_exceeded(self):
        """Both patterns present — insufficient_quota should match first."""
        hint = get_error_hint("insufficient_quota: You exceeded your current quota")
        assert hint is not None
        # Both point to OpenAI, so either match is correct
        assert "openai" in hint.lower()


class TestDeprecatedEngineEndOfLife:
    """Both dead engines print a terminal error and exit 1. The hints must be
    ordered ahead of the generic `invalid_request_error` pattern, which AMP's 426
    payload matches — see test_amp_426_outranks_generic_invalid_request_error."""

    def test_gemini_ineligible_tier(self):
        msg = (
            "Error authenticating: IneligibleTierError: This client is no longer"
            " supported for Gemini Code Assist for individuals. To continue using"
            " Gemini, please migrate to the Antigravity suite of products:"
            " https://antigravity.google"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "end-of-life" in hint
        assert "antigravity" in hint.lower()

    def test_gemini_reason_message_variant(self):
        msg = (
            "reasonMessage: 'This client is no longer supported for Gemini Code"
            " Assist for individuals.'"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "antigravity" in hint.lower()

    def test_amp_stale_client_426(self):
        msg = (
            'Error: 426 {"type":"error","error":{"type":"invalid_request_error",'
            '"message":"This version of Amp is no longer supported. Run `amp'
            ' update` to continue."}}'
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "amp update" in hint

    def test_amp_426_outranks_generic_invalid_request_error(self):
        """The 426 payload also contains "invalid_request_error"; the specific
        AMP hint must win because it is ordered first."""
        msg = (
            'Error: 426 {"error":{"type":"invalid_request_error","message":"This'
            ' version of Amp is no longer supported."}}'
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "amp update" in hint
        # #416: compare against the registered generic text so the assertion
        # can't go vacuous when that wording changes.
        assert hint != _generic_hint()

    def test_generic_unsupported_client_fallback(self):
        """An unrecognised vendor wording still gets an actionable hint."""
        hint = get_error_hint(
            "fatal: this build is no longer supported by the provider"
        )
        assert hint is not None
        assert "no longer supported by its provider" in hint

    def test_unrelated_message_still_unmatched(self):
        assert get_error_hint("everything is fine, supported and happy") is None


class TestCliArgvDrift:
    """#830: a CLI flag removal (Codex `-a untrusted`, 0.149.0) must get an
    actionable hint, and Codex's retired-config-key error must not be read as a
    client end-of-life."""

    def test_clap_invalid_value_hint(self):
        msg = (
            "codex exec failed (rc=2).\n"
            "error: invalid value 'untrusted' for '--ask-for-approval"
            " <APPROVAL_POLICY>'\n  [possible values: on-request, never]"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "rejected a command-line flag" in hint
        assert "Update Untether" in hint

    def test_clap_unexpected_argument_hint(self):
        hint = get_error_hint("error: unexpected argument '--full-auto' found")
        assert hint is not None
        assert "Update Untether" in hint

    def test_clap_value_required_hint(self):
        hint = get_error_hint(
            "error: a value is required for '--sandbox <SANDBOX_MODE>' but none"
            " was supplied"
        )
        assert hint is not None
        assert "rejected a command-line flag" in hint

    def test_codex_config_key_no_longer_supported_hint_outranks_eol(self):
        msg = (
            'Error loading config.toml: approval_policy = "untrusted" is no longer'
            " supported; remove this setting"
        )
        hint = get_error_hint(msg)
        assert hint is not None
        assert "~/.codex/config.toml" in hint
        assert "remove the key" in hint
        assert "no longer supported by its provider" not in hint

    def test_argv_hint_pattern_ignores_prose(self):
        for msg in (
            "the model asked for '--verbose' output",
            "Error: unexpected argument 'foo' in tool call payload",
        ):
            hint = get_error_hint(msg)
            assert hint is None or "command-line flag" not in hint, msg

    def test_openai_invalid_value_prose_not_argv_hint(self):
        msg = (
            "Invalid value: 'minimal'. Supported values are: 'low', 'medium', and"
            " 'high'."
        )
        hint = get_error_hint(msg)
        assert hint is None or "command-line flag" not in hint


class TestReasoningEffortHints:
    """#416: a Codex ``reasoning.effort`` 400 gets a specific hint ahead of
    the generic ``invalid_request_error`` one, which no longer leads with
    "update the CLI"."""

    ISSUE_BODY = (
        '{"type":"error","error":{"type":"invalid_request_error","message":'
        "\"The following tools cannot be used with reasoning.effort 'minimal':"
        ' web_search.","param":"tools"},"status":400}'
    )
    UNSUPPORTED_VALUE = (
        '{"error":{"message":"Unsupported value: \'reasoning.effort\' does not'
        " support 'minimal' with this model. Supported values are: 'low',"
        ' \'medium\', and \'high\'.","type":"invalid_request_error","param":'
        '"reasoning.effort","code":"unsupported_value"}}'
    )

    @property
    def specific(self) -> str:
        return _hint_for("cannot be used with reasoning.effort")

    @property
    def broad(self) -> str:
        return _hint_for("reasoning.effort")

    def test_minimal_web_search_issue_body_verbatim(self):
        hint = get_error_hint(self.ISSUE_BODY)
        assert hint is not None
        assert "web search" in hint
        assert "/config" in hint
        assert "update" not in hint.lower()

    def test_minimal_web_search_outranks_generic_invalid_request_error(self):
        assert get_error_hint(self.ISSUE_BODY) == self.specific

    def test_minimal_web_search_plain_turn_failed_message(self):
        msg = (
            "The following tools cannot be used with reasoning.effort 'minimal':"
            " web_search."
        )
        assert get_error_hint(msg) == self.specific

    def test_minimal_web_search_with_details_suffix(self):
        msg = (
            "The following tools cannot be used with reasoning.effort 'minimal':"
            " web_search. (status 400 Bad Request)"
        )
        assert get_error_hint(msg) == self.specific

    def test_unsupported_effort_value_wording(self):
        hint = get_error_hint(self.UNSUPPORTED_VALUE)
        assert hint == self.broad
        assert hint != _generic_hint()

    def test_reasoning_effort_case_insensitive(self):
        assert (
            get_error_hint("tools CANNOT BE USED WITH REASONING.EFFORT 'minimal'")
            == self.specific
        )
        assert get_error_hint("bad param REASONING.EFFORT") == self.broad

    def test_reasoning_hints_make_no_session_saved_claim(self):
        # A resumed run failing with 0 turns auto-clears the session.
        for hint in (self.specific, self.broad):
            assert "saved" not in hint.lower()

    def test_reasoning_hint_does_not_match_prose(self):
        assert (
            get_error_hint("the model spent more reasoning effort than expected")
            is None
        )

    def test_claude_effort_flag_error_not_matched(self):
        hint = get_error_hint(
            "error: option '--effort <level>' argument 'bogus' is invalid"
        )
        assert hint not in (self.specific, self.broad)

    def test_generic_invalid_request_softened(self):
        hint = get_error_hint(
            '{"type":"invalid_request_error","message":"messages: text content'
            ' blocks must be non-empty"}'
        )
        assert hint == _generic_hint()
        assert "/config" in hint
        assert not hint.startswith("Invalid API request. Try updating")
        assert "update the engine CLI" in hint
        # The CLI update is the last resort, not the lead.
        assert hint.index("/config") < hint.index("update the engine CLI")

    def test_reasoning_hints_are_engine_neutral(self):
        for hint in (self.specific, self.broad):
            assert "/config" in hint
            assert "if this engine offers it" in hint
            assert "~/.codex/config.toml" in hint
            assert "model_reasoning_effort" in hint
            assert "clear the override" not in hint.lower()
            assert "update" not in hint.lower()

    def test_opencode_openai_reasoning_effort_error_gets_neutral_hint(self):
        hint = get_error_hint(
            "AI_APICallError: Unsupported value: 'reasoning.effort' does not"
            " support 'minimal' with this model."
        )
        # Same string as for Codex: no engine-specific branch, and /config is
        # only named conditionally (OpenCode has no Reasoning page).
        assert hint == self.broad
        assert "if this engine offers it" in hint

    def test_codex_minimal_turn_failed_fixture_gets_specific_hint(self):
        """Verbatim codex-cli 0.157.1 capture (U4, 2026-09-30, gpt-5.5): with
        web search on, the tools 400; with it off, the effort is still
        rejected as an unsupported value."""
        from untether.model import CompletedEvent
        from untether.runners.codex import CodexRunner
        from untether.schemas import codex as codex_schema

        runner = CodexRunner(codex_cmd="codex", extra_args=[])
        lines = (
            (_FIXTURES / "codex_turn_failed_minimal_reasoning.jsonl")
            .read_text()
            .splitlines()
        )
        completed: list[CompletedEvent] = []
        for line in lines:
            if not line.strip():
                continue
            state = runner.new_state("hi", None)
            events = runner.translate(
                codex_schema.decode_event(line),
                state=state,
                resume=None,
                found_session=None,
            )
            completed.extend(e for e in events if isinstance(e, CompletedEvent))
        assert len(completed) == 2
        with_search, without_search = completed
        assert with_search.ok is False and with_search.error
        assert get_error_hint(with_search.error) == self.specific
        assert "unsupported_value" in json.dumps(without_search.error)
        assert get_error_hint(without_search.error) == self.broad
