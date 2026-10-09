"""Antigravity (agy) runner: pure tests — resume, argv, stdin, translate,
usage, env, build_runner, version guard (#558, #976).

Translate tests replay the real agy 1.3.x captures in
``tests/fixtures/antigravity/`` (see ``test_antigravity_schema.py`` for
provenance). Subprocess tests through ``run()`` live in
``test_antigravity_subprocess.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runners import antigravity as agy
from untether.runners.antigravity import (
    CONVERSATION_GONE_TEXT,
    ENGINE,
    AntigravityRunner,
    AntigravityStreamState,
)
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as schema

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
ALL_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*.jsonl"))


def _runner(**kwargs: Any) -> AntigravityRunner:
    kwargs.setdefault("antigravity_cmd", "agy")
    return AntigravityRunner(**kwargs)


def _replay(
    name: str,
    *,
    resume: ResumeToken | None = None,
    runner: AntigravityRunner | None = None,
    lines: list[str] | None = None,
) -> tuple[list[Any], AntigravityStreamState]:
    runner = runner or _runner()
    state = runner.new_state("prompt", resume)
    runner.start_run("prompt", resume, state=state)
    if lines is None:
        lines = [
            line
            for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()
            if line.strip()
        ]
    events: list[Any] = []
    for line in lines:
        events.extend(
            runner.translate(
                schema.decode_event(line),
                state=state,
                resume=resume,
                found_session=None,
            )
        )
        if events and isinstance(events[-1], CompletedEvent):
            break
    return events, state


def _completed(events: list[Any]) -> CompletedEvent:
    done = [e for e in events if isinstance(e, CompletedEvent)]
    assert len(done) == 1, events
    return done[0]


def _actions(events: list[Any]) -> list[ActionEvent]:
    return [e for e in events if isinstance(e, ActionEvent)]


# ── resume ──────────────────────────────────────────────────────────────────


def test_resume_format_and_extract() -> None:
    runner = _runner()
    token = ResumeToken(engine=ENGINE, value="90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d")
    line = runner.format_resume(token)
    assert line == "`agy --conversation 90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d`"
    assert runner.extract_resume(line) == token
    assert runner.extract_resume("antigravity --conversation abcdef12") == ResumeToken(
        engine=ENGINE, value="abcdef12"
    )


def test_resume_regex_ignores_other_engines() -> None:
    runner = _runner()
    assert runner.extract_resume("`claude --resume sid`") is None
    assert runner.extract_resume("`opencode --session ses_abc`") is None
    assert runner.extract_resume("`gemini --resume abcdef12`") is None
    assert runner.extract_resume("agy --conversation short") is None  # < 8 chars


def test_format_resume_rejects_other_engine() -> None:
    with pytest.raises(RuntimeError):
        _runner().format_resume(ResumeToken(engine="codex", value="x"))


# ── argv / stdin ────────────────────────────────────────────────────────────


def _args(
    resume: ResumeToken | None = None,
    options: EngineRunOptions | None = None,
    **kwargs: Any,
) -> list[str]:
    runner = _runner(**kwargs)
    state = runner.new_state("do the thing", resume)
    with apply_run_options(options):
        return runner.build_args("do the thing", resume, state=state)


def test_build_args_stream_json_no_prompt_in_argv() -> None:
    args = _args()
    assert args[:4] == [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    assert "do the thing" not in " ".join(args)


def test_build_args_never_contains_print_or_prompt_flags() -> None:
    for args in (_args(), _args(ResumeToken(engine=ENGINE, value="conv-1234"))):
        assert "-p" not in args
        assert "--print" not in args
        assert not any(a.startswith("--prompt") for a in args)
        assert "--dangerously-skip-permissions" not in args


def test_build_args_always_disables_slash_commands_and_print_timeout_zero() -> None:
    args = _args()
    assert "--disable-slash-commands" in args
    idx = args.index("--print-timeout")
    assert args[idx + 1] == "0"


def test_build_args_default_never_bypasses_for_any_mode() -> None:
    for mode in (None, "full", "auto", "bypassPermissions", "plan", "default"):
        args = _args(options=EngineRunOptions(permission_mode=mode))
        assert "--dangerously-skip-permissions" not in args


def test_build_args_resume_uses_conversation() -> None:
    args = _args(ResumeToken(engine=ENGINE, value="conv-1234"))
    idx = args.index("--conversation")
    assert args[idx + 1] == "conv-1234"
    assert "--continue" not in args


def test_build_args_continue_checked_before_value() -> None:
    args = _args(ResumeToken(engine=ENGINE, value="", is_continue=True))
    assert "--continue" in args
    assert "--conversation" not in args
    assert "" not in args


def test_build_args_model_from_run_options_overrides_config() -> None:
    assert "--model" not in _args()
    args = _args(model="gemini-3.8-flash")
    assert args[args.index("--model") + 1] == "gemini-3.8-flash"
    args = _args(
        options=EngineRunOptions(model="gemini-3.1-pro"), model="gemini-3.8-flash"
    )
    assert args[args.index("--model") + 1] == "gemini-3.1-pro"


def test_build_args_stores_argv_on_state() -> None:
    runner = _runner()
    state = runner.new_state("p", None)
    args = runner.build_args("p", None, state=state)
    assert state.argv == args


def test_stdin_payload_is_single_user_event() -> None:
    import json

    runner = _runner()
    state = runner.new_state("héllo", None)
    payload = runner.stdin_payload("héllo", None, state=state)
    assert payload is not None and payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert json.loads(payload) == {"event": "user", "message": {"content": "héllo"}}
    assert "héllo".encode() in payload  # ensure_ascii=False


def test_stdin_payload_leading_dash_prompt_verbatim() -> None:
    import json

    runner = _runner()
    payload = runner.stdin_payload("--help me", None, state=None)
    assert json.loads(payload or b"")["message"]["content"] == "--help me"


def test_stdin_payload_slash_prompt() -> None:
    """P22 (agy 1.3.2): with --disable-slash-commands a ``/``-leading stdin
    prompt is a normal turn, so it is sent verbatim (no ``Task:`` prefix)."""
    import json

    runner = _runner()
    payload = runner.stdin_payload("/review the README", None, state=None)
    assert json.loads(payload or b"")["message"]["content"] == "/review the README"
    assert "--disable-slash-commands" in _args()


# ── translate ───────────────────────────────────────────────────────────────


def test_translate_ok_fixture() -> None:
    events, state = _replay("ok")
    assert isinstance(events[0], StartedEvent)
    assert events[0].resume == ResumeToken(
        engine=ENGINE, value="90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"
    )
    assert events[0].title == "antigravity"
    done = _completed(events)
    assert events[-1] is done
    assert done.ok is True
    assert done.answer == "OK\n"
    assert done.resume == events[0].resume
    assert state.session_id == "90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"


def test_translate_resume_after_interrupt_session_matches() -> None:
    resume = ResumeToken(engine=ENGINE, value="b66a64cf-dd95-4344-9df6-25d35539e10f")
    events, _ = _replay("resume_after_interrupt", resume=resume)
    started = [e for e in events if isinstance(e, StartedEvent)]
    assert len(started) == 1 and started[0].resume == resume
    done = _completed(events)
    assert done.ok is True and done.answer == "RESUMED\n"


def test_translate_no_started_for_empty_conversation_id() -> None:
    for name in ("unauth", "invalid_model"):
        with structlog.testing.capture_logs():
            events, _ = _replay(name)
        assert not any(isinstance(e, StartedEvent) for e in events), name
        done = _completed(events)
        assert done.ok is False
        assert done.resume is None
    lines = ['{"event":"init","conversation_id":"","init":{}}']
    with structlog.testing.capture_logs() as logs:
        events, _ = _replay("", lines=lines)
    assert events == []
    assert any(e["event"] == "antigravity.init.no_conversation_id" for e in logs)


def test_translate_init_id_mismatch_is_conversation_gone() -> None:
    resume = ResumeToken(engine=ENGINE, value="00000000-1111-2222-3333-444444444444")
    lines = (FIXTURES / "unknown_conversation.jsonl").read_text().splitlines()
    init = (
        '{"event":"init","conversation_id":"65f94e86-51cc-46d0-8aee-97c679364d8c",'
        '"init":{"cwd":"<scratch>"}}'
    )
    events, state = _replay("", resume=resume, lines=[init, *lines])
    assert not any(isinstance(e, StartedEvent) for e in events)
    done = _completed(events)
    assert done.ok is False
    assert "antigravity conversation not found" in (done.error or "").lower()
    assert done.error == CONVERSATION_GONE_TEXT
    assert done.resume == resume
    assert state.conversation_missing is True


def test_translate_step_id_mismatch_is_conversation_gone() -> None:
    """The capture has no init line (stdin closed before it): the first
    step's conversation id is just as conclusive."""
    resume = ResumeToken(engine=ENGINE, value="00000000-1111-2222-3333-444444444444")
    events, _ = _replay("unknown_conversation", resume=resume)
    assert not any(isinstance(e, StartedEvent) for e in events)
    assert _completed(events).error == CONVERSATION_GONE_TEXT


