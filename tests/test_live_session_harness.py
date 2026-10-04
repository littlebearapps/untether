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
from structlog.testing import capture_logs

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
from untether.telegram.bridge import TelegramPresenter
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_claude_live.py"
_ENV = (
    "FAKE_CLAUDE_SCENARIO",
    "FAKE_CLAUDE_WAKE_S",
    "FAKE_CLAUDE_TASK_END",
    "FAKE_CLAUDE_ACK_TOOL",
    "FAKE_CLAUDE_EOF_MODE",
    "FAKE_CLAUDE_SIGINT_RC",
    # #684
    "FAKE_CLAUDE_CANCEL_AFTER_S",
    "FAKE_CLAUDE_AFTER_CANCEL_S",
    # #900
    "FAKE_CLAUDE_ERROR_TEXT",
    "FAKE_CLAUDE_WAKE_OK",
    # #921: hold the first turn open while a follow-up waits to be injected.
    "FAKE_CLAUDE_RESULT_DELAY_S",
    # #928
    "FAKE_CLAUDE_NO_QUERY_INIT",
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
    timings: dict[str, float] | None = None,
) -> _OrderedTransport:
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    os.environ["FAKE_CLAUDE_WAKE_S"] = str(wake_s)
    transport = _OrderedTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")
    # Slots dataclass: timing knobs must be set on the instance.
    for name, value in (timings or {}).items():
        setattr(runner, name, value)
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
    # #829: the hold counts quiet time; the outcome follows once it exited.
    assert "with no progress for" in notices[0]
    assert "reply to continue" not in notices[0].lower()
    closed = [
        c for c in transport.send_calls if "Reply to continue" in c["message"].text
    ]
    assert len(closed) == 1
    assert closed[0]["message"].text.endswith("Reply to continue in the same session.")
    assert closed[0]["options"].notify is False
    texts = _texts(transport)
    assert texts.index(notices[0]) < texts.index(closed[0]["message"].text)


_FAST_CLOSE = {
    "_live_poll_s": 0.05,
    "_live_close_grace_s": 0.5,
    "_live_close_sigint_grace_s": 0.8,
}


async def test_829_agent_close_stopped_by_sigint_offers_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2 end to end: an agent ignores EOF, SIGINT stops it with rc 0 — not
    quarantined, so the user is told the same session continues."""
    _watchdog(monkeypatch, post_result_bg_max_hold=0.5)
    os.environ["FAKE_CLAUDE_EOF_MODE"] = "until_sigint"
    transport = await _drive("bg_agent_silent", timings=_FAST_CLOSE)
    texts = _texts(transport)
    assert any("Closing session" in t and "Stopping it" in t for t in texts)
    assert any(t.endswith("Reply to continue in the same session.") for t in texts)
    assert not any("fresh session" in t for t in texts)


async def test_829_unclean_close_warns_of_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch, post_result_bg_max_hold=0.5)
    os.environ["FAKE_CLAUDE_EOF_MODE"] = "until_sigint"
    os.environ["FAKE_CLAUDE_SIGINT_RC"] = "1"
    transport = await _drive("bg_agent_silent", timings=_FAST_CLOSE)
    warning = [c for c in transport.send_calls if "fresh session" in c["message"].text]
    assert len(warning) == 1 and warning[0]["options"].notify is False
    assert not any("Reply to continue" in t for t in _texts(transport))


async def test_829_idle_close_without_tasks_sends_no_closed_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    transport = await _drive("followup", timings={"_live_poll_s": 0.05})
    assert not any("Reply to continue" in t for t in _texts(transport))
    assert not any("fresh session" in t for t in _texts(transport))


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
    assert "✅ bg a1 ·" in final
    # The wake turn's report still arrives as its own message.
    assert any("REPORT: all good" in c["message"].text for c in sends)


# ── #812: hooks ─────────────────────────────────────────────────────────────


async def test_812_hook_flood_no_progress_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """40 PreToolUse/PostToolUse started/response pairs around one tool call:
    the hook frames produce no UntetherEvents, so no progress row, no edit
    and no message mentions them — only the real tool and the answer."""
    _watchdog(monkeypatch)
    transport = await _drive("hook_flood")
    everything = [t for _, t, _ in transport.log]
    assert any("FLOOD DONE" in t for t in everything)
    for text in everything:
        assert "PreToolUse" not in text
        assert "PostToolUse" not in text
        assert "hook" not in text.lower()
    # initial progress (edited into the final) — nothing else was sent.
    assert len(transport.send_calls) == 1


async def test_812_async_rewake_arrives_as_pushed_hook_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the Stop hook outlives the 0.4 s idle grace; the live
    session holds, the rewake turn runs and is delivered as its own pushed
    ``🪝 Hook feedback — Stop`` message quoting the finding."""
    _watchdog(monkeypatch)
    transport = await _drive("async_rewake_idle", wake_s=1.5)
    wake = next(c for c in transport.send_calls if "HOOK: finding" in c["message"].text)
    assert "\N{HOOK} Hook feedback — Stop" in wake["message"].text
    assert wake["options"].notify is True
    assert wake["options"].reply_to.message_id == 10


