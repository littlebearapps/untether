"""Tests for Pi runner context compaction event translation."""

from __future__ import annotations

from untether.model import ActionEvent, ResumeToken
from untether.runners.pi import PiStreamState, translate_pi_event
from untether.schemas import pi as pi_schema


def _make_state() -> PiStreamState:
    return PiStreamState(
        resume=ResumeToken(engine="pi", value="test-session"), started=True
    )


def test_auto_compaction_start():
    """AutoCompactionStart should produce an action_started event."""
    state = _make_state()
    event = pi_schema.AutoCompactionStart(reason="threshold")
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "started"
    assert "compacting context" in events[0].action.title
    assert "threshold" in events[0].action.title
    assert events[0].action.kind == "note"
    assert state.compaction_seq == 1


def test_auto_compaction_end():
    """AutoCompactionEnd should produce an action_completed event."""
    state = _make_state()
    state.compaction_seq = 1
    state.compaction_action_id = "compaction_1"

    result_data = {
        "tokensBefore": 118432,
        "summary": "Summarised context",
    }
    event = pi_schema.AutoCompactionEnd(
        result=result_data, aborted=False, willRetry=False
    )
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "completed"
    assert "118,432 tokens" in events[0].action.title
    assert events[0].ok is True
    assert state.compaction_action_id is None


def test_auto_compaction_end_aborted():
    """Aborted compaction should show as failed."""
    state = _make_state()
    state.compaction_seq = 1
    state.compaction_action_id = "compaction_1"

    event = pi_schema.AutoCompactionEnd(result=None, aborted=True, willRetry=False)
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].ok is False
    assert "aborted" in events[0].action.title


def test_auto_compaction_end_no_tokens():
    """Compaction without token count should show generic message."""
    state = _make_state()
    state.compaction_seq = 1
    state.compaction_action_id = "compaction_1"

    event = pi_schema.AutoCompactionEnd(result={}, aborted=False)
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].action.title == "context compacted"


def test_auto_compaction_start_no_reason():
    """AutoCompactionStart with no reason should still work."""
    state = _make_state()
    event = pi_schema.AutoCompactionStart(reason=None)
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert "compacting context" in events[0].action.title
    # No reason suffix
    assert "(" not in events[0].action.title


def test_compaction_sequence():
    """Full compaction start + end sequence."""
    state = _make_state()

    start_event = pi_schema.AutoCompactionStart(reason="threshold")
    start_events = translate_pi_event(start_event, title="pi", meta=None, state=state)
    assert len(start_events) == 1
    assert isinstance(start_events[0], ActionEvent)
    action_id = start_events[0].action.id

    end_event = pi_schema.AutoCompactionEnd(
        result={"tokensBefore": 50000}, aborted=False
    )
    end_events = translate_pi_event(end_event, title="pi", meta=None, state=state)
    assert len(end_events) == 1
    assert isinstance(end_events[0], ActionEvent)
    assert end_events[0].action.id == action_id


# #1239: Pi >= 0.66.1 renamed auto_compaction_* to compaction_*.


def test_compaction_start_current_tag():
    event = pi_schema.decode_event('{"type":"compaction_start","reason":"threshold"}')
    state = _make_state()
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "started"
    assert events[0].action.title == "compacting context… (threshold)"


def test_compaction_end_current_tag():
    event = pi_schema.decode_event(
        '{"type":"compaction_end","reason":"threshold","result":{"summary":"s",'
        '"firstKeptEntryId":"abc123","tokensBefore":150000,'
        '"estimatedTokensAfter":32000,"usage":{},"details":{}},'
        '"aborted":false,"willRetry":false}'
    )
    state = _make_state()
    state.compaction_seq = 1
    state.compaction_action_id = "compaction_1"
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "completed"
    assert events[0].action.id == "compaction_1"
    assert "150,000 tokens" in events[0].action.title
    assert events[0].ok is True


def test_compaction_end_failed_error_message():
    event = pi_schema.decode_event(
        '{"type":"compaction_end","reason":"threshold","aborted":false,'
        '"willRetry":false,"errorMessage":"summary model failed"}'
    )
    state = _make_state()
    state.compaction_seq = 1
    state.compaction_action_id = "compaction_1"
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].action.title == "context compaction failed"
    assert events[0].ok is False


def test_legacy_compaction_tags_still_decode():
    start = pi_schema.decode_event('{"type":"auto_compaction_start","reason":"x"}')
    end = pi_schema.decode_event('{"type":"auto_compaction_end","aborted":false}')
    assert isinstance(start, pi_schema.AutoCompactionStart)
    assert isinstance(end, pi_schema.AutoCompactionEnd)


# #1182: only a successful ``compaction_end`` is counted.


def _end(payload: str) -> ActionEvent:
    state = _make_state()
    state.compaction_seq = 1
    state.compaction_action_id = "compaction_1"
    [event] = translate_pi_event(
        pi_schema.decode_event(payload), title="pi", meta=None, state=state
    )
    assert isinstance(event, ActionEvent)
    return event


def test_compaction_end_success_flagged():
    event = _end(
        '{"type":"compaction_end","reason":"threshold",'
        '"result":{"summary":"s","tokensBefore":150000},'
        '"aborted":false,"willRetry":false}'
    )
    assert event.action.detail["compaction"] is True
    assert event.action.detail["result"]["tokensBefore"] == 150000


def test_compaction_end_failed_or_aborted_not_flagged():
    for payload in (
        '{"type":"compaction_end","aborted":true,"willRetry":false}',
        '{"type":"compaction_end","aborted":false,"errorMessage":"boom"}',
    ):
        assert "compaction" not in _end(payload).action.detail
    state = _make_state()
    [started] = translate_pi_event(
        pi_schema.decode_event('{"type":"compaction_start","reason":"manual"}'),
        title="pi",
        meta=None,
        state=state,
    )
    assert isinstance(started, ActionEvent)
    assert "compaction" not in started.action.detail
