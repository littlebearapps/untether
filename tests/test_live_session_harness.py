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
from untether.settings import ProgressSettings, WatchdogSettings
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_claude_live.py"
_ENV = (
    "FAKE_CLAUDE_SCENARIO",
    "FAKE_CLAUDE_WAKE_S",
    "FAKE_CLAUDE_TASK_END",
    "FAKE_CLAUDE_ACK_TOOL",
)


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


def _progress(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    import untether.runner_bridge as bridge_mod

    settings = ProgressSettings(**values)
    monkeypatch.setattr(bridge_mod, "_load_progress_settings", lambda: settings)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # The rc11/rc12 assertions below predate the #777 status message and the
    # #785 wake-ack consolidation: pin both off (= rc12 delivery) unless a
    # test opts in via ``_progress``.
    _progress(monkeypatch, show_background_tasks=False, consolidate_wake_turns=False)
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


async def test_wake_turn_pair_one_push_and_real_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#785 end to end: the CLI answers a background agent's result in a
    turn that opens before any task event names it, then again once the
    task's notification lands. The first final carries the task's header
    (not "Claude continued") and pushes; the second doesn't push; the
    subagent-owned task never labels anything."""
    _watchdog(monkeypatch)
    os.environ["FAKE_CLAUDE_TASK_END"] = "mid"
    transport = await _drive("agent_wake_unknown_first")
    sends = transport.send_calls
    first = next(c for c in sends if "The sweep is back" in c["message"].text)
    second = next(c for c in sends if "The sweep has finished" in c["message"].text)
    assert "Background task finished — bg a1" in first["message"].text
    assert "Claude continued" not in first["message"].text
    assert first["options"].notify is True
    assert "Background task finished — bg a1" in second["message"].text
    assert second["options"].notify is False
    assert not any("Inspect nested agents" in c["message"].text for c in sends)


async def test_wake_turn_final_shows_turn_complete_not_its_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#798 end to end: turn 1's final and the wake turn's final both carry
    the #333 "✓ turn complete" marker; the wake turn's in-flight progress
    message never does."""
    from untether.model import TURN_COMPLETE_MARKER

    _watchdog(monkeypatch)
    transport = await _drive("bg_agent_wake")
    turn1_final = [
        c for c in transport.edit_calls if "agent started" in c["message"].text
    ]
    assert turn1_final and TURN_COMPLETE_MARKER in turn1_final[-1]["message"].text
    wake_progress_ref = transport.send_calls[1]["ref"]
    wake_progress = [transport.send_calls[1]["message"].text] + [
        c["message"].text for c in transport.edit_calls if c["ref"] == wake_progress_ref
    ]
    assert all(TURN_COMPLETE_MARKER not in t for t in wake_progress)
    wake_final = next(
        c for c in transport.send_calls if "REPORT: all good" in c["message"].text
    )
    assert TURN_COMPLETE_MARKER in wake_final["message"].text


async def test_max_hold_sends_closing_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _watchdog(monkeypatch, post_result_bg_max_hold=0.5)
    transport = await _drive("bg_bash_wake", wake_s=30)
    notices = [t for t in _texts(transport) if "Closing session" in t]
    assert len(notices) == 1
    assert "1 background task still running" in notices[0]
    assert "bg b1" in notices[0] and "Stopping it" in notices[0]


class _PerSpawnEnvRunner(_LiveRunner):
    """Hands each spawn its own env overrides, in spawn order (#510)."""

    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        spawn_env: list[dict[str, str]] = self._spawn_env  # type: ignore[attr-defined]
        base.update(spawn_env[self.spawned])  # type: ignore[has-type]
        self.spawned += 1  # type: ignore[has-type]
        return base


async def test_510_concurrent_chats_each_bind_their_own_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#510 D3: two chats share ONE ClaudeRunner. Chat A's first result lands
    after chat B's spawn; A's supplementary ``StartedEvent(meta=complete)``
    must not rebind A's progress to B's stream (which then feeds A's stall
    monitor, wake-up countdowns, ``session.summary`` and auto-continue)."""
    import untether.runner_bridge as bridge_mod

    _watchdog(monkeypatch)
    os.environ["FAKE_CLAUDE_SCENARIO"] = "followup"
    runner = _PerSpawnEnvRunner(
        claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions"
    )
    runner.spawned = 0  # type: ignore[attr-defined]
    runner._spawn_env = [  # type: ignore[attr-defined]
        {
            "FAKE_CLAUDE_SESSION_ID": "sess-510-a",
            "FAKE_CLAUDE_RESULT_DELAY_S": "1.5",
        },
        {"FAKE_CLAUDE_SESSION_ID": "sess-510-b"},
    ]

    bound: list[tuple[Any, Any]] = []
    real_run = bridge_mod.run_runner_with_cancel

    async def spy(*args: Any, **kwargs: Any) -> Any:
        outcome = await real_run(*args, **kwargs)
        bound.append((outcome, kwargs["edits"]))
        return outcome

    monkeypatch.setattr(bridge_mod, "run_runner_with_cancel", spy)
    transport = _OrderedTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )

    async def chat(channel_id: int) -> None:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=channel_id, message_id=10, text="go"),
            resume_token=None,
        )

    with anyio.fail_after(25):
        async with anyio.create_task_group() as tg:
            tg.start_soon(chat, 123)
            while runner.spawned < 1:  # type: ignore[attr-defined]
                await anyio.sleep(0.02)
            # Let A's init land so A is bound before B spawns.
            await anyio.sleep(0.3)
            tg.start_soon(chat, 456)

    assert len(bound) == 2
    sessions = set()
    for outcome, edits in bound:
        assert outcome.resume is not None
        assert edits.stream is not None
        assert edits.stream.found_session is not None
        assert edits.stream.found_session.value == outcome.resume.value
        sessions.add(outcome.resume.value)
    assert sessions == {"sess-510-a", "sess-510-b"}


async def test_777_status_message_opens_after_answer_and_finalises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#777 end to end: after the run's answer, live background work gets ONE
    silent status message (replying to the prompt that launched it), edited
    in place and finalised once the task has finished."""
    _watchdog(monkeypatch)
    _progress(monkeypatch, show_background_tasks=True)
    transport = await _drive("bg_agent_wake")
    sends = transport.send_calls
    answer = next(i for i, c in enumerate(transport.log) if "agent started" in c[1])
    status_idx = next(
        i
        for i, (kind, text, _) in enumerate(transport.log)
        if kind == "send" and text.startswith("⏳ background (1)")
    )
    assert answer < status_idx
    status = next(c for c in sends if c["message"].text.startswith("⏳ background"))
    assert status["options"].notify is False
    assert status["options"].reply_to.message_id == 10
    assert "🤖 bg a1" in status["message"].text
    # Exactly one status message; its last edit is the finalised form.
    assert sum(c["message"].text.startswith("⏳ background") for c in sends) == 1
    final = [
        c["message"].text for c in transport.edit_calls if c["ref"] == status["ref"]
    ][-1]
    assert final.splitlines()[0] == "✅ background task done"
    assert "✅ bg a1 done" in final
    # The wake turn's report still arrives as its own message.
    assert any("REPORT: all good" in c["message"].text for c in sends)
