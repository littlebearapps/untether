import json
from pathlib import Path

import anyio
import pytest

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners.opencode import (
    OpenCodeRunner,
    OpenCodeStreamState,
    ENGINE,
    translate_opencode_event,
)
from untether.schemas import opencode as opencode_schema


def _load_fixture(name: str) -> list[opencode_schema.OpenCodeEvent]:
    path = Path(__file__).parent / "fixtures" / name
    events: list[opencode_schema.OpenCodeEvent] = []
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            events.append(opencode_schema.decode_event(line))
        except Exception as exc:
            raise AssertionError(
                f"{name} contained unparseable line: {line!r}"
            ) from exc
    return events


def _decode_event(payload: dict) -> opencode_schema.OpenCodeEvent:
    return opencode_schema.decode_event(json.dumps(payload).encode("utf-8"))


def test_opencode_resume_format_and_extract() -> None:
    runner = OpenCodeRunner(opencode_cmd="opencode")
    token = ResumeToken(engine=ENGINE, value="ses_abc123")

    assert runner.format_resume(token) == "`opencode --session ses_abc123`"
    assert runner.extract_resume("`opencode --session ses_abc123`") == token
    assert runner.extract_resume("opencode run -s ses_other") == ResumeToken(
        engine=ENGINE, value="ses_other"
    )
    assert runner.extract_resume("opencode -s ses_other") == ResumeToken(
        engine=ENGINE, value="ses_other"
    )
    assert runner.extract_resume("`claude --resume sid`") is None
    assert runner.extract_resume("`codex resume sid`") is None


def test_translate_success_fixture() -> None:
    state = OpenCodeStreamState()
    events: list = []
    for event in _load_fixture("opencode_stream_success.jsonl"):
        events.extend(translate_opencode_event(event, title="opencode", state=state))

    assert isinstance(events[0], StartedEvent)
    started = next(evt for evt in events if isinstance(evt, StartedEvent))
    assert started.resume.value == "ses_494719016ffe85dkDMj0FPRbHK"
    assert started.resume.engine == ENGINE

    action_events = [evt for evt in events if isinstance(evt, ActionEvent)]
    assert len(action_events) == 1

    completed_actions = [evt for evt in action_events if evt.phase == "completed"]
    assert len(completed_actions) == 1
    assert completed_actions[0].action.kind == "command"
    assert completed_actions[0].ok is True

    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert events[-1] == completed
    assert completed.ok is True
    assert completed.resume == started.resume
    assert completed.answer == "```\nhello\n```"


def test_translate_missing_reason_success() -> None:
    state = OpenCodeStreamState()
    events: list = []
    for event in _load_fixture("opencode_stream_success_no_reason.jsonl"):
        events.extend(translate_opencode_event(event, title="opencode", state=state))

    started = next(evt for evt in events if isinstance(evt, StartedEvent))
    runner = OpenCodeRunner(opencode_cmd="opencode")
    fallback = runner.stream_end_events(
        resume=None,
        found_session=started.resume,
        state=state,
    )

    completed = next(evt for evt in fallback if isinstance(evt, CompletedEvent))
    assert completed.ok is True
    assert completed.resume == started.resume
    assert completed.answer == "All done."


def test_translate_accumulates_text() -> None:
    state = OpenCodeStreamState()

    events = translate_opencode_event(
        _decode_event({"type": "step_start", "sessionID": "ses_test123", "part": {}}),
        title="opencode",
        state=state,
    )
    assert len(events) == 1
    assert isinstance(events[0], StartedEvent)

    translate_opencode_event(
        _decode_event(
            {
                "type": "text",
                "sessionID": "ses_test123",
                "part": {"type": "text", "text": "Hello "},
            }
        ),
        title="opencode",
        state=state,
    )
    translate_opencode_event(
        _decode_event(
            {
                "type": "text",
                "sessionID": "ses_test123",
                "part": {"type": "text", "text": "World"},
            }
        ),
        title="opencode",
        state=state,
    )

    assert state.last_text == "Hello World"

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_finish",
                "sessionID": "ses_test123",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 100, "output": 10},
                    "cost": 0.005,
                },
            }
        ),
        title="opencode",
        state=state,
    )

    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.answer == "Hello World"
    assert completed.ok is True
    assert completed.usage is not None
    assert completed.usage["total_cost_usd"] == 0.005
    assert completed.usage["usage"]["input_tokens"] == 100
    assert completed.usage["usage"]["output_tokens"] == 10


