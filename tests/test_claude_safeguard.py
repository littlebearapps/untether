"""#814: Claude safeguard stops surface as a progress row, a footer flag and a
``claude.safeguard_stop`` log — never as an error.

Shapes come from ``docs/findings/2026-09-29-claude-rc14-cli-surface.md`` §B:
the refused assistant frame (``stop_reason:"refusal"`` + ``stop_details``),
the ``system/informational`` "continuing once" notice (level notice), and the
undocumented ``model_refusal_fallback`` / ``model_refusal_no_fallback`` /
``model_fallback`` system subtypes. None of this is provoked live (D-16).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import msgspec
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport
from untether import runner_bridge as rb
from untether.markdown import MarkdownPresenter
from untether.model import (
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    TurnEvent,
)
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners import claude as claude_mod
from untether.runners.claude import (
    ClaudeRunner,
    ClaudeStreamState,
    translate_claude_event,
)
from untether.runners.mock import Emit, Return, ScriptRunner
from untether.schemas import claude as claude_schema
from untether.session_costs import SessionCostLedger, set_session_cost_ledger
from untether.session_quarantine import QuarantineStore, set_quarantine_store
from untether.settings import FooterSettings, ProgressSettings, WatchdogSettings

SHIELD = "\N{SHIELD}\N{VARIATION SELECTOR-16}"
NOTICE = (
    "Opus 5.5's safeguards stopped the response above · continuing once with that noted"
)
FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_claude_live.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _state() -> ClaudeStreamState:
    state = ClaudeStreamState()
    _translate(state, _init())  # binds the factory's resume token
    return state


def _translate(state: ClaudeStreamState, payload: dict[str, Any]) -> list[Any]:
    event = msgspec.json.decode(
        msgspec.json.encode(payload), type=claude_schema.StreamJsonMessage
    )
    return translate_claude_event(
        event, title="claude", state=state, factory=state.factory
    )


def _refusal(
    *,
    model: str = "claude-opus-5-5",
    category: str | None = "cyber",
    msg_id: str = "msg_refused",
    parent: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "partial"}],
            "stop_reason": "refusal",
            "stop_details": {
                "type": "refusal",
                "category": category,
                "explanation": "flagged",
            },
        },
    }


def _text(msg: str = "Here is the answer.", *, model: str = "claude-opus-5-5"):
    return {
        "type": "assistant",
        "message": {
            "id": f"msg_{msg[:8]}",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": msg}],
            "stop_reason": "end_turn",
        },
    }


def _informational(content: Any, level: Any = "notice", **extra: Any):
    return {
        "type": "system",
        "subtype": "informational",
        "content": content,
        "level": level,
        "uuid": "info-uuid",
        "session_id": "sess-814",
        **extra,
    }


def _result(answer: str = "Here is the answer.", *, is_error: bool = False):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "duration_ms": 1000,
        "duration_api_ms": 900,
        "num_turns": 2,
        "session_id": "sess-814",
        "result": answer,
        "total_cost_usd": 0.01,
    }


def _init() -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "sess-814",
        "model": "claude-opus-5-5",
        "cwd": "/tmp",
        "tools": [],
    }


def _notes(events: list[Any]) -> list[ActionEvent]:
    return [e for e in events if isinstance(e, ActionEvent) and e.action.kind == "note"]


def _stops(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [log for log in logs if log["event"] == "claude.safeguard_stop"]


def _completed(events: list[Any]) -> CompletedEvent:
    return next(e for e in events if isinstance(e, CompletedEvent))


# ---------------------------------------------------------------------------
# runner translation
# ---------------------------------------------------------------------------


def test_refusal_stop_reason_note_and_log() -> None:
    """The nsd transcript shape: refusal → notice → re-run → result ok."""
    state = _state()
    with capture_logs() as logs:
        refused = _translate(state, _refusal())
        (row,) = {e.action.title for e in _notes(refused)}
        assert row == f"{SHIELD} opus 5.5 safeguards stopped a response"
        # Outcome unknown until the CLI reacts: nothing logged yet.
        assert _stops(logs) == []
        noticed = _translate(state, _informational(NOTICE))
        _translate(state, _text())
        done = _translate(state, _result())
    (log,) = _stops(logs)
    assert log["source"] == "stop_reason"
    assert log["outcome"] == "retried"
    assert log["turn_count"] == 1
    assert log["session_count"] == 1
    assert log["category"] == "cyber"
    assert log["model"] == "claude-opus-5-5"
    assert log["session_id"] == "sess-814"
    notes = _notes(noticed)
    assert {e.action.id for e in notes} == {"claude.safeguard.1"}
    assert notes[-1].action.title == (
        f"{SHIELD} opus 5.5 safeguards stopped a response · retried once"
    )
    completed = _completed(done)
    assert completed.ok is True
    assert completed.usage is not None
    assert completed.usage["safeguard"] == {
        "stops": 1,
        "outcome": "retried",
        "outcome_label": "retried once",
        "category": "cyber",
        "model": "opus 5.5",
    }


def test_refusal_without_notice_resolves_at_result() -> None:
    """No CLI reaction before the result: later output ⇒ re-run; none ⇒ not
    retried. A subagent's refusal and a repeated frame of the same API
    message are not counted."""
    state = _state()
    with capture_logs() as logs:
        _translate(state, _refusal())
        _translate(state, _refusal())  # same message id
        assert _translate(state, _refusal(parent="toolu_sub", msg_id="m2")) == []
        _translate(state, _text())
        done = _translate(state, _result())
    (log,) = _stops(logs)
    assert (log["source"], log["outcome"], log["turn_count"]) == (
        "stop_reason",
        "retried",
        1,
    )
    assert _completed(done).usage["safeguard"]["stops"] == 1

    state = _state()
    with capture_logs() as logs:
        _translate(state, _refusal())
        done = _translate(state, _result(answer=""))
    assert _stops(logs)[0]["outcome"] == "not_retried"
    assert _completed(done).usage["safeguard"]["outcome"] == "not_retried"


def test_informational_notice_pairs_not_double_counted() -> None:
    state = _state()
    with capture_logs() as logs:
        _translate(state, _refusal())
        events = _translate(state, _informational(NOTICE))
        done = _translate(state, _result())
    assert "×" not in _notes(events)[-1].action.title
    assert len(_stops(logs)) == 1
    assert state.safeguard_session_count == 1
    assert _completed(done).usage["safeguard"]["stops"] == 1

    # A second refusal in the same turn is a second stop.
    state = _state()
    with capture_logs() as logs:
        for msg_id in ("m1", "m2"):
            _translate(state, _refusal(msg_id=msg_id))
            events = _translate(state, _informational(NOTICE))
        done = _translate(state, _result())
    assert _notes(events)[-1].action.title.endswith("retried once (×2)")
    assert [log["turn_count"] for log in _stops(logs)] == [1, 2]
    assert _completed(done).usage["safeguard"]["stops"] == 2


def test_informational_after_result_still_counted() -> None:
    """Ordering vs the result is unverified (§B3): a notice landing after
    the turn's result is still counted and logged, just not rendered."""
    # Notice only, after the result of a live session's first turn.
    state = _state()
    state.live_mode = True
    _translate(state, _init())
    _translate(state, _text())
    _translate(state, _result())
    assert state.turn_open is False
    with capture_logs() as logs:
        late = _translate(state, _informational(NOTICE))
    assert late == []
    (log,) = _stops(logs)
    assert (log["source"], log["outcome"], log["turn_count"]) == (
        "informational",
        "retried",
        1,
    )
    assert log["model"] == "Opus 5.5"
    assert state.safeguard_session_count == 1

    # Refusal before the result, notice after: still one stop.
    state = _state()
    state.live_mode = True
    _translate(state, _init())
    with capture_logs() as logs:
        _translate(state, _refusal())
        _translate(state, _text())
        done = _translate(state, _result())
        assert _translate(state, _informational(NOTICE)) == []
    assert len(_stops(logs)) == 1
    assert state.safeguard_session_count == 1
    assert _completed(done).usage["safeguard"]["stops"] == 1