def test_translate_ignored_step_types_emit_nothing() -> None:
    runner = _runner()
    state = runner.new_state("p", None)
    runner.start_run("p", None, state=state)
    runner.translate(
        schema.decode_event(
            '{"event":"init","conversation_id":"c-12345678","init":{}}'
        ),
        state=state,
        resume=None,
        found_session=None,
    )
    for step_type in (
        "system_message",
        "checkpoint",
        "unknown",
        "user_input",
        "finish",
    ):
        line = (
            '{"event":"step_update","step_update":{"conversation_id":"c-12345678",'
            f'"step_index":3,"state":"DONE","step_type":"{step_type}"}}}}'
        )
        out = runner.translate(
            schema.decode_event(line), state=state, resume=None, found_session=None
        )
        assert out == [], step_type


def test_translate_injected_user_input_not_a_new_run() -> None:
    events, _ = _replay("hook_deny_bypass")
    assert sum(isinstance(e, StartedEvent) for e in events) == 1
    assert _completed(events).ok is True


def test_translate_text_fallback_joins_steps() -> None:
    cid = "c-12345678"
    lines = [
        f'{{"event":"init","conversation_id":"{cid}","init":{{}}}}',
        f'{{"event":"step_update","step_update":{{"conversation_id":"{cid}","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"Hello "}}}}',
        f'{{"event":"step_update","step_update":{{"conversation_id":"{cid}","step_index":1,"state":"DONE","step_type":"agent_response","text_delta":"world"}}}}',
        f'{{"event":"step_update","step_update":{{"conversation_id":"{cid}","step_index":3,"state":"DONE","step_type":"agent_response","text_delta":"Again"}}}}',
        f'{{"event":"result","result":{{"conversation_id":"{cid}","status":"SUCCESS","response":""}}}}',
    ]
    events, _ = _replay("", lines=lines)
    assert _completed(events).answer == "Hello world\n\nAgain"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_translate_every_tool_action_closed(name: str) -> None:
    resume = None
    if name == "resume_after_interrupt":
        resume = ResumeToken(
            engine=ENGINE, value="b66a64cf-dd95-4344-9df6-25d35539e10f"
        )
    with structlog.testing.capture_logs():
        events, state = _replay(name, resume=resume)
    opened: dict[str, int] = {}
    for evt in _actions(events):
        if evt.phase == "started":
            opened[evt.action.id] = opened.get(evt.action.id, 0) + 1
        elif evt.phase == "completed" and evt.action.id in opened:
            opened.pop(evt.action.id)
    if any(isinstance(e, CompletedEvent) for e in events):
        assert opened == {}, name
        assert state.pending_actions == {}
        assert state.verdict_pending == {}


