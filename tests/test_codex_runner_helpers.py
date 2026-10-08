from __future__ import annotations

from pathlib import Path

import pytest

from untether.backends import EngineConfig
from untether.config import ConfigError
from untether.events import EventFactory
from untether.model import ActionEvent, CompletedEvent, StartedEvent
from untether.runners.codex import (
    CodexRunner,
    _AgentMessageSummary,
    _format_change_summary,
    _normalize_change_list,
    _parse_reconnect_message,
    _select_final_answer,
    _short_tool_name,
    _summarize_todo_list,
    _summarize_tool_result,
    _todo_title,
    build_runner,
    find_exec_only_flag,
    translate_codex_event,
)
from untether.schemas import codex as codex_schema


def test_codex_helper_functions() -> None:
    assert find_exec_only_flag(["--json"]) == "--json"
    assert find_exec_only_flag(["--output-schema=foo"]) == "--output-schema=foo"
    assert find_exec_only_flag(["--model", "gpt-4"]) is None

    assert _parse_reconnect_message("Reconnecting... 2/5") == (2, 5)
    assert _parse_reconnect_message("Reconnecting... x/y") is None
    assert _parse_reconnect_message("nope") is None

    assert _short_tool_name("docs", "search") == "docs.search"
    assert _short_tool_name(None, "search") == "search"
    assert _short_tool_name(None, None) == "tool"

    summary = _summarize_tool_result({"content": ["hi"], "structured": {"ok": True}})
    assert summary == {"content_blocks": 1, "has_structured": True}
    summary = _summarize_tool_result({"content": "hello", "structured_content": None})
    assert summary == {"content_blocks": 1, "has_structured": False}
    assert _summarize_tool_result({"other": 1}) is None

    changes = [
        codex_schema.FileUpdateChange(path="a.txt", kind="update"),
        {"path": "b.txt", "kind": "delete"},
        {"path": ""},
    ]
    assert _normalize_change_list(changes) == [
        {"path": "a.txt", "kind": "update"},
        {"path": "b.txt", "kind": "delete"},
    ]
    assert _format_change_summary(changes) == "a.txt, b.txt"
    assert _format_change_summary([{"path": ""}]) == "1 files"


def test_summarize_todo_list_and_title() -> None:
    items = [
        codex_schema.TodoItem(text="first", completed=True),
        codex_schema.TodoItem(text="next", completed=False),
        {"text": "later", "completed": False},
    ]
    summary = _summarize_todo_list(items)
    assert summary.done == 1
    assert summary.total == 3
    assert summary.next_text == "next"
    assert _todo_title(summary) == "todo 1/3: next"

    done_summary = _summarize_todo_list([{"text": "done", "completed": True}])
    assert _todo_title(done_summary) == "todo 1/1: done"
    assert _todo_title(_summarize_todo_list("nope")) == "todo"


def test_select_final_answer() -> None:
    assert (
        _select_final_answer(
            [
                _AgentMessageSummary(text="working", phase="commentary"),
                _AgentMessageSummary(text="done", phase="final_answer"),
            ]
        )
        == "done"
    )

    assert (
        _select_final_answer(
            [
                _AgentMessageSummary(text="first", phase=None),
                _AgentMessageSummary(text="second", phase=None),
            ]
        )
        == "second"
    )

    assert (
        _select_final_answer([_AgentMessageSummary(text="working", phase="commentary")])
        is None
    )
    assert (
        _select_final_answer(
            [_AgentMessageSummary(text="intermediate", phase="foobar")]
        )
        is None
    )


