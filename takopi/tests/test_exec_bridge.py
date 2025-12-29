import asyncio

from takopi.exec_bridge import (
    extract_session_id,
    prepare_telegram,
    resolve_resume_session,
    truncate_for_telegram,
)


def test_extract_session_id_finds_uuid_v7() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    text = f"resume: `{uuid}`"

    assert extract_session_id(text) == uuid


def test_extract_session_id_requires_resume_line() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    text = f"here is a uuid {uuid}"

    assert extract_session_id(text) is None


def test_extract_session_id_uses_last_resume_line() -> None:
    uuid_first = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    uuid_last = "123e4567-e89b-12d3-a456-426614174000"
    text = f"resume: `{uuid_first}`\n\nresume: `{uuid_last}`"

    assert extract_session_id(text) == uuid_last


def test_extract_session_id_ignores_malformed_resume_line() -> None:
    text = "resume: not-a-uuid"

    assert extract_session_id(text) is None


def test_resolve_resume_session_prefers_message_text() -> None:
    uuid_message = "123e4567-e89b-12d3-a456-426614174000"
    uuid_reply = "019b66fc-64c2-7a71-81cd-081c504cfeb2"

    assert (
        resolve_resume_session(
            f"resume: `{uuid_message}`", f"resume: `{uuid_reply}`"
        )
        == uuid_message
    )


def test_resolve_resume_session_uses_reply_when_missing() -> None:
    uuid_reply = "019b66fc-64c2-7a71-81cd-081c504cfeb2"

    assert resolve_resume_session("no resume here", f"resume: `{uuid_reply}`") == uuid_reply


def test_truncate_for_telegram_preserves_resume_line() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    md = ("x" * 10_000) + f"\nresume: `{uuid}`"

    out = truncate_for_telegram(md, 400)

    assert len(out) <= 400
    assert uuid in out
    assert out.rstrip().endswith(f"resume: `{uuid}`")


def test_truncate_for_telegram_keeps_last_non_empty_line() -> None:
    md = "intro\n\n" + ("x" * 500) + "\nlast line"

    out = truncate_for_telegram(md, 120)

    assert len(out) <= 120
    assert out.rstrip().endswith("last line")


def test_prepare_telegram_drops_entities_on_truncate() -> None:
    md = ("**bold** " * 200).strip()

    rendered, entities = prepare_telegram(md, limit=40)

    assert len(rendered) <= 40
    assert entities is None


class _FakeBot:
    def __init__(self) -> None:
        self._next_id = 1
        self.send_calls: list[dict] = []
        self.edit_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        self.send_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "disable_notification": disable_notification,
                "entities": entities,
                "parse_mode": parse_mode,
            }
        )
        msg_id = self._next_id
        self._next_id += 1
        return {"message_id": msg_id}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        self.edit_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "entities": entities,
                "parse_mode": parse_mode,
            }
        )
        return {"message_id": message_id}

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.delete_calls.append({"chat_id": chat_id, "message_id": message_id})
        return True


class _FakeRunner:
    def __init__(self, *, answer: str, saw_agent_message: bool = True) -> None:
        self._answer = answer
        self._saw_agent_message = saw_agent_message

    async def run_serialized(self, *_args, **_kwargs) -> tuple[str, str, bool]:
        return ("019b66fc-64c2-7a71-81cd-081c504cfeb2", self._answer, self._saw_agent_message)


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def set(self, value: float) -> None:
        self._now = value


class _FakeRunnerWithEvents:
    def __init__(
        self,
        *,
        events: list[dict],
        times: list[float],
        clock: _FakeClock,
        answer: str = "ok",
        session_id: str = "019b66fc-64c2-7a71-81cd-081c504cfeb2",
    ) -> None:
        self._events = events
        self._times = times
        self._clock = clock
        self._answer = answer
        self._session_id = session_id

    async def run_serialized(self, *_args, **kwargs) -> tuple[str, str, bool]:
        on_event = kwargs.get("on_event")
        if on_event is not None:
            for when, event in zip(self._times, self._events, strict=False):
                self._clock.set(when)
                await on_event(event)
                await asyncio.sleep(0)
        return (self._session_id, self._answer, True)


