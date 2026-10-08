from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import (
    DEFAULT_ENGINE_ID,
    FakeTransport,
    _make_router,
    make_cfg,
)
from untether.config import ProjectConfig, ProjectsConfig
from untether.runner_bridge import RunningTask
from untether.runners.mock import Return, ScriptRunner
from untether.settings import TelegramTopicsSettings
from untether.telegram.chat_prefs import ChatPrefsStore, resolve_prefs_path
from untether.telegram.chat_sessions import ChatSessionStore
from untether.telegram.commands.topics import (
    _cancel_chat_tasks,
    _handle_chat_ctx_command,
    _handle_chat_new_command,
    _handle_ctx_command,
    _handle_new_command,
    _handle_topic_command,
)
from untether.telegram.topic_state import TopicStateStore
from untether.telegram.topics import thread_filter_for
from untether.telegram.types import TelegramIncomingMessage
from untether.transport import MessageRef
from untether.transport_runtime import TransportRuntime


def _msg(
    text: str,
    *,
    chat_id: int = 123,
    message_id: int = 1,
    thread_id: int | None = None,
    chat_type: str | None = "private",
    is_forum: bool | None = None,
    is_topic_message: bool | None = None,
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
        thread_id=thread_id,
        is_topic_message=is_topic_message,
        chat_type=chat_type,
        is_forum=is_forum,
    )


def _runtime(tmp_path: Path) -> tuple[TransportRuntime, Path]:
    runner = ScriptRunner([Return(answer="ok")], engine=DEFAULT_ENGINE_ID)
    projects = ProjectsConfig(
        projects={
            "alpha": ProjectConfig(
                alias="Alpha",
                path=tmp_path,
                worktrees_dir=Path(".worktrees"),
            )
        },
        default_project="alpha",
    )
    state_path = tmp_path / "untether.toml"
    runtime = TransportRuntime(
        router=_make_router(runner),
        projects=projects,
        config_path=state_path,
    )
    return runtime, state_path


