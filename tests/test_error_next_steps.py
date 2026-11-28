"""#1196 slice A: every error hint names a next step; no internal names leak.

The table test is the guard for new hints: a hint that only describes the
problem ("the engine will retry automatically") fails it. A bare "retry"
doesn't count as a next step — it doesn't say who retries, or how.
"""

from __future__ import annotations

import re

import pytest

from tests.telegram_fakes import FakeTransport
from untether import error_hints as eh
from untether import runner_bridge as rb
from untether.markdown import MarkdownPresenter
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import ErrorReturn, Raise, ScriptRunner
from untether.utils.error_display import NO_CONFIG_PATH, error_reply

NEXT_STEP_RE = re.compile(
    r"/(?:new|config|usage|model|reasoning|cancel|auth|export)\b"
    r"|\bsend\b|\breply here\b|\btry again\b|\btry resuming\b|\bwait\b|\brun `"
    r"|\b(?:update|upgrade|switch|set|add|remove|report|rephras\w*|check"
    r"|free up|increase|raise|choose|pick|split|resume|sign in)\b",
    re.IGNORECASE,
)


def _all_hints() -> list[str]:
    hints = {hint for _, hint in eh._HINT_PATTERNS}
    hints |= set(eh.CODEX_ERROR_INFO_HINTS.values())
    hints |= {
        eh.SESSION_NOT_LOADED_HINT,
        eh.SESSION_NOT_FOUND_HINT,
        eh.FALLBACK_HINT,
        eh._needs_effort_hint("--model m requires --effort (available: low, high)"),
        eh._needs_effort_hint("--model m requires --effort"),
        eh._claude_logged_out_hint(),
        eh.codex_error_info_hint("usageLimitExceeded", 1_900_000_000) or "",
        rb._STALE_RESUME_HINT,
        rb._SESSION_NOT_LOADED_CLEARED_HINT,
        rb._SESSION_NOT_FOUND_CLEARED_HINT,
        rb._FINAL_UNCONFIRMED_NOTICE,
        rb._FINAL_FAILED_NOTICE,
        rb._RAM_HOLD_TEXT,
        rb._AUTO_CANCEL_NOTICE,
        rb._STUCK_AFTER_TOOL_RESULT_NOTICE,
    }
    return sorted(hints)


@pytest.mark.parametrize("hint", _all_hints())
def test_every_hint_names_a_next_step(hint: str) -> None:
    assert NEXT_STEP_RE.search(hint), hint


def test_bare_retry_is_not_a_next_step() -> None:
    assert not NEXT_STEP_RE.search(
        "Rate limited \N{EM DASH} the engine will retry automatically."
    )


# ── error_hints fixes ────────────────────────────────────────────────────────


def test_session_not_found_points_to_new() -> None:
    hint = eh.get_error_hint("Session not found for the given ID")
    assert hint == eh.SESSION_NOT_FOUND_HINT
    assert "/new" in hint
    assert "--session" not in hint


@pytest.mark.parametrize("text", ["429 rate limit exceeded", "Too Many Requests"])
def test_rate_limit_says_wait_then_send_again(text: str) -> None:
    hint = eh.get_error_hint(text)
    assert hint is not None
    assert "automatically" not in hint
    assert "wait" in hint.lower()
    assert "send your message again" in hint


def test_connection_refused_names_an_action() -> None:
    hint = eh.get_error_hint("Connection refused on port 8080")
    assert hint is not None
    assert "running" in hint.lower()
    assert "send your message again" in hint


def test_not_signed_in_says_how_to_sign_in() -> None:
    from untether.runners.antigravity import AUTH_TEXT

    hint = eh.get_error_hint(AUTH_TEXT)
    assert hint is not None
    assert "`agy`" in hint


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Claude Code run failed (error_max_turns)", "max_turns"),
        ("Claude Code run failed (error_max_budget_usd)", "max_budget_usd"),
    ],
)
def test_claude_limit_subtypes_have_hints(text: str, key: str) -> None:
    hint = eh.get_error_hint(text)
    assert hint is not None
    assert key in hint
    assert NEXT_STEP_RE.search(hint)


# ── internal names ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_format_error_never_shows_the_class_name() -> None:
    assert "RuntimeError" not in rb._format_error(RuntimeError())
    assert "ValueError" not in rb._format_error(ExceptionGroup("", [ValueError()]))


@pytest.mark.parametrize(
    "notice", [rb._AUTO_CANCEL_NOTICE, rb._STUCK_AFTER_TOOL_RESULT_NOTICE]
)
def test_auto_cancel_notices_hide_reason_codes(notice: str) -> None:
    assert notice.startswith("\N{WARNING SIGN}")
    assert "Auto-cancelled" in notice
    for internal in ("max_warnings", "tool_result", "#322", "untether#", "("):
        assert internal not in notice


def test_codex_quota_error_hides_the_class_name() -> None:
    from untether.telegram.commands.usage import _codex_error_line

    line = _codex_error_line(ValueError("bad frame"))
    assert "ValueError" not in line
    assert "bad frame" not in line
    assert "/usage" in line


def test_error_reply_sanitises_and_names_the_next_step() -> None:
    text = error_reply(
        RuntimeError("failed at /home/someone/project/x: api_key=sk-abc123"),
        "Check the project, then send your message again.",
    )
    assert text.startswith("\N{CROSS MARK} ")
    assert "/home/someone" not in text
    assert "sk-abc123" not in text
    assert text.endswith("Check the project, then send your message again.")


def test_no_config_path_names_the_file_and_the_fix() -> None:
    assert "untether.toml" in NO_CONFIG_PATH
    assert NEXT_STEP_RE.search(NO_CONFIG_PATH) or "Restart" in NO_CONFIG_PATH


# ── the bridge's cards ───────────────────────────────────────────────────────


async def _card_text(script: list[object]) -> str:
    transport = FakeTransport()
    runner = ScriptRunner(script, engine="codex", resume_value="sess-1196")
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="hi"),
        resume_token=None,
    )
    calls = [*transport.send_calls, *transport.edit_calls]
    assert calls
    return "\n".join(call["message"].text for call in calls)


@pytest.mark.anyio
async def test_final_card_without_a_hint_gets_the_fallback() -> None:
    text = await _card_text(
        [ErrorReturn(error="engine exploded", usage={"num_turns": 1})]
    )
    assert "engine exploded" in text
    assert eh.FALLBACK_HINT in text


@pytest.mark.anyio
async def test_final_card_with_a_hint_has_no_fallback() -> None:
    text = await _card_text(
        [ErrorReturn(error="429 rate limit exceeded", usage={"num_turns": 1})]
    )
    assert eh.FALLBACK_HINT not in text


@pytest.mark.anyio
async def test_runner_exception_card_gets_the_fallback() -> None:
    text = await _card_text([Raise(RuntimeError())])
    assert eh.FALLBACK_HINT in text
    assert "RuntimeError" not in text


@pytest.mark.anyio
async def test_final_card_masks_credential_shaped_tokens() -> None:
    text = await _card_text(
        [
            ErrorReturn(
                error=(
                    "request failed: Authorization: Bearer abcdef0123456789xyz"
                    " key sk-ant-api03-SECRETSECRETSECRET"
                ),
                usage={"num_turns": 1},
            )
        ]
    )
    assert "request failed" in text
    assert "abcdef0123456789xyz" not in text
    assert "SECRETSECRETSECRET" not in text
