from typing import Any

import anyio
import pytest

from untether.telegram.api_models import (
    Chat,
    ChatMember,
    File,
    ForumTopic,
    Message,
    Update,
    User,
)
from untether.telegram.client import BotClient, TelegramClient, TelegramRetryAfter
from untether.telegram.client_api import RetryAfter
from untether.telegram.outbox import (
    EDIT_PRIORITY,
    SEND_PRIORITY,
    OutboxOp,
    TelegramOutbox,
)


class FakeBot(BotClient):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.edit_calls: list[str] = []
        self.delete_calls: list[tuple[int, int]] = []
        self.topic_calls: list[tuple[int, int, str]] = []
        self.document_calls: list[
            tuple[int, str, bytes, int | None, int | None, bool | None, str | None]
        ] = []
        self.command_calls: list[
            tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]
        ] = []
        self.callback_calls: list[tuple[str, str | None, bool | None]] = []
        self.chat_calls: list[int] = []
        self.chat_member_calls: list[tuple[int, int]] = []
        self.create_topic_calls: list[tuple[int, str]] = []
        self._edit_attempts = 0
        self._updates_attempts = 0
        self.retry_after: float | None = None
        self.updates_retry_after: float | None = None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        message_thread_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        *,
        replace_message_id: int | None = None,
    ) -> Message | None:
        _ = reply_to_message_id
        _ = disable_notification
        _ = message_thread_id
        _ = entities
        _ = parse_mode
        _ = reply_markup
        _ = replace_message_id
        self.calls.append("send_message")
        return Message(message_id=1, chat=Chat(id=chat_id, type="private"))

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool | None = False,
        caption: str | None = None,
    ) -> Message | None:
        self.calls.append("send_document")
        self.document_calls.append(
            (
                chat_id,
                filename,
                content,
                reply_to_message_id,
                message_thread_id,
                disable_notification,
                caption,
            )
        )
        return Message(message_id=1, chat=Chat(id=chat_id, type="private"))

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        *,
        wait: bool = True,
    ) -> Message | None:
        _ = chat_id
        _ = message_id
        _ = entities
        _ = parse_mode
        _ = reply_markup
        _ = wait
        self.calls.append("edit_message_text")
        self.edit_calls.append(text)
        if self.retry_after is not None and self._edit_attempts == 0:
            self._edit_attempts += 1
            raise TelegramRetryAfter(self.retry_after)
        self._edit_attempts += 1
        return Message(message_id=message_id, chat=Chat(id=chat_id, type="private"))

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> bool:
        self.calls.append("delete_message")
        self.delete_calls.append((chat_id, message_id))
        return True

    async def set_my_commands(
        self,
        commands: list[dict[str, Any]],
        *,
        scope: dict[str, Any] | None = None,
        language_code: str | None = None,
    ) -> bool:
        self.calls.append("set_my_commands")
        self.command_calls.append((commands, scope, language_code))
        return True

    async def get_updates(
        self,
        offset: int | None,
        timeout_s: int = 50,
        allowed_updates: list[str] | None = None,
    ) -> list[Update] | None:
        _ = offset
        _ = timeout_s
        _ = allowed_updates
        if self.updates_retry_after is not None and self._updates_attempts == 0:
            self._updates_attempts += 1
            raise TelegramRetryAfter(self.updates_retry_after)
        self._updates_attempts += 1
        return []

    async def get_file(self, file_id: str) -> File | None:
        _ = file_id
        return None

    async def download_file(self, file_path: str) -> bytes | None:
        _ = file_path
        return None

    async def close(self) -> None:
        return None

    async def get_me(self) -> User | None:
        return User(id=1)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool | None = None,
    ) -> bool:
        self.calls.append("answer_callback_query")
        self.callback_calls.append((callback_query_id, text, show_alert))
        return True

    async def edit_forum_topic(
        self, chat_id: int, message_thread_id: int, name: str
    ) -> bool:
        self.calls.append("edit_forum_topic")
        self.topic_calls.append((chat_id, message_thread_id, name))
        return True

    async def get_chat(self, chat_id: int) -> Chat | None:
        self.calls.append("get_chat")
        self.chat_calls.append(chat_id)
        return Chat(id=chat_id, type="private")

    async def get_chat_member(self, chat_id: int, user_id: int) -> ChatMember | None:
        self.calls.append("get_chat_member")
        self.chat_member_calls.append((chat_id, user_id))
        return ChatMember(status="member")

    async def create_forum_topic(self, chat_id: int, name: str) -> ForumTopic | None:
        self.calls.append("create_forum_topic")
        self.create_topic_calls.append((chat_id, name))
        return ForumTopic(message_thread_id=11)