def test_translate_accumulates_cost_across_steps() -> None:
    """Cost and tokens accumulate across multiple step_finish events."""
    state = OpenCodeStreamState()
    state.session_id = "ses_cost_test"
    state.emitted_started = True

    # First step (tool-calls, not final)
    translate_opencode_event(
        _decode_event(
            {
                "type": "step_finish",
                "sessionID": "ses_cost_test",
                "part": {
                    "reason": "tool-calls",
                    "cost": 0.003,
                    "tokens": {
                        "input": 500,
                        "output": 50,
                        "reasoning": 0,
                        "cache": {"read": 100, "write": 0},
                    },
                },
            }
        ),
        title="opencode",
        state=state,
    )

    # Second step (final)
    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_finish",
                "sessionID": "ses_cost_test",
                "part": {
                    "reason": "stop",
                    "cost": 0.002,
                    "tokens": {
                        "input": 600,
                        "output": 30,
                        "reasoning": 10,
                        "cache": {"read": 200, "write": 50},
                    },
                },
            }
        ),
        title="opencode",
        state=state,
    )

    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.usage is not None
    assert completed.usage["total_cost_usd"] == pytest.approx(0.005)
    assert completed.usage["usage"]["input_tokens"] == 1100
    assert completed.usage["usage"]["output_tokens"] == 80
    assert completed.usage["usage"]["reasoning_tokens"] == 10
    assert completed.usage["usage"]["cache_read_tokens"] == 300
    assert completed.usage["usage"]["cache_write_tokens"] == 50


def test_translate_no_cost_produces_no_usage() -> None:
    """When step_finish has no cost/token data, usage is None."""
    state = OpenCodeStreamState()
    state.session_id = "ses_nocost"
    state.emitted_started = True

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_finish",
                "sessionID": "ses_nocost",
                "part": {"reason": "stop"},
            }
        ),
        title="opencode",
        state=state,
    )

    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.usage is None


def test_translate_tool_use_completed() -> None:
    state = OpenCodeStreamState()
    state.session_id = "ses_test123"
    state.emitted_started = True

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "tool_use",
                "sessionID": "ses_test123",
                "part": {
                    "id": "prt_123",
                    "callID": "call_abc",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls -la"},
                        "output": "file1.txt\nfile2.txt",
                        "title": "List files",
                        "metadata": {"exit": 0},
                    },
                },
            }
        ),
        title="opencode",
        state=state,
    )

    assert len(events) == 1
    action_event = events[0]
    assert isinstance(action_event, ActionEvent)
    assert action_event.phase == "completed"
    assert action_event.action.kind == "command"
    assert action_event.action.title == "List files"
    assert action_event.ok is True


def test_translate_tool_use_with_error() -> None:
    state = OpenCodeStreamState()
    state.session_id = "ses_test123"
    state.emitted_started = True

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "tool_use",
                "sessionID": "ses_test123",
                "part": {
                    "id": "prt_123",
                    "callID": "call_abc",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "exit 1"},
                        "output": "error",
                        "title": "Run failing command",
                        "metadata": {"exit": 1},
                    },
                },
            }
        ),
        title="opencode",
        state=state,
    )

    assert len(events) == 1
    action_event = events[0]
    assert isinstance(action_event, ActionEvent)
    assert action_event.phase == "completed"
    assert action_event.ok is False


