from pathlib import Path

import msgspec
import pytest

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners.antigravity import (
    ENGINE,
    AntigravityRunner,
    AntigravityStreamState,
    translate_antigravity_event,
)
from untether.schemas import antigravity as antigravity_schema


def _load_fixture(name: str) -> list[antigravity_schema.AntigravityEvent]:
    path = Path(__file__).parent / "fixtures" / name
    events: list[antigravity_schema.AntigravityEvent] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            decoded = antigravity_schema.decode_event(line)
        except Exception as exc:
            raise AssertionError(f"{name} contained unparseable line: {line}") from exc
        events.append(decoded)
    return events


def _decode_event(payload: dict) -> antigravity_schema.AntigravityEvent:
    return antigravity_schema.decode_event(msgspec.json.encode(payload))


def test_antigravity_resume_format_and_extract() -> None:
    runner = AntigravityRunner()
    token = ResumeToken(engine=ENGINE, value="abc123def")

    assert runner.format_resume(token) == "`agy --conversation abc123def`"
    assert runner.extract_resume("agy --conversation xyz789") == ResumeToken(
        engine=ENGINE, value="xyz789"
    )
    assert runner.extract_resume("antigravity --conversation xyz789") == ResumeToken(
        engine=ENGINE, value="xyz789"
    )
    assert runner.extract_resume("`claude --resume sid`") is None
    assert runner.extract_resume("`opencode --session ses_abc`") is None


def test_translate_success_fixture() -> None:
    state = AntigravityStreamState()
    events: list = []
    for event in _load_fixture("antigravity_stream_success.jsonl"):
        events.extend(
            translate_antigravity_event(
                event, title="antigravity", state=state, meta=None
            )
        )

    assert isinstance(events[0], StartedEvent)
    started = events[0]
    assert started.resume.value == "abc123def"
    assert started.resume.engine == ENGINE
    assert started.meta is not None
    assert started.meta["model"] == "gemini-3.8-flash-high"

    action_events = [evt for evt in events if isinstance(evt, ActionEvent)]
    assert len(action_events) == 4

    started_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "started"
    }
    assert started_actions[("1", "started")].action.kind == "command"

    completed_actions = {
        (evt.action.id, evt.phase): evt
        for evt in action_events
        if evt.phase == "completed"
    }
    assert completed_actions[("1", "completed")].ok is True

    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is True
    assert completed.answer == "The command output `hello`."
    assert completed.usage is not None
    assert completed.usage["usage"]["input_tokens"] == 100
    assert completed.usage["usage"]["output_tokens"] == 50
    assert completed.usage["usage"]["thinking_tokens"] == 20
    assert completed.usage["duration_ms"] == 1200


def test_translate_error_fixture() -> None:
    state = AntigravityStreamState()
    events: list = []
    for event in _load_fixture("antigravity_stream_error.jsonl"):
        events.extend(
            translate_antigravity_event(
                event, title="antigravity", state=state, meta=None
            )
        )

    assert isinstance(events[0], StartedEvent)
    completed = next(evt for evt in events if isinstance(evt, CompletedEvent))
    assert completed.ok is False
    assert completed.error == "API key invalid or expired"


def test_translate_accumulates_text() -> None:
    state = AntigravityStreamState(session_id="ses1", emitted_started=True)
    translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "Hello ",
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "world!",
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert state.last_text == "Hello world!"


def test_translate_user_input_ignored() -> None:
    state = AntigravityStreamState(session_id="ses1", emitted_started=True)
    results = translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 0,
                    "state": "DONE",
                    "step_type": "user_input",
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert results == []
    assert state.last_text is None


def test_translate_tool_use_and_result() -> None:
    state = AntigravityStreamState(session_id="ses1", emitted_started=True)
    events = translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "ls -la"},
                    },
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "started"
    assert "1" in state.pending_actions

    events = translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "ls -la"},
                        "output": "files",
                    },
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].phase == "completed"
    assert events[0].ok is True