def test_refusal_fallback_outcome_switched() -> None:
    state = _state()
    with capture_logs() as logs:
        _translate(state, _refusal())
        events = _translate(
            state,
            {
                "type": "system",
                "subtype": "model_refusal_fallback",
                "trigger": "refusal",
                "direction": "retry",
                "scope": "session",
                "original_model": "claude-opus-5-5",
                "fallback_model": "claude-opus-4-8",
                "request_id": "req_1",
                "api_refusal_category": "cyber",
                "api_refusal_explanation": "flagged",
                "content": "Switched to Opus 4.8",
                "session_id": "sess-814",
                "uuid": "u1",
            },
        )
        done = _translate(state, _result())
    assert _notes(events)[-1].action.title == (
        f"{SHIELD} opus 5.5 safeguards stopped a response · switched to opus 4.8"
    )
    (log,) = _stops(logs)
    assert log["outcome"] == "switched"
    assert log["fallback_model"] == "claude-opus-4-8"
    assert log["source"] == "stop_reason"
    safeguard = _completed(done).usage["safeguard"]
    assert safeguard["outcome_label"] == "switched to opus 4.8"
    assert safeguard["fallback_model"] == "opus 4.8"
    assert state.session_model == "claude-opus-4-8"


def test_refusal_no_fallback_outcome_not_retried() -> None:
    state = _state()
    with capture_logs() as logs:
        events = _translate(
            state,
            {
                "type": "system",
                "subtype": "model_refusal_no_fallback",
                "original_model": "claude-sonnet-5-5",
                "request_id": None,
                "api_refusal_category": None,
                "content": "stopped",
                "session_id": "sess-814",
                "uuid": "u2",
            },
        )
    notes = _notes(events)
    assert notes[-1].action.title == (
        f"{SHIELD} sonnet 5.5 safeguards stopped a response · not retried"
    )
    assert notes[-1].ok is True
    assert notes[-1].level == "warning"
    (log,) = _stops(logs)
    assert (log["source"], log["outcome"], log["category"]) == (
        "model_refusal_no_fallback",
        "not_retried",
        None,
    )


