from pathlib import Path

import msgspec

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners.gemini import (
    ENGINE,
    GeminiRunner,
    GeminiStreamState,
    translate_gemini_event,
)
from untether.schemas import gemini as gemini_schema


def _load_fixture(name: str) -> list[gemini_schema.GeminiEvent]:
    path = Path(__file__).parent / "fixtures" / name
    events: list[gemini_schema.GeminiEvent] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            decoded = gemini_schema.decode_event(line)
        except Exception as exc:
            raise AssertionError(f"{name} contained unparseable line: {line}") from exc
        events.append(decoded)
    return events


def _decode_event(payload: dict) -> gemini_schema.GeminiEvent:
    return gemini_schema.decode_event(msgspec.json.encode(payload))


def test_gemini_resume_format_and_extract() -> None:
    runner = GeminiRunner()
    token = ResumeToken(engine=ENGINE, value="abc123def")

    assert runner.format_resume(token) == "`gemini --resume abc123def`"
    assert runner.extract_resume("gemini --resume xyz789") == ResumeToken(
        engine=ENGINE, value="xyz789"
    )
    assert runner.extract_resume("`claude --resume sid`") is None
    assert runner.extract_resume("`opencode --session ses_abc`") is None


def test_translate_success_fixture() -> None:
    state = GeminiStreamState()
    events: list = []
    for event in _load_fixture("gemini_stream_success.jsonl"):
        events.extend(
            translate_gemini_event(event, title="gemini", state=state, meta=None)
        )

    assert isinstance(events[0], StartedEvent)
    started = events[0]
    assert started.resume.value == "abc123def"
    assert started.resume.engine == ENGINE
    assert started.meta is not None
    assert started.meta["model"] == "gemini-2.0-flash-exp"

    action_events = [evt for evt in events if isinstance(evt, ActionEvent)]
    assert len(action_events) == 4

    started_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "started"
    }
    assert started_actions[("tool_1", "started")].action.kind == "command"

    completed_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "completed"
    }
    assert completed_actions[("tool_1", "completed")].ok is True

    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is True
    assert completed.answer == "The command output `hello`."
    assert completed.usage is not None
    assert completed.usage["usage"]["input_tokens"] == 100
    assert completed.usage["usage"]["output_tokens"] == 50


def test_translate_error_fixture() -> None:
    state = GeminiStreamState()
    events: list = []
    for event in _load_fixture("gemini_stream_error.jsonl"):
        events.extend(
            translate_gemini_event(event, title="gemini", state=state, meta=None)
        )

    assert isinstance(events[0], StartedEvent)
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is False
    assert completed.error == "API key invalid or expired"