def test_translate_hook_denied_tool_error_ok_false() -> None:
    events, _ = _replay("hook_deny_bypass")
    failed = [e for e in _actions(events) if e.phase == "completed" and e.ok is False]
    assert len(failed) == 1
    assert failed[0].action.kind == "command"
    assert "echo denyme" in failed[0].action.title
    assert (failed[0].message or "").startswith("tool call denied by pre-tool hook")
    assert failed[0].action.detail["error_type"] == "TOOL_ERROR"


def test_translate_soft_denied_done_parked_until_result() -> None:
    lines = [
        line
        for line in (FIXTURES / "tools_denied.jsonl").read_text().splitlines()
        if line.strip()
    ]
    events, state = _replay("", lines=lines[:-1])  # everything before the result
    run_cmd_done = [
        e
        for e in _actions(events)
        if e.phase == "completed" and "probe-shell" in e.action.title
    ]
    assert run_cmd_done == []
    assert list(state.verdict_pending) == ["step-4"]

    events, _ = _replay("tools_denied")
    done = [
        e
        for e in _actions(events)
        if e.phase == "completed" and "probe-shell" in e.action.title
    ]
    assert len(done) == 1
    assert done[0].ok is False
    assert done[0].action.detail.get("denied") is True
    view = [
        e
        for e in _actions(events)
        if e.phase == "completed" and e.action.kind == "tool"
    ]
    assert view and view[0].ok is True