def test_model_fallback_switched_row_and_log() -> None:
    state = _state()
    with capture_logs() as logs:
        events = _translate(
            state,
            {
                "type": "system",
                "subtype": "model_fallback",
                "trigger": "overloaded",
                "original_model": "claude-opus-5-5",
                "fallback_model": "claude-sonnet-5-5",
                "content": "switched",
                "session_id": "sess-814",
                "uuid": "u3",
            },
        )
    assert _notes(events)[-1].action.title == (
        "↪️ Switched model opus 5.5 → sonnet 5.5 (overloaded)"
    )
    (log,) = [x for x in logs if x["event"] == "claude.model_fallback"]
    assert log["log_level"] == "info"
    assert log["trigger"] == "overloaded"
    # Not a safeguard stop.
    assert state.safeguard.stops == 0


def test_generic_informational_warning_row() -> None:
    state = _state()
    content = "UserPromptSubmit operation blocked by hook: " + "x" * 400
    events = _translate(
        state,
        _informational(content + "\nsecond line", "warning", prevent_continuation=True),
    )
    (row,) = {e.action.title for e in _notes(events)}
    assert row.startswith("⚠️ UserPromptSubmit operation blocked by hook: ")
    assert row.endswith("…")
    assert len(row) <= 200 + len("⚠️ ")
    assert "second line" not in row
    assert state.safeguard.stops == 0
    # notice level → the info icon; the same tool_use_id updates one row.
    ids = set()
    for n in (1, 2):
        ev = _translate(state, _informational(f"step {n}", tool_use_id="toolu_9"))
        assert _notes(ev)[-1].action.title == f"ℹ️ step {n}"
        ids |= {e.action.id for e in _notes(ev)}
    assert ids == {"claude.informational.toolu_9"}


def test_info_level_informational_is_log_only() -> None:
    state = _state()
    with capture_logs() as logs:
        for level in ("info", "suggestion", None, 3):
            assert _translate(state, _informational("fyi", level)) == []
        # Non-string content decodes (Any-typed) and renders nothing.
        assert _translate(state, _informational({"blocks": []}, "warning")) == []
    info = [x for x in logs if x["event"] == "claude.informational"]
    assert len(info) == 5
    assert {x["log_level"] for x in info} == {"debug"}


