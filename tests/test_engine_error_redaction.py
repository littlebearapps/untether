"""#1151: engine error text on a Telegram card gets the URL query / userinfo
redaction (``utils.error_display.redact_url_secrets``).

#1033 fixed the shared stderr sanitiser; an engine's own *error event*
(OpenCode ``error``, Codex ``turn.failed`` / API-error JSON, a Claude error
``result``, Pi ``errorMessage``) still reached the card with a URL's query
string in clear. The bridge's final-delivery path is the choke point, so each
runner's real translation is driven here and its ``CompletedEvent`` rendered
through ``handle_message``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.telegram_fakes import FakeTransport
from untether.markdown import MarkdownPresenter
from untether.model import ActionEvent, CompletedEvent, ResumeToken
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.antigravity import _error_text as agy_error_text
from untether.runners.claude import ClaudeStreamState, translate_claude_event
from untether.runners.codex import CodexRunner
from untether.runners.mock import ErrorReturn, ScriptRunner
from untether.runners.opencode import OpenCodeStreamState, translate_opencode_event
from untether.runners.pi import PiStreamState, translate_pi_event
from untether.schemas import claude as claude_schema
from untether.schemas import codex as codex_schema
from untether.schemas import opencode as opencode_schema
from untether.schemas import pi as pi_schema
from untether.utils.error_display import redact_url_secrets

# The #1151 repro: a per-chat ``/model set`` to a URL the engine echoes back.
SECRET_URL = "https://user:hunter2pw@example.invalid/cb?token=T0KSECRET&code=C0DESECRET"
LEAKS = ("T0KSECRET", "C0DESECRET", "hunter2pw", "user:")
REDACTED_URL = "https://[REDACTED]@example.invalid/cb?token=[REDACTED]&code=[REDACTED]"


# ── the redactor ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Model not found: qa8/https://example.invalid/cb?token=T&code=C.",
            "Model not found: qa8/https://example.invalid/cb"
            "?token=[REDACTED]&code=[REDACTED].",
        ),
        (
            "The 'https://example.invalid/cb?token=T&code=C' model is not supported",
            "The 'https://example.invalid/cb?token=[REDACTED]&code=[REDACTED]' "
            "model is not supported",
        ),
        (
            "connect to wss://bob:pw@relay.example/ws failed",
            "connect to wss://[REDACTED]@relay.example/ws failed",
        ),
        (
            "HTTPS://host.example/cb#access_token=abc&state=xyz",
            "HTTPS://host.example/cb#access_token=[REDACTED]&state=[REDACTED]",
        ),
        (
            '{"url":"https://h.example/x?sig=abc\\u0026exp=1"}',
            '{"url":"https://h.example/x?sig=[REDACTED]\\u0026exp=[REDACTED]"}',
        ),
    ],
)
def test_redacts_query_fragment_and_userinfo(text: str, expected: str) -> None:
    assert redact_url_secrets(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Rate limit exceeded. Try again in 20s.",
        "See https://docs.example.com/errors/rate-limits for details.",
        'Error: Model "[url]" not found. Use --list-models',
        "failed reading [path]/config.toml: key=value is not valid",
        "https://example.invalid/page#section-2",
        "https://example.invalid/search?flag",
        "",
    ],
)
def test_ordinary_text_unchanged(text: str) -> None:
    assert redact_url_secrets(text) == text


def test_idempotent() -> None:
    once = redact_url_secrets(f"see {SECRET_URL}.")
    assert once == f"see {REDACTED_URL}."
    assert redact_url_secrets(once) == once


# ── per-runner error event → rendered card ──────────────────────────────────


async def _card_texts(completed: CompletedEvent, engine: str) -> list[str]:
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            ErrorReturn(
                error=completed.error or "",
                answer=completed.answer,
                usage={"num_turns": 1},
            )
        ],
        engine=engine,
        resume_value="sess-1151",
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="reply OK"),
        resume_token=None,
    )
    calls = [*transport.send_calls, *transport.edit_calls]
    assert calls
    return [call["message"].text for call in calls]


def _assert_redacted(texts: list[str], expected_fragment: str) -> None:
    joined = "\n".join(texts)
    for leak in LEAKS:
        assert leak not in joined, leak
    assert expected_fragment in joined


def _only_completed(events: list[Any]) -> CompletedEvent:
    completed = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(completed) == 1
    assert completed[0].ok is False
    return completed[0]


@pytest.mark.anyio
async def test_opencode_error_event_card_redacted() -> None:
    state = OpenCodeStreamState()
    state.session_id = "ses_1151"
    state.emitted_started = True
    event = opencode_schema.decode_event(
        json.dumps(
            {
                "type": "error",
                "sessionID": "ses_1151",
                "error": {
                    "name": "ProviderModelNotFoundError",
                    "data": {"message": f"Model not found: qa8/{SECRET_URL}."},
                },
            }
        ).encode()
    )
    completed = _only_completed(
        translate_opencode_event(event, title="opencode", state=state)
    )
    # The answer is the error text itself (no assistant text yet).
    assert SECRET_URL in completed.answer
    texts = await _card_texts(completed, "opencode")
    _assert_redacted(texts, f"Model not found: qa8/{REDACTED_URL}.")


@pytest.mark.anyio
async def test_codex_turn_failed_card_redacted() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": f"The '{SECRET_URL}' model is not supported.",
            },
            "status": 400,
        },
        indent=2,
    )
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    events: list[Any] = []
    for raw in (
        {"type": "error", "message": body},
        {"type": "turn.failed", "error": {"message": body}},
    ):
        events.extend(
            runner.translate(
                codex_schema.decode_event(json.dumps(raw)),
                state=state,
                resume=None,
                found_session=None,
            )
        )
    # The engine's ``error`` event becomes a warning row on the progress card.
    notes = [e for e in events if isinstance(e, ActionEvent)]
    assert notes
    for note in notes:
        for leak in LEAKS:
            assert leak not in note.action.title
            assert leak not in (note.message or "")
    completed = _only_completed(events)
    texts = await _card_texts(completed, "codex")
    _assert_redacted(texts, f"The '{REDACTED_URL}' model is not supported.")


@pytest.mark.anyio
async def test_claude_error_result_card_redacted() -> None:
    state = ClaudeStreamState()
    event = claude_schema.decode_stream_json_line(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "uuid": "u",
                "session_id": "sess-1151",
                "duration_ms": 60,
                "duration_api_ms": 10,
                "num_turns": 1,
                "result": f"API Error: 404 model not found: {SECRET_URL}",
                "total_cost_usd": 0.0,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        ).encode()
    )
    completed = _only_completed(
        translate_claude_event(
            event, title="claude", state=state, factory=state.factory
        )
    )
    texts = await _card_texts(completed, "claude")
    _assert_redacted(texts, f"API Error: 404 model not found: {REDACTED_URL}")


@pytest.mark.anyio
async def test_pi_error_message_card_redacted() -> None:
    message = {
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorMessage": f"Upstream error calling {SECRET_URL}",
        "timestamp": 1,
    }
    state = PiStreamState(resume=ResumeToken(engine="pi", value="session.jsonl"))
    events: list[Any] = []
    for raw in (
        {"type": "agent_start"},
        {"type": "message_end", "message": message},
        {"type": "agent_end", "messages": [message]},
    ):
        events.extend(
            translate_pi_event(
                pi_schema.decode_event(json.dumps(raw)),
                title="pi",
                meta=None,
                state=state,
            )
        )
    completed = _only_completed(events)
    texts = await _card_texts(completed, "pi")
    _assert_redacted(texts, f"Upstream error calling {REDACTED_URL}")


@pytest.mark.anyio
async def test_antigravity_error_not_double_redacted() -> None:
    """agy text is already whole-URL redacted (``[url]``); the bridge pass
    leaves that marker alone."""
    error = agy_error_text({"message": f"quota check failed: {SECRET_URL}"})
    assert error == "quota check failed: [url]"
    completed = CompletedEvent(engine="antigravity", ok=False, answer="", error=error)
    texts = await _card_texts(completed, "antigravity")
    _assert_redacted(texts, "quota check failed: [url]")
    assert "[REDACTED]" not in "\n".join(texts)


@pytest.mark.anyio
async def test_ordinary_error_card_unchanged() -> None:
    error = "Rate limit exceeded; see https://docs.example.com/limits?page=2"
    completed = CompletedEvent(engine="codex", ok=False, answer="", error=error)
    texts = await _card_texts(completed, "codex")
    # The query is redacted, everything else (host, path, prose) kept.
    assert any(
        "Rate limit exceeded; see https://docs.example.com/limits?page=[REDACTED]" in t
        for t in texts
    )
    plain = CompletedEvent(
        engine="opencode", ok=False, answer="", error="Rate limit exceeded"
    )
    texts = await _card_texts(plain, "opencode")
    assert any("Rate limit exceeded" in t for t in texts)
    assert "[REDACTED]" not in "\n".join(texts)


@pytest.mark.anyio
async def test_assistant_answer_kept_when_error_differs() -> None:
    """A failed run's own assistant text is not the error — only the error
    block is redacted."""
    answer = "Results: https://example.com/search?q=untether"
    completed = CompletedEvent(
        engine="codex",
        ok=False,
        answer=answer,
        error=f"stream failed at {SECRET_URL}",
    )
    texts = await _card_texts(completed, "codex")
    _assert_redacted(texts, f"stream failed at {REDACTED_URL}")
    assert any(answer in t for t in texts)