def test_translate_tool_use_read_title_wraps_path() -> None:
    state = OpenCodeStreamState()
    state.session_id = "ses_test123"
    state.emitted_started = True
    path = Path.cwd() / "src" / "untether" / "runners" / "opencode.py"

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "tool_use",
                "sessionID": "ses_test123",
                "part": {
                    "id": "prt_123",
                    "callID": "call_abc",
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": str(path)},
                        "output": "file contents",
                        "title": "src/untether/runners/opencode.py",
                    },
                },
            }
        ),
        title="opencode",
        state=state,
    )

    assert len(events) == 1
    action_event = events[0]
    assert isinstance(action_event, ActionEvent)
    assert action_event.action.kind == "tool"
    assert action_event.action.title == "`src/untether/runners/opencode.py`"


def test_translate_error_fixture() -> None:
    state = OpenCodeStreamState()
    events: list = []
    for event in _load_fixture("opencode_stream_error.jsonl"):
        events.extend(translate_opencode_event(event, title="opencode", state=state))

    started = next(evt for evt in events if isinstance(evt, StartedEvent))
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))

    assert completed.ok is False
    assert completed.error == "Rate limit exceeded"
    assert completed.resume == started.resume


def test_translate_step_start_with_meta() -> None:
    state = OpenCodeStreamState()
    meta = {"model": "openai/gpt-5.2"}
    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_start",
                "sessionID": "ses_meta_test",
                "part": {"id": "prt_1", "sessionID": "ses_meta_test"},
            }
        ),
        title="opencode",
        state=state,
        meta=meta,
    )
    assert len(events) == 1
    assert isinstance(events[0], StartedEvent)
    assert events[0].meta == {"model": "openai/gpt-5.2"}


def test_translate_step_start_no_meta() -> None:
    state = OpenCodeStreamState()
    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_start",
                "sessionID": "ses_no_meta",
                "part": {"id": "prt_1", "sessionID": "ses_no_meta"},
            }
        ),
        title="opencode",
        state=state,
    )
    assert len(events) == 1
    assert isinstance(events[0], StartedEvent)
    assert events[0].meta is None


def test_step_finish_tool_calls_does_not_complete() -> None:
    state = OpenCodeStreamState()
    state.session_id = "ses_test123"
    state.emitted_started = True

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_finish",
                "sessionID": "ses_test123",
                "part": {
                    "reason": "tool-calls",
                    "tokens": {"input": 100, "output": 10},
                },
            }
        ),
        title="opencode",
        state=state,
    )

    assert len(events) == 0


def test_build_args_new_session() -> None:
    runner = OpenCodeRunner(opencode_cmd="opencode", model="claude-sonnet")
    args = runner.build_args("hello world", None, state=OpenCodeStreamState())

    assert args == [
        "run",
        "--format",
        "json",
        "--model",
        "claude-sonnet",
        "--",
        "hello world",
    ]


def test_build_args_with_resume() -> None:
    runner = OpenCodeRunner(opencode_cmd="opencode")
    token = ResumeToken(engine=ENGINE, value="ses_abc123")
    args = runner.build_args("continue", token, state=OpenCodeStreamState())

    assert args == [
        "run",
        "--format",
        "json",
        "--session",
        "ses_abc123",
        "--",
        "continue",
    ]


def test_stdin_payload_returns_none() -> None:
    runner = OpenCodeRunner(opencode_cmd="opencode")
    payload = runner.stdin_payload("prompt", None, state=OpenCodeStreamState())
    assert payload is None


