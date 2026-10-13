"""#775: follow-up mode prefs, resolver, /steer + /queue, /config page and the
loop-side steer helper (:func:`untether.telegram.steer.maybe_steer`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import structlog

from tests.telegram_fakes import FakeTransport
from untether import runner_bridge as rb
from untether.model import Action, ActionEvent, ResumeToken
from untether.runners import claude as claude_mod
from untether.runners.claude import ClaudeStreamState, LiveSession
from untether.settings import TelegramTransportSettings
from untether.telegram import steer as steer_mod
from untether.telegram.chat_prefs import ChatPrefsStore
from untether.telegram.commands.config import ConfigCommand
from untether.telegram.commands.followup import (
    QUEUE_BACKEND,
    STEER_BACKEND,
    followup_state_text,
    handle_followup_default_command,
)
from untether.telegram.followup_mode import (
    normalize_followup_mode,
    resolve_followup_mode,
)
from untether.telegram.steer import (
    STEERED_ACK,
    fallback_notice,
    maybe_steer,
    split_followup_command,
)
from untether.telegram.topic_state import TopicStateStore
from untether.transport import MessageRef

pytestmark = pytest.mark.anyio

SID = "sid-steer"
CHAT = 555
TOKEN = ResumeToken(engine="claude", value=SID)


# ── prefs + resolver ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("steer", "steer"),
        (" STEER ", "steer"),
        ("queue", "queue"),
        ("bogus", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_followup_mode(raw: str | None, expected: str | None) -> None:
    assert normalize_followup_mode(raw) == expected


async def test_chat_prefs_followup_roundtrip(tmp_path: Path) -> None:
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    assert await prefs.get_followup_mode(CHAT) is None
    await prefs.set_followup_mode(CHAT, "steer")
    assert await ChatPrefsStore(tmp_path / "prefs.json").get_followup_mode(CHAT) == (
        "steer"
    )
    await prefs.clear_followup_mode(CHAT)
    assert await prefs.get_followup_mode(CHAT) is None
    # A chat left with nothing else set is removed entirely.
    assert str(CHAT) not in prefs._state.chats


async def test_topic_state_followup_roundtrip(tmp_path: Path) -> None:
    store = TopicStateStore(tmp_path / "topics.json")
    assert await store.get_followup_mode(CHAT, 7) is None
    await store.set_followup_mode(CHAT, 7, "steer")
    assert await store.get_followup_mode(CHAT, 7) == "steer"
    await store.clear_followup_mode(CHAT, 7)
    assert await store.get_followup_mode(CHAT, 7) is None


async def test_resolver_order_topic_chat_config_default(tmp_path: Path) -> None:
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    topics = TopicStateStore(tmp_path / "topics.json")

    async def resolve(default: str | None = None, thread: int | None = 7) -> str:
        return await resolve_followup_mode(
            chat_id=CHAT,
            thread_id=thread,
            chat_prefs=prefs,
            topic_store=topics,
            default=default,
        )

    assert await resolve() == "queue"
    assert await resolve(default="steer") == "steer"
    await prefs.set_followup_mode(CHAT, "queue")
    assert await resolve(default="steer") == "queue"  # chat beats config
    await topics.set_followup_mode(CHAT, 7, "steer")
    assert await resolve() == "steer"  # topic beats chat
    assert await resolve(thread=None) == "queue"  # outside the topic


def test_settings_followup_mode_validated() -> None:
    base = {"bot_token": "t", "chat_id": 1, "allow_any_user": True}
    assert TelegramTransportSettings(**base).followup_mode == "queue"
    assert (
        TelegramTransportSettings(**base, followup_mode="steer").followup_mode
        == "steer"
    )
    with pytest.raises(ValueError, match="followup_mode"):
        TelegramTransportSettings(**base, followup_mode="later")


def test_followup_mode_hot_reloads() -> None:
    from untether.telegram.bridge import TelegramBridgeConfig

    cfg = TelegramBridgeConfig(
        bot=MagicMock(),
        runtime=MagicMock(),
        chat_id=1,
        startup_msg="",
        exec_cfg=MagicMock(),
    )
    assert cfg.followup_mode == "queue"
    cfg.update_from(
        TelegramTransportSettings(
            bot_token="t", chat_id=1, allow_any_user=True, followup_mode="steer"
        )
    )
    assert cfg.followup_mode == "steer"
    assert "followup_mode" not in TelegramTransportSettings.RESTART_REQUIRED_FIELDS


# ── /steer <text> parsing ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command_id", "args", "expected"),
    [
        ("steer", "also X", ("steer", "also X")),
        ("queue", "  later  ", ("queue", "later")),
        ("steer", "", None),
        ("steer", "   ", None),
        ("queue", None, None),
        ("listen", "all", None),
        (None, "x", None),
    ],
)
def test_split_followup_command(
    command_id: str | None, args: str | None, expected: tuple[str, str] | None
) -> None:
    assert split_followup_command(command_id, args) == expected


# ── maybe_steer ─────────────────────────────────────────────────────────────


class _Pipe:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[bytes] = []
        self.fail = fail

    async def send(self, data: bytes) -> None:
        if self.fail:
            raise anyio.BrokenResourceError
        self.sent.append(data)

    async def aclose(self) -> None:
        pass


def _install(*, idle: bool = False, fail: bool = False) -> tuple[LiveSession, _Pipe]:
    pipe = _Pipe(fail=fail)
    state = ClaudeStreamState()
    state.live_mode = True
    state.completed_turns = 1 if idle else 0
    state.turn_open = not idle
    state.spawn_run_options = "opts-A"
    live = LiveSession(session_id=SID, state=state, stdin=pipe)
    claude_mod._LIVE_SESSIONS[SID] = live
    claude_mod._SESSION_STDIN[SID] = pipe
    claude_mod._SESSION_BG_STATE[SID] = state
    return live, pipe


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for reg in (
        claude_mod._LIVE_SESSIONS,
        claude_mod._SESSION_STDIN,
        claude_mod._SESSION_BG_STATE,
    ):
        reg.pop(SID, None)
    claude_mod._PENDING_ASK_REQUESTS.clear()
    rb._FOLLOWUP_ANCHORS.clear()
    rb._FOLLOWUP_IN_FLIGHT.clear()
    steer_mod._NOTICED.clear()


class _Busy:
    """A RunningTask stand-in doing work (not live-idle, not done)."""

    def __init__(self) -> None:
        self.done = anyio.Event()
        self.edits = None


def _cfg(followup_mode: str = "queue") -> Any:
    cfg = MagicMock()
    cfg.followup_mode = followup_mode
    cfg.exec_cfg.transport = FakeTransport()
    return cfg


async def _steer(
    cfg: Any,
    *,
    engine: str = "claude",
    token: ResumeToken | None = TOKEN,
    override: str | None = None,
    running: dict | None = None,
    chat_prefs: Any = None,
    run_options: Any = None,
    run_cwd: Any = None,
    text: str = "also the hostname",
    user_msg_id: int = 42,
    thread_id: int | None = None,
    **extra: Any,
) -> bool:
    return await maybe_steer(
        cfg,
        chat_id=CHAT,
        user_msg_id=user_msg_id,
        thread_id=thread_id,
        topic_thread_id=None,
        prompt_text=text,
        engine=engine,
        resume_token=token,
        override=override,
        running_tasks=running or {},
        chat_prefs=chat_prefs,
        topic_store=None,
        run_options=run_options,
        run_cwd=run_cwd,
        **extra,
    )


def _texts(cfg: Any) -> list[str]:
    return [c["message"].text for c in cfg.exec_cfg.transport.send_calls]


async def test_queue_mode_never_steers() -> None:
    _live, pipe = _install()
    cfg = _cfg("queue")
    assert await _steer(cfg) is False
    assert pipe.sent == [] and _texts(cfg) == []


async def test_steer_mode_writes_now_and_acks() -> None:
    live, pipe = _install()  # mid-turn: NOT held until idle
    cfg = _cfg("steer")
    assert await _steer(cfg) is True
    assert len(pipe.sent) == 1 and b"also the hostname" in pipe.sent[0]
    assert _texts(cfg) == [STEERED_ACK]
    opts = cfg.exec_cfg.transport.send_calls[0]["options"]
    assert opts.notify is False and opts.reply_to.message_id == 42
    # Anchor registered so a post-last-tool steer (own turn) replies here.
    (uuid_,) = rb._FOLLOWUP_ANCHORS
    assert rb._FOLLOWUP_ANCHORS[uuid_][1].message_id == 42
    assert live.state.steered_commands[uuid_] == "also the hostname"
    assert uuid_ in live.state.awaiting_injected


async def test_explicit_steer_overrides_queue_default() -> None:
    _live, pipe = _install()
    cfg = _cfg("queue")
    assert await _steer(cfg, override="steer") is True
    assert len(pipe.sent) == 1


async def test_explicit_queue_overrides_steer_default() -> None:
    _live, pipe = _install()
    cfg = _cfg("steer")
    assert await _steer(cfg, override="queue") is False
    assert pipe.sent == [] and _texts(cfg) == []


async def test_chat_pref_steer_used_when_no_override(tmp_path: Path) -> None:
    _live, pipe = _install()
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    await prefs.set_followup_mode(CHAT, "steer")
    assert await _steer(_cfg("queue"), chat_prefs=prefs) is True
    assert len(pipe.sent) == 1


@pytest.mark.parametrize("override", [None, "steer"])
async def test_idle_live_session_written_without_steer_ack(
    override: str | None,
) -> None:
    """Between turns nothing is running: the line is written (it runs as the
    next turn, replying to the message) but it is not acked as a steer."""
    live, pipe = _install(idle=True)
    cfg = _cfg("steer")
    assert await _steer(cfg, override=override) is True
    assert len(pipe.sent) == 1
    assert live.idle_since is not None
    assert _texts(cfg) == []
    # The anchor stays so the follow-up turn replies to this message.
    assert len(rb._FOLLOWUP_ANCHORS) == 1


async def test_steer_outcome_reports_idle_vs_mid_turn() -> None:
    from untether.runners.claude import steer_into_session

    live, _pipe = _install(idle=True)
    assert await steer_into_session(SID, "a", command_uuid="u1") == "written_idle"
    live.state.turn_open = True
    assert await steer_into_session(SID, "b", command_uuid="u2") == "steered"


@pytest.mark.parametrize("explicit", [True, False])
async def test_non_claude_engine_falls_back(explicit: bool) -> None:
    cfg = _cfg("steer")
    running = {MessageRef(channel_id=CHAT, message_id=9): _Busy()}
    ok = await _steer(
        cfg,
        engine="codex",
        token=ResumeToken(engine="codex", value="thread-1"),
        override="steer" if explicit else None,
        running=running,
    )
    assert ok is False
    assert _texts(cfg) == [fallback_notice("engine", "codex")]
    assert "codex" in _texts(cfg)[0]


async def test_live_backend_without_steer_falls_back_with_engine_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#977: the gate is the backend's ``supports_steer``, not the engine
    name — a live-session engine that cannot take a mid-turn line queues,
    and nothing is asked of its (or Claude's) registry."""
    from untether import live_sessions

    class _NoSteerLive:
        engine = "warm"
        supports_steer = False

        def get(self, session_id: str):
            raise AssertionError("registry consulted for a no-steer backend")

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError("Claude consulted for another engine")

    monkeypatch.setitem(live_sessions._BACKENDS, "warm", _NoSteerLive())
    monkeypatch.setattr("untether.interactive.questions.get_pending_ask_request", _boom)
    monkeypatch.setattr(claude_mod, "get_live_session", _boom)
    cfg = _cfg("steer")
    running = {MessageRef(channel_id=CHAT, message_id=9): _Busy()}
    ok = await _steer(
        cfg,
        engine="warm",
        token=ResumeToken(engine="warm", value="thread-1"),
        override="steer",
        running=running,
    )
    assert ok is False
    assert _texts(cfg) == [fallback_notice("engine", "warm")]


