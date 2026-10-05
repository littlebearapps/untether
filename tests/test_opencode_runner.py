import json
from pathlib import Path

import anyio
import msgspec
import pytest

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners.opencode import (
    ENGINE,
    OpenCodeRunner,
    OpenCodeStreamState,
    _read_opencode_default_model,
    build_runner,
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


def test_opencode_two_runs_through_real_translation() -> None:
    """#417: the real nested OpenCode usage shape accumulates as ``per_run``
    in the session ledger (two runs of the success fixture)."""
    from untether import runner_bridge as rb
    from untether.session_costs import get_session_cost_ledger

    sid = "ses_417_two_runs"
    for i in range(2):
        state = OpenCodeStreamState()
        events: list = []
        for event in _load_fixture("opencode_stream_success.jsonl"):
            events.extend(
                translate_opencode_event(event, title="opencode", state=state)
            )
        completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
        usage = completed.usage
        assert usage is not None
        out = rb._apply_token_delta("opencode", sid, usage, resumed=i > 0)
        assert out is not None
        assert out["usage"] == usage["usage"]
        assert "session_total_usage" in out
    tokens = get_session_cost_ledger().session_tokens("opencode", sid)
    assert tokens is not None
    assert tokens.totals["input_tokens"] == 44886
    assert tokens.totals["cache_read_tokens"] == 42830
    assert tokens.totals["output_tokens"] == 236
    assert tokens.runs == 2


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
                "part": {"id": "prt_1", "type": "text", "text": "Hello"},
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
                "part": {"id": "prt_2", "type": "text", "text": "World"},
            }
        ),
        title="opencode",
        state=state,
    )

    # #955: distinct text parts are separated, never glued together.
    assert state.last_text == "Hello\n\nWorld"

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
    assert completed.answer == "Hello\n\nWorld"
    assert completed.ok is True
    assert completed.usage is not None
    assert completed.usage["total_cost_usd"] == 0.005
    assert completed.usage["usage"]["input_tokens"] == 100
    assert completed.usage["usage"]["output_tokens"] == 10


def _text_event(text: str, part_id: str | None) -> opencode_schema.OpenCodeEvent:
    part: dict = {"type": "text", "text": text}
    if part_id is not None:
        part["id"] = part_id
    return _decode_event({"type": "text", "sessionID": "ses_t955", "part": part})


def test_translate_text_parts_across_tool_call_keep_separator() -> None:
    """#955: text, a tool call, then more text must not run together."""
    state = OpenCodeStreamState(session_id="ses_t955", emitted_started=True)
    translate_opencode_event(
        _text_event("I'll read CLAUDE.md.", "prt_a"), title="opencode", state=state
    )
    translate_opencode_event(
        _decode_event(
            {
                "type": "tool_use",
                "sessionID": "ses_t955",
                "part": {
                    "id": "prt_tool",
                    "callID": "call_1",
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": "CLAUDE.md"},
                        "output": "...",
                    },
                },
            }
        ),
        title="opencode",
        state=state,
    )
    translate_opencode_event(
        _text_event("Files listed below.", "prt_b"), title="opencode", state=state
    )
    events = translate_opencode_event(
        _decode_event(
            {"type": "step_finish", "sessionID": "ses_t955", "part": {"reason": "stop"}}
        ),
        title="opencode",
        state=state,
    )
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert "CLAUDE.md.Files" not in completed.answer
    assert completed.answer == "I'll read CLAUDE.md.\n\nFiles listed below."


def test_translate_repeated_text_part_id_replaces_not_duplicates() -> None:
    """#955: a re-emitted part (same part.id) updates in place, no duplicate."""
    state = OpenCodeStreamState(session_id="ses_t955", emitted_started=True)
    translate_opencode_event(
        _text_event("Draft", "prt_a"), title="opencode", state=state
    )
    translate_opencode_event(
        _text_event("Draft, revised", "prt_a"), title="opencode", state=state
    )
    translate_opencode_event(
        _text_event("Tail", "prt_b"), title="opencode", state=state
    )
    assert state.last_text == "Draft, revised\n\nTail"


def test_translate_text_parts_without_id_are_separated() -> None:
    """#955: parts with no id are still treated as distinct parts."""
    state = OpenCodeStreamState(session_id="ses_t955", emitted_started=True)
    translate_opencode_event(_text_event("One.", None), title="opencode", state=state)
    translate_opencode_event(_text_event("Two.", None), title="opencode", state=state)
    assert state.last_text == "One.\n\nTwo."


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


