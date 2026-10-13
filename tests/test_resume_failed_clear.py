"""#1052: a failed resume clears the saved session only when it is the one
that failed.

A stale resume line (a reply to an old resume footer, or one pasted by hand)
names some other session. Its failure must leave the chat's or topic's own
saved session alone, for every engine, and must not say anything was
cleared. A failed resume of the saved session itself still clears it (#45,
#952), and a failed ``/continue`` still clears nothing (#1039).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.markdown import MarkdownPresenter
from untether.model import ResumeToken
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import ErrorReturn, ScriptRunner
from untether.telegram.chat_sessions import ChatSessionStore
from untether.telegram.loop import _clear_failed_session
from untether.telegram.topic_state import TopicStateStore
from untether.transport import MessageRef, RenderedMessage, SendOptions

SAVED = "healthy-session"
STALE = "00000000-1111-2222-3333-444444444444"
CLEARED_TEXT = "couldn't be resumed, so it was cleared"

# Each engine's own "that session doesn't exist" failure — the errors that
# clear a saved session (#952). Claude reports turns, so a failed resume with
# none clears whatever the text says.
RESUME_FAILURES: list[tuple[str, str]] = [
    ("claude", "claude failed (rc=1).\nNo conversation found with session ID"),
    (
        "codex",
        "codex exec failed (rc=1).\nError: thread/resume: thread/resume failed:"
        " no rollout found for thread id 0000 (code -32600)",
    ),
    ("opencode", 'NotFoundError data: {message: "Session not found: ses_x"}'),
    ("pi", "pi failed (rc=1).\nNo session found matching 'zzzz'"),
    (
        "antigravity",
        "Antigravity conversation not found: That Antigravity conversation no"
        " longer exists.",
    ),
]
ENGINES = [engine for engine, _ in RESUME_FAILURES]


class _Transport:
    def __init__(self) -> None:
        self._next_id = 1
        self.texts: list[str] = []

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        self.texts.append(message.text)
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        return ref

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef:
        self.texts.append(message.text)
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        return True

    async def close(self) -> None:
        return None


# -- the stores ---------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ENGINES)
async def test_chat_store_keeps_saved_session_for_another_id(
    tmp_path: Path, engine: str
) -> None:
    store = ChatSessionStore(tmp_path / "sessions.json")
    await store.set_session_resume(1, None, ResumeToken(engine=engine, value=SAVED))

    with structlog.testing.capture_logs() as logs:
        removed = await store.clear_engine_session(1, None, engine, resume=STALE)

    assert removed is False
    assert await store.get_session_resume(1, None, engine) == ResumeToken(
        engine=engine, value=SAVED
    )
    (skip,) = [e for e in logs if e["event"] == "session.engine_clear_skipped"]
    assert skip["reason"] == "not_saved_session"
    # Survives a reload: nothing was written over the saved session.
    reloaded = ChatSessionStore(tmp_path / "sessions.json")
    assert await reloaded.get_session_resume(1, None, engine) == ResumeToken(
        engine=engine, value=SAVED
    )


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ENGINES)
async def test_chat_store_clears_the_saved_session_by_id(
    tmp_path: Path, engine: str
) -> None:
    store = ChatSessionStore(tmp_path / "sessions.json")
    await store.set_session_resume(1, 7, ResumeToken(engine=engine, value=SAVED))
    await store.set_session_resume(1, 7, ResumeToken(engine="other", value="x"))

    assert await store.clear_engine_session(1, 7, engine, resume=SAVED) is True
    assert await store.get_session_resume(1, 7, engine) is None
    # Other engines' sessions in the chat are untouched.
    assert await store.get_session_resume(1, 7, "other") == ResumeToken(
        engine="other", value="x"
    )


@pytest.mark.anyio
async def test_chat_store_without_resume_clears_by_engine(tmp_path: Path) -> None:
    store = ChatSessionStore(tmp_path / "sessions.json")
    await store.set_session_resume(1, None, ResumeToken(engine="codex", value=SAVED))

    assert await store.clear_engine_session(1, None, "codex") is True
    assert await store.get_session_resume(1, None, "codex") is None
    # Nothing saved → nothing removed.
    assert await store.clear_engine_session(1, None, "codex") is False
    assert await store.clear_engine_session(9, None, "codex", resume=SAVED) is False


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ENGINES)
async def test_topic_store_compares_session_ids(tmp_path: Path, engine: str) -> None:
    store = TopicStateStore(tmp_path / "topics.json")
    await store.set_session_resume(1, 10, ResumeToken(engine=engine, value=SAVED))

    assert await store.clear_engine_session(1, 10, engine, resume=STALE) is False
    assert await store.get_session_resume(1, 10, engine) == ResumeToken(
        engine=engine, value=SAVED
    )
    assert await store.clear_engine_session(1, 10, engine, resume=SAVED) is True
    assert await store.get_session_resume(1, 10, engine) is None
    assert await store.clear_engine_session(1, 99, engine, resume=SAVED) is False


# -- the Telegram loop's on_resume_failed -------------------------------------


@pytest.mark.anyio
async def test_clear_failed_session_reports_whether_it_cleared(
    tmp_path: Path,
) -> None:
    chats = ChatSessionStore(tmp_path / "sessions.json")
    topics = TopicStateStore(tmp_path / "topics.json")
    await chats.set_session_resume(5, None, ResumeToken(engine="pi", value=SAVED))
    await topics.set_session_resume(5, 3, ResumeToken(engine="pi", value=SAVED))

    async def clear(value: str, **keys: Any) -> bool:
        return await _clear_failed_session(
            ResumeToken(engine="pi", value=value),
            topic_store=topics,
            chat_session_store=chats,
            **keys,
        )

    assert await clear(STALE, topic_key=(5, 3), chat_session_key=(5, None)) is False
    assert await chats.get_session_resume(5, None, "pi") is not None
    assert await topics.get_session_resume(5, 3, "pi") is not None

    assert await clear(SAVED, topic_key=None, chat_session_key=(5, None)) is True
    assert await chats.get_session_resume(5, None, "pi") is None
    assert await topics.get_session_resume(5, 3, "pi") is not None

    assert await clear(SAVED, topic_key=(5, 3), chat_session_key=None) is True
    assert await topics.get_session_resume(5, 3, "pi") is None


# -- end to end through handle_message ----------------------------------------


async def _failed_resume(
    tmp_path: Path, engine: str, error: str, resume: ResumeToken
) -> tuple[ChatSessionStore, list[str], list[dict[str, Any]]]:
    """A resumed run that fails with the engine's own "no such session" error,
    in a chat whose saved session is ``SAVED``. ``on_resume_failed`` is the
    Telegram loop's (via ``_clear_failed_session``)."""
    store = ChatSessionStore(tmp_path / "sessions.json")
    await store.set_session_resume(42, None, ResumeToken(engine=engine, value=SAVED))
    transport = _Transport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )

    async def on_resume_failed(token: ResumeToken) -> bool:
        return await _clear_failed_session(
            token,
            topic_store=None,
            topic_key=None,
            chat_session_store=store,
            chat_session_key=(42, None),
        )

    runner = ScriptRunner(
        [ErrorReturn(error=error, usage={})],
        engine=engine,
        resume_value=resume.value or None,
    )
    with structlog.testing.capture_logs() as logs:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=42, message_id=10, text="Reply OK"),
            resume_token=resume,
            on_resume_failed=on_resume_failed,
        )
    return store, transport.texts, logs