async def test_default_mode_notice_only_once_per_run() -> None:
    cfg = _cfg("steer")
    running = {MessageRef(channel_id=CHAT, message_id=9): _Busy()}
    token = ResumeToken(engine="codex", value="thread-1")
    for _ in range(3):
        await _steer(cfg, engine="codex", token=token, running=running)
    assert len(_texts(cfg)) == 1
    # A new run gets its own (single) notice.
    running = {MessageRef(channel_id=CHAT, message_id=10): _Busy()}
    await _steer(cfg, engine="codex", token=token, running=running)
    assert len(_texts(cfg)) == 2


# ── #990: the busy-run lookup is scoped to the sender's thread ───────────────

_CODEX = ResumeToken(engine="codex", value="thread-1")


def _busy_in(thread_id: int | None, *, ref_thread: int | None = None) -> dict:
    """One busy run started from ``thread_id``; ``ref_thread`` is the thread
    the progress ref echoes, which is not the scope key (#826)."""
    task = _Busy()
    task.thread_id = thread_id  # type: ignore[attr-defined]
    return {MessageRef(channel_id=CHAT, message_id=9, thread_id=ref_thread): task}


def _forum_filter(thread_id: int | None):
    from untether.telegram.topics import thread_filter_for
    from untether.telegram.types import TelegramIncomingMessage

    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=CHAT,
        message_id=42,
        text="hi",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
        thread_id=thread_id,
        chat_type="supergroup",
        is_forum=True,
    )
    return thread_filter_for(msg)