def test_translate_empty_output_done_resolved_ok_by_next_step() -> None:
    """background_held: the bypass-mode ``run_command`` is DONE with no
    output (parked), then step 3 arrives → resolved ok before the result."""
    lines = [
        line
        for line in (FIXTURES / "background_held.jsonl").read_text().splitlines()
        if line.strip()
    ]
    events, state = _replay("", lines=lines[:5])  # … run_command DONE
    assert list(state.verdict_pending) == ["step-2"]
    events, state = _replay("", lines=lines[:6])  # + agent_response step 3
    cmd = [
        e
        for e in _actions(events)
        if e.phase == "completed" and e.action.kind == "command"
    ]
    assert len(cmd) == 1 and cmd[0].ok is True
    assert state.verdict_pending == {}
    events, _ = _replay("background_held")
    assert _completed(events).answer.startswith("STARTED\n")


def test_translate_subagent_step_kind() -> None:
    events, _ = _replay("subagent")
    sub = [e for e in _actions(events) if e.action.kind == "subagent"]
    assert [e.phase for e in sub] == ["started", "completed"]
    assert "Secret Reader" in sub[0].action.title or "research" in sub[0].action.title
    assert sub[1].ok is True


def test_translate_json_schema_finish_tool_closed() -> None:
    events, _ = _replay("json_schema_finish")
    finish = [e for e in _actions(events) if e.action.id == "step-2"]
    assert [e.phase for e in finish] == ["started", "completed"]
    assert _completed(events).ok is True


def test_translate_interrupted_maps_to_resumable_error() -> None:
    for name, cid in (
        ("interrupted_sigint", "b66a64cf-dd95-4344-9df6-25d35539e10f"),
        ("interrupted_sigterm", "f40de548-26ff-4fa2-b472-0d4ac3347b54"),
    ):
        events, _ = _replay(name)
        done = _completed(events)
        assert done.ok is False
        assert "interrupted" in (done.error or "")
        assert "resume" in (done.error or "")
        assert done.resume == ResumeToken(engine=ENGINE, value=cid)
        cmd = [e for e in _actions(events) if e.action.kind == "command"]
        assert [e.phase for e in cmd] == ["started", "completed"]
        assert cmd[-1].ok is False


def test_status_error_result_completed_error() -> None:
    events, _ = _replay("invalid_model")
    done = _completed(events)
    assert done.ok is False
    assert (done.error or "").startswith("invalid model selection")


def test_status_error_structured_error_is_stringified() -> None:
    lines = [
        '{"event":"init","conversation_id":"c-12345678","init":{}}',
        '{"event":"result","result":{"conversation_id":"c-12345678","status":"ERROR","error":{"message":"boom","code":7}}}',
    ]
    events, _ = _replay("", lines=lines)
    assert _completed(events).error == "boom"
    lines[1] = (
        '{"event":"result","result":{"conversation_id":"c-12345678","status":"WEIRD"}}'
    )
    events, _ = _replay("", lines=lines)
    assert _completed(events).error == "antigravity ended with status WEIRD"


def test_tool_name_mapping() -> None:
    cases = [
        ("view_file", {"AbsolutePath": "/x/test.py"}, "tool", "test.py"),
        ("write_to_file", {"TargetFile": "/x/foo.py"}, "file_change", "foo.py"),
        ("replace_file_content", {"TargetFile": "/x/foo.py"}, "file_change", "foo.py"),
        ("run_command", {"CommandLine": "ls -la"}, "command", "ls -la"),
        ("grep_search", {"Query": "needle", "SearchPath": "/x"}, "tool", "needle"),
        ("find_by_name", {"Pattern": "*.py", "SearchDirectory": "/x"}, "tool", "*.py"),
        ("list_dir", {"DirectoryPath": "/x/src"}, "tool", "src"),
        ("search_web", {"Query": "agy docs"}, "web_search", "agy docs"),
        (
            "read_url_content",
            {"Url": "https://example.com"},
            "web_search",
            "example.com",
        ),
        ("browser_click_element", {}, "tool", "browser"),
        ("call_mcp_tool", {"ServerName": "srv", "ToolName": "t1"}, "tool", "srv"),
        ("manage_task", {"Action": "status", "TaskId": "t-1"}, "tool", "manage_task"),
    ]
    for name, params, kind, needle in cases:
        got_kind, title = agy._antigravity_tool_kind_and_title(name, params)
        assert got_kind == kind, name
        assert needle in title, (name, title)


