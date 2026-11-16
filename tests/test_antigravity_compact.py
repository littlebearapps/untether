"""Antigravity ``/compact`` passthrough (#1114).

agy 1.3.5 ships ``/compact`` (``/compact <message>`` summarises, then answers
the message). Untether runs agy with ``--disable-slash-commands``, so until
#1114 a ``/compact`` typed in an Antigravity chat reached the model as text
and the model could claim a compaction that never happened. Now a
``/compact`` prompt (and only that one) is sent to the chat's conversation
without the flag; agy's ``checkpoint`` step plus its empty ``SUCCESS`` result
is a confirmed compaction; with no conversation nothing spawns.

The ``compact`` fixture is a real agy 1.3.5 capture (Workspace flags:
``--mode=accept-edits --agent=untether-workspace``), see
``test_antigravity_schema.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from untether.runner import PRESPAWN_BLOCKED_KEY
from untether.runner_bridge import _apply_preamble
from untether.runners import antigravity as agy
from untether.runners.antigravity import ENGINE, AntigravityRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as schema
from untether.utils.antigravity_argv import COMPACT_COMMAND, native_compact_prompt
from untether.utils.paths import reset_run_base_dir, set_run_base_dir

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
CID = "153ba40d-5f0b-4d81-b343-a31c89360d14"
RESUME = ResumeToken(engine=ENGINE, value=CID)
MANUAL_SUCCESS = {
    "count": 1,
    "trigger": "manual",
    "result": "success",
    "manual_success": True,
}


def _runner(**kwargs: Any) -> AntigravityRunner:
    kwargs.setdefault("antigravity_cmd", "agy")
    return AntigravityRunner(**kwargs)


def _fixture_lines(name: str = "compact") -> list[str]:
    return [
        line
        for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _args(
    prompt: str,
    resume: ResumeToken | None = RESUME,
    options: EngineRunOptions | None = None,
) -> list[str]:
    runner = _runner()
    with apply_run_options(options):
        state = runner.new_state(prompt, resume)
        return runner.build_args(prompt, resume, state=state)


def _content(
    prompt: str,
    resume: ResumeToken | None = RESUME,
    options: EngineRunOptions | None = None,
) -> str:
    runner = _runner()
    with apply_run_options(options):
        state = runner.new_state(prompt, resume)
        runner.build_args(prompt, resume, state=state)
        payload = runner.stdin_payload(prompt, resume, state=state)
    assert payload is not None
    return json.loads(payload)["message"]["content"]


def _replay(
    prompt: str,
    lines: list[str] | None = None,
    *,
    resume: ResumeToken | None = RESUME,
    options: EngineRunOptions | None = None,
) -> list[Any]:
    runner = _runner()
    events: list[Any] = []
    with apply_run_options(options):
        state = runner.new_state(prompt, resume)
        runner.start_run(prompt, resume, state=state)
        for line in lines if lines is not None else _fixture_lines():
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
    return events


def _completed(events: list[Any]) -> CompletedEvent:
    assert isinstance(events[0], StartedEvent)
    assert isinstance(events[-1], CompletedEvent)
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    assert all(isinstance(e, ActionEvent) for e in events[1:-1])
    return events[-1]


# ── which prompts are agy's /compact ────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "line"),
    [
        ("/compact", "/compact"),
        ("  /compact \n", "/compact"),
        ("/compact@untether_dev_bot", "/compact"),
        ("/compact keep the API notes", "/compact keep the API notes"),
        ("/compact\n\nkeep the API notes", "/compact keep the API notes"),
        ("/compact@untether_dev_bot keep it short", "/compact keep it short"),
    ],
)
def test_native_compact_prompt_accepts_compact(prompt: str, line: str) -> None:
    assert native_compact_prompt(prompt) == line
    assert line.split()[0] == COMPACT_COMMAND


@pytest.mark.parametrize(
    "prompt",
    [
        "/compacting",
        "/compactx",
        "/compact-now",
        "/Compact",
        "/review the README",
        "/clear",
        "/config",
        "compact",
        "please /compact",
        "Task: /compact",
        "//compact",
        "/ compact",
        "",
        "   ",
        None,
        5,
    ],
)
def test_native_compact_prompt_refuses_everything_else(prompt: Any) -> None:
    assert native_compact_prompt(prompt) is None


# ── argv: slash commands stay off for every other prompt ────────────────────


def test_compact_run_leaves_slash_commands_on_and_keeps_the_workspace_flags() -> None:
    args = _args("/compact")
    assert "--disable-slash-commands" not in args
    assert f"--conversation={CID}" in args
    assert "--mode=accept-edits" in args
    assert "--dangerously-skip-permissions" not in args
    assert "/compact" not in args  # the prompt goes on stdin, never argv


@pytest.mark.parametrize(
    "prompt", ["/review the README", "/clear", "/compacting", "compact", "hi"]
)
def test_every_other_prompt_still_disables_slash_commands(prompt: str) -> None:
    assert "--disable-slash-commands" in _args(prompt)
    assert "--disable-slash-commands" in _args(prompt, resume=None)


def test_compact_respects_full_access() -> None:
    args = _args("/compact", options=EngineRunOptions(permission_mode="full"))
    assert args[-1] == "--dangerously-skip-permissions"
    assert "--disable-slash-commands" not in args


# ── stdin: the command line itself, nothing added ───────────────────────────


def test_stdin_carries_the_command_verbatim() -> None:
    assert _content("/compact") == "/compact"
    assert _content("/compact@untether_dev_bot") == "/compact"
    assert _content("/compact keep the notes") == "/compact keep the notes"


def test_plan_first_does_not_escape_compact() -> None:
    plan = EngineRunOptions(permission_mode="plan")
    assert _content("/compact", options=plan) == "/compact"
    # D30 still escapes every other ``/``-leading plan prompt.
    assert _content("/review it", options=plan).startswith("Task: ")


def test_scheduled_compact_gets_no_note() -> None:
    cron = EngineRunOptions(unattended_trigger="cron:nightly")
    assert _content("/compact", options=cron) == "/compact"
    # The #1055 note still leads every other scheduled Workspace prompt.
    assert _content("tidy up", options=cron) != "tidy up"


# ── translate: agy's checkpoint + empty SUCCESS is the confirmation ─────────


def test_compact_result_is_a_confirmed_compaction() -> None:
    events = _replay("/compact")
    done = _completed(events)
    assert done.ok is True
    assert done.answer == ""
    assert done.resume == RESUME
    assert done.usage["compaction"] == MANUAL_SUCCESS
    assert events[0].resume == RESUME


def test_compact_without_a_checkpoint_is_not_claimed() -> None:
    lines = [line for line in _fixture_lines() if '"checkpoint"' not in line]
    done = _completed(_replay("/compact", lines))
    assert done.ok is True
    assert "compaction" not in done.usage
    assert done.answer == agy.COMPACT_UNCONFIRMED_TEXT


def test_a_checkpoint_in_an_ordinary_run_is_not_a_manual_compaction() -> None:
    done = _completed(_replay("summarise the README"))
    assert "compaction" not in done.usage


def test_compact_with_a_message_keeps_the_answer_under_a_confirmation() -> None:
    lines = _fixture_lines()
    result = json.loads(lines[-1])
    result["result"]["response"] = "Here is the summary.\n"
    lines[-1] = json.dumps(result)
    done = _completed(_replay("/compact then summarise", lines))
    assert done.usage["compaction"] == MANUAL_SUCCESS
    assert done.answer.startswith(agy.COMPACTED_TEXT)
    assert done.answer.rstrip().endswith("Here is the summary.")


def test_compact_failure_is_not_a_compaction() -> None:
    lines = _fixture_lines()
    result = json.loads(lines[-1])
    result["result"]["status"] = "ERROR"
    result["result"]["error"] = "compaction failed"
    lines[-1] = json.dumps(result)
    done = _completed(_replay("/compact", lines))
    assert done.ok is False
    assert "compaction" not in (done.usage or {})


def test_compact_in_plan_first_skips_plan_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from untether.utils import antigravity_plan

    calls: list[str] = []
    monkeypatch.setattr(
        antigravity_plan, "supersede", lambda cid: calls.append(cid) or []
    )

    def _no_review(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a /compact run has no plan to review")

    monkeypatch.setattr(AntigravityRunner, "_plan_review", _no_review)
    plan = EngineRunOptions(permission_mode="plan")
    done = _completed(_replay("/compact", options=plan))
    assert calls == []
    assert done.ok is True and done.answer == ""
    assert done.usage["compaction"] == MANUAL_SUCCESS


# ── end to end through run() with the fake agy ──────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    token = set_run_base_dir(proj)
    try:
        yield proj
    finally:
        reset_run_base_dir(token)


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(path))
    return path


async def _run(prompt: str, resume: ResumeToken | None) -> list[Any]:
    assert os.access(FAKE_AGY, os.X_OK), f"chmod +x {FAKE_AGY}"
    runner = AntigravityRunner(antigravity_cmd=str(FAKE_AGY))
    return [evt async for evt in runner.run(prompt, resume)]


@pytest.mark.anyio
async def test_run_compact_end_to_end(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "compact")
    events = await _run("/compact", RESUME)
    done = _completed(events)
    assert done.ok is True and done.answer == ""
    assert done.usage["compaction"] == MANUAL_SUCCESS
    rec = json.loads(record.read_text())
    assert "--disable-slash-commands" not in rec["argv"]
    assert f"--conversation={CID}" in rec["argv"]
    assert json.loads(rec["stdin"]) == {
        "event": "user",
        "message": {"content": "/compact"},
    }


@pytest.mark.anyio
async def test_run_other_slash_prompt_still_disables_slash_commands(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "ok")
    events = await _run("/review the README", RESUME)
    assert isinstance(events[-1], CompletedEvent)
    rec = json.loads(record.read_text())
    assert "--disable-slash-commands" in rec["argv"]
    assert json.loads(rec["stdin"])["message"]["content"] == "/review the README"


@pytest.mark.anyio
@pytest.mark.parametrize("prompt", ["/compact", "/compact keep the notes"])
async def test_compact_with_no_conversation_never_spawns(
    project: Path, record: Path, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SCENARIO", "compact")
    with structlog.testing.capture_logs() as logs:
        events = await _run(prompt, None)
    assert not record.exists(), "agy must not be spawned"
    (done,) = events
    assert isinstance(done, CompletedEvent) and done.ok is False
    assert done.error == agy.NOTHING_TO_COMPACT_TEXT
    assert done.resume is None
    assert done.usage == {PRESPAWN_BLOCKED_KEY: agy.COMPACT_NO_CONVERSATION_BLOCK}
    names = [e["event"] for e in logs]
    assert "antigravity.compact.no_conversation" in names
    assert "subprocess.spawn" not in names


def test_texts_are_plain() -> None:
    for text in (
        agy.NOTHING_TO_COMPACT_TEXT,
        agy.COMPACT_UNCONFIRMED_TEXT,
        agy.COMPACTED_TEXT,
    ):
        assert "<" not in text and "`" not in text and len(text) < 200


# ── bridge: the preamble never hides the command ────────────────────────────


def test_preamble_is_skipped_only_for_agy_compact() -> None:
    assert _apply_preamble("/compact", "antigravity") == "/compact"
    assert _apply_preamble("/compact keep it", "antigravity") == "/compact keep it"
    assert _apply_preamble("/review it", "antigravity") != "/review it"
    assert _apply_preamble("compact", "antigravity") != "compact"
    # Claude's /compact outside a live session is #834, unchanged here.
    assert _apply_preamble("/compact", "claude") != "/compact"