@pytest.mark.parametrize("engine_token", [_CODEX, None])
@pytest.mark.parametrize("general", [None, 1])
async def test_990_general_ignores_another_topics_busy_run(
    engine_token: ResumeToken | None, general: int | None
) -> None:
    """A default-steer message in General with a busy run only in topic 7
    simply runs — no "queued instead" notice (every engine)."""
    cfg = _cfg("steer")
    ok = await _steer(
        cfg,
        engine="codex" if engine_token else "claude",
        token=engine_token,
        running=_busy_in(7, ref_thread=7),
        thread_id=general,
        thread_filter=_forum_filter(general),
    )
    assert ok is False
    assert _texts(cfg) == []


async def test_990_topic_matches_its_own_busy_run() -> None:
    cfg = _cfg("steer")
    await _steer(
        cfg,
        engine="codex",
        token=_CODEX,
        running=_busy_in(7, ref_thread=7),
        thread_id=7,
        thread_filter=_forum_filter(7),
    )
    assert _texts(cfg) == [fallback_notice("engine", "codex")]


async def test_990_general_matches_generals_busy_run() -> None:
    """General is its own thread: ``None`` and topic id 1 are the same."""
    cfg = _cfg("steer")
    await _steer(
        cfg,
        engine="codex",
        token=_CODEX,
        running=_busy_in(1),
        thread_id=None,
        thread_filter=_forum_filter(None),
    )
    assert _texts(cfg) == [fallback_notice("engine", "codex")]


async def test_990_scope_is_the_tasks_thread_not_the_echoed_ref() -> None:
    """A topic-7 run whose progress ref carries no thread is not topic 8's."""
    cfg = _cfg("steer")
    await _steer(
        cfg,
        engine="codex",
        token=_CODEX,
        running=_busy_in(7, ref_thread=None),
        thread_id=8,
        thread_filter=_forum_filter(8),
    )
    assert _texts(cfg) == []
    await _steer(
        cfg,
        engine="codex",
        token=_CODEX,
        running=_busy_in(7, ref_thread=None),
        thread_id=7,
        thread_filter=_forum_filter(7),
    )
    assert _texts(cfg) == [fallback_notice("engine", "codex")]


