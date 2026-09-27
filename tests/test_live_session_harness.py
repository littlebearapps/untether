"""#776 end to end: a REAL ``handle_message`` + REAL ``ClaudeRunner`` against
the control-channel fake ``tests/fake_clis/fake_claude_live.py``.

Proves post-result turns reach Telegram as their own messages (phase 04).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from tests.telegram_fakes import FakeTransport
from untether.markdown import MarkdownPresenter
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    handle_message,
)
from untether.runners import claude as claude_mod
from untether.runners.claude import ClaudeRunner
from untether.session_quarantine import QuarantineStore, set_quarantine_store
from untether.settings import WatchdogSettings
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_claude_live.py"
_ENV = ("FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_WAKE_S")


class _OrderedTransport(FakeTransport):
    """FakeTransport that also keeps one ordered log of sends and edits."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple[str, str, Any]] = []

    async def send(self, *, channel_id, message, options=None):  # type: ignore[override]
        ref = await super().send(
            channel_id=channel_id, message=message, options=options
        )
        self.log.append(("send", message.text, options))
        return ref

    async def edit(self, *, ref, message, wait=True):  # type: ignore[override]
        self.log.append(("edit", message.text, None))
        return await super().edit(ref=ref, message=message, wait=wait)


class _LiveRunner(ClaudeRunner):
    _live_poll_s = 0.05
    _live_close_grace_s = 2.0

    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        for key in _ENV:
            if key in os.environ:
                base[key] = os.environ[key]
        return base


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    set_quarantine_store(QuarantineStore(tmp_path / "q.json"))
    yield
    set_quarantine_store(None)
    for key in _ENV:
        os.environ.pop(key, None)


def _watchdog(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values = {"post_result_limbo_grace": 0.4, **overrides}
    watchdog = WatchdogSettings.model_construct(
        **{**WatchdogSettings().model_dump(), **values}
    )
    monkeypatch.setattr(
        claude_mod,
        "load_settings_if_exists",
        lambda *a, **k: (SimpleNamespace(watchdog=watchdog), Path("x")),
    )


async def _drive(
    scenario: str,
    *,
    wake_s: float = 0.3,
    running_tasks: dict[MessageRef, RunningTask] | None = None,
    timeout: float = 25.0,
) -> _OrderedTransport:
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    os.environ["FAKE_CLAUDE_WAKE_S"] = str(wake_s)
    transport = _OrderedTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")
    with anyio.fail_after(timeout):
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
            resume_token=None,
            running_tasks=running_tasks,
        )
    return transport


def _texts(transport: FakeTransport) -> list[str]:
    return [c["message"].text for c in transport.send_calls]


async def test_wake_turn_sends_new_final_with_bell_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    transport = await _drive("bg_bash_wake")
    log = transport.log
    first = next(i for i, (_, t, _) in enumerate(log) if "waiting" in t)
    wake = next(i for i, (_, t, _) in enumerate(log) if "GOT: BG-FINISHED" in t)
    # Turn 1's final lands first (#591: at result time, not at exit) and the
    # wake turn is a separate, later *new* message.
    assert first < wake
    kind, text, options = log[wake]
    assert kind == "send"
    assert "Background task finished" in text and "bg b1" in text
    # Anchored under the user's original message.
    assert options.reply_to.message_id == 10


async def test_short_wake_turn_sends_final_only_no_progress_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    transport = await _drive("bg_bash_wake")
    # initial progress (turn-1 final edits it) + the wake final. No progress
    # message for a short, tool-less wake turn.
    assert [t for k, t, _ in transport.log if k == "send"][1:] == [
        t for k, t, _ in transport.log if k == "send" and "GOT: BG-FINISHED" in t
    ]
    assert len(transport.send_calls) == 2


async def test_wake_turn_final_notify_true_monitor_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    transport = await _drive("monitor_ticks")
    by_text = {c["message"].text: c for c in transport.send_calls}
    tick1 = next(c for t, c in by_text.items() if "TICK 1" in t)
    tick3 = next(c for t, c in by_text.items() if "TICK 3" in t)
    assert tick1["options"].notify is False  # Monitor tick: silent
    assert tick3["options"].notify is True  # the stream ended: task finished


async def test_tool_using_wake_turn_gets_progress_then_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    running_tasks: dict[MessageRef, RunningTask] = {}
    transport = await _drive("bg_agent_wake", running_tasks=running_tasks)
    texts = _texts(transport)
    assert any("REPORT: all good" in t for t in texts)
    # initial progress (edited into turn 1's final), wake progress, wake
    # final.
    assert len(transport.send_calls) == 3
    wake_progress_ref = transport.send_calls[1]["ref"]
    # The wake final replaced its progress message (the Telegram client
    # deletes the replaced message; FakeTransport just records it).
    wake_final = next(
        c for c in transport.send_calls if "REPORT: all good" in c["message"].text
    )
    assert wake_final["options"].replace == wake_progress_ref
    # Every alias for the live run was released at the end.
    assert running_tasks == {}


async def test_max_hold_sends_closing_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _watchdog(monkeypatch, post_result_bg_max_hold=0.5)
    transport = await _drive("bg_bash_wake", wake_s=30)
    notices = [t for t in _texts(transport) if "Closing session" in t]
    assert len(notices) == 1
    assert "1 background task" in notices[0] and "bg b1" in notices[0]
