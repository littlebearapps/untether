from pathlib import Path

import msgspec

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners.amp import (
    ENGINE,
    AmpRunner,
    AmpStreamState,
    translate_amp_event,
)
from untether.schemas import amp as amp_schema


def _load_fixture(name: str) -> list[amp_schema.AmpEvent]:
    path = Path(__file__).parent / "fixtures" / name
    events: list[amp_schema.AmpEvent] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            decoded = amp_schema.decode_event(line)
        except Exception as exc:
            raise AssertionError(f"{name} contained unparseable line: {line}") from exc
        events.append(decoded)
    return events


def _decode_event(payload: dict) -> amp_schema.AmpEvent:
    return amp_schema.decode_event(msgspec.json.encode(payload))


def test_amp_resume_format_and_extract() -> None:
    runner = AmpRunner()
    token = ResumeToken(engine=ENGINE, value="T-2775dc92-90ed-4f85-8b73-8f9766029e83")

    assert (
        runner.format_resume(token)
        == "`amp threads continue T-2775dc92-90ed-4f85-8b73-8f9766029e83`"
    )
    assert runner.extract_resume("amp threads continue T-abc-def-123") == ResumeToken(
        engine=ENGINE, value="T-abc-def-123"
    )
    assert runner.extract_resume("`claude --resume sid`") is None
    assert runner.extract_resume("`gemini --resume abc`") is None


def test_translate_success_fixture() -> None:
    state = AmpStreamState()
    events: list = []
    for event in _load_fixture("amp_stream_success.jsonl"):
        events.extend(translate_amp_event(event, title="amp", state=state, meta=None))

    assert isinstance(events[0], StartedEvent)
    started = events[0]
    assert started.resume.value == "T-2775dc92-90ed-4f85-8b73-8f9766029e83"
    assert started.resume.engine == ENGINE

    action_events = [evt for evt in events if isinstance(evt, ActionEvent)]
    assert len(action_events) >= 2

    started_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "started"
    }
    assert ("toolu_01", "started") in started_actions
    assert started_actions[("toolu_01", "started")].action.kind == "command"

    completed_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "completed"
    }
    assert ("toolu_01", "completed") in completed_actions
    assert completed_actions[("toolu_01", "completed")].ok is True

    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is True
    assert completed.answer == "hello"
    assert completed.usage is not None
    assert completed.usage["usage"]["input_tokens"] > 0
    assert completed.usage["usage"]["output_tokens"] > 0


def test_translate_error_fixture() -> None:
    state = AmpStreamState()
    events: list = []
    for event in _load_fixture("amp_stream_error.jsonl"):
        events.extend(translate_amp_event(event, title="amp", state=state, meta=None))

    assert isinstance(events[0], StartedEvent)
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is False
    assert completed.error == "Authentication failed"