def test_translate_result_success() -> None:
    state = AntigravityStreamState(
        session_id="ses1", emitted_started=True, last_text="done"
    )
    events = translate_antigravity_event(
        _decode_event(
            {
                "event": "result",
                "result": {
                    "conversation_id": "ses1",
                    "status": "SUCCESS",
                    "response": "done",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is True
    assert completed.answer == "done"


def test_translate_result_error() -> None:
    state = AntigravityStreamState(session_id="ses1", emitted_started=True)
    events = translate_antigravity_event(
        _decode_event(
            {
                "event": "result",
                "result": {
                    "conversation_id": "ses1",
                    "status": "ERROR",
                    "error": "command failed",
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False
    assert completed.error == "command failed"


def test_translate_error_event() -> None:
    state = AntigravityStreamState(session_id="ses1", emitted_started=True)
    events = translate_antigravity_event(
        _decode_event({"event": "error", "message": "something broke"}),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    completed = events[0]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok is False
    assert completed.error == "something broke"


def test_build_args_new_session() -> None:
    runner = AntigravityRunner()
    state = AntigravityStreamState()
    args = runner.build_args("hello world", None, state=state)
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--conversation" not in args
    assert "--continue" not in args
    assert "--dangerously-skip-permissions" in args
    assert "--prompt=hello world" in args


def test_build_args_with_resume() -> None:
    runner = AntigravityRunner()
    state = AntigravityStreamState()
    token = ResumeToken(engine=ENGINE, value="conv123")
    args = runner.build_args("continue", token, state=state)
    assert "--conversation" in args
    assert "conv123" in args


def test_build_args_with_continue() -> None:
    runner = AntigravityRunner()
    state = AntigravityStreamState()
    token = ResumeToken(engine=ENGINE, value="", is_continue=True)
    args = runner.build_args("continue", token, state=state)
    assert "--continue" in args
    assert "--conversation" not in args


def test_build_args_with_model() -> None:
    runner = AntigravityRunner(model="gemini-3.8-flash-high")
    state = AntigravityStreamState()
    args = runner.build_args("hello", None, state=state)
    assert "--model" in args
    assert "gemini-3.8-flash-high" in args


def test_stdin_payload_returns_none() -> None:
    runner = AntigravityRunner()
    state = AntigravityStreamState()
    assert runner.stdin_payload("hello", None, state=state) is None


def test_init_carries_model_meta() -> None:
    state = AntigravityStreamState()
    events = translate_antigravity_event(
        _decode_event(
            {
                "event": "init",
                "conversation_id": "sid1",
                "init": {
                    "model": "gemini-3.8-flash-high",
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    started = events[0]
    assert isinstance(started, StartedEvent)
    assert started.meta is not None
    assert started.meta["model"] == "gemini-3.8-flash-high"


def test_tool_name_mapping() -> None:
    state = AntigravityStreamState(session_id="ses1", emitted_started=True)
    events = translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "tool_info": {
                        "name": "view_file",
                        "parameters": {"AbsolutePath": "/tmp/test.py"},
                    },
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events) == 1
    assert isinstance(events[0], ActionEvent)
    assert events[0].action.kind == "tool"
    assert "test.py" in events[0].action.title

    events2 = translate_antigravity_event(
        _decode_event(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 2,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "write_to_file",
                    "tool_info": {
                        "name": "write_to_file",
                        "parameters": {"TargetFile": "/tmp/foo.py"},
                    },
                },
            }
        ),
        title="antigravity",
        state=state,
        meta=None,
    )
    assert len(events2) == 1
    assert isinstance(events2[0], ActionEvent)
    assert events2[0].action.kind == "file_change"


def test_build_args_approval_mode_from_run_options() -> None:
    from untether.runners.run_options import EngineRunOptions, apply_run_options

    runner = AntigravityRunner()
    state = AntigravityStreamState()
    with apply_run_options(EngineRunOptions(permission_mode="plan")):
        args = runner.build_args("hello", None, state=state)
    assert "--mode" in args
    assert "plan" in args
    assert "--dangerously-skip-permissions" not in args

    with apply_run_options(EngineRunOptions(permission_mode="accept-edits")):
        args = runner.build_args("hello", None, state=state)
    assert "--mode" in args
    assert "accept-edits" in args
    assert "--dangerously-skip-permissions" not in args

    with apply_run_options(EngineRunOptions(permission_mode="auto")):
        args = runner.build_args("hello", None, state=state)
    assert "--mode" not in args
    assert "--dangerously-skip-permissions" in args


def test_build_args_reasoning_effort_from_run_options() -> None:
    from untether.runners.run_options import EngineRunOptions, apply_run_options

    runner = AntigravityRunner()
    state = AntigravityStreamState()
    with apply_run_options(EngineRunOptions(reasoning="high")):
        args = runner.build_args("hello", None, state=state)
    assert "--effort" in args
    assert "high" in args


def test_build_runner_from_config(tmp_path: Path) -> None:
    from untether.runners.antigravity import build_runner

    cfg = {
        "model": "gemini-3.8-flash-high",
        "cmd": "/custom/bin/agy",
        "dangerously_skip_permissions": False,
    }
    runner = build_runner(cfg, tmp_path / "untether.toml")
    assert isinstance(runner, AntigravityRunner)
    assert runner.command() == "/custom/bin/agy"
    assert runner.model == "gemini-3.8-flash-high"
    assert runner.dangerously_skip_permissions is False


def test_default_antigravity_cmd_local_bin(monkeypatch) -> None:
    from untether.runners import antigravity

    fake_home = Path("/fake/home/user")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def fake_exists(self: Path) -> bool:
        return self == fake_home / ".local" / "bin" / "agy"

    monkeypatch.setattr(Path, "exists", fake_exists)
    assert antigravity.default_antigravity_cmd() == "/fake/home/user/.local/bin/agy"


def test_default_antigravity_cmd_fallback_which(monkeypatch) -> None:
    from untether.runners import antigravity

    fake_home = Path("/fake/home/user")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(
        antigravity.shutil,
        "which",
        lambda cmd: "/usr/bin/agy" if cmd == "agy" else None,
    )

    assert antigravity.default_antigravity_cmd() == "/usr/bin/agy"


def test_default_antigravity_cmd_fallback_default(monkeypatch) -> None:
    from untether.runners import antigravity

    fake_home = Path("/fake/home/user")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(antigravity.shutil, "which", lambda _cmd: None)

    assert antigravity.default_antigravity_cmd() == "/fake/home/user/.local/bin/agy"


def test_build_runner_expands_tilde_cmd(tmp_path: Path, monkeypatch) -> None:
    from untether.runners.antigravity import AntigravityRunner, build_runner

    fake_home = "/fake/home/user"
    monkeypatch.setenv("HOME", fake_home)

    cfg = {"cmd": "~/.local/bin/agy"}
    runner = build_runner(cfg, tmp_path / "untether.toml")
    assert isinstance(runner, AntigravityRunner)
    assert runner.command() == f"{fake_home}/.local/bin/agy"


def test_build_runner_omitted_cmd_uses_default(tmp_path: Path) -> None:
    from untether.runners.antigravity import (
        AntigravityRunner,
        build_runner,
        default_antigravity_cmd,
    )

    runner = build_runner({}, tmp_path / "untether.toml")
    assert isinstance(runner, AntigravityRunner)
    assert runner.command() == default_antigravity_cmd()


def test_build_runner_invalid_cmd_raises(tmp_path: Path) -> None:
    import pytest

    from untether.config import ConfigError
    from untether.runners.antigravity import build_runner

    with pytest.raises(ConfigError, match=r"Invalid `antigravity\.cmd`"):
        build_runner({"cmd": 12345}, tmp_path / "untether.toml")


def test_decode_command_result_event() -> None:
    raw = b'{"event":"command_result","command":{"name":"usage","data":{"groups":[{"name":"Gemini"}]}}}'
    evt = antigravity_schema.decode_event(raw)
    assert isinstance(evt, antigravity_schema.CommandResult)
    assert evt.command is not None
    assert evt.command.name == "usage"
    assert evt.command.data == {"groups": [{"name": "Gemini"}]}


def test_translate_command_result_event() -> None:
    state = AntigravityStreamState()
    raw = b'{"event":"command_result","command":{"name":"usage","data":{"groups":[{"name":"Gemini"}]}}}'
    evt = antigravity_schema.decode_event(raw)
    events = translate_antigravity_event(
        evt, title="antigravity", state=state, meta=None
    )

    assert len(events) == 1
    assert isinstance(events[0], StartedEvent)
    assert state.command_usage == {"groups": [{"name": "Gemini"}]}


def test_format_antigravity_usage() -> None:
    from untether.telegram.commands.usage import format_antigravity_usage

    data = {
        "groups": [
            {
                "name": "Gemini Models",
                "buckets": [
                    {
                        "name": "Five Hour Limit Remaining",
                        "remaining_fraction": 0.4,
                        "reset_time": "2030-01-01T00:00:00Z",
                    },
                    {
                        "name": "Weekly Limit Remaining",
                        "remaining_fraction": 0.7,
                        "reset_time": "2030-01-08T00:00:00Z",
                    },
                ],
            }
        ]
    }
    formatted = format_antigravity_usage(data)
    assert "Gemini Models" in formatted
    assert "Five Hour Limit Remaining" in formatted
    assert "60%" in formatted
    assert "40% left" in formatted


@pytest.mark.anyio
async def test_fetch_antigravity_usage_parsing(monkeypatch) -> None:
    from untether.telegram.commands.usage import fetch_antigravity_usage

    output_lines = [
        '{"event":"command_result","command":{"name":"usage","data":{"groups":[{"name":"Gemini Models","buckets":[{"name":"Five Hour Limit Remaining","window":"5h","remaining_fraction":0.3,"reset_time":"2030-01-01T00:00:00Z"}]}]}}}\n',
        '{"event":"result","result":{"status":"SUCCESS","response":"ok"}}\n',
    ]

    from dataclasses import dataclass

    import anyio

    @dataclass
    class FakeResult:
        returncode: int = 0
        stdout: bytes = "".join(output_lines).encode("utf-8")
        stderr: bytes = b""

    async def fake_run_process(*a, **kw):
        return FakeResult()

    monkeypatch.setattr(anyio, "run_process", fake_run_process)
    monkeypatch.setattr("shutil.which", lambda _c: "/usr/bin/agy")

    res = await fetch_antigravity_usage(conversation_id="conv123")
    assert res["engine"] == "antigravity"
    assert len(res["groups"]) == 1
    assert res["five_hour"] == {
        "utilization": 70.0,
        "resets_at": "2030-01-01T00:00:00Z",
    }


def test_decode_jsonl_brace_salvage() -> None:
    runner = AntigravityRunner()
    line = b'Warning: arbitrary prefix warning text\n{"event":"result","result":{"status":"SUCCESS","response":"ok"}}'
    ev = runner.decode_jsonl(line=line)
    assert isinstance(ev, antigravity_schema.AntigravityResult)
    assert ev.result is not None
    assert ev.result.status == "SUCCESS"


def test_decode_jsonl_invalid_raises() -> None:
    runner = AntigravityRunner()
    with pytest.raises(msgspec.DecodeError):
        runner.decode_jsonl(line=b"completely non-json output without braces")