def test_title_uses_event_model_not_hardcoded() -> None:
    state = _state()
    events = _translate(state, _refusal(model="claude-sonnet-5-5"))
    title = _notes(events)[-1].action.title
    assert "sonnet 5.5" in title
    assert "opus" not in title.lower()
    # The notice's own display name when no frame named a model.
    state = _state()
    events = _translate(state, _informational(NOTICE.replace("Opus 5.5", "Fable 5")))
    assert _notes(events)[-1].action.title.startswith(
        f"{SHIELD} Fable 5 safeguards stopped a response"
    )
    # No model anywhere (not even system/init) → neutral wording.
    state = ClaudeStreamState()
    events = _translate(
        state,
        {
            "type": "system",
            "subtype": "model_refusal_no_fallback",
            "content": "x",
            "session_id": "s",
            "uuid": "u",
        },
    )
    assert _notes(events)[-1].action.title.startswith(
        f"{SHIELD} Safeguards stopped a response"
    )


def test_live_turn_usage_carries_safeguard() -> None:
    """D-12: the flag rides on usage, so it reaches a live turn's TurnEvent
    (live turns filter StartedEvents); per-turn state resets on turn open."""
    state = _state()
    state.live_mode = True
    _translate(state, _init())
    _translate(state, _refusal(msg_id="t1"))
    first = _completed(_translate(state, _result()))
    assert first.usage["safeguard"]["stops"] == 1

    # A follow-up turn opens with a fresh tally.
    _translate(state, _init())
    assert state.safeguard.stops == 0
    events = _translate(state, _refusal(msg_id="t2"))
    assert {e.action.id for e in _notes(events)} == {"claude.safeguard.2"}
    _translate(state, _informational(NOTICE))
    out = _translate(state, _result())
    turn_done = next(
        e for e in out if isinstance(e, TurnEvent) and e.phase == "completed"
    )
    assert turn_done.usage["safeguard"]["stops"] == 1
    assert turn_done.usage["safeguard"]["outcome"] == "retried"
    assert state.safeguard_session_count == 2

    # A clean third turn carries no flag.
    _translate(state, _init())
    out = _translate(state, _result())
    turn_done = next(
        e for e in out if isinstance(e, TurnEvent) and e.phase == "completed"
    )
    assert "safeguard" not in (turn_done.usage or {})


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------


@pytest.fixture
def _bridge_isolation(monkeypatch: pytest.MonkeyPatch):
    set_session_cost_ledger(SessionCostLedger(path=None))
    monkeypatch.setattr(
        rb,
        "_load_footer_settings",
        lambda: FooterSettings(show_api_cost=False, show_subscription_usage=False),
    )
    monkeypatch.setattr(rb, "_SAFEGUARD_HINTED", {})
    yield
    set_session_cost_ledger(None)


async def _final(
    *, answer: str, safeguard: dict[str, Any], session: str, ok: bool = True
) -> str:
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(
                StartedEvent(
                    engine="claude",
                    resume=ResumeToken(engine="claude", value=session),
                    meta={"model": "claude-opus-5-5"},
                )
            ),
            Return(answer=answer, usage={"num_turns": 1, "safeguard": safeguard}),
        ],
        engine="claude",
        resume_value=session,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
        resume_token=None,
    )
    return transport.send_calls[-1]["message"].text


_RETRIED = {
    "stops": 1,
    "outcome": "retried",
    "outcome_label": "retried once",
    "category": "cyber",
    "model": "opus 5.5",
}


@pytest.mark.anyio
@pytest.mark.usefixtures("_bridge_isolation")
async def test_safeguard_never_marks_error() -> None:
    text = await _final(answer="The answer.", safeguard=_RETRIED, session="s-ok")
    assert text.splitlines()[0].startswith("done")
    assert "The answer." in text
    # Not retried + empty answer: an explanation, not an empty error.
    text = await _final(
        answer="",
        safeguard={
            **_RETRIED,
            "outcome": "not_retried",
            "outcome_label": "not retried",
        },
        session="s-empty",
    )
    head = text.splitlines()[0]
    assert head.startswith("done")
    assert "error" not in head
    assert "opus 5.5's safeguards stopped this response" in text
    assert f"{SHIELD} safeguards stopped 1 response · not retried" in text