def test_translate_tool_use_bash() -> None:
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    events = translate_amp_event(
        _decode_event(
            {
                "type": "assistant",
                "session_id": "T-ses1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool1",
                            "name": "Bash",
                            "input": {"command": "ls -la"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "started"
    assert events[0].action.kind == "command"
    assert "tool1" in state.pending_actions


def test_translate_tool_result_ok() -> None:
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    # First register a pending action
    from untether.model import Action

    state.pending_actions["tool1"] = Action(
        id="tool1", kind="command", title="ls -la", detail={"command": "ls -la"}
    )
    events = translate_amp_event(
        _decode_event(
            {
                "type": "user",
                "session_id": "T-ses1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool1",
                            "content": [
                                {"type": "text", "text": "file1.txt\nfile2.txt"}
                            ],
                        },
                    ],
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "completed"
    assert events[0].ok is True


def test_translate_tool_result_error() -> None:
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    from untether.model import Action

    state.pending_actions["tool2"] = Action(
        id="tool2", kind="command", title="false", detail={"command": "false"}
    )
    events = translate_amp_event(
        _decode_event(
            {
                "type": "user",
                "session_id": "T-ses1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool2",
                            "is_error": True,
                            "content": [{"type": "text", "text": "command failed"}],
                        },
                    ],
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert events[0].ok is False


def test_translate_text_accumulation() -> None:
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    translate_amp_event(
        _decode_event(
            {
                "type": "assistant",
                "session_id": "T-ses1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello "}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    translate_amp_event(
        _decode_event(
            {
                "type": "assistant",
                "session_id": "T-ses1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "world!"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert state.last_text == "Hello world!"
    assert state.accumulated_usage["input_tokens"] == 20
    assert state.accumulated_usage["output_tokens"] == 10


def test_translate_result_success() -> None:
    state = AmpStreamState(session_id="T-ses1", emitted_started=True, last_text="done")
    events = translate_amp_event(
        _decode_event(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "duration_ms": 1000,
                "session_id": "T-ses1",
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is True
    assert completed.answer == "done"


def test_translate_result_error() -> None:
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    events = translate_amp_event(
        _decode_event(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "error": "rate limited",
                "session_id": "T-ses1",
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False


def test_build_args_new_session() -> None:
    runner = AmpRunner()
    state = AmpStreamState()
    args = runner.build_args("hello world", None, state=state)
    assert "--stream-json" in args
    assert "--dangerously-allow-all" in args
    assert "threads" not in args


def test_build_args_with_resume() -> None:
    runner = AmpRunner()
    state = AmpStreamState()
    token = ResumeToken(engine=ENGINE, value="T-abc-123")
    args = runner.build_args("continue", token, state=state)
    assert "threads" in args
    assert "continue" in args
    assert "T-abc-123" in args
    assert "--stream-json" in args


def test_build_args_dangerously_allow_all_false() -> None:
    runner = AmpRunner(dangerously_allow_all=False)
    state = AmpStreamState()
    args = runner.build_args("hello", None, state=state)
    assert "--dangerously-allow-all" not in args


def test_build_args_mode() -> None:
    runner = AmpRunner(mode="rush")
    state = AmpStreamState()
    args = runner.build_args("hello", None, state=state)
    assert "--mode" in args
    assert "rush" in args


def test_stdin_payload_returns_none() -> None:
    runner = AmpRunner()
    state = AmpStreamState()
    assert runner.stdin_payload("hello", None, state=state) is None


def test_system_init_non_init_subtype_ignored() -> None:
    state = AmpStreamState()
    events = translate_amp_event(
        _decode_event(
            {
                "type": "system",
                "subtype": "other",
                "session_id": "T-should-not-appear",
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert events == []
    assert state.session_id is None


def test_subagent_parent_tool_use_id_tracked() -> None:
    """parent_tool_use_id from subagent messages is stored in action detail."""
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    # Assistant message with parent_tool_use_id (subagent)
    events = translate_amp_event(
        _decode_event(
            {
                "type": "assistant",
                "session_id": "T-ses1",
                "parent_tool_use_id": "toolu_parent_01",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_sub_01",
                            "name": "Read",
                            "input": {"file_path": "src/main.py"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 50, "output_tokens": 10},
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].action.detail["parent_tool_use_id"] == "toolu_parent_01"

    # Now test the tool_result side
    from untether.model import Action

    state.pending_actions["toolu_sub_01"] = Action(
        id="toolu_sub_01",
        kind="tool",
        title="read: src/main.py",
        detail={"tool_name": "Read", "input": {}, "tool_id": "toolu_sub_01"},
    )
    result_events = translate_amp_event(
        _decode_event(
            {
                "type": "user",
                "session_id": "T-ses1",
                "parent_tool_use_id": "toolu_parent_01",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_sub_01",
                            "content": [{"type": "text", "text": "file contents"}],
                        },
                    ],
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(result_events) == 1
    assert result_events[0].action.detail["parent_tool_use_id"] == "toolu_parent_01"


def test_no_parent_tool_use_id_when_absent() -> None:
    """When parent_tool_use_id is None, it should not appear in detail."""
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    events = translate_amp_event(
        _decode_event(
            {
                "type": "assistant",
                "session_id": "T-ses1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_normal",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert "parent_tool_use_id" not in events[0].action.detail


def test_orphan_tool_result_ignored() -> None:
    """A tool_result with no matching pending action should be ignored."""
    state = AmpStreamState(session_id="T-ses1", emitted_started=True)
    events = translate_amp_event(
        _decode_event(
            {
                "type": "user",
                "session_id": "T-ses1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "unknown",
                            "content": [{"type": "text", "text": "output"}],
                        },
                    ],
                },
            }
        ),
        title="amp",
        state=state,
        meta=None,
    )
    assert events == []
