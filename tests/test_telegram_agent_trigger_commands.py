from dataclasses import replace
from pathlib import Path

import pytest

import untether.runtime_loader as runtime_loader
from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg
from untether.config import ProjectsConfig
from untether.settings import TelegramTopicsSettings, UntetherSettings
from untether.telegram.api_models import ChatMember
from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.commands.agent import _handle_agent_command
from untether.telegram.commands.listen import (
    _handle_listen_command as _handle_trigger_command,
)
from untether.telegram.commands.model import _handle_model_command
from untether.telegram.topic_state import TopicStateStore
from untether.telegram.types import TelegramIncomingMessage
from untether.transport_runtime import TransportRuntime


def _msg(
    text: str,
    *,
    chat_id: int = 123,
    message_id: int = 10,
    sender_id: int | None = 42,
    chat_type: str | None = "private",
    thread_id: int | None = None,
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=sender_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )


def _last_text(transport: FakeTransport) -> str:
    assert transport.send_calls
    return transport.send_calls[-1]["message"].text


class _MemberBot(FakeBot):
    async def get_chat_member(self, chat_id: int, user_id: int) -> ChatMember | None:
        _ = chat_id, user_id
        return ChatMember(status="member", can_manage_topics=False)


@pytest.mark.anyio
async def test_agent_show_private_defaults() -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/agent", chat_type="private")

    await _handle_agent_command(
        cfg,
        msg,
        args_text="",
        ambient_context=None,
        topic_store=None,
        chat_prefs=None,
    )

    text = _last_text(transport)
    assert "engine: codex" in text
    assert "available: codex" in text


@pytest.mark.anyio
async def test_agent_set_clear_group_admin(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    msg = _msg("/agent set codex", chat_type="supergroup")

    await _handle_agent_command(
        cfg,
        msg,
        args_text="set codex",
        ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )

    assert await prefs.get_default_engine(msg.chat_id) == "codex"
    assert "chat default engine set" in _last_text(transport)

    await _handle_agent_command(
        cfg,
        msg,
        args_text="clear",
        ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )

    assert await prefs.get_default_engine(msg.chat_id) is None
    assert "chat default engine cleared" in _last_text(transport)


@pytest.mark.anyio
async def test_agent_set_denied_for_non_admin(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(make_cfg(transport), bot=_MemberBot())
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    msg = _msg("/agent set codex", chat_type="supergroup")

    await _handle_agent_command(
        cfg,
        msg,
        args_text="set codex",
        ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )

    assert await prefs.get_default_engine(msg.chat_id) is None
    assert "restricted to group admins" in _last_text(transport)


@pytest.mark.anyio
async def test_agent_set_invalid_engine(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/agent set nope", chat_type="private")

    await _handle_agent_command(
        cfg,
        msg,
        args_text="set nope",
        ambient_context=None,
        topic_store=None,
        chat_prefs=ChatPrefsStore(tmp_path / "prefs.json"),
    )

    text = _last_text(transport)
    assert "unknown engine" in text
    assert "available engines" in text


# ---------------------------------------------------------------------------
# #1165: engine lists only name engines that can run; a known engine whose CLI
# isn't on PATH is reported as "not installed", the startup message's wording.
# ---------------------------------------------------------------------------


def _runtime_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, missing: set[str]
) -> TransportRuntime:
    """A runtime built by the real availability check (`build_router`), with
    `shutil.which` finding every engine CLI except those in ``missing``."""

    def fake_which(cmd: str) -> str | None:
        return None if cmd in missing else f"/usr/bin/{cmd}"

    monkeypatch.setattr(runtime_loader.shutil, "which", fake_which)
    config_path = tmp_path / "untether.toml"
    config_path.touch()
    settings = UntetherSettings.model_validate(
        {
            "transport": "telegram",
            "transports": {
                "telegram": {"bot_token": "t", "chat_id": 123, "allow_any_user": True}
            },
        }
    )
    backends = runtime_loader.load_backends(
        engine_ids=["codex", "claude", "pi"], allowlist=None, default_engine="codex"
    )
    router = runtime_loader.build_router(
        settings=settings,
        config_path=config_path,
        backends=backends,
        default_engine="codex",
    )
    return TransportRuntime(
        router=router, projects=ProjectsConfig(projects={}, default_project=None)
    )


@pytest.mark.anyio
async def test_1165_agent_show_omits_not_installed_engines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime_missing(monkeypatch, tmp_path, missing={"claude"}),
    )

    await _handle_agent_command(
        cfg,
        _msg("/agent", chat_type="private"),
        args_text="",
        ambient_context=None,
        topic_store=None,
        chat_prefs=None,
    )

    text = _last_text(transport)
    assert "available: codex, pi (not installed: claude)" in text


@pytest.mark.anyio
async def test_1165_agent_show_all_installed_has_no_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime_missing(monkeypatch, tmp_path, missing=set()),
    )

    await _handle_agent_command(
        cfg,
        _msg("/agent", chat_type="private"),
        args_text="",
        ambient_context=None,
        topic_store=None,
        chat_prefs=None,
    )

    text = _last_text(transport)
    assert "available: codex, claude, pi" in text
    assert "not installed" not in text


