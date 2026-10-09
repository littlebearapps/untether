"""Antigravity (agy) stream-json schema decoding (#558).

Fixtures in ``tests/fixtures/antigravity/`` are scrubbed copies of the probe
captures in ``docs/plans/v0.36.1-antigravity/probes/`` (paths → ``<scratch>``,
ids kept). Provenance (``_FIXTURE_SOURCES``) is kept here rather than in a
README so the drift suite (03) can re-point at re-captures.
"""

from __future__ import annotations

from pathlib import Path

import msgspec
import pytest

from untether.schemas import antigravity as schema

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"

# fixture name -> capture it was generated from (agy version)
_FIXTURE_SOURCES: dict[str, str] = {
    "ok": "1.3.x/d01-ok (1.3.1)",
    "tools_denied": "1.3.x/d03-tools (1.3.1)",
    "write_ok_shell_denied": "1.3.x/d04-accept-edits (1.3.1)",
    "ask_question_nohook": "1.3.x/d11-ask-question-nohook (1.3.1)",
    "hook_deny_bypass": "1.3.x/e2-hooks-bypass (1.3.1)",
    "background_held": "1.3.x/a1-background-default (1.3.1)",
    "subagent": "1.3.x/c-subagent (1.3.1)",
    "json_schema_finish": "1.3.x/h-json-schema (1.3.1)",
    "interrupted_sigint": "1.3.x/i-cancel-sigINT (1.3.1)",
    "interrupted_sigterm": "1.3.x/i-cancel-sigTERM (1.3.1)",
    "resume_after_interrupt": "1.3.x/i-resume-after-sigint (1.3.1)",
    "unknown_conversation": "1.3.x/z-unknown-conversation (1.3.1)",
    "continue_fresh": "1.3.x/z-continue-scope (1.3.1)",
    "continue_workspace": "1.3.x/z-continue-scope-positive (1.3.1)",
    "stream_slash_fatal": "1.3.x/z-stream-slash (1.3.1)",
    "slash_prompt_literal": "1.3.x/p22-slash-stream (1.3.2)",
    "usage": "1.3.x/z-usage (1.3.1)",
    "model": "1.3.x/z-model (1.3.1)",
    "effort": "1.3.x/z-effort (1.3.1)",
    "invalid_model": "z1-invalid-model (1.2.14; signature unchanged on 1.3.1)",
    "unauth": "1.3.x/p17-signed-out (1.3.2)",
    "synthetic_no_result": "synthetic: ok minus its result line",
    "synthetic_result_then_agy_error": "synthetic: ok + AGY_ERROR stderr after the result, rc 3",
    "synthetic_agy_error_rc3": "synthetic: partial deltas, no result, AGY_ERROR, rc 3",
}


def _lines(name: str) -> list[str]:
    return [
        line
        for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _decode_all(name: str) -> list[schema.AntigravityEvent]:
    return [schema.decode_event(line) for line in _lines(name)]


def test_every_fixture_has_a_recorded_source() -> None:
    names = {p.stem for p in FIXTURES.glob("*.jsonl")}
    assert names == set(_FIXTURE_SOURCES)
    for name in names:
        assert (FIXTURES / f"{name}.script").exists(), name


@pytest.mark.parametrize("name", sorted(_FIXTURE_SOURCES))
def test_every_fixture_line_decodes(name: str) -> None:
    events = _decode_all(name)
    assert events, name


def test_decode_hook_denied_tool_error_state() -> None:
    errors = [
        e.step_update
        for e in _decode_all("hook_deny_bypass")
        if isinstance(e, schema.StepUpdate)
        and e.step_update is not None
        and e.step_update.state == "ERROR"
    ]
    assert len(errors) == 1
    info = errors[0].tool_info
    assert info is not None and info.error is not None
    assert info.error.type == "TOOL_ERROR"
    assert (info.error.message or "").startswith("tool call denied by pre-tool hook")


def test_decode_soft_denied_step_is_done_without_error() -> None:
    steps = [
        e.step_update
        for e in _decode_all("tools_denied")
        if isinstance(e, schema.StepUpdate) and e.step_update is not None
    ]
    step4 = [s for s in steps if s.step_index == 4]
    assert [s.state for s in step4] == ["ACTIVE", "DONE"]
    done = step4[-1]
    assert done.tool_name == "run_command"
    assert done.tool_info is not None
    assert done.tool_info.error is None
    assert done.tool_info.output is None


def test_decode_result_denied_actions() -> None:
    result = _decode_all("tools_denied")[-1]
    assert isinstance(result, schema.AntigravityResult)
    assert result.result is not None
    assert result.result.denied_actions == [
        schema.DeniedAction(action="command", display_name="RunCommand")
    ]


def test_decode_result_structured_error_kept() -> None:
    line = (
        b'{"event":"result","result":{"conversation_id":"c1","status":"ERROR",'
        b'"error":{"code":1,"message":"boom"}}}'
    )
    event = schema.decode_event(line)
    assert isinstance(event, schema.AntigravityResult)
    assert event.result is not None
    assert event.result.error == {"code": 1, "message": "boom"}


def test_decode_new_step_types_and_extra_fields_tolerated() -> None:
    types = {
        e.step_update.step_type
        for name in ("json_schema_finish", "subagent", "ask_question_nohook")
        for e in _decode_all(name)
        if isinstance(e, schema.StepUpdate) and e.step_update is not None
    }
    assert {"finish", "subagent", "unknown"} <= types

    sub = next(
        e.step_update
        for e in _decode_all("subagent")
        if isinstance(e, schema.StepUpdate)
        and e.step_update is not None
        and e.step_update.step_type == "subagent"
    )
    assert sub.subagent_info is not None
    assert sub.subagent_info["subagents"][0]["type_name"] == "research"

    result = _decode_all("json_schema_finish")[-1]
    assert isinstance(result, schema.AntigravityResult)
    assert result.result is not None
    assert result.result.structured_output == {
        "answer": "Hello!\n\n1, 2, 3.",
        "count": 3,
    }
    assert result.result.json_schema is not None

    extra = schema.decode_event(
        b'{"event":"step_update","future_key":1,"step_update":'
        b'{"step_index":0,"state":"DONE","step_type":"brand_new","new_field":true}}'
    )
    assert isinstance(extra, schema.StepUpdate)
    assert extra.step_update is not None
    assert extra.step_update.step_type == "brand_new"


def test_decode_init_model_on_1_3_2() -> None:
    init = _decode_all("slash_prompt_literal")[0]
    assert isinstance(init, schema.Init)
    assert init.init is not None
    assert init.init.model == "gemini-3.8-flash"
    assert len(init.init.tools or []) >= 58  # never pin the exact list


def test_decode_command_result_usage_groups() -> None:
    first = _decode_all("usage")[0]
    assert isinstance(first, schema.CommandResult)
    assert first.command is not None
    assert first.command.name == "usage"
    data = first.command.data or {}
    groups = data.get("groups")
    assert isinstance(groups, list) and groups
    assert {g["name"] for g in groups} >= {"Gemini Models"}


def test_decode_unknown_event_tag_raises() -> None:
    with pytest.raises(msgspec.DecodeError):
        schema.decode_event(b'{"event":"interrupt"}')
