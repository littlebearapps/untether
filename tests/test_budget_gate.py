"""#896: ``[cost_budget] auto_cancel`` ("Stop at limit") is enforced.

Daily gate: once today's (persisted, #898) total reaches ``max_cost_per_day``
new runs are refused — prompts, crons, webhooks, live follow-ups and steers.
Attended chats get a one-shot **Run anyway** button.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import (
    FakeTransport,
    _empty_projects,
    _make_router,
    make_cfg,
)
from untether import budget_gate, cost_tracker
from untether.context import RunContext
from untether.markdown import MarkdownPresenter
from untether.runner_bridge import ExecBridgeConfig
from untether.runners.mock import Return, ScriptRunner
from untether.runners.run_options import EngineRunOptions
from untether.telegram.commands.executor import _run_engine
from untether.telegram.types import TelegramCallbackQuery
from untether.transport import MessageRef
from untether.transport_runtime import TransportRuntime

_BASE = (
    'transport = "telegram"\n'
    "[transports.telegram]\n"
    'bot_token = "1:test"\n'
    "chat_id = 1\n"
    "allow_any_user = true\n"
)

BLOCK_TEXT = (
    "\N{OCTAGONAL SIGN} Daily budget reached ($0.50 of $0.40). "
    "New runs are paused until midnight."
)


@pytest.fixture
def cfg_path() -> Path:
    """The per-test config path (#808 ``_isolated_config`` patched it)."""
    from untether import settings

    return settings.HOME_CONFIG_PATH


@pytest.fixture(autouse=True)
def _clear_pending() -> Any:
    budget_gate._PENDING_RUNS.clear()
    yield
    budget_gate._PENDING_RUNS.clear()


def _write_config(path: Path, budget: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = f"[cost_budget]\n{budget}\n" if budget is not None else ""
    path.write_text(_BASE + extra)


_ON = "enabled = true\nmax_cost_per_day = 0.40\nauto_cancel = true"


def _spend(usd: float) -> None:
    cost_tracker.record_run_cost(usd)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestDailyGatePolicy:
    def test_no_config_never_blocks(self) -> None:
        _spend(100.0)
        assert budget_gate.daily_gate(None) is None

    def test_budget_unset_never_blocks(self, cfg_path: Path) -> None:
        _write_config(cfg_path, None)
        _spend(100.0)
        assert budget_gate.daily_gate(None) is None

    def test_auto_cancel_false_never_blocks(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_day = 0.40\nauto_cancel = false",
        )
        _spend(1.0)
        assert budget_gate.daily_gate(None) is None

    def test_disabled_never_blocks(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = false\nmax_cost_per_day = 0.40\nauto_cancel = true",
        )
        _spend(1.0)
        assert budget_gate.daily_gate(None) is None

    def test_no_daily_limit_never_blocks(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_run = 0.40\nauto_cancel = true",
        )
        _spend(1.0)
        assert budget_gate.daily_gate(None) is None

    def test_below_limit_open(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.39)
        assert budget_gate.daily_gate(None) is None

    def test_at_limit_blocks(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.40)
        assert budget_gate.daily_gate(None) == pytest.approx((0.40, 0.40))

    def test_per_chat_auto_cancel_off_overrides_global_on(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(1.0)
        opts = EngineRunOptions(budget_auto_cancel=False)
        assert budget_gate.daily_gate(opts) is None

    def test_per_chat_auto_cancel_on_overrides_global_off(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_day = 0.40\nauto_cancel = false",
        )
        _spend(1.0)
        opts = EngineRunOptions(budget_auto_cancel=True)
        assert budget_gate.daily_gate(opts) is not None

    def test_per_chat_budget_off_overrides(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(1.0)
        assert budget_gate.daily_gate(EngineRunOptions(budget_enabled=False)) is None

    def test_per_chat_budget_on_overrides_global_off(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = false\nmax_cost_per_day = 0.40\nauto_cancel = true",
        )
        _spend(1.0)
        assert budget_gate.daily_gate(EngineRunOptions(budget_enabled=True)) is not None


class TestRunStopPolicy:
    def test_per_run_cumulative(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_run = 1.00\nauto_cancel = true",
        )
        assert budget_gate.run_stop(0.99, None) is None
        assert budget_gate.run_stop(1.00, None) == ("per_run", 1.00, 1.00)

    def test_per_day(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.45)
        stop = budget_gate.run_stop(0.05, None)
        assert stop is not None
        assert stop[0] == "per_day"
        assert stop[1:] == pytest.approx((0.45, 0.40))

    def test_off_without_auto_cancel(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_run = 0.10\nmax_cost_per_day = 0.10",
        )
        _spend(5.0)
        assert budget_gate.run_stop(5.0, None) is None

    def test_stop_text(self) -> None:
        assert budget_gate.run_stop_text(("per_run", 2.345, 2.0)) == (
            "\N{OCTAGONAL SIGN} Stopped: run cost $2.35 passed the per-run budget $2.00"
        )
        assert budget_gate.run_stop_text(("per_day", 10.5, 10.0)) == (
            "\N{OCTAGONAL SIGN} Stopped: today's cost $10.50 reached the daily "
            "budget $10.00"
        )


class TestPendingRegistry:
    async def _noop(self) -> None:
        return None

    def test_claim_is_one_shot(self) -> None:
        token = budget_gate.register_pending_run(5, self._noop)
        assert budget_gate.claim_pending_run(token, chat_id=5) is not None
        assert budget_gate.claim_pending_run(token, chat_id=5) is None

    def test_foreign_chat_reads_expired_and_keeps_entry(self) -> None:
        """#388 pattern: a tap from another chat never runs (or burns) it."""
        token = budget_gate.register_pending_run(5, self._noop)
        assert budget_gate.claim_pending_run(token, chat_id=6) is None
        assert budget_gate.claim_pending_run(token, chat_id=5) is not None

    def test_unknown_token(self) -> None:
        assert budget_gate.claim_pending_run("deadbeef", chat_id=5) is None

    def test_callback_data_fits_telegram_limit(self) -> None:
        token = budget_gate.register_pending_run(-1001234567890, self._noop)
        data = budget_gate.run_anyway_callback_data(token)
        assert data.startswith(budget_gate.RUN_ANYWAY_PREFIX)
        assert len(data.encode()) <= 64

    def test_registry_bounded(self) -> None:
        for _ in range(budget_gate._PENDING_MAX + 10):
            budget_gate.register_pending_run(5, self._noop)
        assert len(budget_gate._PENDING_RUNS) == budget_gate._PENDING_MAX


# ---------------------------------------------------------------------------
# _run_engine — the choke point for prompts, /continue, crons, webhooks
# ---------------------------------------------------------------------------


def _engine_kit(answer: str = "ok"):
    runner = ScriptRunner([Return(answer=answer)], engine="codex", resume_value="r1")
    transport = FakeTransport()
    exec_cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    runtime = TransportRuntime(router=_make_router(runner), projects=_empty_projects())
    return runner, transport, exec_cfg, runtime


async def _run(exec_cfg, runtime, *, context=None, run_options=None, **kw) -> None:
    await _run_engine(
        exec_cfg=exec_cfg,
        runtime=runtime,
        running_tasks={},
        chat_id=123,
        user_msg_id=7,
        text="hello",
        resume_token=None,
        context=context,
        run_options=run_options,
        **kw,
    )


def _buttons(call: dict) -> list:
    markup = call["message"].extra.get("reply_markup") or {}
    return markup.get("inline_keyboard") or []


@pytest.mark.anyio
class TestRunEngineGate:
    async def test_prompt_blocked_with_run_anyway(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.50)
        runner, transport, exec_cfg, runtime = _engine_kit()
        with capture_logs() as logs:
            await _run(exec_cfg, runtime)
        assert runner.calls == []
        assert len(transport.send_calls) == 1
        call = transport.send_calls[0]
        assert call["message"].text == BLOCK_TEXT
        rows = _buttons(call)
        assert len(rows) == 1 and len(rows[0]) == 1
        assert rows[0][0]["text"] == "Run anyway"
        assert rows[0][0]["callback_data"].startswith(budget_gate.RUN_ANYWAY_PREFIX)
        assert call["options"].reply_to.message_id == 7
        blocked = [e for e in logs if e["event"] == "cost_budget.run_blocked"]
        assert blocked and blocked[0]["scope"] == "per_day"
        assert blocked[0]["attended"] is True

    @pytest.mark.parametrize("source", ["cron:daily", "webhook:gh-push"])
    async def test_unattended_trigger_skipped_with_notice(
        self, cfg_path: Path, source: str
    ) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.50)
        runner, transport, exec_cfg, runtime = _engine_kit()
        with capture_logs() as logs:
            await _run(exec_cfg, runtime, context=RunContext(trigger_source=source))
        assert runner.calls == []
        assert len(transport.send_calls) == 1
        call = transport.send_calls[0]
        assert call["message"].text == (
            "\N{OCTAGONAL SIGN} Daily budget reached ($0.50 of $0.40). "
            f"Skipped {source}; new runs are paused until midnight."
        )
        assert _buttons(call) == []
        assert budget_gate._PENDING_RUNS == {}
        blocked = [e for e in logs if e["event"] == "cost_budget.run_blocked"]
        assert blocked[0]["scope"] == "per_day"
        assert blocked[0]["trigger"] == source

    async def test_queued_placeholder_removed(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.50)
        _runner, transport, exec_cfg, runtime = _engine_kit()
        placeholder = MessageRef(channel_id=123, message_id=99)
        await _run(exec_cfg, runtime, progress_ref=placeholder)
        assert placeholder in transport.delete_calls

    async def test_runs_normally_below_limit(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(0.10)
        runner, _transport, exec_cfg, runtime = _engine_kit()
        await _run(exec_cfg, runtime)
        assert len(runner.calls) == 1

    async def test_no_change_when_auto_cancel_false(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_day = 0.40\nauto_cancel = false",
        )
        _spend(5.0)
        runner, _transport, exec_cfg, runtime = _engine_kit()
        await _run(exec_cfg, runtime)
        assert len(runner.calls) == 1

    async def test_no_change_when_budget_unset(self, cfg_path: Path) -> None:
        _write_config(cfg_path, None)
        _spend(5.0)
        runner, _transport, exec_cfg, runtime = _engine_kit()
        await _run(exec_cfg, runtime)
        assert len(runner.calls) == 1

    async def test_per_chat_override_lets_run_through(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(5.0)
        runner, _transport, exec_cfg, runtime = _engine_kit()
        await _run(
            exec_cfg, runtime, run_options=EngineRunOptions(budget_auto_cancel=False)
        )
        assert len(runner.calls) == 1

    async def test_per_chat_override_blocks(self, cfg_path: Path) -> None:
        _write_config(
            cfg_path,
            "enabled = true\nmax_cost_per_day = 0.40\nauto_cancel = false",
        )
        _spend(5.0)
        runner, _transport, exec_cfg, runtime = _engine_kit()
        await _run(
            exec_cfg, runtime, run_options=EngineRunOptions(budget_auto_cancel=True)
        )
        assert runner.calls == []

    async def test_bypass_runs(self, cfg_path: Path) -> None:
        _write_config(cfg_path, _ON)
        _spend(5.0)
        runner, _transport, exec_cfg, runtime = _engine_kit()
        await _run(exec_cfg, runtime, budget_bypass=True)
        assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Run anyway callback
# ---------------------------------------------------------------------------


def _query(chat_id: int, data: str, message_id: int = 1) -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        callback_query_id="cbq-1",
        data=data,
        sender_id=42,
    )


@pytest.mark.anyio
class TestRunAnywayCallback:
    async def _blocked(self, cfg_path: Path):
        from untether.telegram.budget_notice import handle_budget_run_callback

        _write_config(cfg_path, _ON)
        _spend(0.50)
        transport = FakeTransport()
        runner = ScriptRunner([Return(answer="ok")], engine="codex", resume_value="r1")
        cfg = make_cfg(transport, runner)
        await _run(cfg.exec_cfg, cfg.runtime)
        assert runner.calls == []
        notice = transport.send_calls[-1]
        data = _buttons(notice)[0][0]["callback_data"]
        return handle_budget_run_callback, cfg, transport, runner, notice, data

    async def test_run_anyway_runs_exactly_once(self, cfg_path: Path) -> None:
        handle, cfg, transport, runner, notice, data = await self._blocked(cfg_path)
        with capture_logs() as logs:
            await handle(cfg, _query(123, data, notice["ref"].message_id))
        assert len(runner.calls) == 1
        assert cfg.bot.callback_calls[-1]["text"] == "Running once"
        # The notice loses its button.
        edits = [e for e in transport.edit_calls if e["ref"] == notice["ref"]]
        assert edits and edits[0]["message"].extra["reply_markup"] == {
            "inline_keyboard": []
        }
        assert any(e["event"] == "cost_budget.run_anyway" for e in logs)
        # A second tap is spent.
        await handle(cfg, _query(123, data, notice["ref"].message_id))
        assert len(runner.calls) == 1
        assert cfg.bot.callback_calls[-1]["text"] == "This button has expired"

    async def test_foreign_chat_cannot_use_it(self, cfg_path: Path) -> None:
        handle, cfg, _transport, runner, notice, data = await self._blocked(cfg_path)
        await handle(cfg, _query(999, data, notice["ref"].message_id))
        assert runner.calls == []
        assert cfg.bot.callback_calls[-1]["text"] == "This button has expired"
        # Still usable from its own chat.
        await handle(cfg, _query(123, data, notice["ref"].message_id))
        assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Live follow-ups and steers would start a turn without _run_engine
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestLiveFollowupGate:
    async def test_inject_refused_and_session_closed(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from untether import live_followup
        from untether.model import ResumeToken
        from untether.runners import claude as claude_mod
        from untether.scheduler import ThreadJob

        _write_config(cfg_path, _ON)
        _spend(0.50)
        closes: list[tuple] = []
        injected: list[str] = []

        class _Live:
            closing = False

            class state:
                plan_rearm_failed = False
                spawn_run_options = EngineRunOptions()

        async def _close(sid, reason, *, notice=False, only_if_idle=False):
            closes.append((sid, reason, only_if_idle))
            return True

        async def _inject(sid, text, *, command_uuid=None):
            injected.append(text)
            return True

        monkeypatch.setattr(claude_mod, "is_session_accepting", lambda sid: True)
        monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: _Live())
        monkeypatch.setattr(claude_mod, "close_live_session", _close)
        monkeypatch.setattr(claude_mod, "inject_when_idle", _inject)

        async def _opts(job):
            return EngineRunOptions()

        job = ThreadJob(
            chat_id=123,
            user_msg_id=7,
            text="more",
            resume_token=ResumeToken(engine="claude", value="sess-1"),
        )
        ok = await live_followup.inject_live_followup(job, options_for=_opts)
        assert ok is False
        assert injected == []
        assert closes == [("sess-1", "budget_stop", True)]

    async def test_inject_unchanged_when_open(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from untether import live_followup
        from untether.model import ResumeToken
        from untether.runners import claude as claude_mod
        from untether.scheduler import ThreadJob

        _write_config(cfg_path, _ON)
        _spend(0.10)
        injected: list[str] = []

        class _Live:
            closing = False

            class state:
                plan_rearm_failed = False
                spawn_run_options = EngineRunOptions()

        async def _inject(sid, text, *, command_uuid=None):
            injected.append(text)
            return True

        monkeypatch.setattr(claude_mod, "is_session_accepting", lambda sid: True)
        monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: _Live())
        monkeypatch.setattr(claude_mod, "inject_when_idle", _inject)

        async def _opts(job):
            return EngineRunOptions()

        job = ThreadJob(
            chat_id=123,
            user_msg_id=7,
            text="more",
            resume_token=ResumeToken(engine="claude", value="sess-1"),
        )
        assert await live_followup.inject_live_followup(job, options_for=_opts)
        assert injected == ["more"]

    async def test_steer_refused(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from untether.model import ResumeToken
        from untether.runners import claude as claude_mod
        from untether.telegram import steer

        _write_config(cfg_path, _ON)
        _spend(0.50)
        steered: list[str] = []

        async def _steer(*a, **k):
            steered.append("x")
            return "steered"

        monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: object())
        monkeypatch.setattr(claude_mod, "get_pending_ask_request", lambda **k: None)
        monkeypatch.setattr(claude_mod, "steer_into_session", _steer)

        async def _opts(token):
            return EngineRunOptions()

        cfg = make_cfg(FakeTransport())
        with capture_logs() as logs:
            ok = await steer.maybe_steer(
                cfg,
                chat_id=123,
                user_msg_id=7,
                thread_id=None,
                topic_thread_id=None,
                prompt_text="nudge",
                engine="claude",
                resume_token=ResumeToken(engine="claude", value="sess-1"),
                override="steer",
                running_tasks={},
                chat_prefs=None,
                topic_store=None,
                run_options=_opts,
            )
        assert ok is False
        assert steered == []
        assert any(
            e["event"] == "steer.fallback" and e.get("reason") == "budget" for e in logs
        )


# ---------------------------------------------------------------------------
# End to end through the Telegram loop: message refused, button routed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_loop_refuses_prompt_and_routes_run_anyway(
    cfg_path: Path,
) -> None:
    import anyio

    from untether.telegram.loop import run_main_loop
    from untether.telegram.types import TelegramIncomingMessage

    _write_config(cfg_path, _ON)
    _spend(0.50)
    transport = FakeTransport()
    runner = ScriptRunner([Return(answer="ok")], engine="codex", resume_value="r1")
    cfg = make_cfg(transport, runner)
    sent = anyio.Event()

    async def poller(_cfg):
        yield TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=123,
        )
        with anyio.fail_after(5):
            while not transport.send_calls:
                await anyio.sleep(0.01)
        notice = transport.send_calls[0]
        sent.set()
        yield TelegramCallbackQuery(
            transport="telegram",
            chat_id=123,
            message_id=notice["ref"].message_id,
            callback_query_id="cbq-run",
            data=_buttons(notice)[0][0]["callback_data"],
            sender_id=123,
        )
        with anyio.fail_after(5):
            while not runner.calls:
                await anyio.sleep(0.01)

    await run_main_loop(cfg, poller)

    assert sent.is_set()
    assert transport.send_calls[0]["message"].text == BLOCK_TEXT
    assert len(runner.calls) == 1
    assert cfg.bot.callback_calls[-1]["text"] == "Running once"
