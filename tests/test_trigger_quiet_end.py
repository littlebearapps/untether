"""#1220: opt-in quiet end for nothing-to-report trigger runs, and
``fetch.skip_if_empty``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import untether.runner_bridge as bridge
from tests.test_exec_bridge import FakeTransport
from tests.test_trigger_dispatcher import FakeTransport as DispatchTransport
from tests.test_trigger_dispatcher import RunJobCapture
from untether.context import RunContext, attended_context
from untether.markdown import MarkdownPresenter
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    handle_message,
    quiet_end_line,
)
from untether.runners.mock import ErrorReturn, Return, ScriptRunner
from untether.triggers import journal
from untether.triggers.dispatcher import (
    TriggerDispatcher,
    build_run_context,
    fetch_is_empty,
)
from untether.triggers.settings import CronConfig, WebhookConfig
from untether.utils.paths import reset_run_base_dir, set_run_base_dir


def _cron(**kw: Any) -> CronConfig:
    return CronConfig.model_validate(
        {"id": "watch", "schedule": "0 9 * * *", "prompt": "go", **kw}
    )


@pytest.fixture
def journal_path(tmp_path):
    journal.init_journal(tmp_path / "untether.toml")
    yield tmp_path / journal.JOURNAL_FILENAME
    journal.reset_journal()


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- config -------------------------------------------------------------------


def test_quiet_keys_and_context() -> None:
    assert build_run_context(_cron()).quiet_sentinel is None
    on = _cron(quiet_unless_output=True)
    assert build_run_context(on).quiet_sentinel == "[quiet]"
    custom = _cron(quiet_unless_output=True, quiet_sentinel="NOTHING")
    ctx = build_run_context(custom)
    assert ctx.quiet_sentinel == "NOTHING"
    assert attended_context(ctx) == RunContext()  # a reply is never quiet
    assert build_run_context(_cron(quiet_unless_output=False)).quiet_sentinel is None


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"quiet_sentinel": "[x]"}, "needs quiet_unless_output"),
        ({"quiet_unless_output": True, "quiet_sentinel": "x" * 33}, "quiet_sentinel"),
        ({"quiet_unless_output": True, "quiet_sentinel": "  "}, "quiet_sentinel"),
        ({"quiet_unless_output": True, "quiet_sentinel": "a\tb"}, "printable"),
        ({"quiet_unless_output": "yes"}, "quiet_unless_output"),
    ],
)
def test_bad_quiet_config(kw: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _cron(**kw)


def test_quiet_on_non_agent_webhook_is_an_error() -> None:
    with pytest.raises(ValidationError, match="quiet_unless_output only applies"):
        WebhookConfig.model_validate(
            {
                "id": "w",
                "path": "/w",
                "auth": "none",
                "action": "notify_only",
                "message_template": "hi",
                "quiet_unless_output": True,
            }
        )


def test_existing_cron_dumps_unchanged() -> None:
    dumped = _cron().model_dump(exclude_none=True)
    assert "quiet_unless_output" not in dumped and "quiet_sentinel" not in dumped
    assert (
        _cron(fetch={"type": "file_read", "file_path": "/x"}).fetch.skip_if_empty
        is False
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("[quiet] nothing to draft", "[quiet] nothing to draft"),
        ("\n\n  [quiet]\nmore text", "[quiet]"),
        ("Found 2 new issues\n[quiet]", None),
        ("", None),
        (None, None),
        ("[quiet]" + "x" * 300, "[quiet]" + "x" * 193),
    ],
)
def test_quiet_end_line(answer: str | None, expected: str | None) -> None:
    assert quiet_end_line(answer, "[quiet]") == expected


@pytest.mark.parametrize(
    ("data", "empty"),
    [
        ("", True),
        ("  \n", True),
        ([], True),
        ({}, True),
        (None, True),
        ("x", False),
        ([1], False),
        ({"a": 1}, False),
        (0, False),
    ],
)
def test_fetch_is_empty(data: Any, empty: bool) -> None:
    assert fetch_is_empty(data) is empty


# --- dispatcher: skip_if_empty ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(("content", "skip"), [("", True), ("data", False)])
async def test_skip_if_empty_file_read(
    tmp_path, journal_path, content: str, skip: bool
) -> None:
    src = tmp_path / "inbox.txt"
    src.write_text(content)
    cron = _cron(
        prompt=None,
        prompt_template="Look at {{fetch_result}}",
        fetch={"type": "file_read", "file_path": str(src), "skip_if_empty": True},
    )
    transport = DispatchTransport()
    run_job = RunJobCapture()
    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job, transport=transport, default_chat_id=1, task_group=tg
        )
        with capture_logs() as logs:
            result = await dispatcher.dispatch_cron(cron)
    assert result is True  # counts as fired either way (run_once consumed)
    if skip:
        assert transport.sent == [] and run_job.calls == []
        assert any(e["event"] == "triggers.cron.fetch_empty_skip" for e in logs)
        (line,) = _lines(journal_path)
        assert line["outcome"] == "skipped_empty" and line["ok"] is True
    else:
        assert len(transport.sent) == 1 and len(run_job.calls) == 1


@pytest.mark.anyio
async def test_skip_if_empty_off_still_runs(tmp_path) -> None:
    src = tmp_path / "inbox.txt"
    src.write_text("")
    cron = _cron(fetch={"type": "file_read", "file_path": str(src)})
    run_job = RunJobCapture()
    async with anyio.create_task_group() as tg:
        dispatcher = TriggerDispatcher(
            run_job=run_job,
            transport=DispatchTransport(),
            default_chat_id=1,
            task_group=tg,
        )
        await dispatcher.dispatch_cron(cron)
    assert len(run_job.calls) == 1


# --- bridge: quiet final delivery ------------------------------------------------


async def _run(
    step: Any, *, sentinel: str | None = "[quiet]", cfg_kw: dict | None = None
) -> FakeTransport:
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
        **(cfg_kw or {}),
    )
    ctx = RunContext(trigger_source="cron:watch", run_id="rid", quiet_sentinel=sentinel)
    runner = ScriptRunner([step], engine="pi", resume_value="s-1220")
    with anyio.fail_after(5):
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
            context=ctx,
            running_tasks={},
        )
    return transport


def _final_texts(transport: FakeTransport) -> list[str]:
    return [c["message"].text for c in transport.send_calls[1:]] + [
        c["message"].text for c in transport.edit_calls
    ]


@pytest.mark.anyio
async def test_sentinel_answer_ends_quietly(journal_path) -> None:
    with capture_logs() as logs:
        transport = await _run(Return(answer="[quiet] nothing to draft"))
    progress_ref = transport.send_calls[0]["ref"]
    # Nothing but the progress message was ever sent, and it is gone, as is
    # the trigger's label (the incoming message the run replied to).
    assert len(transport.send_calls) == 1
    assert progress_ref in transport.delete_calls
    assert any(r.message_id == 10 for r in transport.delete_calls)
    assert not any("nothing to draft" in t for t in _final_texts(transport))
    assert any(e["event"] == "trigger.quiet_end" for e in logs)
    (line,) = _lines(journal_path)
    assert line["outcome"] == "ok" and line["quiet"] is True
    assert line["quiet_line"] == "[quiet] nothing to draft"


@pytest.mark.anyio
async def test_answer_without_sentinel_posts_normally(journal_path) -> None:
    transport = await _run(Return(answer="Found 2 new issues"))
    assert any("Found 2 new issues" in t for t in _final_texts(transport))
    assert not any(r.message_id == 10 for r in transport.delete_calls)
    (line,) = _lines(journal_path)
    assert line["quiet"] is False and line["quiet_line"] is None


@pytest.mark.anyio
async def test_quiet_off_posts_the_sentinel_answer(journal_path) -> None:
    transport = await _run(Return(answer="[quiet] nothing"), sentinel=None)
    assert any("nothing" in t for t in _final_texts(transport))


@pytest.mark.anyio
async def test_error_with_sentinel_is_never_quiet(journal_path) -> None:
    transport = await _run(ErrorReturn(error="boom", answer="[quiet] nothing"))
    assert any("boom" in t for t in _final_texts(transport))
    (line,) = _lines(journal_path)
    assert line["outcome"] == "error" and line["quiet"] is False


@pytest.mark.anyio
async def test_unattended_denial_is_never_quiet(journal_path) -> None:
    usage = {"unattended": {"trigger": "cron:watch", "denied": {"Write": 1}}}
    transport = await _run(Return(answer="[quiet] nothing", usage=usage))
    assert any("nothing" in t for t in _final_texts(transport))


@pytest.mark.anyio
async def test_outbox_file_is_never_quiet(journal_path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "outbox_has_files", lambda cfg: True)
    transport = await _run(Return(answer="[quiet] nothing"))
    assert any("nothing" in t for t in _final_texts(transport))
    (line,) = _lines(journal_path)
    assert line["quiet"] is False


def test_outbox_has_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        bridge,
        "_load_outbox_settings",
        lambda cfg: SimpleNamespace(outbox_dir=".untether-outbox"),
    )
    cfg = SimpleNamespace(send_file=object())
    token = set_run_base_dir(tmp_path)
    try:
        assert bridge.outbox_has_files(cfg) is False  # no outbox dir
        (tmp_path / ".untether-outbox").mkdir()
        assert bridge.outbox_has_files(cfg) is False  # empty
        (tmp_path / ".untether-outbox" / "report.txt").write_text("x")
        assert bridge.outbox_has_files(cfg) is True
        # No file sending configured: nothing would be delivered anyway.
        assert bridge.outbox_has_files(SimpleNamespace(send_file=None)) is False
    finally:
        reset_run_base_dir(token)


@pytest.mark.anyio
async def test_progress_delete_failure_falls_back_to_a_final(
    journal_path, monkeypatch
) -> None:
    async def _fail(self, *, ref):
        self.delete_calls.append(ref)
        return False

    monkeypatch.setattr(FakeTransport, "delete", _fail)
    with capture_logs() as logs:
        transport = await _run(Return(answer="[quiet] nothing"))
    assert any("nothing" in t for t in _final_texts(transport))
    assert any(e["event"] == "trigger.quiet_end.delete_failed" for e in logs)
    (line,) = _lines(journal_path)
    assert line["quiet"] is False
