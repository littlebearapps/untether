"""#896: per-run stop at the turn boundary ("Stop at limit").

When a live Claude session's cumulative run cost passes ``max_cost_per_run``
(or the day's total reaches ``max_cost_per_day``) with ``auto_cancel`` on,
the reply is delivered with a stop line and the session is then closed, so
no further wake turns or follow-ups add spend. A turn is never cut.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import anyio
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport
from tests.test_budget_gate import _ON, _write_config
from untether.markdown import MarkdownPresenter
from untether.model import (
    Action,
    ActionEvent,
    CompletedEvent,
    ResumeToken,
    StartedEvent,
    TurnEvent,
)
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import Emit, ScriptRunner

_TOKEN = ResumeToken(engine="claude", value="sess-896")
_PER_RUN = "enabled = true\nmax_cost_per_run = 1.00\nauto_cancel = true"
STOP_LINE = "\N{OCTAGONAL SIGN} Stopped: run cost $1.20 passed the per-run budget $1.00"


@pytest.fixture
def cfg_path() -> Path:
    """The per-test config path (#808 ``_isolated_config`` patched it)."""
    from untether import settings

    return settings.HOME_CONFIG_PATH


class _FakeLive:
    closing = False


def _live_script(first_cost: float, turn_cost: float | None) -> ScriptRunner:
    steps: list = [
        Emit(StartedEvent(engine="claude", resume=_TOKEN)),
        Emit(
            CompletedEvent(
                engine="claude",
                resume=_TOKEN,
                ok=True,
                answer="FIRST-ANSWER",
                usage={"total_cost_usd": first_cost, "num_turns": 1},
            )
        ),
    ]
    if turn_cost is not None:
        steps += [
            Emit(
                TurnEvent(engine="claude", phase="started", turn=2, reason="followup")
            ),
            Emit(
                ActionEvent(
                    engine="claude",
                    action=Action(id="toolu_1", kind="command", title="ls"),
                    phase="started",
                )
            ),
            Emit(
                TurnEvent(
                    engine="claude",
                    phase="completed",
                    turn=2,
                    reason="followup",
                    ok=True,
                    answer="TURN-ANSWER",
                    resume=_TOKEN,
                    usage={"total_cost_usd": turn_cost, "num_turns": 2},
                )
            ),
        ]
    return ScriptRunner(steps, engine="claude", resume_value=_TOKEN.value)


async def _drive_live(monkeypatch: pytest.MonkeyPatch, runner: ScriptRunner):
    from untether.runners import claude as claude_mod

    closes: list[tuple] = []
    transport = FakeTransport()

    async def _close(sid, reason, *, notice=False, only_if_idle=False):
        closes.append((sid, reason, only_if_idle, len(transport.send_calls)))
        return True

    monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: _FakeLive())
    monkeypatch.setattr(claude_mod, "close_live_session", _close)
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    with capture_logs() as logs:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
        )
    return transport, closes, logs


def _texts_with(transport: FakeTransport, needle: str) -> list[str]:
    calls = [*transport.send_calls, *transport.edit_calls]
    return [c["message"].text for c in calls if needle in c["message"].text]


