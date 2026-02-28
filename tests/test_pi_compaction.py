"""Tests for Pi runner context compaction event translation."""

from __future__ import annotations

from untether.model import ActionEvent
from untether.runners.pi import PiStreamState, translate_pi_event
from untether.model import ResumeToken
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
    assert events[0].action.title == "context compacted"


def test_auto_compaction_start_no_reason():
    """AutoCompactionStart with no reason should still work."""
    state = _make_state()
    event = pi_schema.AutoCompactionStart(reason=None)
    events = translate_pi_event(event, title="pi", meta=None, state=state)
    assert len(events) == 1
    assert "compacting context" in events[0].action.title
    # No reason suffix
    assert "(" not in events[0].action.title


def test_compaction_sequence():
    """Full compaction start + end sequence."""
    state = _make_state()

    start_event = pi_schema.AutoCompactionStart(reason="threshold")
    start_events = translate_pi_event(start_event, title="pi", meta=None, state=state)
    assert len(start_events) == 1
    action_id = start_events[0].action.id

    end_event = pi_schema.AutoCompactionEnd(
        result={"tokensBefore": 50000}, aborted=False
    )
    end_events = translate_pi_event(end_event, title="pi", meta=None, state=state)
    assert len(end_events) == 1
    assert end_events[0].action.id == action_id
