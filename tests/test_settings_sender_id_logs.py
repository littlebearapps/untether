"""#1153: every settings-changing text command logs ``sender_id``.

The `/config` button paths log who changed a setting (#1042); the text
commands that write the same settings must too, so a group chat's journal
shows who made each change whichever surface they used.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport, make_cfg
from tests.test_command_engine_gates import (
    FakeCommandContext,
    FakeMessage,
    FakeTransportRuntime,
)
from tests.test_telegram_topics_command import _runtime
from untether.settings import TelegramTopicsSettings
from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
from untether.telegram.commands.agent import _handle_agent_command
from untether.telegram.commands.followup import (
    STEER_BACKEND,
    handle_followup_default_command,
)
from untether.telegram.commands.listen import _handle_listen_command
from untether.telegram.commands.model import _handle_model_command
from untether.telegram.commands.planmode import PlanModeCommand
from untether.telegram.commands.reasoning import _handle_reasoning_command
from untether.telegram.commands.topics import (
    _handle_chat_ctx_command,
    _handle_ctx_command,
    _handle_topic_command,
)
from untether.telegram.topic_state import TopicStateStore
from untether.telegram.types import TelegramIncomingMessage
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

SENDER = 4242
CHAT = 123
FORUM = -1001234


def _msg(
    text: str,
    *,
    chat_id: int = CHAT,
    thread_id: int | None = None,
    chat_type: str = "private",
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=10,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=SENDER,
        chat_type=chat_type,
        thread_id=thread_id,
        is_forum=True if thread_id is not None else None,
        is_topic_message=True if thread_id is not None else None,
    )


def _events(logs: list[dict], event: str) -> list[dict]:
    found = [entry for entry in logs if entry["event"] == event]
    assert found, f"no {event} log line"
    return found


def _assert_sender(logs: list[dict], event: str) -> None:
    for entry in _events(logs, event):
        assert entry.get("sender_id") == SENDER, entry


# --- /planmode -------------------------------------------------------------


async def _planmode(tmp_path: Path, args: str, *, engine: str = "claude") -> None:
    runtime = FakeTransportRuntime(
        default_engine=engine, runner=SimpleNamespace(default_permission_mode=None)
    )
    ctx = FakeCommandContext(
        args_text=args,
        config_path=tmp_path / "untether.toml",
        message=FakeMessage(channel_id=CHAT, sender_id=SENDER),
        runtime=runtime,
    )
    assert await PlanModeCommand().handle(ctx) is not None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("args", "event"),
    [("on", "planmode.set"), ("off", "planmode.set"), ("clear", "planmode.cleared")],
)
async def test_planmode_claude_logs_sender(
    tmp_path: Path, args: str, event: str
) -> None:
    with capture_logs() as logs:
        await _planmode(tmp_path, args)
    _assert_sender(logs, event)


@pytest.mark.parametrize(
    ("args", "event"),
    [("on", "planmode.set"), ("off", "planmode.set"), ("clear", "planmode.cleared")],
)
async def test_planmode_antigravity_logs_sender(
    tmp_path: Path, args: str, event: str
) -> None:
    with capture_logs() as logs:
        await _planmode(tmp_path, args, engine="antigravity")
    _assert_sender(logs, event)


# --- /model, /reasoning ----------------------------------------------------


async def test_model_set_and_clear_log_sender(tmp_path: Path) -> None:
    cfg = make_cfg(FakeTransport())
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    with capture_logs() as logs:
        for args in ("set gpt-4.1-mini", "clear"):
            await _handle_model_command(
                cfg, _msg(f"/model {args}"), args, None, None, prefs
            )
    _assert_sender(logs, "model.set")
    _assert_sender(logs, "model.cleared")


async def test_reasoning_set_and_clear_log_sender(tmp_path: Path) -> None:
    cfg = make_cfg(FakeTransport())
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    with capture_logs() as logs:
        for args in ("set high", "clear"):
            await _handle_reasoning_command(
                cfg, _msg(f"/reasoning {args}"), args, None, None, prefs
            )
    _assert_sender(logs, "reasoning.set")
    _assert_sender(logs, "reasoning.cleared")


# --- /agent, /listen -------------------------------------------------------


async def test_agent_set_and_clear_log_sender(tmp_path: Path) -> None:
    cfg = make_cfg(FakeTransport())
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    with capture_logs() as logs:
        for args in ("set codex", "clear"):
            await _handle_agent_command(
                cfg,
                _msg(f"/agent {args}"),
                args_text=args,
                ambient_context=None,
                topic_store=None,
                chat_prefs=prefs,
            )
    _assert_sender(logs, "agent.set.chat")
    _assert_sender(logs, "agent.clear.chat")


async def test_agent_topic_set_and_clear_log_sender(tmp_path: Path) -> None:
    cfg = replace(
        make_cfg(FakeTransport()),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/agent", chat_id=FORUM, thread_id=10, chat_type="supergroup")
    with capture_logs() as logs:
        for args in ("set codex", "clear"):
            await _handle_agent_command(
                cfg,
                msg,
                args_text=args,
                ambient_context=None,
                topic_store=store,
                chat_prefs=None,
                resolved_scope="all",
                scope_chat_ids=frozenset({FORUM}),
            )
    _assert_sender(logs, "agent.set.topic")
    _assert_sender(logs, "agent.clear.topic")


async def test_listen_set_and_clear_log_sender(tmp_path: Path) -> None:
    cfg = make_cfg(FakeTransport())
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    with capture_logs() as logs:
        for args in ("mentions", "clear"):
            await _handle_listen_command(
                cfg, _msg(f"/listen {args}"), args, None, None, prefs
            )
    _assert_sender(logs, "listen.set")
    _assert_sender(logs, "listen.clear")


# --- /steer, /queue --------------------------------------------------------


@pytest.mark.parametrize("command_id", ["steer", "queue"])
async def test_followup_text_command_logs_sender(
    tmp_path: Path, command_id: str
) -> None:
    cfg = MagicMock()
    cfg.exec_cfg.transport = FakeTransport()
    cfg.topics.enabled = False
    cfg.runtime.resolve_engine = MagicMock(return_value="claude")
    cfg.runtime.default_engine = "claude"
    cfg.runtime.project_default_engine = MagicMock(return_value=None)
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    with capture_logs() as logs:
        await handle_followup_default_command(
            cfg, _msg(f"/{command_id}"), command_id, None, None, prefs
        )
    _assert_sender(logs, "followup.set")


async def test_followup_backend_fallback_logs_sender(tmp_path: Path) -> None:
    ctx = MagicMock()
    ctx.args_text = ""
    ctx.config_path = tmp_path / "untether.toml"
    ctx.message = MessageRef(channel_id=CHAT, message_id=1, sender_id=SENDER)
    with capture_logs() as logs:
        await STEER_BACKEND.handle(ctx)
    _assert_sender(logs, "followup.set")
    prefs = ChatPrefsStore(resolve_prefs_path(tmp_path / "untether.toml"))
    assert await prefs.get_followup_mode(CHAT) == "steer"


# --- /ctx, /topic ----------------------------------------------------------


async def test_chat_ctx_set_and_clear_log_sender(tmp_path: Path) -> None:
    runtime, state_path = _runtime(tmp_path)
    cfg = replace(make_cfg(FakeTransport()), runtime=runtime, session_mode="chat")
    prefs = ChatPrefsStore(resolve_prefs_path(state_path))
    with capture_logs() as logs:
        for args in ("set alpha @dev", "clear"):
            await _handle_chat_ctx_command(
                cfg, _msg(f"/ctx {args}"), args_text=args, chat_prefs=prefs
            )
    (set_line,) = _events(logs, "ctx.set")
    assert set_line["scope"] == "chat"
    assert (set_line["project"], set_line["branch"]) == ("alpha", "dev")
    _assert_sender(logs, "ctx.set")
    _assert_sender(logs, "ctx.cleared")


async def test_topic_ctx_set_and_clear_log_sender(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    cfg = replace(
        make_cfg(FakeTransport()),
        runtime=runtime,
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/ctx", chat_id=FORUM, thread_id=10, chat_type="supergroup")
    with capture_logs() as logs:
        for args in ("set alpha @dev", "clear"):
            await _handle_ctx_command(
                cfg,
                msg,
                args_text=args,
                store=store,
                resolved_scope="all",
                scope_chat_ids=frozenset({FORUM}),
            )
    (set_line,) = _events(logs, "ctx.set")
    assert (set_line["scope"], set_line["thread_id"]) == ("topic", 10)
    _assert_sender(logs, "ctx.set")
    _assert_sender(logs, "ctx.cleared")


async def test_topic_create_logs_sender(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    cfg = replace(
        make_cfg(FakeTransport()),
        runtime=runtime,
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/topic alpha @dev", chat_id=FORUM, chat_type="supergroup")
    with capture_logs() as logs:
        await _handle_topic_command(
            cfg,
            msg,
            args_text="alpha @dev",
            store=store,
            resolved_scope="all",
            scope_chat_ids=frozenset({FORUM}),
        )
    (line,) = _events(logs, "topic.created")
    assert (line["project"], line["branch"]) == ("alpha", "dev")
    _assert_sender(logs, "topic.created")