def test_translate_zero_cost_with_tokens_renders_usage() -> None:
    """#316: OpenCode must not hide token usage when cost is zero (free tier)."""
    state = OpenCodeStreamState()
    state.session_id = "ses_freetier"
    state.emitted_started = True

    events = translate_opencode_event(
        _decode_event(
            {
                "type": "step_finish",
                "sessionID": "ses_freetier",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 100, "output": 10},
                    "cost": 0.0,
                },
            }
        ),
        title="opencode",
        state=state,
    )

    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.usage is not None
    # Cost omitted when zero, but tokens must still render.
    assert "total_cost_usd" not in completed.usage
    assert completed.usage["usage"]["input_tokens"] == 100
    assert completed.usage["usage"]["output_tokens"] == 10


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
        await anyio.lowlevel.checkpoint()
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


# ---------------------------------------------------------------------------
# decode_error_events: unsupported event type visibility (#183)
# ---------------------------------------------------------------------------


class TestDecodeErrorEvents:
    """Verify that unsupported OpenCode event types produce visible warnings."""

    def _runner(self) -> OpenCodeRunner:
        return OpenCodeRunner(opencode_cmd="opencode")

    def test_unsupported_type_emits_warning_event(self) -> None:
        """DecodeError with extractable type produces a visible ActionEvent."""
        runner = self._runner()
        state = OpenCodeStreamState()
        raw = '{"type": "question", "sessionID": "ses_test"}'
        error = msgspec.DecodeError("Invalid type")
        events = runner.decode_error_events(raw=raw, line=raw, error=error, state=state)
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ActionEvent)
        assert "question" in event.message

    def test_unsupported_type_permission(self) -> None:
        """Permission event type also surfaces as warning."""
        runner = self._runner()
        state = OpenCodeStreamState()
        raw = '{"type": "permission", "sessionID": "ses_test"}'
        error = msgspec.DecodeError("Invalid type")
        events = runner.decode_error_events(raw=raw, line=raw, error=error, state=state)
        assert len(events) == 1
        assert isinstance(events[0], ActionEvent)
        assert "permission" in events[0].message

    def test_unextractable_type_returns_empty(self) -> None:
        """DecodeError with no extractable type returns [] (existing behaviour)."""
        runner = self._runner()
        state = OpenCodeStreamState()
        raw = "not valid json at all"
        error = msgspec.DecodeError("Invalid JSON")
        events = runner.decode_error_events(raw=raw, line=raw, error=error, state=state)
        assert events == []

    def test_missing_type_field_returns_empty(self) -> None:
        """Valid JSON but no 'type' field returns []."""
        runner = self._runner()
        state = OpenCodeStreamState()
        raw = '{"sessionID": "ses_test", "data": "something"}'
        error = msgspec.DecodeError("Missing type tag")
        events = runner.decode_error_events(raw=raw, line=raw, error=error, state=state)
        assert events == []

    def test_non_decode_error_delegates_to_super(self) -> None:
        """Non-DecodeError exceptions use the base class handler."""
        runner = self._runner()
        state = OpenCodeStreamState()
        raw = '{"type": "step_start"}'
        error = ValueError("something else")
        events = runner.decode_error_events(raw=raw, line=raw, error=error, state=state)
        assert len(events) == 1
        assert isinstance(events[0], ActionEvent)

    def test_note_seq_increments(self) -> None:
        """Each unsupported event increments note_seq for unique IDs."""
        runner = self._runner()
        state = OpenCodeStreamState()
        raw1 = '{"type": "question"}'
        raw2 = '{"type": "reasoning"}'
        error = msgspec.DecodeError("Invalid")
        e1 = runner.decode_error_events(raw=raw1, line=raw1, error=error, state=state)
        e2 = runner.decode_error_events(raw=raw2, line=raw2, error=error, state=state)
        assert isinstance(e1[0], ActionEvent)
        assert isinstance(e2[0], ActionEvent)
        assert e1[0].action.id != e2[0].action.id
        assert state.note_seq == 2


# --- _read_opencode_default_model tests ---


def test_read_opencode_default_model_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"model": "openai/gpt-5.2"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _read_opencode_default_model() == "openai/gpt-5.2"


def test_read_opencode_default_model_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _read_opencode_default_model() is None


def test_read_opencode_default_model_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text("not valid json")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _read_opencode_default_model() is None


def test_read_opencode_default_model_empty_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"model": ""}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _read_opencode_default_model() is None


def test_read_opencode_default_model_no_model_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"other": "value"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _read_opencode_default_model() is None


def test_build_runner_falls_back_to_opencode_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"model": "openai/gpt-4o"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = build_runner({}, tmp_path / "untether.toml")
    assert runner.model == "openai/gpt-4o"
    assert runner.session_title == "openai/gpt-4o"


def test_build_runner_prefers_untether_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"model": "openai/gpt-4o"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = build_runner(
        {"model": "anthropic/claude-sonnet"}, tmp_path / "untether.toml"
    )
    assert runner.model == "anthropic/claude-sonnet"


def test_build_runner_no_opencode_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = build_runner({}, tmp_path / "untether.toml")
    assert runner.model is None
    assert runner.session_title == "opencode"