@pytest.mark.parametrize("thread_id", [None, 31])
async def test_990_non_forum_chat_stays_chat_wide(thread_id: int | None) -> None:
    """No thread filter (basic group, or a non-forum supergroup whose thread
    id is only a reply-chain root): any busy run in the chat counts."""
    cfg = _cfg("steer")
    await _steer(
        cfg,
        engine="codex",
        token=_CODEX,
        running=_busy_in(None),
        thread_id=thread_id,
        thread_filter=None,
    )
    assert _texts(cfg) == [fallback_notice("engine", "codex")]


async def test_default_mode_silent_when_nothing_running() -> None:
    cfg = _cfg("steer")
    assert await _steer(cfg, token=None) is False
    assert _texts(cfg) == []


async def test_explicit_steer_without_live_session_notices() -> None:
    cfg = _cfg("queue")
    assert await _steer(cfg, override="steer") is False  # nothing installed
    assert _texts(cfg) == [fallback_notice("no_live", "claude")]


@pytest.mark.parametrize(
    "token",
    [None, ResumeToken(engine="claude", value="", is_continue=True)],
)
async def test_no_session_token_falls_back(token: ResumeToken | None) -> None:
    _install()
    cfg = _cfg("queue")
    assert await _steer(cfg, override="steer", token=token) is False
    assert _texts(cfg) == [fallback_notice("no_live", "claude")]


async def test_window_closed_falls_back_and_drops_anchor() -> None:
    live, pipe = _install()
    await claude_mod.close_steer_window(SID, "cancel")
    cfg = _cfg("steer")
    running = {MessageRef(channel_id=CHAT, message_id=9): _Busy()}
    assert await _steer(cfg, running=running) is False
    assert pipe.sent == []
    assert rb._FOLLOWUP_ANCHORS == {}
    assert _texts(cfg) == [fallback_notice("closing", "claude")]
    assert live.steer_closed_reason == "cancel"


async def test_closing_session_falls_back() -> None:
    live, pipe = _install()
    live.closing = True
    assert await _steer(_cfg("queue"), override="steer") is False
    assert pipe.sent == []


async def test_write_failure_falls_back() -> None:
    live, _pipe = _install(fail=True)
    cfg = _cfg("queue")
    assert await _steer(cfg, override="steer") is False
    assert rb._FOLLOWUP_ANCHORS == {}
    assert live.state.steered_commands == {}
    assert live.state.awaiting_injected == {}


async def test_ask_pending_wins_over_steer() -> None:
    _live, pipe = _install()
    claude_mod._PENDING_ASK_REQUESTS["req-1"] = (CHAT, "Which one?")
    cfg = _cfg("steer")
    assert await _steer(cfg) is False
    assert pipe.sent == [] and _texts(cfg) == []


async def test_options_changed_on_idle_session_defers_to_queue() -> None:
    _live, pipe = _install(idle=True)

    async def opts(_token: ResumeToken) -> object:
        return "opts-B"

    cfg = _cfg("steer")
    assert await _steer(cfg, run_options=opts) is False
    assert pipe.sent == [] and _texts(cfg) == []


async def test_stale_reasoning_steer_not_options_changed(monkeypatch) -> None:
    """#416: an idle live session spawned from the sanitising resolver is not
    `options_changed` by a steer whose options come from the same resolver,
    even when the stored level has been retired."""
    from untether.runners.run_options import EngineRunOptions
    from untether.telegram import engine_overrides
    from untether.telegram.engine_overrides import drop_unsupported_reasoning

    monkeypatch.setitem(
        engine_overrides._ENGINE_REASONING_LEVELS, "claude", ("low", "medium", "high")
    )
    live, pipe = _install(idle=True)

    def resolved() -> EngineRunOptions | None:
        return drop_unsupported_reasoning(
            "claude",
            EngineRunOptions(reasoning="retired-level", permission_mode="plan"),
        )

    live.state.spawn_run_options = resolved()

    async def opts(_token: ResumeToken) -> object:
        return resolved()

    assert await _steer(_cfg("steer"), run_options=opts) is True
    assert len(pipe.sent) == 1


async def test_options_changed_mid_turn_still_steers() -> None:
    """Mid-turn the steer joins the turn that is already running with the
    old options — that is the point of steering."""
    _live, pipe = _install(idle=False)

    async def opts(_token: ResumeToken) -> object:
        return "opts-B"

    assert await _steer(_cfg("steer"), run_options=opts) is True
    assert len(pipe.sent) == 1


async def test_steers_in_quick_succession_are_written_in_order() -> None:
    _live, pipe = _install()
    cfg = _cfg("steer")
    for n in range(4):
        assert await _steer(cfg, text=f"s{n}", user_msg_id=40 + n)
    assert [b'"content": "s%d"' % n in raw for n, raw in enumerate(pipe.sent)] == [
        True
    ] * 4