def test_translate_codex_events_for_items() -> None:
    factory = EventFactory("codex")
    event = codex_schema.ItemStarted(
        item=codex_schema.WebSearchItem(id="w1", query="query")
    )
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert len(out) == 1
    assert isinstance(out[0], ActionEvent)
    assert out[0].action.kind == "web_search"
    assert out[0].phase == "started"

    event = codex_schema.ItemCompleted(
        item=codex_schema.WebSearchItem(id="w1", query="query")
    )
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert isinstance(out[0], ActionEvent)
    assert out[0].phase == "completed"
    assert out[0].ok is True

    event = codex_schema.ItemStarted(
        item=codex_schema.ReasoningItem(id="r1", text="thinking")
    )
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert isinstance(out[0], ActionEvent)
    assert out[0].action.kind == "note"
    assert out[0].action.title == "thinking"

    event = codex_schema.ItemCompleted(
        item=codex_schema.AgentMessageItem(
            id="m1",
            text="working",
            phase="commentary",
        )
    )
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert isinstance(out[0], ActionEvent)
    assert out[0].action.kind == "note"
    assert out[0].action.title == "working"
    assert out[0].phase == "completed"
    assert out[0].ok is True

    event = codex_schema.ItemUpdated(
        item=codex_schema.TodoListItem(
            id="t1",
            items=[
                codex_schema.TodoItem(text="todo one", completed=False),
                codex_schema.TodoItem(text="todo two", completed=True),
            ],
        )
    )
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert isinstance(out[0], ActionEvent)
    assert out[0].action.detail["done"] == 1
    assert out[0].action.detail["total"] == 2
    assert "todo 1/2" in out[0].action.title

    started = codex_schema.ItemStarted(
        item=codex_schema.ErrorItem(id="e1", message="boom")
    )
    assert translate_codex_event(started, title="Codex", factory=factory) == []

    completed = codex_schema.ItemCompleted(
        item=codex_schema.ErrorItem(id="e1", message="boom")
    )
    out = translate_codex_event(completed, title="Codex", factory=factory)
    assert isinstance(out[0], ActionEvent)
    assert out[0].action.kind == "warning"
    # #987: a non-fatal warning, not a failed step.
    assert out[0].ok is True
    assert out[0].level == "warning"
    assert out[0].action.title == "⚠️ boom"
    assert out[0].message == "boom"


def test_translate_codex_thread_started() -> None:
    factory = EventFactory("codex")
    event = codex_schema.ThreadStarted(thread_id="sess-1")
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert len(out) == 1
    assert isinstance(out[0], StartedEvent)
    assert out[0].resume.value == "sess-1"


def test_translate_codex_thread_started_with_meta() -> None:
    factory = EventFactory("codex")
    event = codex_schema.ThreadStarted(thread_id="sess-2")
    meta = {"model": "o3-pro"}
    out = translate_codex_event(event, title="Codex", factory=factory, meta=meta)
    assert len(out) == 1
    assert isinstance(out[0], StartedEvent)
    assert out[0].meta == {"model": "o3-pro"}


def test_translate_codex_thread_started_no_meta() -> None:
    factory = EventFactory("codex")
    event = codex_schema.ThreadStarted(thread_id="sess-3")
    out = translate_codex_event(event, title="Codex", factory=factory)
    assert len(out) == 1
    assert isinstance(out[0], StartedEvent)
    assert out[0].meta is None


def test_codex_runner_translate_reconnect_message() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    event = codex_schema.StreamError(message="Reconnecting... 2/3")
    out = runner.translate(event, state=state, resume=None, found_session=None)
    assert len(out) == 1
    assert isinstance(out[0], ActionEvent)
    assert out[0].phase == "updated"
    assert out[0].action.detail["attempt"] == 2
    assert out[0].action.detail["max"] == 3


# Captured from codex 0.160 with a `[project]` table in ~/.codex/config.toml:
# the same warning arrives twice under two item ids.
_CONFIG_WARNING_987 = (
    "Codex is ignoring 1 unrecognized configuration setting. Check for typos or"
    " deprecated settings.\n  user (/home/u/.codex/config.toml): `project` is"
    " ignored."
)


def test_987_config_warning_renders_once_as_warning() -> None:
    """#987: Codex's non-fatal config warning is one ⚠️ row, not two ✗ rows."""
    import json

    from untether.markdown import format_action_line

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    lines = [
        {"type": "thread.started", "thread_id": "t-987"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "error", "message": _CONFIG_WARNING_987},
        },
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "error", "message": _CONFIG_WARNING_987},
        },
        {"type": "turn.started"},
    ]
    events = []
    for line in lines:
        data = codex_schema.decode_event(json.dumps(line))
        events.extend(
            runner.translate(data, state=state, resume=None, found_session=None)
        )

    warnings = [
        e for e in events if isinstance(e, ActionEvent) and e.action.kind == "warning"
    ]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.ok is True
    assert warning.level == "warning"
    rendered = format_action_line(
        warning.action, warning.phase, warning.ok, command_width=200
    )
    assert rendered.startswith("⚠️ Codex is ignoring")
    assert "✗" not in rendered