@pytest.mark.anyio
@pytest.mark.parametrize(("engine", "error"), RESUME_FAILURES)
async def test_stale_resume_line_keeps_the_saved_session(
    tmp_path: Path, engine: str, error: str
) -> None:
    """The issue's repro: the explicit resume line names a gone session; the
    chat's healthy saved session survives and nothing claims a clear."""
    store, texts, logs = await _failed_resume(
        tmp_path, engine, error, ResumeToken(engine=engine, value=STALE)
    )

    assert await store.get_session_resume(42, None, engine) == ResumeToken(
        engine=engine, value=SAVED
    )
    assert not any(CLEARED_TEXT in t for t in texts)
    assert not [e for e in logs if e["event"] == "session.auto_cleared"]
    assert not [e for e in logs if e["event"] == "session.engine_cleared"]
    (skip,) = [e for e in logs if e["event"] == "session.auto_clear_skipped"]
    assert skip["reason"] == "not_saved_session"
    assert skip["resume"] == STALE


@pytest.mark.anyio
@pytest.mark.parametrize(("engine", "error"), RESUME_FAILURES)
async def test_failed_resume_of_the_saved_session_still_clears(
    tmp_path: Path, engine: str, error: str
) -> None:
    """Unchanged (#45/#952): when the saved session itself is gone, it is
    cleared and the card says so."""
    store, texts, logs = await _failed_resume(
        tmp_path, engine, error, ResumeToken(engine=engine, value=SAVED)
    )

    assert await store.get_session_resume(42, None, engine) is None
    assert any(CLEARED_TEXT in t for t in texts)
    assert [e for e in logs if e["event"] == "session.auto_cleared"]


@pytest.mark.anyio
@pytest.mark.parametrize(("engine", "error"), RESUME_FAILURES)
async def test_failed_continue_still_clears_nothing(
    tmp_path: Path, engine: str, error: str
) -> None:
    """#1039 is kept: a /continue token names no session."""
    store, texts, logs = await _failed_resume(
        tmp_path, engine, error, ResumeToken(engine=engine, value="", is_continue=True)
    )

    assert await store.get_session_resume(42, None, engine) == ResumeToken(
        engine=engine, value=SAVED
    )
    assert not any(CLEARED_TEXT in t for t in texts)
    (skip,) = [e for e in logs if e["event"] == "session.auto_clear_skipped"]
    assert skip["reason"] == "continue_token"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("engine", "error"),
    [
        *RESUME_FAILURES,
        # The live repro: Claude's 0-turn resume failure, whose generic hint
        # said "Send /new to start a fresh session." (#228).
        ("claude", "claude failed (rc=1).\nerror_during_execution"),
    ],
)
async def test_stale_resume_card_keeps_the_saved_session_in_view(
    tmp_path: Path, engine: str, error: str
) -> None:
    """#1070: the stale resume line's error card says the named session
    couldn't be loaded and the saved one isn't affected (no /new advice), and
    its footer no longer offers the stale id as a resume target."""
    _store, texts, _logs = await _failed_resume(
        tmp_path, engine, error, ResumeToken(engine=engine, value=STALE)
    )
    final = texts[-1]
    assert "saved session isn't affected" in final
    assert "Send /new" not in final
    assert STALE not in final


@pytest.mark.anyio
async def test_failed_resume_of_the_saved_session_keeps_its_hint(
    tmp_path: Path,
) -> None:
    """Unchanged: when the saved session itself fails to load it is cleared,
    and the generic #228 hint and the cleared notice stay."""
    _store, texts, _logs = await _failed_resume(
        tmp_path,
        "claude",
        "claude failed (rc=1).\nerror_during_execution",
        ResumeToken(engine="claude", value=SAVED),
    )
    final = texts[-1]
    assert "could not be loaded" in final
    assert CLEARED_TEXT in final
    assert "saved session isn't affected" not in final