async def test_steer_with_pending_approval_does_not_deadlock() -> None:
    """A tool approval is pending (the CLI waits on a control_response):
    the steer is just written — the CLI consumes it once the tool resolves —
    and a control response can still be written afterwards."""
    _live, pipe = _install()
    claude_mod._REQUEST_TO_SESSION["req-ap"] = SID
    try:
        with anyio.fail_after(2):
            assert await _steer(_cfg("steer")) is True
            await claude_mod._locked_send(pipe, b'{"type":"control_response"}\n')
        assert len(pipe.sent) == 2
    finally:
        claude_mod._REQUEST_TO_SESSION.pop("req-ap", None)


# ── bridge: an absorbed steer's anchor is dropped ───────────────────────────


def test_absorbed_action_drops_anchor() -> None:
    rb.register_followup_anchor(
        "u-abs",
        session_id=SID,
        reply_to=MessageRef(channel_id=CHAT, message_id=1),
        placeholder=None,
    )
    evt = ActionEvent(
        engine="claude",
        action=Action(
            id="claude.steer.1",
            kind="note",
            title="↪️ steer received",
            detail={"absorbed_command_uuid": "u-abs", "steer": True},
        ),
        phase="completed",
        ok=True,
    )
    rb._consume_absorbed_anchor(evt)
    assert rb.drain_followup_anchors(SID) == []


def test_unrelated_action_keeps_anchor() -> None:
    rb.register_followup_anchor(
        "u-keep",
        session_id=SID,
        reply_to=MessageRef(channel_id=CHAT, message_id=1),
        placeholder=None,
    )
    evt = ActionEvent(
        engine="claude",
        action=Action(id="t1", kind="tool", title="Read", detail={}),
        phase="completed",
        ok=True,
    )
    rb._consume_absorbed_anchor(evt)
    assert len(rb.drain_followup_anchors(SID)) == 1


# ── #921: a steer's anchor is in flight until the write is decided ──────────


def _spy_steer(monkeypatch: pytest.MonkeyPatch, outcome: str) -> list[bool]:
    """Replace ``steer_into_session``: record whether its anchor was in flight
    (owned by the writer, invisible to the run-end sweep) during the call."""
    seen: list[bool] = []

    async def fake(session_id: str, text: str, *, command_uuid: str, **_k: Any):
        seen.append(command_uuid in rb._FOLLOWUP_IN_FLIGHT)
        assert rb.drain_followup_anchors(SID) == []  # the sweep leaves it alone
        if outcome == "raises":
            raise RuntimeError("boom")
        return outcome

    monkeypatch.setattr(claude_mod, "steer_into_session", fake)
    return seen


@pytest.mark.parametrize(
    "outcome",
    [
        "options_changed",
        "result_pending",
        "window_closed",
        "no_live_session",
        "write_failed",
        "raises",
    ],
)
async def test_921_steer_fallback_settles_in_flight(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    _install()
    seen = _spy_steer(monkeypatch, outcome)
    cfg = _cfg("steer")
    if outcome == "raises":
        with pytest.raises(RuntimeError, match="boom"):
            await _steer(cfg)
    else:
        assert await _steer(cfg) is False
    assert seen == [True]
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}


@pytest.mark.parametrize("outcome", ["steered", "written_idle"])
async def test_921_steer_written_anchor_not_in_flight(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    _install()
    seen = _spy_steer(monkeypatch, outcome)
    assert await _steer(_cfg("steer")) is True
    assert seen == [True]
    # Kept for the turn that answers it; no longer in flight, so the run-end
    # sweep can report it if the session dies before that turn opens.
    (uuid_,) = rb._FOLLOWUP_ANCHORS
    assert rb._FOLLOWUP_ANCHORS[uuid_][1].message_id == 42
    assert rb._FOLLOWUP_IN_FLIGHT == {}


async def test_921_steer_budget_refusal_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from untether import budget_gate

    _install()
    monkeypatch.setattr(budget_gate, "daily_gate", lambda _opts=None: (5.0, 1.0))
    assert await _steer(_cfg("steer")) is False
    assert rb._FOLLOWUP_ANCHORS == {}
    assert rb._FOLLOWUP_IN_FLIGHT == {}


async def test_921_real_steer_settles_on_written_and_closed() -> None:
    """Unpatched path: a real mid-turn steer settles written; a steer into a
    closed window settles not-written. Neither leaves an in-flight entry."""
    live, _pipe = _install()
    assert await _steer(_cfg("steer")) is True
    assert len(rb._FOLLOWUP_ANCHORS) == 1 and rb._FOLLOWUP_IN_FLIGHT == {}
    rb._FOLLOWUP_ANCHORS.clear()
    await claude_mod.close_steer_window(SID, "cancel")
    assert await _steer(_cfg("steer"), user_msg_id=43) is False
    assert rb._FOLLOWUP_ANCHORS == {} and rb._FOLLOWUP_IN_FLIGHT == {}
    assert live.steer_closed_reason == "cancel"


# ── /steer + /queue commands ────────────────────────────────────────────────


def _cmd_msg(text: str, *, thread_id: int | None = None, private: bool = True):
    from untether.telegram.types import TelegramIncomingMessage

    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=CHAT,
        message_id=77,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
        thread_id=thread_id,
        chat_type="private" if private else "supergroup",
    )