def test_987_distinct_warnings_both_render() -> None:
    """#987: only an identical repeat is dropped."""
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    out = []
    for i, msg in enumerate(["first warning", "second warning", "first warning"]):
        data = codex_schema.ItemCompleted(
            item=codex_schema.ErrorItem(id=f"item_{i}", message=msg)
        )
        out.extend(runner.translate(data, state=state, resume=None, found_session=None))
    assert [e.message for e in out if isinstance(e, ActionEvent)] == [
        "first warning",
        "second warning",
    ]


def test_codex_runner_process_and_stream_end_events() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)

    out = runner.process_error_events(2, resume=None, found_session=None, state=state)
    assert len(out) == 2
    completed = out[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False

    end = runner.stream_end_events(resume=None, found_session=None, state=state)
    assert len(end) == 1
    end_event = end[0]
    assert isinstance(end_event, CompletedEvent)
    assert end_event.ok is False

    started = translate_codex_event(
        codex_schema.ThreadStarted(thread_id="sess-2"),
        title="Codex",
        factory=EventFactory("codex"),
    )[0]
    assert isinstance(started, StartedEvent)
    end = runner.stream_end_events(
        resume=None,
        found_session=started.resume,
        state=state,
    )
    end_event = end[0]
    assert isinstance(end_event, CompletedEvent)
    assert end_event.ok is True


def test_codex_build_runner_configs(tmp_path: Path) -> None:
    cfg: EngineConfig = {}
    runner = build_runner(cfg, tmp_path)
    assert isinstance(runner, CodexRunner)
    assert runner.extra_args == ["-c", "notify=[]"]

    cfg = {"extra_args": ["--foo"], "profile": "Demo"}
    runner = build_runner(cfg, tmp_path)
    assert isinstance(runner, CodexRunner)
    assert runner.extra_args[-2:] == ["--profile", "Demo"]
    assert runner.session_title == "Demo"

    with pytest.raises(ConfigError):
        build_runner({"extra_args": ["--json"]}, tmp_path)

    with pytest.raises(ConfigError):
        build_runner({"extra_args": ["--foo", 1]}, tmp_path)

    with pytest.raises(ConfigError):
        build_runner({"profile": 123}, tmp_path)


# --- #830: safe-mode footer + argv-rejection diagnostics ---


def test_codex_meta_permission_mode_safe_unchanged() -> None:
    from untether.runners.run_options import EngineRunOptions, apply_run_options

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    with apply_run_options(EngineRunOptions(permission_mode="safe")):
        out = runner.translate(
            codex_schema.ThreadStarted(thread_id="sess-safe"),
            state=state,
            resume=None,
            found_session=None,
        )
    started = out[0]
    assert isinstance(started, StartedEvent)
    assert started.meta is not None
    assert started.meta["permissionMode"] == "safe"


_CLAP_UNTRUSTED = [
    "error: invalid value 'untrusted' for '--ask-for-approval <APPROVAL_POLICY>'",
    "  [possible values: on-request, never]",
]


def test_process_error_events_logs_argv_rejected() -> None:
    from structlog.testing import capture_logs

    from untether.runners.run_options import EngineRunOptions, apply_run_options

    runner = CodexRunner(codex_cmd="codex", extra_args=["-c", "notify=[]"])
    state = runner.new_state("hi", None)
    with apply_run_options(EngineRunOptions(permission_mode="safe")):
        runner.build_args("hi", None, state=state)
    with capture_logs() as logs:
        out = runner.process_error_events(
            2,
            resume=None,
            found_session=None,
            state=state,
            stderr_lines=list(_CLAP_UNTRUSTED),
        )
    events = [e["event"] for e in logs]
    assert "codex.argv.rejected" in events
    assert "codex.process.failed" in events
    rejected = next(e for e in logs if e["event"] == "codex.argv.rejected")
    assert rejected["log_level"] == "error"
    assert rejected["rc"] == 2
    assert rejected["first_error_line"].startswith("error: invalid value 'untrusted'")
    assert rejected["args"][:2] == ["-c", "notify=[]"]
    completed = out[-1]
    assert isinstance(completed, CompletedEvent)
    assert "invalid value 'untrusted'" in (completed.error or "")


def test_process_error_events_argv_rejected_unexpected_argument() -> None:
    from structlog.testing import capture_logs

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    with capture_logs() as logs:
        runner.process_error_events(
            2,
            resume=None,
            found_session=None,
            state=state,
            stderr_lines=["error: unexpected argument '--full-auto' found"],
        )
    rejected = [e for e in logs if e["event"] == "codex.argv.rejected"]
    assert len(rejected) == 1
    assert rejected[0]["args"] is None  # build_args never ran for this state


def test_process_error_events_no_argv_event_for_api_errors() -> None:
    from structlog.testing import capture_logs

    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    with capture_logs() as logs:
        runner.process_error_events(
            1,
            resume=None,
            found_session=None,
            state=state,
            stderr_lines=["stream error: 500"],
        )
        # rc=2 without a clap line is not an argv rejection either.
        runner.process_error_events(
            2,
            resume=None,
            found_session=None,
            state=state,
            stderr_lines=["stream error: 500"],
        )
    events = [e["event"] for e in logs]
    assert "codex.argv.rejected" not in events
    assert events.count("codex.process.failed") == 2


# --- #419: web-search titles + five usage fields -----------------------------


def _ws(phase: str, **fields: object) -> codex_schema.ThreadEvent:
    item = codex_schema.WebSearchItem(**fields)  # type: ignore[arg-type]
    if phase == "started":
        return codex_schema.ItemStarted(item=item)
    return codex_schema.ItemCompleted(item=item)


def _translate_one(event: codex_schema.ThreadEvent) -> ActionEvent:
    out = translate_codex_event(event, title="Codex", factory=EventFactory("codex"))
    assert len(out) == 1
    assert isinstance(out[0], ActionEvent)
    return out[0]


def test_web_search_started_empty_query_placeholder() -> None:
    evt = _translate_one(
        _ws("started", id="ws_abc", query="", action={"type": "other"})
    )
    assert evt.phase == "started"
    assert evt.action.title == "web search"
    assert evt.action.detail["action_type"] == "other"
    assert evt.action.detail["query"] == ""


@pytest.mark.parametrize(
    ("fields", "title", "action_type"),
    [
        (
            {"query": "", "action": {"type": "search", "query": "codex release"}},
            "codex release",
            "search",
        ),
        (
            {"query": "", "action": {"type": "search", "queries": ["a", "b"]}},
            "a · b",
            "search",
        ),
        (
            {
                "query": "",
                "action": {"type": "search", "queries": ["a", "b", "c", "d", "e"]},
            },
            "a · b · c (+2 more)",
            "search",
        ),
        (
            {"query": "", "action": {"type": "open_page", "url": "https://x.io/r"}},
            "https://x.io/r",
            "open_page",
        ),
        (
            {
                "query": "",
                "action": {
                    "type": "find_in_page",
                    "url": "https://x.io/r",
                    "pattern": "pat",
                },
            },
            '"pat" in https://x.io/r',
            "find_in_page",
        ),
        ({"query": "legacy query"}, "legacy query", "search"),
        ({"query": "", "action": {"type": "brand_new"}}, "web search", "other"),
    ],
)
def test_web_search_completed_title_from_action(
    fields: dict[str, object], title: str, action_type: str
) -> None:
    evt = _translate_one(_ws("completed", id="ws", **fields))
    assert evt.phase == "completed"
    assert evt.ok is True
    assert evt.action.title == title
    assert evt.action.detail["action_type"] == action_type


def test_web_search_started_and_completed_share_action_id() -> None:
    started = _translate_one(
        _ws("started", id="ws_abc", query="", action={"type": "other"})
    )
    completed = _translate_one(
        _ws(
            "completed",
            id="ws_abc",
            query="q",
            action={"type": "search", "query": "q"},
        )
    )
    assert started.action.id == completed.action.id == "ws_abc"


def test_web_search_detail_has_result_count_not_results() -> None:
    evt = _translate_one(
        _ws(
            "completed",
            id="ws",
            query="q",
            action={"type": "open_page", "url": "https://x.io"},
            results=[{"url": "https://x.io", "content": "big"}, {"url": "y"}],
        )
    )
    assert evt.action.detail["result_count"] == 2
    assert evt.action.detail["url"] == "https://x.io"
    assert "results" not in evt.action.detail


def test_turn_completed_usage_carries_five_fields() -> None:
    runner = CodexRunner(codex_cmd="codex", extra_args=[])
    state = runner.new_state("hi", None)
    event = codex_schema.TurnCompleted(
        usage=codex_schema.Usage(
            input_tokens=100,
            cached_input_tokens=40,
            output_tokens=20,
            cache_write_input_tokens=5,
            reasoning_output_tokens=7,
        )
    )
    out = runner.translate(event, state=state, resume=None, found_session=None)
    completed = out[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.usage is not None
    assert completed.usage["reasoning_output_tokens"] == 7
    assert completed.usage["cache_write_input_tokens"] == 5