# ── #383: a queued wake turn after a plan approval starts in plan mode ──────


class _PlanTransport(_OrderedTransport):
    """Taps Approve on the ExitPlanMode keyboard and delivers turn 1's final
    slowly (2 s), so the bridge's on_completed genuinely holds the yield."""

    def __init__(self) -> None:
        super().__init__()
        self.approved: set[str] = set()

    async def _tap_approvals(self, message: Any) -> None:
        # MarkdownPresenter renders no keyboard; tap on the approval's text
        # (the fake's ExitPlanMode request id is fixed).
        from untether.runners.claude import send_claude_control_response

        if "tool: ExitPlanMode" in (message.text or "") and not self.approved:
            self.approved.add("req-epm-1")
            assert "Approve" not in message.text  # the caption, not a button
            assert await send_claude_control_response("req-epm-1", True)

    async def _slow_final(self, message: Any) -> None:
        if "PLANNED" in (message.text or ""):
            await anyio.sleep(2.0)

    async def send(self, *, channel_id, message, options=None):  # type: ignore[override]
        await self._slow_final(message)
        ref = await super().send(
            channel_id=channel_id, message=message, options=options
        )
        await self._tap_approvals(message)
        return ref

    async def edit(self, *, ref, message, wait=True):  # type: ignore[override]
        await self._slow_final(message)
        out = await super().edit(ref=ref, message=message, wait=wait)
        await self._tap_approvals(message)
        return out


class _PlanLiveRunner(_LiveRunner):
    def env(self, *, state: Any) -> dict[str, str] | None:
        base = super().env(state=state) or {}
        for key in (
            "FAKE_CLAUDE_START_MODE",
            "FAKE_CLAUDE_WAKE_AFTER_RESULT_S",
        ):
            if key in os.environ:
                base[key] = os.environ[key]
        return base


async def test_383_queued_wake_with_slow_final_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "plan_approve_queued_wake")
    monkeypatch.setenv("FAKE_CLAUDE_START_MODE", "plan")
    # The bridge consumes the events before the result at its own pace (one
    # run measured ~100 ms), so the wake turn starts 0.5 s after the result:
    # still far inside the 2 s slow final, which a post-yield re-arm misses.
    monkeypatch.setenv("FAKE_CLAUDE_WAKE_AFTER_RESULT_S", "0.5")
    transport = _PlanTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )
    runner = _PlanLiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="plan")
    with anyio.fail_after(25.0):
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
            resume_token=None,
        )
    assert transport.approved  # the plan was approved from the keyboard
    wake = [t for k, t, _ in transport.log if k == "send" and "Background task" in t]
    assert wake and "MODE: plan" in wake[-1]
    # Approved (not timed out): the plan was re-shown as approved.
    assert any("Plan (approved)" in t for _, t, _ in transport.log)


# ---------------------------------------------------------------------------
# #684: control_request.unanswerable + withdrawn requests, end to end
# ---------------------------------------------------------------------------


class _RenderLog(_OrderedTransport):
    """Keeps every rendered message (sends and edits) in order."""

    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[Any] = []

    async def send(self, *, channel_id, message, options=None):  # type: ignore[override]
        self.rendered.append(message)
        return await super().send(
            channel_id=channel_id, message=message, options=options
        )

    async def edit(self, *, ref, message, wait=True):  # type: ignore[override]
        self.rendered.append(message)
        return await super().edit(ref=ref, message=message, wait=wait)