def _command_cfg(engine: str = "claude") -> Any:
    cfg = MagicMock()
    cfg.exec_cfg.transport = FakeTransport()
    cfg.topics.enabled = False
    cfg.runtime.resolve_engine = MagicMock(return_value=engine)
    cfg.runtime.default_engine = engine
    cfg.runtime.project_default_engine = MagicMock(return_value=None)
    return cfg


@pytest.mark.parametrize("command_id", ["steer", "queue"])
async def test_bare_command_sets_chat_default(tmp_path: Path, command_id: str) -> None:
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    cfg = _command_cfg()
    await handle_followup_default_command(
        cfg, _cmd_msg(f"/{command_id}"), command_id, None, None, prefs
    )
    assert await prefs.get_followup_mode(CHAT) == command_id
    (sent,) = _texts(cfg)
    assert f"set to {command_id}" in sent and "this chat" in sent


async def test_bare_steer_in_topic_sets_topic_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.telegram.commands import followup as followup_cmd

    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    topics = TopicStateStore(tmp_path / "topics.json")
    monkeypatch.setattr(
        followup_cmd, "_topic_key", lambda msg, cfg, scope_chat_ids=None: (CHAT, 9)
    )
    cfg = _command_cfg()
    await handle_followup_default_command(
        cfg, _cmd_msg("/steer", thread_id=9), "steer", None, topics, prefs
    )
    assert await topics.get_followup_mode(CHAT, 9) == "steer"
    assert await prefs.get_followup_mode(CHAT) is None
    assert "this topic" in _texts(cfg)[0]


async def test_bare_steer_on_non_claude_engine_notes_claude_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from untether.telegram import engine_defaults

    async def fake_resolve(**_kw: Any) -> Any:
        return MagicMock(engine="codex")

    monkeypatch.setattr(engine_defaults, "resolve_engine_for_message", fake_resolve)
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    cfg = _command_cfg("codex")
    await handle_followup_default_command(
        cfg, _cmd_msg("/steer"), "steer", None, None, prefs
    )
    assert "Claude Code and Antigravity only" in _texts(cfg)[0]


async def test_bare_command_denied_for_group_non_admin(tmp_path: Path) -> None:
    prefs = ChatPrefsStore(tmp_path / "prefs.json")
    cfg = _command_cfg()
    member = MagicMock(status="member")
    cfg.bot.get_chat_member = AsyncMock(return_value=member)
    await handle_followup_default_command(
        cfg, _cmd_msg("/steer", private=False), "steer", None, None, prefs
    )
    assert await prefs.get_followup_mode(CHAT) is None
    assert "admins" in _texts(cfg)[0]


def test_state_text_mentions_the_other_command() -> None:
    text = followup_state_text("steer", scope="this chat")
    assert "`/queue <text>`" in text and "`/queue`" in text


def test_backends_registered_ids() -> None:
    assert STEER_BACKEND.id == "steer" and QUEUE_BACKEND.id == "queue"
    assert STEER_BACKEND.description and QUEUE_BACKEND.description


def test_entry_points_listed_in_command_menu() -> None:
    from untether.commands import list_command_ids

    ids = list_command_ids()
    assert "steer" in ids and "queue" in ids


async def test_backend_fallback_sets_chat_default(tmp_path: Path) -> None:
    ctx = MagicMock()
    ctx.args_text = ""
    ctx.config_path = tmp_path / "untether.toml"
    ctx.message = MessageRef(channel_id=CHAT, message_id=1)
    result = await STEER_BACKEND.handle(ctx)
    assert result is not None and "steer" in result.text
    prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    assert await prefs.get_followup_mode(CHAT) == "steer"


async def test_backend_with_text_outside_loop() -> None:
    ctx = MagicMock()
    ctx.args_text = "hello"
    result = await QUEUE_BACKEND.handle(ctx)
    assert result is not None and "only available" in result.text


# ── /config Follow-up page ──────────────────────────────────────────────────


def _config_ctx(args: str, *, config_path: Path | None, engine: str = "claude"):
    ctx = MagicMock()
    ctx.args_text = args
    ctx.text = f"config:{args}" if args else "/config"
    ctx.message.channel_id = CHAT
    ctx.config_path = config_path
    ctx.runtime.engine_ids = ("codex", "claude")
    ctx.runtime.default_engine = engine
    ctx.runtime.default_context_for_chat.return_value = None
    ctx.runtime.project_default_engine.return_value = None
    ctx.executor = AsyncMock()
    ctx.trigger_manager = None
    ctx.default_chat_id = None
    return ctx


def _edited(ctx: Any):
    return ctx.executor.edit.call_args[0][1]


def _data(msg: Any) -> list[str]:
    return [
        b["callback_data"]
        for row in msg.extra["reply_markup"]["inline_keyboard"]
        for b in row
    ]


