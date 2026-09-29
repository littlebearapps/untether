"""#775 end to end: REAL ``handle_message`` + REAL ``ClaudeRunner`` + the
control-channel fake, with the loop-side steer helper writing into the run.

- mid-tool steer → folded: ONE final that honours it, a "steer received" row
  in the run's progress, and no "not run" notice for the steer;
- post-last-tool steer → its own follow-up turn, replying to the steer;
- /cancel closes the steer window: a later steer falls back to the queue.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest

from tests.test_live_session_harness import (  # noqa: F401 — fixture
    FAKE_CLI,
    _isolate,
    _LiveRunner,
    _OrderedTransport,
    _watchdog,
)
from untether.markdown import MarkdownPresenter
from untether.model import ResumeToken
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    handle_message,
    unique_running_tasks,
)
from untether.runners import claude as claude_mod
from untether.telegram import steer as steer_mod
from untether.telegram.steer import STEERED_ACK, fallback_notice, maybe_steer
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

SID = "fake-live-session"
TOKEN = ResumeToken(engine="claude", value=SID)


@pytest.fixture(autouse=True)
def _reset_env():
    yield
    os.environ.pop("FAKE_CLAUDE_STEER_WAIT_S", None)
    steer_mod._NOTICED.clear()


async def _wait_live(timeout: float = 10.0) -> None:
    with anyio.fail_after(timeout):
        while claude_mod.get_live_session(SID) is None:
            await anyio.sleep(0.02)


async def _run(
    scenario: str,
    steer_step: Any,
    *,
    running_tasks: dict[MessageRef, RunningTask] | None = None,
) -> tuple[_OrderedTransport, dict[str, Any]]:
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    transport = _OrderedTransport()
    exec_cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=False
    )
    cfg = MagicMock()
    cfg.followup_mode = "steer"
    cfg.exec_cfg = SimpleNamespace(transport=transport)
    runner = _LiveRunner(claude_cmd=str(FAKE_CLI), permission_mode="bypassPermissions")
    out: dict[str, Any] = {}
    tasks = running_tasks if running_tasks is not None else {}

    async def drive() -> None:
        with anyio.fail_after(25):
            await handle_message(
                exec_cfg,
                runner=runner,
                incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
                resume_token=None,
                running_tasks=tasks,
            )

    async def steer(**kw: Any) -> bool:
        return await maybe_steer(
            cfg,
            chat_id=123,
            user_msg_id=kw.pop("user_msg_id", 50),
            thread_id=None,
            topic_thread_id=None,
            engine="claude",
            resume_token=TOKEN,
            override=None,
            running_tasks=tasks,
            chat_prefs=None,
            topic_store=None,
            **kw,
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(drive)
        await _wait_live()
        await steer_step(steer, out, tasks)
    return transport, out


def _all_texts(transport: _OrderedTransport) -> list[str]:
    return [text for _, text, _ in transport.log]


async def test_mid_tool_steer_one_final_that_honours_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)

    async def step(steer: Any, out: dict[str, Any], _tasks: Any) -> None:
        out["ok"] = await steer(prompt_text="also the hostname")

    transport, out = await _run("steer_mid_tool", step)
    assert out["ok"] is True
    texts = _all_texts(transport)
    assert STEERED_ACK in texts
    finals = [t for t in texts if "DONE" in t and "steer received" not in t]
    assert any("DONE + also the hostname" in t for t in finals)
    # The steer did not become its own turn (the "steer received" row is
    # asserted at runner level; progress edits are rate-limited here).
    assert not any("ECHO:" in t for t in texts)
    assert not any("session ended before this message ran" in t for t in texts)


async def test_post_last_tool_steer_is_its_own_followup_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)

    async def step(steer: Any, out: dict[str, Any], _tasks: Any) -> None:
        out["ok"] = await steer(prompt_text="and a haiku", user_msg_id=51)

    transport, out = await _run("steer_post_last_tool", step)
    assert out["ok"] is True
    log = transport.log
    first = next(i for i, (_, t, _) in enumerate(log) if "FIRST" in t)
    echo = next(i for i, (k, t, _) in enumerate(log) if "ECHO: and a haiku" in t)
    assert first < echo
    kind, _text, options = log[echo]
    # Delivered as its own message, replying to the steer (not orphaned).
    assert kind == "send" and options.reply_to.message_id == 51
    assert not any(
        "session ended before this message ran" in t for t in _all_texts(transport)
    )


async def test_steer_after_cancel_falls_back_to_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _watchdog(monkeypatch)
    os.environ["FAKE_CLAUDE_STEER_WAIT_S"] = "3"
    running: dict[MessageRef, RunningTask] = {}

    async def step(steer: Any, out: dict[str, Any], tasks: Any) -> None:
        with anyio.fail_after(5):
            while not tasks:
                await anyio.sleep(0.02)
        (_, task), *_ = unique_running_tasks(tasks)
        task.cancel_requested.set()
        with anyio.fail_after(5):
            while True:
                live = claude_mod.get_live_session(SID)
                if live is None or not live.accepting_steer:
                    break
                await anyio.sleep(0.01)
        out["ok"] = await steer(prompt_text="too late")

    transport, out = await _run("steer_mid_tool", step, running_tasks=running)
    assert out["ok"] is False
    texts = _all_texts(transport)
    assert STEERED_ACK not in texts
    assert not any("too late" in t for t in texts)
    # The window-closed notice (run still tearing down), the no-live notice
    # (process already gone — which one depends on teardown timing), or
    # nothing — never a steer written into the cancelled process.
    notices = [t for t in texts if t.startswith("↪️")]
    assert notices in (
        [],
        [fallback_notice("closing", "claude")],
        [fallback_notice("no_live", "claude")],
    )