class _SwallowingPresenter(TelegramPresenter):
    """A #683-style swallowed keyboard: control rows never reach Telegram
    (the cancel row stays)."""

    def render_progress(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        rendered = super().render_progress(*args, **kwargs)
        markup = rendered.extra.get("reply_markup")
        if isinstance(markup, dict):
            rows = [
                row
                for row in markup.get("inline_keyboard", [])
                if not any(
                    str(b.get("callback_data", "")).startswith(
                        ("claude_control:", "aq:")
                    )
                    for b in row
                )
            ]
            rendered.extra["reply_markup"] = {**markup, "inline_keyboard": rows}
        return rendered


def _fast_684(monkeypatch: pytest.MonkeyPatch, **watchdog: Any) -> None:
    """Tiny tool_timeout and heartbeat so the run-level monitor checks fast."""
    import untether.runner_bridge as bridge_mod

    _watchdog(monkeypatch)
    progress = ProgressSettings.model_construct(
        **{
            **ProgressSettings().model_dump(),
            "heartbeat_interval": 0.05,
            "min_render_interval": 0.0,
            "show_background_tasks": False,
            "consolidate_wake_turns": False,
        }
    )
    monkeypatch.setattr(bridge_mod, "_load_progress_settings", lambda: progress)
    wd = WatchdogSettings.model_construct(
        **{**WatchdogSettings().model_dump(), "tool_timeout": 0.5, **watchdog}
    )
    monkeypatch.setattr(bridge_mod, "_load_watchdog_settings", lambda: wd)


async def _drive_684(
    scenario: str,
    *,
    presenter: Any = None,
    cancel_after: float | None = None,
    timeout: float = 20.0,
) -> _RenderLog:
    """Drive a Bash-approval scenario (permission_mode=default, #749) through
    the real handle_message; optionally /cancel the run after a delay."""
    from untether.runner_bridge import unique_running_tasks

    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    transport = _RenderLog()
    cfg = ExecBridgeConfig(
        transport=transport,
        # TelegramPresenter attaches the approval keyboard (MarkdownPresenter
        # renders none).
        presenter=presenter or TelegramPresenter(),
        final_notify=False,
    )
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="default")
    running: dict[MessageRef, RunningTask] = {}

    async def _cancel() -> None:
        assert cancel_after is not None
        await anyio.sleep(cancel_after)
        while not running:
            await anyio.sleep(0.02)
        (_, task), *_ = unique_running_tasks(running)
        task.cancel_requested.set()

    with anyio.fail_after(timeout):
        async with anyio.create_task_group() as tg:
            if cancel_after is not None:
                tg.start_soon(_cancel)
            await handle_message(
                cfg,
                runner=runner,
                incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
                resume_token=None,
                running_tasks=running,
            )
    return transport


def _control_cbs(msg: Any) -> frozenset[str]:
    from untether.runner_bridge import control_callbacks_in

    return control_callbacks_in(msg)


def _unanswerable(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == "control_request.unanswerable"]


async def test_684_harness_rendered_keyboard_suppresses_unanswerable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: the approval keyboard is on the progress message, so a wait
    past tool_timeout is a healthy one — ``control_surface_probe`` sees the
    real render."""
    from structlog.testing import capture_logs

    _fast_684(monkeypatch)
    with capture_logs() as logs:
        transport = await _drive_684("control_unanswered", cancel_after=1.5)
    assert any(
        "claude_control:approve:req-unanswered-1" in _control_cbs(m)
        for m in transport.rendered
    )
    assert _unanswerable(logs) == []
    (summary,) = [e for e in logs if e.get("event") == "session.summary"]
    assert summary["unanswerable_control_requests"] == 0


async def test_684_harness_swallowed_keyboard_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced positive (R15-6e/f are opportunistic live): the keyboard never
    reaches Telegram, so the pending Bash approval is unanswerable."""
    from structlog.testing import capture_logs

    _fast_684(monkeypatch)
    with capture_logs() as logs:
        transport = await _drive_684(
            "control_unanswered", presenter=_SwallowingPresenter(), cancel_after=1.5
        )
    assert not any(_control_cbs(m) for m in transport.rendered)
    (hit,) = _unanswerable(logs)
    assert hit["log_level"] == "warning"
    assert hit["reasons"] == ["no_keyboard"] and hit["kind"] == "tool"
    assert hit["request_id"] == "req-unanswered-1" and hit["live_idle"] is False
    (summary,) = [e for e in logs if e.get("event") == "session.summary"]
    assert summary["unanswerable_control_requests"] == 1


async def test_684_harness_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    _fast_684(monkeypatch, detect_unanswerable_control_requests=False)
    with capture_logs() as logs:
        await _drive_684(
            "control_unanswered", presenter=_SwallowingPresenter(), cancel_after=1.0
        )
    assert _unanswerable(logs) == []