async def test_config_page_renders_and_sets(tmp_path: Path) -> None:
    path = tmp_path / "untether.toml"
    cmd = ConfigCommand()
    ctx = _config_ctx("fu", config_path=path)
    await cmd.handle(ctx)
    msg = _edited(ctx)
    assert "Follow-up mode" in msg.text and "Current: <b>queue</b>" in msg.text
    assert {"config:fu:q", "config:fu:s", "config:fu:clr"} <= set(_data(msg))
    assert all(len(d.encode()) <= 64 for d in _data(msg))
    assert "Claude Code only" not in msg.text

    await cmd.handle(_config_ctx("fu:s", config_path=path))
    prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    assert await prefs.get_followup_mode(CHAT) == "steer"

    ctx = _config_ctx("fu", config_path=path)
    await cmd.handle(ctx)
    assert "Current: <b>steer</b> (chat)" in _edited(ctx).text

    await cmd.handle(_config_ctx("fu:clr", config_path=path))
    assert await prefs.get_followup_mode(CHAT) is None
    await cmd.handle(_config_ctx("fu:q", config_path=path))
    assert await prefs.get_followup_mode(CHAT) == "queue"


async def test_config_page_notice_on_other_engines(tmp_path: Path) -> None:
    ctx = _config_ctx("fu", config_path=tmp_path / "untether.toml", engine="codex")
    await ConfigCommand().handle(ctx)
    assert "Claude Code and Antigravity only" in _edited(ctx).text


async def test_config_page_names_the_chat_engine(tmp_path: Path) -> None:
    """#1077: an Antigravity chat's page said "while Claude is working"."""
    path = tmp_path / "untether.toml"
    ctx = _config_ctx("fu", config_path=path, engine="antigravity")
    await ConfigCommand().handle(ctx)
    text = _edited(ctx).text
    assert "while Antigravity is working" in text
    assert "Antigravity picks it up" in text
    assert "Claude" not in text
    ctx = _config_ctx("fu", config_path=path)
    await ConfigCommand().handle(ctx)
    assert "while Claude is working" in _edited(ctx).text


async def test_config_page_no_config_path() -> None:
    ctx = _config_ctx("fu", config_path=None)
    await ConfigCommand().handle(ctx)
    assert "Unavailable" in _edited(ctx).text


async def test_config_home_shows_followup_for_claude(tmp_path: Path) -> None:
    path = tmp_path / "untether.toml"
    prefs = ChatPrefsStore(tmp_path / "telegram_chat_prefs_state.json")
    await prefs.set_followup_mode(CHAT, "steer")
    ctx = _config_ctx("", config_path=path)
    await ConfigCommand().handle(ctx)
    msg = ctx.executor.send.call_args[0][0]
    assert "Follow-up: <b>steer</b>" in msg.text
    assert "config:fu" in _data(msg)


async def test_config_home_hides_followup_on_codex(tmp_path: Path) -> None:
    ctx = _config_ctx("", config_path=tmp_path / "untether.toml", engine="codex")
    await ConfigCommand().handle(ctx)
    msg = ctx.executor.send.call_args[0][0]
    assert "Follow-up" not in msg.text
    assert "config:fu" not in _data(msg)


@pytest.mark.parametrize(
    ("args", "toast"),
    [
        ("fu:q", "Follow-up: queue"),
        ("fu:s", "Follow-up: steer"),
        ("fu:clr", "Follow-up: cleared"),
        ("fu", None),
    ],
)
def test_config_toasts(args: str, toast: str | None) -> None:
    assert ConfigCommand.early_answer_toast(args) == toast


async def test_session_resolved_lazily_only_when_steering() -> None:
    _install()
    looked_up: list[int] = []

    async def resolve() -> ResumeToken:
        looked_up.append(1)
        return TOKEN

    async def run(cfg: Any) -> bool:
        return await maybe_steer(
            cfg,
            chat_id=CHAT,
            user_msg_id=1,
            thread_id=None,
            topic_thread_id=None,
            prompt_text="x",
            engine="claude",
            resume_token=None,
            resolve_token=resolve,
            override=None,
            running_tasks={},
            chat_prefs=None,
            topic_store=None,
        )

    assert await run(_cfg("queue")) is False
    assert looked_up == []
    assert await run(_cfg("steer")) is True
    assert looked_up == [1]


# ── #996: a /ctx change never steers into the old cwd ──────────────────────


def _cwd_fn(value: Any):
    async def run_cwd() -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    return run_cwd


async def test_996_idle_steer_after_ctx_change_takes_queue_path(
    tmp_path: Path,
) -> None:
    live, pipe = _install(idle=True)
    live.state.spawn_cwd = tmp_path / "old"
    cfg = _cfg("steer")
    assert await _steer(cfg, run_cwd=_cwd_fn(tmp_path / "new")) is False
    assert pipe.sent == [] and _texts(cfg) == []  # silent: queue path says it
    assert rb._FOLLOWUP_ANCHORS == {}


async def test_996_idle_steer_same_cwd_still_written(tmp_path: Path) -> None:
    live, pipe = _install(idle=True)
    live.state.spawn_cwd = tmp_path / "proj"
    assert await _steer(_cfg("steer"), run_cwd=_cwd_fn(tmp_path / "proj")) is True
    assert len(pipe.sent) == 1