def test_tool_output_strips_carriage_returns() -> None:
    events, _ = _replay("hook_deny_bypass")
    previews = [
        e.action.detail.get("output_preview")
        for e in _actions(events)
        if e.phase == "completed" and e.action.detail.get("output_preview")
    ]
    assert "allowme\n" in previews
    assert all("\r" not in p for p in previews)


def test_usage_flat_per_run_no_num_turns() -> None:
    resume = ResumeToken(engine=ENGINE, value="b66a64cf-dd95-4344-9df6-25d35539e10f")
    events, _ = _replay("resume_after_interrupt", resume=resume)
    usage = _completed(events).usage
    assert usage is not None
    assert usage["input_tokens"] == 26537
    assert usage["output_tokens"] == 89
    assert usage["cache_read_tokens"] == 0
    assert usage["reasoning_tokens"] == 0
    assert "num_turns" not in usage
    assert "total_tokens" not in usage
    assert "usage" not in usage
    # per-run wall clock, never agy's cumulative 71 s
    assert 0 <= usage["duration_ms"] < 60_000


def test_meta_model_from_options_config_or_init() -> None:
    events, _ = _replay("slash_prompt_literal")
    assert events[0].meta == {"model": "gemini-3.8-flash"}
    events, _ = _replay("slash_prompt_literal", runner=_runner(model="cfg-model"))
    assert events[0].meta == {"model": "cfg-model"}
    with apply_run_options(EngineRunOptions(model="opt-model")):
        events, _ = _replay("ok", runner=_runner(model="cfg-model"))
    assert events[0].meta == {"model": "opt-model"}
    events, _ = _replay("ok")
    assert events[0].meta is None


def test_invalid_json_events_truncated() -> None:
    runner = _runner()
    state = runner.new_state("p", None)
    raw = "x" * 1000
    (evt,) = runner.invalid_json_events(raw=raw, line=raw, state=state)
    assert isinstance(evt, ActionEvent)
    assert len(evt.action.detail["line"]) == 200


def test_decode_jsonl_brace_salvage_and_invalid() -> None:
    import msgspec

    runner = _runner()
    ev = runner.decode_jsonl(
        line=b'Warning: prefix {"event":"result","result":{"status":"SUCCESS"}}'
    )
    assert isinstance(ev, schema.AntigravityResult)
    with pytest.raises(msgspec.DecodeError):
        runner.decode_jsonl(line=b"completely non-json output")


def test_decode_error_unknown_event_is_dropped_with_warning() -> None:
    import msgspec

    runner = _runner()
    state = runner.new_state("p", None)
    with structlog.testing.capture_logs() as logs:
        out = runner.decode_error_events(
            raw='{"event":"brand_new"}',
            line='{"event":"brand_new"}',
            error=msgspec.DecodeError("unknown tag"),
            state=state,
        )
    assert out == []
    assert any(e["event"] == "jsonl.msgspec.invalid" for e in logs)


def test_run_timing_log_fields() -> None:
    with structlog.testing.capture_logs() as logs:
        _replay("ok")
    (timing,) = [e for e in logs if e["event"] == "antigravity.run.timing"]
    for key in (
        "spawn_to_init_ms",
        "init_to_result_ms",
        "total_ms",
        "resumed",
        "session_id",
        "cumulative_input_tokens",
        "cumulative_num_turns",
    ):
        assert key in timing, key
    assert timing["resumed"] is False
    assert timing["cumulative_num_turns"] == 1


def test_session_started_logs_agy_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "cached_agy_version", lambda cmd: "1.3.2")
    with structlog.testing.capture_logs() as logs:
        _replay("ok")
    (started,) = [e for e in logs if e["event"] == "antigravity.session.started"]
    assert started["agy_version"] == "1.3.2"
    assert started["resumed"] is False
    assert started["session_id"] == "90ca6744-e8d4-46d4-b5fc-64a30ae2fa6d"


# ── env ─────────────────────────────────────────────────────────────────────