async def test_684_harness_cancel_strips_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI withdraws the request: the progress message is re-rendered
    without the approval keyboard before the turn's answer lands."""
    from structlog.testing import capture_logs

    _fast_684(monkeypatch)
    os.environ["FAKE_CLAUDE_CANCEL_AFTER_S"] = "0.8"
    os.environ["FAKE_CLAUDE_AFTER_CANCEL_S"] = "0.8"
    with capture_logs() as logs:
        transport = await _drive_684("control_cancel")
    with_kb = [i for i, m in enumerate(transport.rendered) if _control_cbs(m)]
    assert with_kb, "the approval keyboard was rendered first"
    later = transport.rendered[with_kb[-1] + 1 :]
    assert any("withdrawn" in m.text and "Stopped." not in m.text for m in later), (
        "a keyboard-free progress render must precede the answer"
    )
    assert any("Stopped." in m.text for m in later)
    # A healthy wait (keyboard on screen), then retired: never unanswerable.
    assert _unanswerable(logs) == []


# ── #819: compaction and % ctx end to end ──────────────────────────────────


@pytest.fixture
def _fake_window():
    claude_mod._CONTEXT_WINDOWS["claude-haiku-fake"] = 200_000
    yield
    claude_mod._CONTEXT_WINDOWS.pop("claude-haiku-fake", None)


@pytest.mark.usefixtures("_fake_window")
async def test_819_live_compact_followup_renders_rows_and_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``/compact`` follow-up into a live session: its own message, a
    ``done`` final with the compaction body (not an empty ``error``), no
    #596/#631 recovery, and the session stays live for the next follow-up,
    answered in the same process with a lower ``% ctx``."""
    from structlog.testing import capture_logs

    from untether.live_followup import inject_live_followup
    from untether.model import ResumeToken
    from untether.scheduler import ThreadJob, ThreadScheduler

    _watchdog(monkeypatch, post_result_limbo_grace=1.0)
    sid = "fake-live-session"

    async def run_job(job: ThreadJob) -> None:  # pragma: no cover
        raise AssertionError("follow-up should be injected, not resumed")

    def _job(text: str, msg_id: int) -> ThreadJob:
        return ThreadJob(
            chat_id=123,
            user_msg_id=msg_id,
            text=text,
            resume_token=ResumeToken(engine="claude", value=sid),
            progress_ref=MessageRef(channel_id=123, message_id=msg_id + 70),
        )

    async def _wait_idle(turns: int) -> None:
        with anyio.fail_after(20):
            while True:
                live = claude_mod.get_live_session(sid)
                if (
                    live is not None
                    and live.idle
                    and live.state.completed_turns >= turns
                ):
                    return
                await anyio.sleep(0.02)

    holder: dict[str, Any] = {}
    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            sched = ThreadScheduler(
                task_group=tg, run_job=run_job, inject_job=inject_live_followup
            )

            async def follow_ups() -> None:
                await _wait_idle(1)
                await sched.enqueue(_job("/compact", 20))
                await _wait_idle(2)
                await sched.enqueue(_job("after", 30))

            async def drive() -> None:
                holder["transport"] = await _drive("compact_followup")

            tg.start_soon(follow_ups)
            tg.start_soon(drive)

    transport = holder["transport"]
    events = [r.get("event") for r in logs]
    assert "runner.empty_result" not in events
    assert "session.quarantined" not in events
    assert "session.auto_resend_fresh" not in events
    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    compact_final = [
        t for t in texts if "🗜️ Context compacted · 60k → 2k tokens (manual)" in t
    ]
    assert compact_final
    header = compact_final[-1].splitlines()[0]
    assert header.startswith("done")
    assert "% ctx" not in header  # D5: dropped after the compaction
    first_final = [t for t in texts if "FIRST" in t and t.startswith("done")]
    assert first_final and "30% ctx" in first_final[-1].splitlines()[0]
    after_final = [t for t in texts if "ECHO: after" in t]
    assert after_final and "10% ctx" in after_final[-1].splitlines()[0]
    completed = [r for r in logs if r.get("event") == "runner.completed"]
    assert any(r.get("compactions") == 1 for r in completed)


@pytest.mark.usefixtures("_fake_window")
async def test_819_context_pct_in_progress_and_final_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    transport = await _drive("context_usage_growth", wake_s=0.7)
    texts = [c["message"].text for c in (*transport.send_calls, *transport.edit_calls)]
    final = [t for t in texts if "GROWN" in t]
    assert final and final[-1].splitlines()[0].endswith("· 62% ctx")
    progress_headers = [
        t.splitlines()[0] for t in texts if "GROWN" not in t and "% ctx" in t
    ]
    # The value rises while the turn runs (the header updates on every
    # progress edit), not only at the final.
    assert any("10% ctx" in h or "30% ctx" in h for h in progress_headers), texts


# ── #900: an errored run final is not overtaken by a wake final ──────────────


def _first_index(transport: _OrderedTransport, needle: str) -> int:
    return next(i for i, (_, t, _) in enumerate(transport.log) if needle in t)


def _bridge_watchdog(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    """The bridge's own ``[watchdog]`` read (the #572 retry gate)."""
    import untether.runner_bridge as bridge_mod

    watchdog = WatchdogSettings.model_construct(
        **{**WatchdogSettings().model_dump(), **values}
    )
    monkeypatch.setattr(bridge_mod, "_load_watchdog_settings", lambda: watchdog)