async def test_996_unresolvable_cwd_never_steers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live, pipe = _install(idle=True)
    live.state.spawn_cwd = tmp_path / "proj"
    called: list[str] = []

    async def fake(*_a: Any, **_k: Any) -> str:  # pragma: no cover
        called.append("steer")
        return "written_idle"

    monkeypatch.setattr(claude_mod, "steer_into_session", fake)
    out = await _steer(_cfg("steer"), run_cwd=_cwd_fn(RuntimeError("gone")))
    assert out is False
    assert called == [] and pipe.sent == []
    assert rb._FOLLOWUP_ANCHORS == {}


# ── Antigravity (#981): steer through Untether's agy plugin ────────────────

AGY_CID = "c752bf1f-9d83-49ad-8a16-e3ae862e6afb"
AGY_TOKEN = ResumeToken(engine="antigravity", value=AGY_CID)


@pytest.fixture
def agy_gate_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from untether.antigravity import gate_plugin, gate_server

    gate_server._reset_for_tests()
    monkeypatch.setattr(gate_plugin, "installed_ready", lambda: True)


async def test_agy_steer_queues_into_the_running_gated_turn(
    agy_gate_installed: None,
) -> None:
    from untether.antigravity import gate_server
    from untether.events import EventFactory

    gate = gate_server.GateRun(config_path=None, factory=EventFactory("antigravity"))
    gate.set_conversation(AGY_CID)
    gate_server._RUNS[gate.session_key] = gate
    cfg = _cfg("steer")
    ok = await _steer(cfg, engine="antigravity", token=AGY_TOKEN)
    assert ok is True
    assert _texts(cfg) == [STEERED_ACK]
    (item,) = gate.steer_queue
    assert item.text == "also the hostname"
    # The anchor waits for the delivery note (or the run-end sweep).
    assert item.command_uuid in rb._FOLLOWUP_ANCHORS


async def test_agy_steer_without_running_turn_queues(agy_gate_installed: None) -> None:
    cfg = _cfg("steer")
    running = {MessageRef(channel_id=CHAT, message_id=9): _Busy()}
    ok = await _steer(
        cfg, engine="antigravity", token=AGY_TOKEN, override="steer", running=running
    )
    assert ok is False
    assert _texts(cfg) == [fallback_notice("no_live", "antigravity")]
    assert "Antigravity" in _texts(cfg)[0] and "Claude" not in _texts(cfg)[0]


async def test_agy_steer_without_plugin_says_how_to_install() -> None:
    cfg = _cfg("steer")
    running = {MessageRef(channel_id=CHAT, message_id=9): _Busy()}
    ok = await _steer(
        cfg, engine="antigravity", token=AGY_TOKEN, override="steer", running=running
    )
    assert ok is False
    assert "untether agy-gate install" in _texts(cfg)[0]
    # #1077: sent as HTML, so the command shows as code, not in backticks.
    (call,) = cfg.exec_cfg.transport.send_calls
    assert call["message"].extra.get("parse_mode") == "HTML"
    assert "`" not in _texts(cfg)[0]
    assert "<code>untether agy-gate install</code>" in _texts(cfg)[0]


async def test_agy_steer_while_the_turn_is_starting_says_so(
    agy_gate_installed: None,
) -> None:
    """#1077: a steer sent before agy reported its session (so before its
    gate is bound) said "No running Antigravity turn" while a turn was
    starting."""
    cfg = _cfg("steer")
    busy = _Busy()
    busy.resume_ready = anyio.Event()  # type: ignore[attr-defined]
    running = {MessageRef(channel_id=CHAT, message_id=9): busy}
    with structlog.testing.capture_logs() as logs:
        ok = await _steer(
            cfg,
            engine="antigravity",
            token=AGY_TOKEN,
            override="steer",
            running=running,
        )
    assert ok is False
    assert _texts(cfg) == [fallback_notice("starting", "antigravity")]
    assert "still starting" in _texts(cfg)[0]
    assert [log["reason"] for log in logs if log["event"] == "steer.fallback"] == [
        "starting"
    ]
    # Once the run has reported its session, a missing gate is "no_live".
    busy.resume_ready.set()  # type: ignore[attr-defined]
    cfg = _cfg("steer")
    await _steer(
        cfg, engine="antigravity", token=AGY_TOKEN, override="steer", running=running
    )
    assert _texts(cfg) == [fallback_notice("no_live", "antigravity")]


async def test_agy_steer_after_result_falls_back(agy_gate_installed: None) -> None:
    from untether.antigravity import gate_server
    from untether.events import EventFactory

    gate = gate_server.GateRun(config_path=None, factory=EventFactory("antigravity"))
    gate.set_conversation(AGY_CID)
    gate.note_result()
    cfg = _cfg("steer")
    assert await _steer(cfg, engine="antigravity", token=AGY_TOKEN) is False
    assert not gate.steer_queue


def test_agy_is_a_steer_target_not_a_live_session() -> None:
    from untether import live_sessions

    assert live_sessions.live_backend("antigravity") is None
    assert not live_sessions.supports_live_session("antigravity")
    target = live_sessions.steer_backend("antigravity")
    assert target is not None and target.engine == "antigravity"
    assert live_sessions.steer_backend("claude") is live_sessions.live_backend("claude")
    assert live_sessions.steer_backend("codex") is None