def test_env_is_filtered_with_agy_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "UNRELATED_SECRET",
        "GEMINI_API_KEY",
        "DBUS_SESSION_BUS_ADDRESS",
        "AGY_ADC_AUTH",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        monkeypatch.setenv(key, "v")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    env = _runner().env(state=None)
    assert env is not None
    assert "UNRELATED_SECRET" not in env
    for key in (
        "GEMINI_API_KEY",
        "DBUS_SESSION_BUS_ADDRESS",
        "AGY_ADC_AUTH",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        assert env[key] == "v", key
    assert env["NO_COLOR"] == "1"
    assert "CI" not in env


def test_env_honours_security_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "untether.utils.env_policy.load_env_extras",
        lambda: (("MY_EXTRA",), ("VAULT_",)),
    )
    monkeypatch.setenv("MY_EXTRA", "1")
    monkeypatch.setenv("VAULT_TOKEN", "2")
    env = _runner().env(state=None) or {}
    assert env["MY_EXTRA"] == "1" and env["VAULT_TOKEN"] == "2"


def test_other_engines_env_unchanged_by_agy_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from untether.runners.pi import PiRunner

    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/x")
    pi = PiRunner(extra_args=[], model=None, provider=None)
    state = pi.new_state("p", None)
    env = pi.env(state=state) or {}
    assert "AGY_ADC_AUTH" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" not in env


def test_load_env_extras_reads_security_settings(tmp_path: Path) -> None:
    from untether.utils.env_policy import load_env_extras

    assert load_env_extras() == ((), ())


# ── build_runner / backend ──────────────────────────────────────────────────


def test_build_runner_config_validation(tmp_path: Path) -> None:
    from untether.config import ConfigError

    cfg_path = tmp_path / "untether.toml"
    runner = agy.build_runner(
        {"model": "gemini-3.8-flash", "cmd": "/opt/agy"}, cfg_path
    )
    assert isinstance(runner, AntigravityRunner)
    assert runner.command() == "/opt/agy"
    assert runner.model == "gemini-3.8-flash"
    for bad in ({"model": 3}, {"cmd": ["agy"]}):
        with pytest.raises(ConfigError):
            agy.build_runner(bad, cfg_path)
    # The PR's bypass/alias keys are gone: ignored, never honoured.
    runner = agy.build_runner(
        {"dangerously_skip_permissions": True, "antigravity_cmd": "/bad"}, cfg_path
    )
    assert runner.command() != "/bad"


def test_build_runner_expands_tilde_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/fake/home/user")
    runner = agy.build_runner({"cmd": "~/.local/bin/agy"}, tmp_path / "u.toml")
    assert runner.command() == "/fake/home/user/.local/bin/agy"


def test_build_runner_prefers_path_over_local_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    local = home / ".local" / "bin" / "agy"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(agy.shutil, "which", lambda cmd: "/usr/bin/agy")
    assert agy.build_runner({}, tmp_path / "u.toml").command() == "/usr/bin/agy"
    monkeypatch.setattr(agy.shutil, "which", lambda cmd: None)
    assert agy.build_runner({}, tmp_path / "u.toml").command() == str(local)
    local.unlink()
    assert agy.build_runner({}, tmp_path / "u.toml").command() == "agy"