# ---------------------------------------------------------------------------
# #970: OpenCode v2 (@opencode/cli) version guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1.14.33\n", (1, 14, 33)),
        ("2.0.23", (2, 0, 23)),
        ("opencode 2.0.23\nextra", (2, 0, 23)),
        ("v1.18.4", (1, 18, 4)),
        ("", None),
        ("not a version", None),
    ],
)
def test_parse_opencode_version(output: str, expected) -> None:
    from untether.runners.opencode import parse_opencode_version

    assert parse_opencode_version(output) == expected


def _fake_opencode_bin(tmp_path: Path) -> Path:
    """A fake ``opencode`` that records each spawn and emits a v1 run."""
    marker = tmp_path / "spawned"
    script = tmp_path / "opencode"
    script.write_text(
        "#!/bin/sh\n"
        f'echo spawn >> "{marker}"\n'
        "echo '"
        '{"type":"step_start","sessionID":"ses_v1ok","part":{}}'
        "'\n"
        "echo '"
        '{"type":"text","sessionID":"ses_v1ok","part":{"id":"p1","text":"ok"}}'
        "'\n"
        "echo '"
        '{"type":"step_finish","sessionID":"ses_v1ok","part":{"reason":"stop"}}'
        "'\n"
    )
    script.chmod(0o755)
    return script


@pytest.mark.anyio
async def test_run_refuses_opencode_v2_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#970: a 2.x CLI fails fast with a clear message and never spawns `run`."""
    from untether.runner import prespawn_blocked_reason
    from untether.runners import opencode as opencode_mod

    script = _fake_opencode_bin(tmp_path)
    monkeypatch.setattr(opencode_mod, "_probe_opencode_version", lambda path: "2.0.23")
    runner = OpenCodeRunner(opencode_cmd=str(script))
    resume = ResumeToken(engine=ENGINE, value="ses_keepme")

    events = [evt async for evt in runner.run("hello", resume)]

    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False
    assert completed.error is not None
    assert "OpenCode 2.0.23" in completed.error
    assert "opencode-ai@1" in completed.error
    assert completed.resume == resume
    # Guard block: the chat's saved session must not be auto-cleared (#838).
    assert prespawn_blocked_reason(completed.usage) == "unsupported_version"
    assert not (tmp_path / "spawned").exists()


@pytest.mark.anyio
async def test_run_allows_opencode_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#970: a 1.x CLI runs as before."""
    from untether.runners import opencode as opencode_mod

    script = _fake_opencode_bin(tmp_path)
    monkeypatch.setattr(opencode_mod, "_probe_opencode_version", lambda path: "1.14.33")
    runner = OpenCodeRunner(opencode_cmd=str(script))

    events = [evt async for evt in runner.run("hello", None)]

    assert isinstance(events[0], StartedEvent)
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].ok is True
    assert events[-1].answer == "ok"
    assert (tmp_path / "spawned").exists()


@pytest.mark.anyio
async def test_run_unknown_opencode_version_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#970: a failed/unparsable probe never blocks a run (logged instead)."""
    from structlog.testing import capture_logs

    from untether.runners import opencode as opencode_mod

    script = _fake_opencode_bin(tmp_path)
    monkeypatch.setattr(opencode_mod, "_probe_opencode_version", lambda path: None)
    runner = OpenCodeRunner(opencode_cmd=str(script))

    with capture_logs() as logs:
        events = [evt async for evt in runner.run("hello", None)]

    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].ok is True
    assert any(e["event"] == "opencode.version.unknown" for e in logs)


def test_opencode_version_probe_cached_per_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#970: one --version probe per (binary, mtime); an upgrade re-probes."""
    import os

    from untether.runners import opencode as opencode_mod

    script = _fake_opencode_bin(tmp_path)
    calls: list[str] = []

    def probe(path: str) -> str:
        calls.append(path)
        return "1.14.33"

    monkeypatch.setattr(opencode_mod, "_probe_opencode_version", probe)
    assert opencode_mod.opencode_cli_version(str(script)) == "1.14.33"
    assert opencode_mod.opencode_cli_version(str(script)) == "1.14.33"
    assert len(calls) == 1

    st = script.stat()
    os.utime(script, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert opencode_mod.opencode_cli_version(str(script)) == "1.14.33"
    assert len(calls) == 2


def test_opencode_version_unresolvable_command_is_unknown() -> None:
    from untether.runners import opencode as opencode_mod

    assert opencode_mod.opencode_cli_version("definitely-not-opencode-xyz") is None


def test_opencode_version_probe_failure_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#970: a failed probe re-probes next time instead of sticking."""
    from untether.runners import opencode as opencode_mod

    script = _fake_opencode_bin(tmp_path)
    results = iter([None, "2.0.23"])
    monkeypatch.setattr(
        opencode_mod, "_probe_opencode_version", lambda path: next(results)
    )
    assert opencode_mod.opencode_cli_version(str(script)) is None
    assert opencode_mod.opencode_cli_version(str(script)) == "2.0.23"