@pytest.mark.anyio
async def test_1165_agent_set_unknown_lists_installed_engines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime_missing(monkeypatch, tmp_path, missing={"claude"}),
    )

    await _handle_agent_command(
        cfg,
        _msg("/agent set nope", chat_type="private"),
        args_text="set nope",
        ambient_context=None,
        topic_store=None,
        chat_prefs=ChatPrefsStore(tmp_path / "prefs.json"),
    )

    text = _last_text(transport)
    # Rendered text: the backticks become a code entity.
    assert "available engines: codex, pi (not installed: claude)" in text


@pytest.mark.anyio
async def test_1165_model_show_omits_not_installed_engines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        runtime=_runtime_missing(monkeypatch, tmp_path, missing={"claude"}),
    )

    await _handle_model_command(
        cfg,
        _msg("/model", chat_type="private"),
        args_text="",
        ambient_context=None,
        topic_store=None,
        chat_prefs=None,
    )

    text = _last_text(transport)
    assert "available engines: codex, pi (not installed: claude)" in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("topic_mode", "chat_mode", "expected_source", "expected_trigger"),
    [
        ("mentions", None, "topic override", "mentions"),
        (None, "mentions", "chat default", "mentions"),
        (None, None, "default", "all"),
    ],
)
async def test_trigger_show_sources(
    tmp_path: Path,
    topic_mode: str | None,
    chat_mode: str | None,
    expected_source: str,
    expected_trigger: str,
) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    topic_store = TopicStateStore(tmp_path / "topics.json")
    chat_prefs = ChatPrefsStore(tmp_path / "prefs.json")
    msg = _msg("/trigger", chat_type="supergroup", thread_id=7)

    if topic_mode is not None:
        await topic_store.set_trigger_mode(msg.chat_id, msg.thread_id or 0, topic_mode)
    if chat_mode is not None:
        await chat_prefs.set_trigger_mode(msg.chat_id, chat_mode)

    await _handle_trigger_command(
        cfg,
        msg,
        args_text="",
        _ambient_context=None,
        topic_store=topic_store,
        chat_prefs=chat_prefs,
    )

    text = _last_text(transport)
    assert f"listen: {expected_trigger} ({expected_source})" in text
    assert "available: all, mentions" in text


@pytest.mark.anyio
async def test_trigger_set_clear_permissions(tmp_path: Path) -> None:
    transport = FakeTransport()
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    msg = _msg("/trigger mentions", chat_type="supergroup")

    denied_cfg = replace(make_cfg(transport), bot=_MemberBot())
    await _handle_trigger_command(
        denied_cfg,
        msg,
        args_text="mentions",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )
    assert await prefs.get_trigger_mode(msg.chat_id) is None
    assert "restricted to group admins" in _last_text(transport)

    transport = FakeTransport()
    allow_cfg = make_cfg(transport)
    await _handle_trigger_command(
        allow_cfg,
        msg,
        args_text="mentions",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )
    assert await prefs.get_trigger_mode(msg.chat_id) == "mentions"
    assert "chat listen mode set" in _last_text(transport)

    await _handle_trigger_command(
        allow_cfg,
        msg,
        args_text="clear",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )
    assert await prefs.get_trigger_mode(msg.chat_id) is None
    assert "chat listen mode reset" in _last_text(transport)


@pytest.mark.anyio
async def test_trigger_missing_sender_denied(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    msg = _msg("/trigger all", chat_type="supergroup", sender_id=None)

    await _handle_trigger_command(
        cfg,
        msg,
        args_text="all",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=prefs,
    )

    assert await prefs.get_trigger_mode(msg.chat_id) is None
    assert "cannot verify sender" in _last_text(transport)


@pytest.mark.anyio
async def test_trigger_topic_unavailable() -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    msg = _msg("/trigger mentions", chat_type="supergroup", thread_id=3)

    await _handle_trigger_command(
        cfg,
        msg,
        args_text="mentions",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=None,
    )

    assert "topic listen settings are unavailable" in _last_text(transport)


@pytest.mark.anyio
async def test_trigger_chat_prefs_unavailable() -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/trigger mentions", chat_type="supergroup")

    await _handle_trigger_command(
        cfg,
        msg,
        args_text="mentions",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=None,
    )

    assert "chat listen settings are unavailable" in _last_text(transport)


@pytest.mark.anyio
async def test_listen_invoked_as_listen_no_deprecation_notice() -> None:
    """#297: /listen invocation should NOT show the /trigger deprecation prefix."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/listen", chat_type="private")

    await _handle_trigger_command(
        cfg,
        msg,
        args_text="",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=None,
        invoked_as="listen",
    )

    text = _last_text(transport)
    assert "/trigger" not in text
    assert "deprecated" not in text.lower()
    assert "listen:" in text


@pytest.mark.anyio
async def test_legacy_trigger_invocation_shows_deprecation_notice() -> None:
    """#297: /trigger invocation should show a deprecation prefix."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/trigger", chat_type="private")

    await _handle_trigger_command(
        cfg,
        msg,
        args_text="",
        _ambient_context=None,
        topic_store=None,
        chat_prefs=None,
        invoked_as="trigger",
    )

    text = _last_text(transport)
    # Markdown backticks may be stripped during rendering — check the
    # human-readable substring without them.
    assert "/trigger is now /listen" in text
    assert "listen:" in text