def test_translate_accumulates_text() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    translate_gemini_event(
        _decode_event({"type": "message", "role": "assistant", "content": "Hello "}),
        title="gemini",
        state=state,
        meta=None,
    )
    translate_gemini_event(
        _decode_event(
            {"type": "message", "role": "assistant", "content": "world!", "delta": True}
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert state.last_text == "Hello world!"


def test_translate_user_message_ignored() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    results = translate_gemini_event(
        _decode_event({"type": "message", "role": "user", "content": "some user text"}),
        title="gemini",
        state=state,
        meta=None,
    )
    assert results == []
    assert state.last_text is None


def test_translate_tool_use_and_result() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    events = translate_gemini_event(
        _decode_event(
            {
                "type": "tool_use",
                "tool_name": "Bash",
                "tool_id": "tool_1",
                "parameters": {"command": "ls -la"},
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "started"
    assert "tool_1" in state.pending_actions

    events = translate_gemini_event(
        _decode_event(
            {
                "type": "tool_result",
                "tool_id": "tool_1",
                "status": "success",
                "output": "files",
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "completed"
    assert events[0].ok is True


def test_translate_tool_result_error() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    translate_gemini_event(
        _decode_event(
            {
                "type": "tool_use",
                "tool_name": "Bash",
                "tool_id": "tool_1",
                "parameters": {"command": "false"},
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    events = translate_gemini_event(
        _decode_event(
            {
                "type": "tool_result",
                "tool_id": "tool_1",
                "status": "error",
                "output": "fail",
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert events[0].ok is False


def test_translate_result_success() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True, last_text="done")
    events = translate_gemini_event(
        _decode_event(
            {
                "type": "result",
                "status": "success",
                "stats": {"input_tokens": 100, "output_tokens": 50},
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is True
    assert completed.answer == "done"


def test_translate_result_error() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    events = translate_gemini_event(
        _decode_event({"type": "result", "status": "error"}),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False


def test_translate_error_event() -> None:
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    events = translate_gemini_event(
        _decode_event({"type": "error", "message": "something broke"}),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False
    assert completed.error == "something broke"


def test_build_args_new_session() -> None:
    runner = GeminiRunner()
    state = GeminiStreamState()
    args = runner.build_args("hello world", None, state=state)
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--resume" not in args
    # --prompt= binds the value directly to avoid yargs flag injection
    assert "--prompt=hello world" in args


def test_build_args_with_resume() -> None:
    runner = GeminiRunner()
    state = GeminiStreamState()
    token = ResumeToken(engine=ENGINE, value="abc123")
    args = runner.build_args("continue", token, state=state)
    assert "--resume" in args
    assert "abc123" in args


def test_build_args_with_model() -> None:
    runner = GeminiRunner(model="gemini-2.5-pro")
    state = GeminiStreamState()
    args = runner.build_args("hello", None, state=state)
    assert "--model" in args
    assert "gemini-2.5-pro" in args


def test_stdin_payload_returns_none() -> None:
    runner = GeminiRunner()
    state = GeminiStreamState()
    assert runner.stdin_payload("hello", None, state=state) is None


def test_init_carries_model_meta() -> None:
    state = GeminiStreamState()
    events = translate_gemini_event(
        _decode_event(
            {
                "type": "init",
                "session_id": "sid1",
                "model": "gemini-2.5-pro",
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    started = events[0]
    assert isinstance(started, StartedEvent)
    assert started.meta is not None
    assert started.meta["model"] == "gemini-2.5-pro"


def test_snake_case_tool_names() -> None:
    """Gemini uses snake_case tool names like read_file, edit_file."""
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    events = translate_gemini_event(
        _decode_event(
            {
                "type": "tool_use",
                "tool_name": "read_file",
                "tool_id": "tool_1",
                "parameters": {"file_path": "/tmp/test.py"},
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert events[0].action.kind == "tool"
    assert "test.py" in events[0].action.title

    events2 = translate_gemini_event(
        _decode_event(
            {
                "type": "tool_use",
                "tool_name": "edit_file",
                "tool_id": "tool_2",
                "parameters": {"file_path": "/tmp/foo.py"},
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert len(events2) == 1
    assert events2[0].action.kind == "file_change"


def test_build_args_approval_mode_from_run_options() -> None:
    from untether.runners.run_options import EngineRunOptions, apply_run_options

    runner = GeminiRunner()
    state = GeminiStreamState()
    with apply_run_options(EngineRunOptions(permission_mode="plan")):
        args = runner.build_args("hello", None, state=state)
    assert "--approval-mode" in args
    assert "plan" in args


def test_build_args_no_approval_mode_by_default() -> None:
    runner = GeminiRunner()
    state = GeminiStreamState()
    args = runner.build_args("hello", None, state=state)
    assert "--approval-mode" not in args


def test_orphan_tool_result_ignored() -> None:
    """A tool_result with no matching tool_use should be ignored."""
    state = GeminiStreamState(session_id="ses1", emitted_started=True)
    events = translate_gemini_event(
        _decode_event(
            {
                "type": "tool_result",
                "tool_id": "unknown",
                "status": "success",
            }
        ),
        title="gemini",
        state=state,
        meta=None,
    )
    assert events == []