@pytest.mark.anyio
async def test_edit_forum_topic_uses_outbox() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    result = await client.edit_forum_topic(
        chat_id=7, message_thread_id=42, name="untether @main"
    )

    assert result is True
    assert bot.calls == ["edit_forum_topic"]
    assert bot.topic_calls == [(7, 42, "untether @main")]


@pytest.mark.anyio
async def test_send_document_uses_outbox() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    result = await client.send_document(
        chat_id=5,
        filename="note.txt",
        content=b"hello",
        caption="greetings",
    )

    assert result is not None
    assert bot.calls == ["send_document"]
    assert bot.document_calls == [
        (5, "note.txt", b"hello", None, None, False, "greetings")
    ]
    await client.close()


@pytest.mark.anyio
async def test_set_my_commands_uses_outbox() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    commands = [{"command": "ping", "description": "Ping the bot"}]
    result = await client.set_my_commands(
        commands,
        scope={"type": "default"},
        language_code="en",
    )

    assert result is True
    assert bot.calls == ["set_my_commands"]
    assert bot.command_calls == [(commands, {"type": "default"}, "en")]
    await client.close()


@pytest.mark.anyio
async def test_answer_callback_query_uses_outbox() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    result = await client.answer_callback_query(
        callback_query_id="cb-1",
        text="ok",
        show_alert=True,
    )

    assert result is True
    assert bot.calls == ["answer_callback_query"]
    assert bot.callback_calls == [("cb-1", "ok", True)]
    await client.close()


@pytest.mark.anyio
async def test_get_chat_and_member_uses_outbox() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    chat = await client.get_chat(9)
    member = await client.get_chat_member(9, 42)

    assert chat is not None
    assert chat.id == 9
    assert member is not None
    assert member.status == "member"
    assert bot.calls == ["get_chat", "get_chat_member"]
    assert bot.chat_calls == [9]
    assert bot.chat_member_calls == [(9, 42)]
    await client.close()


@pytest.mark.anyio
async def test_create_forum_topic_uses_outbox() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    topic = await client.create_forum_topic(3, "status updates")

    assert topic is not None
    assert topic.message_thread_id == 11
    assert bot.calls == ["create_forum_topic"]
    assert bot.create_topic_calls == [(3, "status updates")]
    await client.close()


@pytest.mark.anyio
async def test_edits_coalesce_latest() -> None:
    class _BlockingBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.edit_started = anyio.Event()
            self.release = anyio.Event()
            self._block_first = True

        async def edit_message_text(
            self,
            chat_id: int,
            message_id: int,
            text: str,
            entities: list[dict] | None = None,
            parse_mode: str | None = None,
            reply_markup: dict | None = None,
            *,
            wait: bool = True,
        ) -> Message | None:
            if self._block_first:
                self._block_first = False
                self.edit_started.set()
                await self.release.wait()
            return await super().edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                entities=entities,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                wait=wait,
            )

    bot = _BlockingBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="first",
        wait=False,
    )

    with anyio.fail_after(1):
        await bot.edit_started.wait()

    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="second",
        wait=False,
    )
    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="third",
        wait=False,
    )

    bot.release.set()

    with anyio.fail_after(1):
        while len(bot.edit_calls) < 2:
            await anyio.sleep(0)

    assert bot.edit_calls == ["first", "third"]