@pytest.mark.anyio
async def test_ctx_command_requires_topic(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/ctx")

    await _handle_ctx_command(
        cfg,
        msg,
        args_text="",
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({msg.chat_id}),
    )

    text = transport.send_calls[-1]["message"].text
    assert "only works inside a topic" in text


@pytest.mark.anyio
async def test_chat_ctx_command_sets_binding(tmp_path: Path) -> None:
    transport = FakeTransport()
    runtime, state_path = _runtime(tmp_path)
    cfg = replace(make_cfg(transport), runtime=runtime, session_mode="chat")
    store = ChatPrefsStore(resolve_prefs_path(state_path))

    msg = _msg("/ctx set alpha @dev", chat_type="private")
    await _handle_chat_ctx_command(
        cfg,
        msg,
        args_text="set alpha @dev",
        chat_prefs=store,
    )

    msg_show = _msg("/ctx", chat_type="private")
    await _handle_chat_ctx_command(
        cfg,
        msg_show,
        args_text="",
        chat_prefs=store,
    )

    text = transport.send_calls[-1]["message"].text
    assert "bound ctx: Alpha @dev" in text


@pytest.mark.anyio
async def test_new_command_requires_topic(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/new")

    await _handle_new_command(
        cfg,
        msg,
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({msg.chat_id}),
    )

    text = transport.send_calls[-1]["message"].text
    assert "only works inside a topic" in text


@pytest.mark.anyio
async def test_chat_new_command_no_sessions(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")

    await _handle_chat_new_command(cfg, msg, store, session_key=None)

    text = transport.send_calls[-1]["message"].text
    assert "no stored sessions" in text


@pytest.mark.anyio
async def test_chat_new_command_group_clears(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="supergroup")

    await _handle_chat_new_command(cfg, msg, store, session_key=(msg.chat_id, 1))

    text = transport.send_calls[-1]["message"].text
    assert "cleared stored sessions for you in this chat" in text


@pytest.mark.anyio
async def test_topic_command_requires_args(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/topic")

    await _handle_topic_command(
        cfg,
        msg,
        args_text="",
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({msg.chat_id}),
    )

    text = transport.send_calls[-1]["message"].text
    assert "usage: /topic" in text


# --- /new cancellation tests ---


def test_cancel_chat_tasks_none() -> None:
    """No-op when running_tasks is None."""
    assert _cancel_chat_tasks(123, None) == 0


def test_cancel_chat_tasks_empty() -> None:
    """No-op when no tasks running."""
    assert _cancel_chat_tasks(123, {}) == 0


def test_cancel_chat_tasks_cancels_matching() -> None:
    """Cancels tasks matching the chat_id."""
    task = RunningTask()
    ref = MessageRef(channel_id=123, message_id=1)
    running_tasks = {ref: task}

    cancelled = _cancel_chat_tasks(123, running_tasks)

    assert cancelled == 1
    assert task.cancel_requested.is_set()


def test_cancel_chat_tasks_skips_other_chats() -> None:
    """Does not cancel tasks in other chats."""
    task = RunningTask()
    ref = MessageRef(channel_id=999, message_id=1)
    running_tasks = {ref: task}

    cancelled = _cancel_chat_tasks(123, running_tasks)

    assert cancelled == 0
    assert not task.cancel_requested.is_set()


def test_cancel_chat_tasks_skips_already_cancelled() -> None:
    """Does not double-cancel already-cancelled tasks."""
    task = RunningTask()
    task.cancel_requested.set()
    ref = MessageRef(channel_id=123, message_id=1)
    running_tasks = {ref: task}

    cancelled = _cancel_chat_tasks(123, running_tasks)

    assert cancelled == 0


def test_cancel_chat_tasks_multiple() -> None:
    """Cancels multiple tasks in the same chat."""
    task1 = RunningTask()
    task2 = RunningTask()
    ref1 = MessageRef(channel_id=123, message_id=1)
    ref2 = MessageRef(channel_id=123, message_id=2)
    running_tasks = {ref1: task1, ref2: task2}

    cancelled = _cancel_chat_tasks(123, running_tasks)

    assert cancelled == 2
    assert task1.cancel_requested.is_set()
    assert task2.cancel_requested.is_set()


@pytest.mark.anyio
async def test_chat_new_command_cancels_running(tmp_path: Path) -> None:
    """'/new' cancels a running task and mentions it in the reply."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")

    task = RunningTask()
    ref = MessageRef(channel_id=msg.chat_id, message_id=42)
    running_tasks = {ref: task}

    await _handle_chat_new_command(
        cfg, msg, store, session_key=(msg.chat_id, None), running_tasks=running_tasks
    )

    assert task.cancel_requested.is_set()
    text = transport.send_calls[-1]["message"].text
    assert "cancelled run" in text
    assert "cleared" in text


@pytest.mark.anyio
async def test_chat_new_command_cancel_only_no_sessions(tmp_path: Path) -> None:
    """'/new' with running task but no stored sessions still succeeds."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")

    task = RunningTask()
    ref = MessageRef(channel_id=msg.chat_id, message_id=42)
    running_tasks = {ref: task}

    await _handle_chat_new_command(
        cfg, msg, store, session_key=None, running_tasks=running_tasks
    )

    assert task.cancel_requested.is_set()
    text = transport.send_calls[-1]["message"].text
    assert "cancelled run" in text


@pytest.mark.anyio
async def test_chat_new_command_no_tasks_no_sessions(tmp_path: Path) -> None:
    """'/new' with no running tasks and no sessions shows 'no stored sessions'."""
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")

    await _handle_chat_new_command(cfg, msg, store, session_key=None, running_tasks={})

    text = transport.send_calls[-1]["message"].text
    assert "no stored sessions" in text


@pytest.mark.anyio
async def test_new_command_cancels_running_in_topic(tmp_path: Path) -> None:
    """'/new' in topic mode cancels running tasks."""
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _msg("/new", thread_id=10, chat_type="supergroup")

    task = RunningTask()
    ref = MessageRef(channel_id=msg.chat_id, message_id=42)
    running_tasks = {ref: task}

    await _handle_new_command(
        cfg,
        msg,
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({msg.chat_id}),
        running_tasks=running_tasks,
    )

    assert task.cancel_requested.is_set()
    text = transport.send_calls[-1]["message"].text
    assert "cancelled run" in text
    assert "cleared" in text


# --- #826: /new is scoped to the forum topic ---

FORUM = -1001234


def _forum_msg(thread_id: int | None) -> TelegramIncomingMessage:
    return _msg(
        "/new",
        chat_id=FORUM,
        thread_id=thread_id,
        chat_type="supergroup",
        is_forum=True,
        is_topic_message=True if thread_id is not None else None,
    )


def _tasks(chat_id: int, *thread_ids: int | None) -> dict[MessageRef, RunningTask]:
    return {
        MessageRef(channel_id=chat_id, message_id=100 + i): RunningTask(thread_id=t)
        for i, t in enumerate(thread_ids)
    }


def _by_thread(tasks: dict[MessageRef, RunningTask]) -> dict[int | None, bool]:
    return {t.thread_id: t.cancel_requested.is_set() for t in tasks.values()}


def test_826_cancel_chat_tasks_forum_topic_only() -> None:
    running = _tasks(FORUM, 6, 10)
    msg = _forum_msg(10)

    with capture_logs() as logs:
        cancelled = _cancel_chat_tasks(
            FORUM, running, thread_filter=thread_filter_for(msg), thread_id=10
        )

    assert cancelled == 1
    assert _by_thread(running) == {6: False, 10: True}
    scope = [e for e in logs if e.get("event") == "new.cancel_scope"]
    assert len(scope) == 1
    assert scope[0]["scoped"] is True
    assert scope[0]["cancelled"] == 1
    assert scope[0]["skipped_other_threads"] == 1
    assert scope[0]["thread_id"] == 10


def test_826_cancel_scope_counts_live_run_once() -> None:
    """#776: a live run registered under several refs is skipped once."""
    live = RunningTask(thread_id=6)
    running = {
        MessageRef(channel_id=FORUM, message_id=1): live,
        MessageRef(channel_id=FORUM, message_id=2): live,
    }
    with capture_logs() as logs:
        cancelled = _cancel_chat_tasks(
            FORUM, running, thread_filter=thread_filter_for(_forum_msg(10))
        )
    assert cancelled == 0
    scope = [e for e in logs if e.get("event") == "new.cancel_scope"]
    assert scope[0]["skipped_other_threads"] == 1
    assert scope[0]["cancelled"] == 0


def test_826_general_new_leaves_topics_alone() -> None:
    running = _tasks(FORUM, None, 6)
    cancelled = _cancel_chat_tasks(
        FORUM, running, thread_filter=thread_filter_for(_forum_msg(None))
    )
    assert cancelled == 1
    assert _by_thread(running) == {None: True, 6: False}


def test_826_general_id_1_equals_none() -> None:
    running = _tasks(FORUM, 1)
    assert (
        _cancel_chat_tasks(
            FORUM, running, thread_filter=thread_filter_for(_forum_msg(None))
        )
        == 1
    )
    running = _tasks(FORUM, None)
    assert (
        _cancel_chat_tasks(
            FORUM, running, thread_filter=thread_filter_for(_forum_msg(1))
        )
        == 1
    )


@pytest.mark.anyio
async def test_826_topic_new_handler_scoped(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    msg = _forum_msg(10)
    running = _tasks(FORUM, 10, 6)

    await _handle_new_command(
        cfg,
        msg,
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({FORUM}),
        running_tasks=running,
    )

    assert _by_thread(running) == {10: True, 6: False}
    text = transport.send_calls[-1]["message"].text
    assert "cancelled run and cleared stored sessions for this topic" in text


@pytest.mark.anyio
async def test_826_topic_new_handler_other_topic_only_clears(tmp_path: Path) -> None:
    """R17-14a shape: /new in topic B while only topic A runs."""
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    running = _tasks(FORUM, 6)

    await _handle_new_command(
        cfg,
        _forum_msg(10),
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({FORUM}),
        running_tasks=running,
    )

    assert _by_thread(running) == {6: False}
    text = transport.send_calls[-1]["message"].text
    assert "cancelled run" not in text
    assert "cleared stored sessions for this topic" in text


@pytest.mark.anyio
async def test_826_chat_new_handler_forum_topics_disabled(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _forum_msg(6)
    running = _tasks(FORUM, 6, 10, None)

    with capture_logs() as logs:
        await _handle_chat_new_command(
            cfg, msg, store, session_key=(FORUM, 1), running_tasks=running
        )

    assert _by_thread(running) == {6: True, 10: False, None: False}
    cancelled_log = [e for e in logs if e.get("event") == "new.cancelled_running"]
    assert cancelled_log and cancelled_log[0]["thread_id"] == 6


def test_826_non_forum_group_stays_chat_wide() -> None:
    msg = _msg("/new", chat_id=-100555, chat_type="supergroup")
    assert thread_filter_for(msg) is None
    running = _tasks(-100555, 55, None)
    with capture_logs() as logs:
        cancelled = _cancel_chat_tasks(
            -100555, running, thread_filter=thread_filter_for(msg)
        )
    assert cancelled == 2
    scope = [e for e in logs if e.get("event") == "new.cancel_scope"]
    assert scope[0]["scoped"] is False


def test_826_basic_group_stays_chat_wide() -> None:
    msg = _msg("/new", chat_id=-555, chat_type="group")
    assert thread_filter_for(msg) is None


def test_826_private_chat_unchanged() -> None:
    msg = _msg("/new", chat_type="private")
    running = _tasks(msg.chat_id, None, None)
    assert (
        _cancel_chat_tasks(msg.chat_id, running, thread_filter=thread_filter_for(msg))
        == 2
    )


def test_826_private_chat_without_chat_type_uses_chat_id() -> None:
    """``is_private`` falls back to ``chat_id > 0`` when chat_type is absent."""
    msg = _msg("/new", chat_id=777, thread_id=3, chat_type=None)
    running = _tasks(777, 3, 4)
    _cancel_chat_tasks(777, running, thread_filter=thread_filter_for(msg))
    assert _by_thread(running) == {3: True, 4: False}


def test_826_private_topics_scoped() -> None:
    msg = _msg("/new", thread_id=3, chat_type="private", is_topic_message=True)
    running = _tasks(msg.chat_id, 3, 4)
    assert (
        _cancel_chat_tasks(msg.chat_id, running, thread_filter=thread_filter_for(msg))
        == 1
    )
    assert _by_thread(running) == {3: True, 4: False}


@pytest.mark.anyio
async def test_826_loop_entries_scoped() -> None:
    from untether import loop_scheduler

    class _Transport:
        async def send(self, **_):
            return None

        async def edit(self, **_):
            return None

        async def delete(self, _ref):
            return None

    async def _noop(*_args, **_kwargs):
        return None

    loop_scheduler.uninstall()
    async with anyio.create_task_group() as tg:
        loop_scheduler.install(tg, _noop, _Transport(), 1)
        try:
            for thread_id in (6, 10):
                loop_scheduler.register_pending_cron(
                    session_id=f"sess-{thread_id}",
                    tool_use_id=f"tu-{thread_id}",
                    cron_expression="*/5 * * * *",
                    prompt=f"p{thread_id}",
                    recurring=True,
                    chat_id=50,
                    thread_id=thread_id,
                )
            msg = _msg(
                "/new",
                chat_id=50,
                thread_id=10,
                chat_type="supergroup",
                is_forum=True,
            )
            cancelled = _cancel_chat_tasks(50, {}, thread_filter=thread_filter_for(msg))
            assert cancelled == 1
            assert [e.thread_id for e in loop_scheduler.pending_for_chat(50)] == [6]
        finally:
            tg.cancel_scope.cancel()
            loop_scheduler.uninstall()


@pytest.mark.anyio
async def test_826_stateless_new_scoped_to_topic() -> None:
    """The stateless /new closure (no topic store, no chat store) is scoped too."""
    from untether.telegram.loop import TelegramCommandContext, _dispatch_builtin_command

    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _forum_msg(10)
    running = _tasks(FORUM, 10, 6)
    replies: list[str] = []
    started: list = []

    async def _reply(*, text: str, **_kwargs) -> None:
        replies.append(text)

    class _TG:
        def start_soon(self, func, *args) -> None:
            started.append((func, args))

    ctx = TelegramCommandContext(
        cfg=cfg,
        msg=msg,
        args_text="",
        ambient_context=None,
        topic_store=None,
        chat_prefs=None,
        resolved_scope=None,
        scope_chat_ids=frozenset(),
        reply=_reply,
        task_group=_TG(),  # type: ignore[arg-type]
        running_tasks=running,
    )
    assert _dispatch_builtin_command(ctx=ctx, command_id="new") is True
    func, args = started[0]
    await func(*args)

    assert _by_thread(running) == {10: True, 6: False}
    assert replies == ["cancelled run for this chat."]


# --- #895: /new after a reply closed the idle live session, not a run ---

IDLE_SID = "s-895"


def _live_task(
    *,
    idle: bool = True,
    sid: str = IDLE_SID,
    thread_id: int | None = None,
    background: bool = False,
) -> RunningTask:
    """A live Claude run: idle = between turns after its result (#776)."""
    from types import SimpleNamespace

    from untether.model import ResumeToken
    from untether.runners.claude import ClaudeStreamState, ClaudeTask

    state = ClaudeStreamState()
    state.live_mode = True
    state.completed_turns = 1
    state.turn_open = not idle
    if background:
        state.tasks["t1"] = ClaudeTask(task_id="t1", is_backgrounded=True)
    task = RunningTask(
        edits=SimpleNamespace(stream=SimpleNamespace(engine_state=state)),  # type: ignore[arg-type]
        thread_id=thread_id,
    )
    task.resume = ResumeToken(engine="claude", value=sid)
    return task


def _one(task: RunningTask, chat_id: int = 123) -> dict[MessageRef, RunningTask]:
    return {MessageRef(channel_id=chat_id, message_id=42): task}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("chat_type", "chat_id", "expected"),
    [
        (
            "group",
            -555,
            "\N{BROOM} closed the idle session and cleared stored sessions "
            "for you in this chat.",
        ),
        (
            "private",
            123,
            "\N{BROOM} closed the idle session and cleared stored sessions "
            "for this chat.",
        ),
    ],
)
async def test_895_chat_new_idle_live_session_says_closed(
    tmp_path: Path, chat_type: str, chat_id: int, expected: str
) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_id=chat_id, chat_type=chat_type)
    task = _live_task()

    with capture_logs() as logs:
        await _handle_chat_new_command(
            cfg,
            msg,
            store,
            session_key=(chat_id, None),
            running_tasks=_one(task, chat_id),
        )

    assert task.cancel_requested.is_set()
    text = transport.send_calls[-1]["message"].text
    assert text == expected
    assert "cancelled run" not in text
    scope = [e for e in logs if e.get("event") == "new.cancel_scope"]
    assert scope[0]["cancelled"] == 1 and scope[0]["idle"] == 1
    running_log = [e for e in logs if e.get("event") == "new.cancelled_running"]
    assert running_log[0]["idle"] == 1


@pytest.mark.anyio
async def test_895_chat_new_idle_without_stored_session_still_replies(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")

    await _handle_chat_new_command(
        cfg, msg, store, session_key=None, running_tasks=_one(_live_task())
    )

    text = transport.send_calls[-1]["message"].text
    assert text.startswith("\N{BROOM} closed the idle session")


@pytest.mark.anyio
async def test_895_topic_new_idle_live_session_says_closed(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        topics=TelegramTopicsSettings(enabled=True, scope="all"),
    )
    store = TopicStateStore(tmp_path / "topics.json")
    task = _live_task(thread_id=10)

    await _handle_new_command(
        cfg,
        _forum_msg(10),
        store=store,
        resolved_scope="all",
        scope_chat_ids=frozenset({FORUM}),
        running_tasks=_one(task, FORUM),
    )

    assert task.cancel_requested.is_set()
    text = transport.send_calls[-1]["message"].text
    assert text == (
        "\N{BROOM} closed the idle session and cleared stored sessions for this topic."
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "running",
    [
        pytest.param(lambda: _one(_live_task(idle=False)), id="turn-in-flight"),
        pytest.param(lambda: _one(_live_task(background=True)), id="bg-task-holds"),
        pytest.param(lambda: _one(RunningTask()), id="non-live-run"),
        pytest.param(
            lambda: {
                MessageRef(channel_id=123, message_id=42): _live_task(),
                MessageRef(channel_id=123, message_id=43): RunningTask(),
            },
            id="mixed-idle-and-busy",
        ),
    ],
)
async def test_895_chat_new_in_flight_keeps_cancelled_run(
    tmp_path: Path, running
) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")

    await _handle_chat_new_command(
        cfg, msg, store, session_key=(msg.chat_id, None), running_tasks=running()
    )

    text = transport.send_calls[-1]["message"].text
    assert text == "\N{BROOM} cancelled run and cleared stored sessions for this chat."


@pytest.mark.anyio
async def test_895_queued_followup_is_not_idle(tmp_path: Path) -> None:
    """A follow-up written into the session whose turn hasn't opened yet is
    pending work — /new cancels it, so the reply says so."""
    from untether.runner_bridge import pop_followup_anchor, register_followup_anchor

    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_type="private")
    register_followup_anchor(
        "uuid-895",
        session_id=IDLE_SID,
        reply_to=MessageRef(channel_id=123, message_id=50),
        placeholder=None,
    )
    try:
        await _handle_chat_new_command(
            cfg,
            msg,
            store,
            session_key=(msg.chat_id, None),
            running_tasks=_one(_live_task()),
        )
    finally:
        pop_followup_anchor("uuid-895")

    text = transport.send_calls[-1]["message"].text
    assert "cancelled run" in text


@pytest.mark.anyio
async def test_895_two_idle_sessions_plural(tmp_path: Path) -> None:
    transport = FakeTransport()
    cfg = make_cfg(transport)
    store = ChatSessionStore(tmp_path / "sessions.json")
    msg = _msg("/new", chat_id=-555, chat_type="group")
    running = {
        MessageRef(channel_id=-555, message_id=42): _live_task(sid="a"),
        MessageRef(channel_id=-555, message_id=43): _live_task(sid="b"),
    }

    await _handle_chat_new_command(
        cfg, msg, store, session_key=(-555, None), running_tasks=running
    )

    text = transport.send_calls[-1]["message"].text
    assert text.startswith("\N{BROOM} closed the idle sessions and cleared")


@pytest.mark.anyio
async def test_895_stateless_new_idle_live_session() -> None:
    from untether.telegram.loop import TelegramCommandContext, _dispatch_builtin_command

    transport = FakeTransport()
    cfg = make_cfg(transport)
    msg = _msg("/new", chat_type="private")
    task = _live_task()
    replies: list[str] = []
    started: list = []

    async def _reply(*, text: str, **_kwargs) -> None:
        replies.append(text)

    class _TG:
        def start_soon(self, func, *args) -> None:
            started.append((func, args))

    ctx = TelegramCommandContext(
        cfg=cfg,
        msg=msg,
        args_text="",
        ambient_context=None,
        topic_store=None,
        chat_prefs=None,
        resolved_scope=None,
        scope_chat_ids=frozenset(),
        reply=_reply,
        task_group=_TG(),  # type: ignore[arg-type]
        running_tasks=_one(task),
    )
    assert _dispatch_builtin_command(ctx=ctx, command_id="new") is True
    func, args = started[0]
    await func(*args)

    assert task.cancel_requested.is_set()
    assert replies == ["closed the idle session for this chat."]


def test_895_live_alias_refs_count_once() -> None:
    """A live run sits under its progress ref and each turn's ref (#776)."""
    from untether.telegram.commands.topics import _cancel_chat_tasks_counted

    task = _live_task()
    running = {
        MessageRef(channel_id=123, message_id=42): task,
        MessageRef(channel_id=123, message_id=44): task,
    }
    result = _cancel_chat_tasks_counted(123, running)
    assert (result.cancelled, result.idle) == (1, 1)