@pytest.mark.anyio
class TestPerRunStop:
    async def test_wake_turn_past_per_run_budget_closes_after_reply(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cumulative run cost (0.60 + a 0.60 turn delta) passes $1.00: the
        turn's reply is delivered with the stop line, then the session is
        closed once, never mid-turn (only_if_idle)."""
        _write_config(cfg_path, _PER_RUN)
        transport, closes, logs = await _drive_live(
            monkeypatch, _live_script(0.60, 1.20)
        )
        first = _texts_with(transport, "FIRST-ANSWER")
        assert first and not any("Stopped" in t for t in first)
        turn_finals = _texts_with(transport, "TURN-ANSWER")
        assert turn_finals and STOP_LINE in turn_finals[-1]
        assert len(closes) == 1
        sid, reason, only_if_idle, sends_before = closes[0]
        assert (sid, reason, only_if_idle) == ("sess-896", "budget_stop", True)
        delivered_at = next(
            i
            for i, c in enumerate(transport.send_calls)
            if "TURN-ANSWER" in c["message"].text
        )
        assert delivered_at < sends_before  # the reply went out first
        stopped = [e for e in logs if e["event"] == "cost_budget.run_stopped"]
        assert len(stopped) == 1
        assert stopped[0]["scope"] == "per_run"

    async def test_first_result_past_budget_closes(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(cfg_path, _PER_RUN)
        transport, closes, _logs = await _drive_live(
            monkeypatch, _live_script(1.20, None)
        )
        finals = _texts_with(transport, "FIRST-ANSWER")
        assert finals and STOP_LINE in finals[-1]
        assert [c[1] for c in closes] == ["budget_stop"]

    async def test_no_stop_without_auto_cancel(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(cfg_path, "enabled = true\nmax_cost_per_run = 1.00")
        transport, closes, logs = await _drive_live(
            monkeypatch, _live_script(0.60, 1.20)
        )
        assert closes == []
        assert not _texts_with(transport, "Stopped:")
        assert not [e for e in logs if e["event"] == "cost_budget.run_stopped"]

    async def test_no_stop_when_budget_unset(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(cfg_path, None)
        transport, closes, _logs = await _drive_live(
            monkeypatch, _live_script(5.0, 10.0)
        )
        assert closes == []
        assert not _texts_with(transport, "Stopped:")

    async def test_under_budget_keeps_session(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(cfg_path, _PER_RUN)
        _transport, closes, _logs = await _drive_live(
            monkeypatch, _live_script(0.30, 0.60)
        )
        assert closes == []

    async def test_no_stop_without_a_live_session(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run with no live process (other engines, live sessions off) has
        already ended: no stop line, nothing to close."""
        from untether.runners import claude as claude_mod

        _write_config(cfg_path, _PER_RUN)
        transport, closes, _logs = await _drive_live(
            monkeypatch, _live_script(1.20, None)
        )
        assert closes  # sanity: the live variant does close
        monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: None)
        transport = FakeTransport()
        cfg = ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        )
        await handle_message(
            cfg,
            runner=_live_script(1.20, None),
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
        )
        assert not _texts_with(transport, "Stopped:")

    async def test_daily_reached_mid_session_closes(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The day's total reaching max_cost_per_day ends the session too, so
        wake turns can't keep spending past the daily budget."""
        _write_config(cfg_path, _ON)  # daily $0.40
        transport, closes, _logs = await _drive_live(
            monkeypatch, _live_script(0.20, 0.45)
        )
        finals = _texts_with(transport, "TURN-ANSWER")
        assert (
            finals
            and (
                "\N{OCTAGONAL SIGN} Stopped: today's cost $0.45 reached the daily "
                "budget $0.40"
            )
            in finals[-1]
        )
        assert [c[1] for c in closes] == ["budget_stop"]


def test_budget_stop_is_a_clean_close_reason() -> None:
    """A budget close at idle may stop cleanly (no quarantine), and the
    background panel and closing notice name it."""
    from untether.background_status import _CLOSE_REASONS
    from untether.runner_bridge import _live_closing_notice
    from untether.runners.claude import _STOPPED_CLEAN_REASONS

    assert "budget_stop" in _STOPPED_CLEAN_REASONS
    assert _CLOSE_REASONS["budget_stop"] == "cost budget reached"
    assert "at the cost budget" in _live_closing_notice("budget_stop", ["build"])


# ---------------------------------------------------------------------------
# Review fix: once a stop is decided, nothing new goes into the session
# ---------------------------------------------------------------------------


class _Pipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def aclose(self) -> None:
        pass


def _install_live(monkeypatch: pytest.MonkeyPatch, *, idle: bool):
    from untether.runners import claude as claude_mod
    from untether.runners.claude import ClaudeStreamState, LiveSession

    state = ClaudeStreamState()
    state.live_mode = True
    state.completed_turns = 1 if idle else 0
    state.turn_open = not idle
    live = LiveSession(session_id=_TOKEN.value, state=state, stdin=_Pipe())
    monkeypatch.setitem(claude_mod._LIVE_SESSIONS, _TOKEN.value, live)
    written: list[str] = []
    closes: list[tuple] = []

    async def _write(session_id, text, *, command_uuid):
        written.append(text)
        return True

    async def _close(sid, reason, *, notice=False, only_if_idle=False):
        closes.append((sid, reason, only_if_idle))
        live.closing = True
        return True

    monkeypatch.setattr(claude_mod, "write_user_message", _write)
    monkeypatch.setattr(claude_mod, "close_live_session", _close)
    return live, written, closes


class _BlockingTransport(FakeTransport):
    """The final send blocks until released — the window in which the
    session sits idle after the over-budget result but before the close."""

    def __init__(self, on_final) -> None:
        super().__init__()
        self.release = anyio.Event()
        self._on_final = on_final

    async def _maybe_block(self, message) -> None:
        if "FIRST-ANSWER" in message.text and not self.release.is_set():
            self._on_final()
            await self.release.wait()

    async def send(self, *, channel_id, message, options=None):
        await self._maybe_block(message)
        return await super().send(
            channel_id=channel_id, message=message, options=options
        )

    async def edit(self, *, ref, message, wait=True):
        await self._maybe_block(message)
        return await super().edit(ref=ref, message=message, wait=wait)


@pytest.mark.anyio
class TestStopShutsInput:
    async def test_queued_followup_not_written_while_final_sends(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A follow-up already waiting in ``inject_when_idle`` must not be
        written once the stop is decided — even though the session goes idle
        while the stopping reply is still being sent. It falls back (False)
        to the resume path instead of running an extra paid turn."""
        from untether.runners import claude as claude_mod

        _write_config(cfg_path, _PER_RUN)
        live, written, closes = _install_live(monkeypatch, idle=False)

        def _went_idle() -> None:
            live.state.completed_turns = 1
            live.state.turn_open = False

        transport = _BlockingTransport(_went_idle)
        cfg = ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        )
        result: dict[str, bool] = {}

        async def _inject() -> None:
            result["ok"] = await claude_mod.inject_when_idle(
                _TOKEN.value, "more", command_uuid="u-1", poll_s=0.01
            )

        async def _drive() -> None:
            await handle_message(
                cfg,
                runner=_live_script(1.20, None),
                incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
                resume_token=None,
            )

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_inject)
                tg.start_soon(_drive)
                await anyio.sleep(0.2)  # the injector polls the idle session
                transport.release.set()
        assert written == []
        assert result["ok"] is False
        assert [c[1] for c in closes] == ["budget_stop"]
        assert live.budget_stopped is True

    async def test_steer_refused_after_stop(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A steer landing between the stop decision and the close is not
        written (``written_idle`` would be a new paid turn)."""
        from untether.runners import claude as claude_mod

        live, written, _closes = _install_live(monkeypatch, idle=True)
        live.budget_stopped = True
        outcome = await claude_mod.steer_into_session(
            _TOKEN.value, "nudge", command_uuid="u-2"
        )
        assert outcome == "window_closed"
        assert written == []
        assert claude_mod.is_session_accepting(_TOKEN.value) is False

    async def test_injector_waits_for_the_result_to_be_accounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With "Stop at limit" armed, a session that has just gone idle is
        not written into until the bridge has accounted the result (and so
        decided whether to stop) — closing the gap between the CLI's result
        and the bridge's budget check."""
        from untether.runners import claude as claude_mod

        live, written, _closes = _install_live(monkeypatch, idle=True)
        live.accounting_armed = True
        result: dict[str, bool] = {}

        async def _inject() -> None:
            result["ok"] = await claude_mod.inject_when_idle(
                _TOKEN.value, "more", command_uuid="u-3", poll_s=0.01
            )

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_inject)
                await anyio.sleep(0.1)
                assert written == []  # result not accounted yet
                live.budget_stopped = True
                claude_mod.note_turn_accounted(_TOKEN.value)
        assert written == []
        assert result["ok"] is False

    async def test_injector_writes_once_accounted_under_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from untether.runners import claude as claude_mod

        live, written, _closes = _install_live(monkeypatch, idle=True)
        live.accounting_armed = True
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: claude_mod.inject_when_idle(
                        _TOKEN.value, "more", command_uuid="u-4", poll_s=0.01
                    )
                )
                await anyio.sleep(0.1)
                assert written == []
                claude_mod.note_turn_accounted(_TOKEN.value)
        assert written == ["more"]

    async def test_accounting_wait_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A result the bridge never accounts can't hold follow-ups forever."""
        from untether.runners import claude as claude_mod

        live, written, _closes = _install_live(monkeypatch, idle=True)
        live.accounting_armed = True
        monkeypatch.setattr(claude_mod, "_ACCOUNTING_WAIT_S", 0.05)
        with capture_logs() as logs, anyio.fail_after(5):
            ok = await claude_mod.inject_when_idle(
                _TOKEN.value, "more", command_uuid="u-5", poll_s=0.01
            )
        assert ok is True
        assert written == ["more"]
        assert any(
            e["event"] == "claude.live_session.accounting_wait_expired" for e in logs
        )

    async def test_steer_into_unaccounted_idle_session_takes_queue_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from untether.runners import claude as claude_mod

        live, written, _closes = _install_live(monkeypatch, idle=True)
        live.accounting_armed = True
        outcome = await claude_mod.steer_into_session(
            _TOKEN.value, "nudge", command_uuid="u-6"
        )
        assert outcome == "result_pending"
        assert written == []

    async def test_maybe_steer_result_pending_queues_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.telegram_fakes import make_cfg
        from untether.runners import claude as claude_mod
        from untether.telegram import steer

        async def _steer(*a, **k):
            return "result_pending"

        monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: object())
        monkeypatch.setattr(claude_mod, "get_pending_ask_request", lambda **k: None)
        monkeypatch.setattr(claude_mod, "steer_into_session", _steer)
        transport = FakeTransport()
        with capture_logs() as logs:
            ok = await steer.maybe_steer(
                make_cfg(transport),
                chat_id=123,
                user_msg_id=7,
                thread_id=None,
                topic_thread_id=None,
                prompt_text="nudge",
                engine="claude",
                resume_token=_TOKEN,
                override="steer",
                running_tasks={},
                chat_prefs=None,
                topic_store=None,
            )
        assert ok is False
        assert transport.send_calls == []  # no "queued instead" notice
        assert any(
            e["event"] == "steer.fallback" and e.get("reason") == "result_pending"
            for e in logs
        )

    async def test_bridge_marks_turn_accounted(
        self, cfg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(cfg_path, _PER_RUN)
        live, _written, _closes = _install_live(monkeypatch, idle=True)
        live.accounting_armed = True
        transport = FakeTransport()
        cfg = ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        )
        await handle_message(
            cfg,
            runner=_live_script(0.10, None),
            incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
            resume_token=None,
        )
        assert live.accounted_turns == live.state.completed_turns == 1
        assert live.budget_stopped is False


