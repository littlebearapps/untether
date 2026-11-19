"""#1265: a prompt waits for the settings commands sent before it in its chat."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

import untether.telegram.loop as telegram_loop
from tests.telegram_fakes import FakeBot, FakeTransport, _empty_projects, _make_router
from untether.markdown import MarkdownPresenter
from untether.model import CompletedEvent, ResumeToken, StartedEvent, UntetherEvent
from untether.runner_bridge import ExecBridgeConfig
from untether.runners.mock import MockRunner
from untether.runners.run_options import get_run_options
from untether.telegram import settings_barrier
from untether.telegram.bridge import TelegramBridgeConfig
from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.types import TelegramIncomingMessage
from untether.transport_runtime import TransportRuntime

pytestmark = pytest.mark.anyio

CHAT = 123


class ModeRecordingRunner(MockRunner):
    """Records the permission mode each run starts with."""

    def __init__(self) -> None:
        super().__init__(engine="claude")
        self.modes: list[str | None] = []

    async def run(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[UntetherEvent]:
        options = get_run_options()
        self.modes.append(options.permission_mode if options else None)
        token = ResumeToken(engine=self.engine, value="conv-1")
        yield StartedEvent(engine=self.engine, resume=token, title="t")
        yield CompletedEvent(engine=self.engine, resume=token, ok=True, answer="ok")


def _msg(text: str, message_id: int) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=CHAT,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=CHAT,
        chat_type="private",
    )


async def test_prompt_after_planmode_runs_in_the_new_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's timeline: `/planmode auto`, then a prompt while the change
    is still on its way (the group admin lookup took ~2.6 s). Before the fix
    the prompt ran in the old mode."""
    original = ChatPrefsStore.set_engine_override

    async def slow_set(self, *args, **kwargs):
        await anyio.sleep(0.5)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ChatPrefsStore, "set_engine_override", slow_set)
    runner = ModeRecordingRunner()
    transport = FakeTransport()
    state_path = tmp_path / "untether.toml"
    cfg = TelegramBridgeConfig(
        bot=FakeBot(),
        runtime=TransportRuntime(
            router=_make_router(runner),
            projects=_empty_projects(),
            config_path=state_path,
        ),
        chat_id=CHAT,
        startup_msg="",
        exec_cfg=ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        ),
        forward_coalesce_s=0.0,
        media_group_debounce_s=0.0,
    )

    async def poller(_cfg: TelegramBridgeConfig):
        yield _msg("/planmode auto", 10)
        await anyio.sleep(0.05)
        yield _msg("hello", 11)
        with anyio.fail_after(5):
            while not runner.modes:
                await anyio.sleep(0.01)

    await telegram_loop.run_main_loop(cfg, poller)
    assert runner.modes == ["auto"]


async def test_wait_before_only_waits_for_earlier_commands() -> None:
    release = anyio.Event()
    order: list[str] = []

    async def command() -> None:
        await release.wait()
        order.append("command")

    async def prompt(message_id: int, label: str) -> None:
        await settings_barrier.wait_before(CHAT, message_id)
        order.append(label)

    async with anyio.create_task_group() as tg:
        settings_barrier.start_soon(tg, CHAT, 20, command)
        tg.start_soon(prompt, 19, "earlier")  # sent before the command
        tg.start_soon(prompt, 21, "later")
        tg.start_soon(settings_barrier.wait_before, CHAT + 1, 99)  # other chat
        await anyio.sleep(0.05)
        assert order == ["earlier"]
        release.set()
    assert order == ["earlier", "command", "later"]
    assert CHAT not in settings_barrier._IN_FLIGHT


async def test_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_barrier, "WAIT_S", 0.1)
    stuck = anyio.Event()
    async with anyio.create_task_group() as tg:
        settings_barrier.start_soon(tg, CHAT, 30, stuck.wait)
        with anyio.fail_after(2):
            await settings_barrier.wait_before(CHAT, 31)
        stuck.set()