def test_backend_cli_cmd_agy_install_url_cli_path_and_no_fs_access_at_import() -> None:
    import importlib
    import inspect

    assert agy.BACKEND.id == "antigravity"
    assert agy.BACKEND.cli_cmd == "agy"
    assert agy.BACKEND.install_cmd == (
        "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    )
    assert agy.BACKEND.build_runner is agy.build_runner
    source = inspect.getsource(importlib.import_module("untether.runners.antigravity"))
    assert "cli_cmd=default_antigravity_cmd()" not in source


def test_backend_registered_as_entry_point() -> None:
    from untether.engines import get_backend

    assert get_backend("antigravity") is agy.BACKEND


def test_runner_exposes_engine_state_and_records_pid() -> None:
    runner = _runner()
    assert runner._EXPOSE_ENGINE_STATE is True
    state = runner.new_state("p", None)
    runner.on_spawned(state=state, pid=4242)
    assert state.agy_pid == 4242
    assert state.orphan_pid_snapshot == []


def test_background_steps_tracked_and_cleared() -> None:
    lines = [
        line
        for line in (FIXTURES / "background_held.jsonl").read_text().splitlines()
        if line.strip()
    ]
    runner = _runner()
    state = runner.new_state("p", None)
    runner.start_run("p", None, state=state)
    for line in lines[:4]:  # init, user_input, agent_response, run_command ACTIVE
        runner.translate(
            schema.decode_event(line), state=state, resume=None, found_session=None
        )
    assert list(state.bg_steps) == [2]
    assert state.has_live_background_work() is True
    runner.translate(
        schema.decode_event(lines[4]), state=state, resume=None, found_session=None
    )
    assert state.bg_steps == {}
    assert state.has_live_background_work() is False


# ── version guard (08 §4) ───────────────────────────────────────────────────


def test_parse_agy_version() -> None:
    assert agy.parse_agy_version("1.3.1") == (1, 3, 1)
    assert agy.parse_agy_version("agy version 1.3.2\n") == (1, 3, 2)
    assert agy.parse_agy_version("v1.4") == (1, 4)
    assert agy.parse_agy_version("garbage") is None
    assert agy.parse_agy_version("") is None


@pytest.mark.anyio
async def test_version_guard_refuses_older_than_1_3_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from untether.runner import PRESPAWN_BLOCKED_KEY
    from untether.utils.paths import reset_run_base_dir, set_run_base_dir

    monkeypatch.setattr(agy, "_probe_agy_version", lambda path: "1.2.14")
    monkeypatch.setattr(agy, "_cache_key", lambda cmd: ("/usr/bin/agy", 0.0))

    def _no_spawn(*a: object, **k: object) -> object:
        raise AssertionError("spawned past the version guard")

    import untether.runner as runner_mod

    monkeypatch.setattr(runner_mod, "manage_subprocess", _no_spawn)
    token = set_run_base_dir(tmp_path)
    try:
        with structlog.testing.capture_logs() as logs:
            events = [e async for e in _runner().run_impl("hi", None)]
    finally:
        reset_run_base_dir(token)
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert "1.2.14" in (done.error or "") and "agy update" in (done.error or "")
    assert done.usage == {PRESPAWN_BLOCKED_KEY: "unsupported_version"}
    assert any(e["event"] == "antigravity.version.unsupported" for e in logs)


@pytest.mark.anyio
async def test_version_guard_fails_open_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agy, "agy_cli_version", lambda cmd: "not-a-version")
    with structlog.testing.capture_logs() as logs:
        assert await _runner()._unsupported_version_event(None) is None
    assert any(e["event"] == "antigravity.version.unknown" for e in logs)
    monkeypatch.setattr(agy, "agy_cli_version", lambda cmd: None)
    assert await _runner()._unsupported_version_event(None) is None
    monkeypatch.setattr(agy, "agy_cli_version", lambda cmd: "1.3.1")
    assert await _runner()._unsupported_version_event(None) is None
    monkeypatch.setattr(agy, "agy_cli_version", lambda cmd: "9.0.0")
    with structlog.testing.capture_logs() as logs:
        assert await _runner()._unsupported_version_event(None) is None
    assert any(e["event"] == "antigravity.version.newer_than_probed" for e in logs)


def test_version_probe_cached_per_mtime(tmp_path: Path) -> None:
    script = tmp_path / "agy"
    script.write_text("#!/bin/sh\necho 1.3.1\n")
    script.chmod(0o755)
    calls: list[str] = []

    def probe(path: str) -> str | None:
        calls.append(path)
        return agy._run_agy_version(path)

    agy._VERSION_CACHE.clear()
    import untether.runners.antigravity as mod

    original = mod._probe_agy_version
    mod._probe_agy_version = probe
    try:
        assert agy.agy_cli_version(str(script)) == "1.3.1"
        assert agy.agy_cli_version(str(script)) == "1.3.1"
        assert len(calls) == 1
        assert agy.cached_agy_version(str(script)) == "1.3.1"
        script.write_text("#!/bin/sh\necho 1.3.2\n")
        os.utime(script, (1, 1))
        assert agy.agy_cli_version(str(script)) == "1.3.2"
        assert len(calls) == 2
        assert agy.agy_cli_version(str(tmp_path / "missing")) is None
        assert agy.cached_agy_version(str(tmp_path / "missing")) is None
    finally:
        mod._probe_agy_version = original
        agy._VERSION_CACHE.clear()


def test_tos_notice_logged_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "_TOS_NOTICE_LOGGED", False)
    with structlog.testing.capture_logs() as logs:
        agy._log_tos_notice_once()
        agy._log_tos_notice_once()
    notices = [e for e in logs if e["event"] == "antigravity.tos_notice"]
    assert len(notices) == 1
    assert notices[0]["docs"] == "docs/reference/runners/antigravity/runner.md"