@pytest.mark.anyio
async def test_run_serializes_same_session() -> None:
    runner = OpenCodeRunner(opencode_cmd="opencode")
    gate = anyio.Event()
    in_flight = 0
    max_in_flight = 0

    async def run_stub(*_args, **_kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await gate.wait()
            yield CompletedEvent(
                engine=ENGINE,
                resume=ResumeToken(engine=ENGINE, value="ses_test"),
                ok=True,
                answer="ok",
            )
        finally:
            in_flight -= 1

    runner.run_impl = run_stub  # type: ignore[assignment]

    async def drain(prompt: str, resume: ResumeToken | None) -> None:
        async for _event in runner.run(prompt, resume):
            pass

    token = ResumeToken(engine=ENGINE, value="ses_test")
    async with anyio.create_task_group() as tg:
        tg.start_soon(drain, "a", token)
        tg.start_soon(drain, "b", token)
        await anyio.sleep(0)
        gate.set()
    assert max_in_flight == 1


# ---------------------------------------------------------------------------
# Issue #146 — error events with no prior text should show error message
# ---------------------------------------------------------------------------


def test_error_event_no_prior_text_uses_error_message() -> None:
    """When Error arrives with no prior Text events, answer must contain error text."""
    state = OpenCodeStreamState()
    state.session_id = "ses_err"
    state.emitted_started = True

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "error",
                "sessionID": "ses_err",
                "error": "Rate limit exceeded",
            }
        ),
        title="opencode",
        state=state,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False
    assert completed.answer != ""
    assert "Rate limit" in completed.answer


def test_process_error_no_prior_text_uses_error_message() -> None:
    """process_error_events with empty state produces non-empty answer."""
    runner = OpenCodeRunner(opencode_cmd="opencode")
    state = OpenCodeStreamState()
    events = runner.process_error_events(
        1, resume=None, found_session=None, state=state
    )
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.answer != ""
    assert "opencode failed" in completed.answer


def test_stream_end_no_session_no_text_uses_error_message() -> None:
    """stream_end_events no-session path with no text produces non-empty answer."""
    runner = OpenCodeRunner(opencode_cmd="opencode")
    state = OpenCodeStreamState()
    events = runner.stream_end_events(resume=None, found_session=None, state=state)
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.answer != ""
    assert "no session_id" in completed.answer


# --- #150: OpenCode empty body fallback to last_tool_error ---


def test_translate_stop_no_text_falls_back_to_tool_error() -> None:
    """StepFinish reason=stop with no Text events uses last_tool_error."""
    state = OpenCodeStreamState(session_id="ses_test")
    state.last_tool_error = "file not found: /nonexistent/path.txt"
    event = opencode_schema.StepFinish(part={"reason": "stop"})
    events = translate_opencode_event(event, title="opencode", state=state)
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.ok is True
    assert completed.answer == "file not found: /nonexistent/path.txt"


def test_translate_stop_with_text_ignores_tool_error() -> None:
    """StepFinish reason=stop prefers last_text over last_tool_error."""
    state = OpenCodeStreamState(session_id="ses_test")
    state.last_text = "Here are the files"
    state.last_tool_error = "some earlier error"
    event = opencode_schema.StepFinish(part={"reason": "stop"})
    events = translate_opencode_event(event, title="opencode", state=state)
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.answer == "Here are the files"


def test_translate_tool_error_status_captures_last_tool_error() -> None:
    """ToolUse error status populates state.last_tool_error."""
    from untether.model import Action

    state = OpenCodeStreamState(session_id="ses_test")
    state.pending_actions["tool_1"] = Action(
        id="tool_1", kind="tool", title="read", detail={}
    )
    event = opencode_schema.ToolUse(
        part={
            "id": "tool_1",
            "state": {
                "status": "error",
                "error": "ENOENT: /nonexistent/path.txt",
            },
        },
    )
    translate_opencode_event(event, title="opencode", state=state)
    assert state.last_tool_error == "ENOENT: /nonexistent/path.txt"


def test_stream_end_saw_step_finish_no_text_falls_back_to_tool_error() -> None:
    """stream_end_events with saw_step_finish and no text uses last_tool_error."""
    runner = OpenCodeRunner(opencode_cmd="opencode")
    session = ResumeToken(engine=ENGINE, value="ses_test")
    state = OpenCodeStreamState(saw_step_finish=True)
    state.last_tool_error = "permission denied"
    events = runner.stream_end_events(resume=None, found_session=session, state=state)
    completed = next(e for e in events if isinstance(e, CompletedEvent))
    assert completed.answer == "permission denied"