@pytest.mark.anyio
async def test_send_preempts_pending_edit() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="first",
    )

    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="progress",
        wait=False,
    )

    with anyio.fail_after(1):
        await client.send_message(chat_id=1, text="final")

    with anyio.fail_after(1):
        while len(bot.calls) < 3:
            await anyio.sleep(0)
    assert bot.calls[0] == "edit_message_text"
    assert bot.calls[1] == "send_message"
    assert bot.calls[-1] == "edit_message_text"


@pytest.mark.anyio
async def test_delete_drops_pending_edits() -> None:
    bot = FakeBot()
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="first",
    )

    await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="progress",
        wait=False,
    )

    with anyio.fail_after(1):
        await client.delete_message(
            chat_id=1,
            message_id=1,
        )

    with anyio.fail_after(1):
        while not bot.delete_calls:
            await anyio.sleep(0)
    assert bot.delete_calls == [(1, 1)]
    assert bot.edit_calls == ["first"]


@pytest.mark.anyio
async def test_retry_after_retries_once() -> None:
    bot = FakeBot()
    bot.retry_after = 0.0
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    result = await client.edit_message_text(
        chat_id=1,
        message_id=1,
        text="retry",
    )

    assert result is not None
    assert result.message_id == 1
    assert bot._edit_attempts == 2


@pytest.mark.anyio
async def test_get_updates_retries_on_retry_after() -> None:
    bot = FakeBot()
    bot.updates_retry_after = 0.0
    client = TelegramClient(client=bot, private_chat_rps=0.0, group_chat_rps=0.0)

    with anyio.fail_after(1):
        updates = await client.get_updates(offset=None, timeout_s=0)

    assert updates == []
    assert bot._updates_attempts == 2


# ---------------------------------------------------------------------------
# Per-chat pacing tests (issue #48)
# ---------------------------------------------------------------------------


def _make_outbox(
    *,
    private_rps: float = 1.0,
    group_rps: float = 20.0 / 60.0,
) -> tuple[TelegramOutbox, list[float], list[float]]:
    """Outbox with controllable clock. Returns (outbox, sleep_log, clock_state).

    clock_state is a single-element list: [current_time].  Advance time by
    mutating clock_state[0].  sleep_log records every sleep duration.
    """
    clock_state: list[float] = [0.0]
    sleep_log: list[float] = []
    private_interval = 0.0 if private_rps <= 0 else 1.0 / private_rps
    group_interval = 0.0 if group_rps <= 0 else 1.0 / group_rps

    def clock() -> float:
        return clock_state[0]

    async def fake_sleep(seconds: float) -> None:
        sleep_log.append(seconds)
        clock_state[0] += seconds

    def interval_for_chat(chat_id: int | None) -> float:
        if chat_id is None:
            return private_interval
        if chat_id < 0:
            return group_interval
        return private_interval

    outbox = TelegramOutbox(
        interval_for_chat=interval_for_chat,
        clock=clock,
        sleep=fake_sleep,
    )
    return outbox, sleep_log, clock_state


def _noop_op(chat_id: int | None, priority: int, queued_at: float) -> OutboxOp:
    async def execute() -> str:
        return "ok"

    return OutboxOp(
        execute=execute,
        priority=priority,
        queued_at=queued_at,
        chat_id=chat_id,
        label="test",
    )


@pytest.mark.anyio
async def test_per_chat_pacing_independent() -> None:
    """Edits to different private chats execute back-to-back, not serialised."""
    outbox, sleep_log, clock = _make_outbox(private_rps=1.0)
    results: list[str] = []

    async def execute_100() -> str:
        results.append("chat_100")
        return "ok"

    async def execute_200() -> str:
        results.append("chat_200")
        return "ok"

    op1 = OutboxOp(
        execute=execute_100,
        priority=EDIT_PRIORITY,
        queued_at=0.0,
        chat_id=100,
        label="edit_100",
    )
    op2 = OutboxOp(
        execute=execute_200,
        priority=EDIT_PRIORITY,
        queued_at=0.1,
        chat_id=200,
        label="edit_200",
    )

    await outbox.enqueue(key=("edit", 100, 1), op=op1, wait=False)
    await outbox.enqueue(key=("edit", 200, 1), op=op2, wait=False)

    with anyio.fail_after(2):
        while len(results) < 2:
            await anyio.sleep(0)

    assert len(results) == 2
    assert "chat_100" in results
    assert "chat_200" in results
    # No sleep needed between different chats
    assert sum(sleep_log) == 0.0
    await outbox.close()


