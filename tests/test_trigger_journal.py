"""Tests for the trigger run journal ``triggers_runs.jsonl`` (#1123)."""

from __future__ import annotations

import json
import os
import stat

import anyio
import pytest
import structlog.testing

from tests.test_exec_bridge import FakeTransport as BridgeTransport
from untether.context import RunContext
from untether.markdown import MarkdownPresenter
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import Return, ScriptRunner
from untether.triggers import journal
from untether.triggers.dispatcher import TriggerDispatcher
from untether.triggers.settings import CronConfig, WebhookConfig


@pytest.fixture
def journal_path(tmp_path):
    journal.init_journal(tmp_path / "untether.toml")
    yield tmp_path / journal.JOURNAL_FILENAME
    journal.reset_journal()


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ctx(source: str = "cron:nightly") -> RunContext:
    return RunContext(project="proj", trigger_source=source, run_id="rid-1")


def test_record_run_fields(journal_path) -> None:
    journal.record_run(
        _ctx(),
        outcome="ok",
        ok=True,
        engine="claude",
        session_id="sess",
        model="opus",
        elapsed_s=2.0,
        usage={
            "num_turns": 3,
            "total_cost_usd": 0.12,
            "throttle": {
                "rate_limit_count": 2,
                "rate_limit_total_s": 60.0,
                "api_retry_count": 1,
                "api_retry_total_s": 4.0,
                "rejected": True,
            },
        },
    )
    (line,) = _lines(journal_path)
    assert line["v"] == 1
    assert (line["trigger_kind"], line["trigger_id"]) == ("cron", "nightly")
    assert line["run_id"] == "rid-1"
    assert line["project"] == "proj"
    assert (line["engine"], line["model"], line["session_id"]) == (
        "claude",
        "opus",
        "sess",
    )
    assert (line["outcome"], line["ok"]) == ("ok", True)
    assert (line["num_turns"], line["cost_usd"]) == (3, 0.12)
    assert line["rate_limit_count"] == 2 and line["rejected"] is True
    assert line["api_retry_count"] == 1
    assert line["finished_at"] - line["started_at"] == pytest.approx(2.0, abs=0.01)


def test_unthrottled_run_has_zero_counts(journal_path) -> None:
    journal.record_run(_ctx("webhook:gh"), outcome="error", ok=False, error="boom")
    (line,) = _lines(journal_path)
    assert line["trigger_kind"] == "webhook"
    assert line["rate_limit_count"] == 0 and line["rejected"] is False
    assert line["error"] == "boom"


@pytest.mark.parametrize(
    "context",
    [None, RunContext(project="p"), RunContext(trigger_source="at:5")],
)
def test_non_trigger_runs_are_not_journaled(journal_path, context) -> None:
    journal.record_run(context, outcome="ok", ok=True)
    assert not journal_path.exists()


def test_file_is_private_under_loose_umask(journal_path) -> None:
    old = os.umask(0o002)
    try:
        journal.record_run(_ctx(), outcome="ok", ok=True)
    finally:
        os.umask(old)
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600


def test_rotates_at_cap(journal_path, monkeypatch) -> None:
    monkeypatch.setattr(journal, "MAX_BYTES", 200)
    for _ in range(3):
        journal.record_run(_ctx(), outcome="ok", ok=True)
    rotated = journal_path.with_name(journal_path.name + ".1")
    assert rotated.exists()
    assert len(_lines(journal_path)) < 3


def test_unwritable_dir_is_logged_not_raised(tmp_path) -> None:
    journal.init_journal(tmp_path / "missing-dir" / "untether.toml")
    try:
        with structlog.testing.capture_logs() as logs:
            journal.record_run(_ctx(), outcome="ok", ok=True)
    finally:
        journal.reset_journal()
    assert any(e["event"] == "triggers.journal.write_failed" for e in logs)


# ── dispatcher outcomes that never reach the bridge ────────────────────────


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def send(self, *, channel_id, message, options=None):
        from untether.transport import MessageRef

        return None if self.fail else MessageRef(channel_id=channel_id, message_id=1)


async def _noop_run_job(*args, **kwargs) -> None:
    return None


@pytest.mark.anyio
async def test_dispatch_outcomes(journal_path, monkeypatch) -> None:
    from untether.triggers import dispatcher as dispatcher_mod
    from untether.triggers import fetch as fetch_mod

    monkeypatch.setattr(dispatcher_mod, "SEND_RETRY_DELAYS", ())

    async def refuse(*_a) -> bool:
        return True

    async def fetch_fails(_cfg):
        return False, "down", None

    monkeypatch.setattr(fetch_mod, "execute_fetch", fetch_fails)
    webhook = WebhookConfig(id="gh", path="/gh", auth="none", prompt_template="x")
    async with anyio.create_task_group() as tg:
        refused = TriggerDispatcher(_noop_run_job, _Transport(), 1, tg, refuse)
        await refused.dispatch_webhook(webhook, "x")
        failing = TriggerDispatcher(_noop_run_job, _Transport(fail=True), 1, tg)
        await failing.dispatch_cron(
            CronConfig(id="c", schedule="* * * * *", prompt="x")
        )
        ok = TriggerDispatcher(_noop_run_job, _Transport(), 1, tg)
        await ok.dispatch_cron(
            CronConfig(
                id="f",
                schedule="* * * * *",
                prompt="x",
                fetch={
                    "type": "file_read",
                    "file_path": "/tmp/x",
                    "on_failure": "abort",
                },
            )
        )
        tg.cancel_scope.cancel()
    got = [(line["trigger_id"], line["outcome"]) for line in _lines(journal_path)]
    assert got == [("gh", "refused"), ("c", "announce_failed"), ("f", "fetch_aborted")]


# ── bridge: one line per triggered run, throttle on runner.completed ──────


@pytest.mark.anyio
async def test_bridge_writes_one_line_with_throttle(journal_path) -> None:
    runner = ScriptRunner(
        [
            Return(
                answer="done",
                usage={
                    "num_turns": 2,
                    "throttle": {
                        "rate_limit_count": 1,
                        "rate_limit_total_s": 30.0,
                        "api_retry_count": 0,
                        "api_retry_total_s": 0.0,
                        "rejected": True,
                    },
                },
            )
        ],
        engine="codex",
    )
    cfg = ExecBridgeConfig(
        transport=BridgeTransport(), presenter=MarkdownPresenter(), final_notify=True
    )
    with structlog.testing.capture_logs() as logs:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
            resume_token=None,
            context=_ctx(),
        )
    (completed,) = [e for e in logs if e["event"] == "runner.completed"]
    assert completed["rate_limit_count"] == 1 and completed["rejected"] is True
    (line,) = _lines(journal_path)
    assert (line["outcome"], line["ok"], line["run_id"]) == ("ok", True, "rid-1")
    assert line["engine"] == "codex" and line["num_turns"] == 2
    assert line["rate_limit_count"] == 1 and line["rejected"] is True
