"""#896: per-run stop at the turn boundary ("Stop at limit").

When a live Claude session's cumulative run cost passes ``max_cost_per_run``
(or the day's total reaches ``max_cost_per_day``) with ``auto_cancel`` on,
the reply is delivered with a stop line and the session is then closed, so
no further wake turns or follow-ups add spend. A turn is never cut.
"""

from __future__ import annotations

from pathlib import Path

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