@pytest.mark.anyio
async def test_private_not_blocked_by_group_interval() -> None:
    """A private chat (1s interval) is not delayed by a group chat's 3s pacing."""
    outbox, sleep_log, clock = _make_outbox(private_rps=1.0, group_rps=20.0 / 60.0)
    executed: list[int] = []

    async def execute_group() -> str:
        executed.append(-1000)
        return "ok"

    async def execute_private() -> str:
        executed.append(100)
        return "ok"

    group_op = OutboxOp(
        execute=execute_group,
        priority=EDIT_PRIORITY,
        queued_at=0.0,
        chat_id=-1000,
        label="edit_group",
    )
    private_op = OutboxOp(
        execute=execute_private,
        priority=EDIT_PRIORITY,
        queued_at=0.1,
        chat_id=100,
        label="edit_private",
    )

    await outbox.enqueue(key=("edit", -1000, 1), op=group_op, wait=False)
    await outbox.enqueue(key=("edit", 100, 1), op=private_op, wait=False)

    with anyio.fail_after(2):
        while len(executed) < 2:
            await anyio.sleep(0)

    assert len(executed) == 2
    # Private chat should NOT have waited 3s for the group interval
    # Both execute at time 0.0, so no sleeps needed
    assert sum(sleep_log) == 0.0
    await outbox.close()


@pytest.mark.anyio
async def test_retry_after_blocks_all_chats() -> None:
    """A 429 RetryAfter blocks all chats globally via retry_at."""
    outbox, sleep_log, clock = _make_outbox(private_rps=1.0)
    executed: list[int] = []
    first_call = True

    async def execute_chat_100() -> str:
        nonlocal first_call
        if first_call:
            first_call = False
            raise RetryAfter(5.0)
        executed.append(100)
        return "ok"

    op1 = OutboxOp(
        execute=execute_chat_100,
        priority=SEND_PRIORITY,
        queued_at=0.0,
        chat_id=100,
        label="send_100",
    )

    async def execute_chat_200() -> str:
        executed.append(200)
        return "ok"

    op2 = OutboxOp(
        execute=execute_chat_200,
        priority=SEND_PRIORITY,
        queued_at=0.1,
        chat_id=200,
        label="send_200",
    )

    await outbox.enqueue(key=("send", 100), op=op1, wait=False)
    await outbox.enqueue(key=("send", 200), op=op2, wait=False)

    with anyio.fail_after(5):
        while len(executed) < 2:
            await anyio.sleep(0)

    # retry_at should have caused a sleep of 5.0s for all chats
    assert 5.0 in sleep_log
    assert 100 in executed
    assert 200 in executed
    await outbox.close()


@pytest.mark.anyio
async def test_cross_chat_priority() -> None:
    """Send to chat A (priority 0) executes first; edit to chat B executes
    immediately after, not blocked by A's pacing interval."""
    outbox, sleep_log, clock = _make_outbox(private_rps=1.0)
    order: list[str] = []

    async def execute_send_a() -> str:
        order.append("send_A")
        return "ok"

    async def execute_edit_b() -> str:
        order.append("edit_B")
        return "ok"

    send_a = OutboxOp(
        execute=execute_send_a,
        priority=SEND_PRIORITY,
        queued_at=0.0,
        chat_id=100,
        label="send_100",
    )
    edit_b = OutboxOp(
        execute=execute_edit_b,
        priority=EDIT_PRIORITY,
        queued_at=0.0,
        chat_id=200,
        label="edit_200",
    )

    await outbox.enqueue(key=("send", 100, 1), op=send_a, wait=False)
    await outbox.enqueue(key=("edit", 200, 1), op=edit_b, wait=False)

    with anyio.fail_after(2):
        while len(order) < 2:
            await anyio.sleep(0)

    assert order == ["send_A", "edit_B"]
    # No sleep between them: different chats
    assert sum(sleep_log) == 0.0
    await outbox.close()