@pytest.mark.anyio
@pytest.mark.usefixtures("_bridge_isolation")
async def test_footer_and_one_time_hint() -> None:
    first = await _final(answer="A.", safeguard=_RETRIED, session="s-hint")
    assert f"{SHIELD} safeguards stopped 1 response · retried once" in first
    assert rb.SAFEGUARD_CVP_URL in first
    # Same session: flag again, hint not repeated.
    second = await _final(
        answer="B.", safeguard={**_RETRIED, "stops": 2}, session="s-hint"
    )
    assert f"{SHIELD} safeguards stopped 2 responses · retried once" in second
    assert rb.SAFEGUARD_CVP_URL not in second
    # Another session, non-cyber category → the fallback-model docs.
    other = await _final(
        answer="C.", safeguard={**_RETRIED, "category": "bio"}, session="s-bio"
    )
    assert rb.SAFEGUARD_FALLBACK_URL in other
    assert rb.SAFEGUARD_CVP_URL not in other
    # No flag without a stop.
    assert rb._safeguard_usage(None) is None
    assert rb._safeguard_usage({"safeguard": {"stops": 0}}) is None
    assert rb._safeguard_usage({"safeguard": {"stops": True}}) is None


def test_hinted_sessions_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rb, "_SAFEGUARD_HINTED", {})
    monkeypatch.setattr(rb, "_SAFEGUARD_HINTED_MAX", 3)
    for n in range(5):
        rb._safeguard_footer(_RETRIED, f"claude:s{n}")
    assert list(rb._SAFEGUARD_HINTED) == ["claude:s2", "claude:s3", "claude:s4"]


# ---------------------------------------------------------------------------
# replay: real ClaudeRunner + handle_message over the fake live CLI
# ---------------------------------------------------------------------------


class _LiveRunner(ClaudeRunner):
    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        for key in ("FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_WAKE_S"):
            if key in os.environ:
                base[key] = os.environ[key]
        return base


@pytest.mark.anyio
async def test_nsd_transcript_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit gate: refusal → notice → continue → result ok logs
    ``claude.safeguard_stop source=stop_reason outcome=retried
    turn_count=1``, renders the footer, and the run is ok."""
    set_session_cost_ledger(SessionCostLedger(path=None))
    set_quarantine_store(QuarantineStore(tmp_path / "q.json"))
    monkeypatch.setattr(rb, "_SAFEGUARD_HINTED", {})
    settings = ProgressSettings(
        show_background_tasks=False, consolidate_wake_turns=False
    )
    monkeypatch.setattr(rb, "_load_progress_settings", lambda: settings)
    monkeypatch.setattr(
        rb,
        "_load_footer_settings",
        lambda: FooterSettings(show_api_cost=False, show_subscription_usage=False),
    )
    watchdog = WatchdogSettings.model_construct(
        **{**WatchdogSettings().model_dump(), "post_result_limbo_grace": 0.4}
    )
    monkeypatch.setattr(
        claude_mod,
        "load_settings_if_exists",
        lambda *a, **k: (SimpleNamespace(watchdog=watchdog), Path("x")),
    )
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "safeguard_refusal_retry")
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")
    runner._live_poll_s = 0.05
    try:
        with capture_logs() as logs, anyio.fail_after(25):
            await handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
                resume_token=None,
            )
    finally:
        set_quarantine_store(None)
        set_session_cost_ledger(None)
    (log,) = _stops(logs)
    assert (log["source"], log["outcome"], log["turn_count"]) == (
        "stop_reason",
        "retried",
        1,
    )
    completed = [x for x in logs if x["event"] == "runner.completed"]
    assert completed and completed[0]["ok"] is True
    texts = [c["message"].text for c in transport.send_calls] + [
        c["message"].text for c in transport.edit_calls
    ]
    final = next(t for t in texts if "Here is the defensive summary." in t)
    assert final.splitlines()[0].startswith("done")
    assert f"{SHIELD} safeguards stopped 1 response · retried once" in final
    assert rb.SAFEGUARD_CVP_URL in final