def test_final_notify_sends_loud_final_message() -> None:
    from takopi.exec_bridge import BridgeConfig, _handle_message

    bot = _FakeBot()
    runner = _FakeRunner(answer="ok")
    cfg = BridgeConfig(
        bot=bot,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        chat_id=123,
        final_notify=True,
        startup_msg="",
        max_concurrency=1,
    )

    asyncio.run(
        _handle_message(
            cfg,
            chat_id=123,
            user_msg_id=10,
            text="hi",
            resume_session=None,
        )
    )

    assert len(bot.send_calls) == 2
    assert bot.send_calls[0]["disable_notification"] is True
    assert bot.send_calls[1]["disable_notification"] is False


def test_new_final_message_forces_notification_when_too_long_to_edit() -> None:
    from takopi.exec_bridge import BridgeConfig, _handle_message

    bot = _FakeBot()
    runner = _FakeRunner(answer="x" * 10_000)
    cfg = BridgeConfig(
        bot=bot,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        chat_id=123,
        final_notify=False,
        startup_msg="",
        max_concurrency=1,
    )

    asyncio.run(
        _handle_message(
            cfg,
            chat_id=123,
            user_msg_id=10,
            text="hi",
            resume_session=None,
        )
    )

    assert len(bot.send_calls) == 2
    assert bot.send_calls[0]["disable_notification"] is True
    assert bot.send_calls[1]["disable_notification"] is False


def test_progress_edits_are_rate_limited() -> None:
    from takopi.exec_bridge import BridgeConfig, _handle_message

    bot = _FakeBot()
    clock = _FakeClock()
    events = [
        {
            "type": "item.started",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "echo 1",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "echo 1",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "echo 2",
                "status": "in_progress",
            },
        },
    ]
    runner = _FakeRunnerWithEvents(
        events=events,
        times=[0.2, 0.4, 1.2],
        clock=clock,
    )
    cfg = BridgeConfig(
        bot=bot,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        chat_id=123,
        final_notify=True,
        startup_msg="",
        max_concurrency=1,
    )

    asyncio.run(
        _handle_message(
            cfg,
            chat_id=123,
            user_msg_id=10,
            text="hi",
            resume_session=None,
            clock=clock,
            progress_edit_every=1.0,
        )
    )

    assert len(bot.edit_calls) == 1


def test_bridge_flow_sends_progress_edits_and_final_resume() -> None:
    from takopi.exec_bridge import BridgeConfig, _handle_message

    bot = _FakeBot()
    clock = _FakeClock()
    events = [
        {
            "type": "item.started",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "echo ok",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "echo ok",
                "exit_code": 0,
                "status": "completed",
            },
        },
    ]
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    runner = _FakeRunnerWithEvents(
        events=events,
        times=[0.0, 2.1],
        clock=clock,
        answer="done",
        session_id=session_id,
    )
    cfg = BridgeConfig(
        bot=bot,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        chat_id=123,
        final_notify=True,
        startup_msg="",
        max_concurrency=1,
    )

    asyncio.run(
        _handle_message(
            cfg,
            chat_id=123,
            user_msg_id=42,
            text="do it",
            resume_session=None,
            clock=clock,
            progress_edit_every=1.0,
        )
    )

    assert bot.send_calls[0]["reply_to_message_id"] == 42
    assert "working" in bot.send_calls[0]["text"]
    assert len(bot.edit_calls) >= 1
    assert session_id in bot.send_calls[-1]["text"]
    assert "resume:" in bot.send_calls[-1]["text"].lower()
    assert len(bot.delete_calls) == 1