_LIMIT = (
    "You've hit your session limit · resets 5:30pm (Australia/Melbourne) RUN-FAILED"
)


@pytest.mark.parametrize(
    ("wake_ok", "error_text"),
    [(False, None), (True, None), (False, _LIMIT)],
    ids=["wake-error", "wake-ok", "usage-limit"],
)
async def test_900_errored_run_final_lands_before_the_wake_final(
    monkeypatch: pytest.MonkeyPatch, wake_ok: bool, error_text: str | None
) -> None:
    """#900 (mac 2026-10-02): the run's own result is an error (there: the
    usage limit) while a background agent runs on; the agent's wake turn
    finishes before the CLI exits. The run's error final must go out first,
    and exactly once — not be held until the session closes while the wake
    final overtakes it."""
    _watchdog(monkeypatch)
    # The usage-limit text latches a process-wide reset; keep it per test.
    monkeypatch.setattr(claude_mod, "_RATE_LIMIT_RESET_LATCH", {})
    if wake_ok:
        os.environ["FAKE_CLAUDE_WAKE_OK"] = "1"
    if error_text:
        os.environ["FAKE_CLAUDE_ERROR_TEXT"] = error_text
    wake_needle = "WAKE-REPORT" if wake_ok else "WAKE-FAILED"
    with capture_logs() as logs:
        transport = await _drive("error_first_agent_wake", wake_s=0.5)
    run_final = _first_index(transport, "RUN-FAILED")
    wake_final = _first_index(transport, wake_needle)
    assert run_final < wake_final, transport.log
    assert sum("RUN-FAILED" in t for _, t, _ in transport.log) == 1
    assert sum(wake_needle in t for _, t, _ in transport.log) == 1
    assert transport.log[run_final][1].startswith("error")
    early = [r for r in logs if r.get("event") == "final.error_delivered_early"]
    assert len(early) == 1
    # Accounted once: the run's result is not delivered (or costed) again
    # on the post-return path.
    completed = [
        r
        for r in logs
        if r.get("event") == "runner.completed" and "RUN-FAILED" in str(r.get("error"))
    ]
    assert len(completed) == 1


async def test_900_stream_idle_retry_still_sees_the_errored_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An errored result the #572 Type-A retry acts on keeps today's path:
    no early error final — the retry notice, then the resumed run's result
    (here the same stall again, retries exhausted) is the one delivered."""
    _watchdog(monkeypatch)
    _bridge_watchdog(monkeypatch, stream_idle_auto_retry=True)
    # Type A: real output began (num_turns 1, api 400 ms) before the stall.
    os.environ["FAKE_CLAUDE_ERROR_TEXT"] = (
        "API Error: Stream idle timeout - partial response received RUN-FAILED"
    )
    with capture_logs() as logs:
        transport = await _drive("error_first", timeout=40)
    notice = _first_index(transport, "Stream stalled mid-generation")
    run_finals = [i for i, (_, t, _) in enumerate(transport.log) if "RUN-FAILED" in t]
    # Only the resumed (second) run's error is ever rendered.
    assert len(run_finals) == 1 and notice < run_finals[0], transport.log
    events = [r.get("event") for r in logs]
    assert events.count("claude.stream_idle.auto_retry") == 1
    assert events.count("final.error_held_for_recovery") == 1
    # The first run's result was held for the retry; the retry's own run
    # (retries exhausted, nothing left to act) delivers early.
    assert events.count("final.error_delivered_early") == 1


async def test_900_non_live_errored_run_final_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``live_sessions = false``: no live session, so no early error
    delivery — the error final rides the post-return path, once."""
    _watchdog(monkeypatch, live_sessions=False, post_result_idle_timeout=30)
    with capture_logs() as logs:
        transport = await _drive("error_first_agent_wake", wake_s=0.3)
    assert sum("RUN-FAILED" in t for _, t, _ in transport.log) == 1
    assert not any("WAKE-FAILED" in t for _, t, _ in transport.log)
    events = [r.get("event") for r in logs]
    assert "final.error_delivered_early" not in events