@pytest.mark.anyio
async def test_same_chat_pacing_preserved() -> None:
    """Two ops to the same chat are still paced by the chat's interval."""
    outbox, sleep_log, clock = _make_outbox(private_rps=1.0)
    executed: list[int] = []

    async def execute_first() -> str:
        executed.append(1)
        return "ok"

    async def execute_second() -> str:
        executed.append(2)
        return "ok"

    op1 = OutboxOp(
        execute=execute_first,
        priority=EDIT_PRIORITY,
        queued_at=0.0,
        chat_id=100,
        label="edit_1",
    )
    op2 = OutboxOp(
        execute=execute_second,
        priority=EDIT_PRIORITY,
        queued_at=0.1,
        chat_id=100,
        label="edit_2",
    )

    await outbox.enqueue(key=("edit", 100, 1), op=op1, wait=False)
    await outbox.enqueue(key=("edit", 100, 2), op=op2, wait=False)

    with anyio.fail_after(5):
        while len(executed) < 2:
            await anyio.sleep(0)

    assert executed == [1, 2]
    # Should have slept 1.0s (private interval) between the two ops
    assert 1.0 in sleep_log
    await outbox.close()


@pytest.mark.anyio
async def test_many_concurrent_chats() -> None:
    """7 group chats with pending edits: total time ~1 interval, not 7x."""
    outbox, sleep_log, clock = _make_outbox(group_rps=20.0 / 60.0)
    chat_ids = [-1001, -1002, -1003, -1004, -1005, -1006, -1007]
    executed: list[int] = []

    for i, cid in enumerate(chat_ids):

        async def execute(chat_id: int = cid) -> str:
            executed.append(chat_id)
            return "ok"

        op = OutboxOp(
            execute=execute,
            priority=EDIT_PRIORITY,
            queued_at=float(i),
            chat_id=cid,
            label=f"edit_{cid}",
        )
        await outbox.enqueue(key=("edit", cid, 1), op=op, wait=False)

    with anyio.fail_after(5):
        while len(executed) < 7:
            await anyio.sleep(0)

    assert len(executed) == 7
    assert set(executed) == set(chat_ids)
    # Total sleep should be 0 — all 7 chats are independent and unblocked
    total_sleep = sum(sleep_log)
    assert total_sleep == 0.0
    # Old behaviour would have been 6 * 3.0s = 18.0s of total sleep
    await outbox.close()


@pytest.mark.anyio
async def test_none_chat_id_independent() -> None:
    """chat_id=None ops don't block numbered chat ops."""
    outbox, sleep_log, clock = _make_outbox(private_rps=1.0)
    executed: list[str] = []

    async def execute_none() -> str:
        executed.append("none")
        return "ok"

    async def execute_chat() -> str:
        executed.append("chat_100")
        return "ok"

    op_none = OutboxOp(
        execute=execute_none,
        priority=SEND_PRIORITY,
        queued_at=0.0,
        chat_id=None,
        label="get_me",
    )
    op_chat = OutboxOp(
        execute=execute_chat,
        priority=SEND_PRIORITY,
        queued_at=0.1,
        chat_id=100,
        label="send_100",
    )

    await outbox.enqueue(key=("get_me", 1), op=op_none, wait=False)
    await outbox.enqueue(key=("send", 100, 1), op=op_chat, wait=False)

    with anyio.fail_after(2):
        while len(executed) < 2:
            await anyio.sleep(0)

    assert len(executed) == 2
    assert "none" in executed
    assert "chat_100" in executed
    # No sleep — different "chats" (None vs 100)
    assert sum(sleep_log) == 0.0
    await outbox.close()