def test_stop_at_limit_arms_turn_accounting(cfg_path: Path) -> None:
    from untether import budget_gate

    _write_config(cfg_path, None)
    assert budget_gate.stop_at_limit_active(None) is False
    _write_config(cfg_path, "enabled = true\nmax_cost_per_run = 1.00")
    assert budget_gate.stop_at_limit_active(None) is False
    _write_config(cfg_path, _PER_RUN)
    assert budget_gate.stop_at_limit_active(None) is True


# ---------------------------------------------------------------------------
# #890 interplay: a coalesced repeat error must not swallow the stop line
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_capped_repeat_that_passes_budget_keeps_stop_line(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two background wakes fail on the same latched usage limit; the second
    takes the run past the per-run budget. It must get its own final with
    the stop line rather than being folded into the first error's counter."""
    from tests.test_live_session_bridge import _capped_turn, _run_with_turn
    from untether.runners import claude as claude_mod

    _write_config(cfg_path, _PER_RUN)
    closes: list[str] = []

    async def _close(sid, reason, *, notice=False, only_if_idle=False):
        closes.append(reason)
        return True

    monkeypatch.setattr(claude_mod, "get_live_session", lambda sid: _FakeLive())
    monkeypatch.setattr(claude_mod, "close_live_session", _close)

    def _with_cost(steps: list, cost: float) -> list:
        done = steps[-1].event
        usage = {**(done.usage or {}), "total_cost_usd": cost}
        return [steps[0], Emit(dataclasses.replace(done, usage=usage))]

    transport, _ = await _run_with_turn(
        *_with_cost(_capped_turn(2), 0.60),
        *_with_cost(_capped_turn(3), 1.20),
        end_mid_turn=True,
    )
    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    assert any(STOP_LINE in t for t in texts)
    assert not any("more background wake-up" in t for t in texts)
    assert closes == ["budget_stop"]
