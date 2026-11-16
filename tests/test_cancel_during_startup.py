"""#1082: ``/cancel`` while a run is still starting.

A run used to reach ``running_tasks`` only once its progress message had
been sent (or the queued placeholder edited to "starting") — a send that
waits behind group-chat pacing for seconds. A ``/cancel`` in that window
answered "nothing running" and the run then went ahead. The run is now
visible to ``/cancel`` and ``/new`` from before that send, and a run
cancelled before it starts never spawns the engine. The scheduler likewise
keeps a follow-up that it has taken off the queue — but that is still
waiting for the session's previous run — cancellable from its placeholder.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport as ReplyTransport
from tests.telegram_fakes import make_cfg
from untether.markdown import MarkdownPresenter
from untether.model import ResumeToken
from untether.runner_bridge import (
    ExecBridgeConfig,
    IncomingMessage,
    RunningTask,
    handle_message,
)
from untether.runners.mock import ErrorReturn, Return, ScriptRunner
from untether.scheduler import ThreadJob, ThreadScheduler
from untether.telegram.commands.cancel import handle_cancel
from untether.telegram.commands.topics import _cancel_chat_tasks
from untether.telegram.types import TelegramIncomingMessage
from untether.transport import MessageRef, RenderedMessage, SendOptions

pytestmark = pytest.mark.anyio

CHAT = 123
USER_MSG = 10
PLACEHOLDER = MessageRef(channel_id=CHAT, message_id=500)


class _PacedTransport:
    """A transport whose first progress send / placeholder edit waits for
    ``release`` — the group-chat outbox pacing seen live (~6 s)."""

    def __init__(self, *, fail_edit: bool = False, raise_send: bool = False) -> None:
        self.release = anyio.Event()
        self.waiting = anyio.Event()
        self._held = False
        self._next_id = 900
        self._fail_edit = fail_edit
        self._raise_send = raise_send
        self.send_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []

    async def _pace(self) -> None:
        if not self._held:
            self._held = True
            self.waiting.set()
            await self.release.wait()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef:
        await self._pace()
        if self._raise_send:
            raise RuntimeError("transport down")
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.send_calls.append({"ref": ref, "message": message, "options": options})
        return ref

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef | None:
        await self._pace()
        self.edit_calls.append({"ref": ref, "message": message})
        if self._fail_edit:
            self._fail_edit = False
            return None
        return ref

    async def delete(self, *, ref: MessageRef) -> bool:
        return True

    async def close(self) -> None:
        return None

    def texts(self) -> list[str]:
        return [c["message"].text for c in self.send_calls + self.edit_calls]


def _exec_cfg(transport: _PacedTransport) -> ExecBridgeConfig:
    return ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )


def _cancel_msg(
    *,
    chat_id: int = CHAT,
    reply_to: int | None = None,
    thread_id: int | None = None,
    forum: bool = False,
) -> TelegramIncomingMessage:
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=chat_id,
        message_id=77,
        text="/cancel",
        reply_to_message_id=reply_to,
        reply_to_text=None,
        sender_id=123,
        thread_id=thread_id,
        is_topic_message=True if thread_id is not None else None,
        chat_type="supergroup" if forum else None,
        is_forum=True if forum else None,
    )


async def _start(
    tg: Any,
    transport: _PacedTransport,
    runner: ScriptRunner,
    running_tasks: dict,
    *,
    progress_ref: MessageRef | None = None,
    resume_token: ResumeToken | None = None,
    chat_id: int = CHAT,
    thread_id: int | None = None,
) -> None:
    async def run() -> None:
        await handle_message(
            _exec_cfg(transport),
            runner=runner,
            incoming=IncomingMessage(
                channel_id=chat_id,
                message_id=USER_MSG,
                text="do something",
                thread_id=thread_id,
            ),
            resume_token=resume_token,
            running_tasks=running_tasks,
            progress_ref=progress_ref,
        )

    tg.start_soon(run)
    with anyio.fail_after(2):
        await transport.waiting.wait()


async def test_1082_cancel_fallback_finds_a_resumed_run_still_starting() -> None:
    """The live repro: the queued card is being edited to "starting" behind
    pacing; a plain /cancel cancels the run instead of "nothing running",
    and the engine is never started."""
    transport = _PacedTransport()
    replies = ReplyTransport()
    runner = ScriptRunner([Return(answer="should not run")], engine="codex")
    running_tasks: dict = {}
    resume = ResumeToken(engine="codex", value="sess-1082")

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            await _start(
                tg,
                transport,
                runner,
                running_tasks,
                progress_ref=PLACEHOLDER,
                resume_token=resume,
            )
            # Visible under the placeholder the user can see.
            assert list(running_tasks) == [PLACEHOLDER]
            await handle_cancel(make_cfg(replies), _cancel_msg(), running_tasks)
            transport.release.set()

    assert replies.send_calls == []  # no "nothing running in this chat."
    assert runner.calls == []  # never spawned
    assert running_tasks == {}
    final = transport.edit_calls[-1]
    assert final["ref"] == PLACEHOLDER
    assert "cancelled" in final["message"].text
    assert "sess-1082" in final["message"].text  # the session is still named
    events = [e["event"] for e in logs]
    assert "cancel.requested" in events
    assert "handle.cancelled_before_start" in events
    assert "handle.cancelled" in events
    assert "session.summary.no_events" not in events


async def test_1082_cancel_reply_to_starting_placeholder() -> None:
    """Replying /cancel to the queued/starting card finds the run too."""
    transport = _PacedTransport()
    replies = ReplyTransport()
    runner = ScriptRunner([Return(answer="should not run")], engine="claude")
    running_tasks: dict = {}

    async with anyio.create_task_group() as tg:
        await _start(
            tg,
            transport,
            runner,
            running_tasks,
            progress_ref=PLACEHOLDER,
            resume_token=ResumeToken(engine="claude", value="s"),
        )
        await handle_cancel(
            make_cfg(replies),
            _cancel_msg(reply_to=PLACEHOLDER.message_id),
            running_tasks,
        )
        transport.release.set()

    assert replies.send_calls == []
    assert runner.calls == []
    assert "cancelled" in transport.edit_calls[-1]["message"].text


async def test_1082_new_run_without_placeholder_is_cancellable() -> None:
    """A fresh run has no placeholder: it is keyed by the user's message
    (with its topic) until its progress message exists."""
    transport = _PacedTransport()
    replies = ReplyTransport()
    runner = ScriptRunner([Return(answer="should not run")], engine="opencode")
    running_tasks: dict = {}

    async with anyio.create_task_group() as tg:
        await _start(tg, transport, runner, running_tasks)
        ((ref, task),) = running_tasks.items()
        assert (ref.channel_id, ref.message_id) == (CHAT, USER_MSG)
        assert task.edits is None
        await handle_cancel(make_cfg(replies), _cancel_msg(), running_tasks)
        transport.release.set()

    assert replies.send_calls == []
    assert runner.calls == []
    assert running_tasks == {}
    # The progress message it was sending becomes the cancelled card.
    (progress,) = transport.send_calls[:1]
    assert transport.edit_calls[-1]["ref"] == progress["ref"]
    assert "cancelled" in transport.edit_calls[-1]["message"].text


async def test_1082_new_during_startup_cancels_the_run() -> None:
    """/new's cancel sweep sees the starting run too."""
    transport = _PacedTransport()
    runner = ScriptRunner([Return(answer="should not run")], engine="pi")
    running_tasks: dict = {}

    async with anyio.create_task_group() as tg:
        await _start(tg, transport, runner, running_tasks)
        assert _cancel_chat_tasks(CHAT, running_tasks) == 1
        transport.release.set()

    assert runner.calls == []
    assert running_tasks == {}


async def test_1082_cancel_in_another_topic_leaves_starting_run_alone() -> None:
    """#826 scoping holds for a starting run: /cancel in topic 10 never
    touches topic 6's run, which then starts normally."""
    forum = -1001082
    transport = _PacedTransport()
    replies = ReplyTransport()
    runner = ScriptRunner([Return(answer="done")], engine="codex")
    running_tasks: dict = {}

    async with anyio.create_task_group() as tg:
        await _start(tg, transport, runner, running_tasks, chat_id=forum, thread_id=6)
        ((ref, task),) = running_tasks.items()
        assert task.thread_id == 6
        assert ref.thread_id == 6
        await handle_cancel(
            make_cfg(replies),
            _cancel_msg(chat_id=forum, thread_id=10, forum=True),
            running_tasks,
        )
        assert not task.cancel_requested.is_set()
        transport.release.set()

    assert replies.send_calls[-1]["message"].text == "nothing running in this topic."
    assert len(runner.calls) == 1
    assert "done" in transport.texts()[-1]


async def test_1082_uncancelled_run_moves_to_its_progress_message() -> None:
    """Not cancelled: the run starts as before, re-keyed from the
    placeholder to the progress message it actually got (here the edit
    failed and a new message was sent), and the start-up key is gone."""
    transport = _PacedTransport(fail_edit=True)
    hold = anyio.Event()
    from untether.runners.mock import Wait

    runner = ScriptRunner([Wait(hold), Return(answer="done")], engine="codex")
    running_tasks: dict = {}
    seen: list[MessageRef] = []

    async with anyio.create_task_group() as tg:
        await _start(tg, transport, runner, running_tasks, progress_ref=PLACEHOLDER)
        starting = running_tasks[PLACEHOLDER]
        transport.release.set()
        with anyio.fail_after(2):
            await starting.resume_ready.wait()
        seen = list(running_tasks)
        assert running_tasks[seen[0]] is starting
        assert starting.edits is not None
        hold.set()

    (sent,) = transport.send_calls[:1]
    assert seen == [sent["ref"]]
    assert PLACEHOLDER not in seen
    assert len(runner.calls) == 1
    assert running_tasks == {}


async def test_1082_failed_progress_send_does_not_leak_the_starting_entry() -> None:
    transport = _PacedTransport(raise_send=True)
    runner = ScriptRunner([Return(answer="x")], engine="codex")
    running_tasks: dict = {}

    raised: list[BaseException] = []
    starting: list[RunningTask] = []
    try:
        async with anyio.create_task_group() as tg:
            await _start(tg, transport, runner, running_tasks)
            starting.extend(running_tasks.values())
            transport.release.set()
    except* RuntimeError as eg:
        raised.extend(eg.exceptions)

    assert [str(e) for e in raised] == ["transport down"]
    assert running_tasks == {}
    # A reply routed to it while it was starting stops waiting.
    (task,) = starting
    assert task.done.is_set()
    assert runner.calls == []


async def test_1082_cancel_while_progress_send_fails_still_stops_the_run() -> None:
    """No progress message at all (the send failed): the cancel asked for
    during start-up is still honoured and the engine never starts."""

    class _NoRefTransport(_PacedTransport):
        async def send(self, **kwargs: Any) -> MessageRef | None:  # type: ignore[override]
            await self._pace()
            self.send_calls.append(kwargs)
            return None

    transport = _NoRefTransport()
    runner = ScriptRunner([Return(answer="x")], engine="codex")
    running_tasks: dict = {}

    async with anyio.create_task_group() as tg:
        await _start(tg, transport, runner, running_tasks)
        assert _cancel_chat_tasks(CHAT, running_tasks) == 1
        transport.release.set()

    assert runner.calls == []
    assert running_tasks == {}


# --- scheduler: a follow-up held for the session's previous run ---------


async def test_1082_scheduler_held_job_is_cancellable_from_its_placeholder() -> None:
    """The worker takes a follow-up off the queue and waits for the
    session's previous run. Replying /cancel to its queued card used to
    say "nothing is currently running for that message"."""
    ran: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        ran.append(job)

    async with anyio.create_task_group() as tg:
        scheduler = ThreadScheduler(task_group=tg, run_job=run_job)
        token = ResumeToken(engine="codex", value="busy")
        previous_done = anyio.Event()
        await scheduler.note_thread_known(token, previous_done)
        await scheduler.enqueue_resume(
            CHAT, USER_MSG, "follow-up", token, progress_ref=PLACEHOLDER
        )
        for _ in range(20):
            await anyio.lowlevel.checkpoint()
        # Held by the worker, still listed and cancellable.
        assert [j.user_msg_id for j in scheduler.queued_for_chat(CHAT)] == [USER_MSG]
        job = await scheduler.cancel_queued(CHAT, PLACEHOLDER.message_id)
        assert job is not None and job.user_msg_id == USER_MSG
        assert scheduler.queued_for_chat(CHAT) == []
        assert await scheduler.cancel_queued(CHAT, PLACEHOLDER.message_id) is None
        previous_done.set()
        for _ in range(20):
            await anyio.lowlevel.checkpoint()

    assert ran == []


async def test_1082_scheduler_held_job_runs_when_not_cancelled() -> None:
    ran: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        ran.append(job)

    async with anyio.create_task_group() as tg:
        scheduler = ThreadScheduler(task_group=tg, run_job=run_job)
        token = ResumeToken(engine="codex", value="busy")
        previous_done = anyio.Event()
        await scheduler.note_thread_known(token, previous_done)
        await scheduler.enqueue_resume(
            CHAT, USER_MSG, "follow-up", token, progress_ref=PLACEHOLDER
        )
        for _ in range(20):
            await anyio.lowlevel.checkpoint()
        previous_done.set()
        with anyio.fail_after(2):
            while not ran:
                await anyio.lowlevel.checkpoint()
        # Started: no longer listed as queued.
        assert scheduler.queued_for_chat(CHAT) == []

    assert [j.user_msg_id for j in ran] == [USER_MSG]


async def test_1082_handle_cancel_reply_to_held_job_edits_cancelled() -> None:
    replies = ReplyTransport()
    ran: list[ThreadJob] = []

    async def run_job(job: ThreadJob) -> None:
        ran.append(job)

    async with anyio.create_task_group() as tg:
        scheduler = ThreadScheduler(task_group=tg, run_job=run_job)
        token = ResumeToken(engine="codex", value="busy")
        previous_done = anyio.Event()
        await scheduler.note_thread_known(token, previous_done)
        await scheduler.enqueue_resume(
            CHAT, USER_MSG, "follow-up", token, progress_ref=PLACEHOLDER
        )
        for _ in range(20):
            await anyio.lowlevel.checkpoint()
        await handle_cancel(
            make_cfg(replies),
            _cancel_msg(reply_to=PLACEHOLDER.message_id),
            {},
            scheduler,
        )
        previous_done.set()
        for _ in range(20):
            await anyio.lowlevel.checkpoint()

    assert ran == []
    assert not [c for c in replies.send_calls if "nothing" in c["message"].text]
    assert "cancelled" in replies.edit_calls[-1]["message"].text


# --- #1082 (also seen): invalid-argument refusals log at INFO ------------


async def test_1082_invalid_argument_refusal_logs_no_events_at_info() -> None:
    """A refused bad session/model id (``invalid_argument``) is the user's
    input, not a fault: still an error card, but INFO like #1047's plain
    refusals, with the reason."""
    transport = ReplyTransport()
    runner = ScriptRunner(
        [
            ErrorReturn(
                error="🛑 That session id isn't valid",
                usage={"prespawn_blocked": "invalid_argument"},
            )
        ],
        engine="codex",
    )
    with capture_logs() as logs:
        await handle_message(
            ExecBridgeConfig(
                transport=transport, presenter=MarkdownPresenter(), final_notify=True
            ),
            runner=runner,
            incoming=IncomingMessage(channel_id=CHAT, message_id=USER_MSG, text="hi"),
            resume_token=None,
        )
    assert transport.send_calls[-1]["message"].text.startswith("error · codex")
    (summary,) = [r for r in logs if r.get("event") == "session.summary.no_events"]
    assert summary["log_level"] == "info"
    assert summary["prespawn_blocked"] == "invalid_argument"


@pytest.mark.parametrize(
    ("resume_token", "expected_line"),
    [
        (ResumeToken(engine="codex", value="sess-1084"), "codex resume sess-1084"),
        (ResumeToken(engine="codex", value="", is_continue=True), None),
    ],
)
async def test_1084_cancel_after_start_before_events_keeps_resume_line(
    resume_token: ResumeToken, expected_line: str | None
) -> None:
    """#1084: a resumed run cancelled after the engine started but before
    its first event (``handle.cancelled resume=None``) dropped the resume
    line from the cancelled card. It names the session it was started
    with — except ``/continue``, whose token names no session."""
    from untether.runners.mock import Wait

    transport = ReplyTransport()
    never = anyio.Event()
    runner = ScriptRunner([Wait(never)], engine="codex", emit_session_start=False)
    running_tasks: dict = {}

    async def run() -> None:
        await handle_message(
            ExecBridgeConfig(
                transport=transport, presenter=MarkdownPresenter(), final_notify=True
            ),
            runner=runner,
            incoming=IncomingMessage(channel_id=CHAT, message_id=USER_MSG, text="go"),
            resume_token=resume_token,
            running_tasks=running_tasks,
        )

    with capture_logs() as logs:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            with anyio.fail_after(2):
                while not runner.calls:
                    await anyio.sleep(0.01)
            (task,) = running_tasks.values()
            task.cancel_requested.set()

    final = transport.edit_calls[-1]["message"].text
    assert "cancelled" in final
    (cancelled,) = [e for e in logs if e["event"] == "handle.cancelled"]
    assert "handle.cancelled_before_start" not in [e["event"] for e in logs]
    if expected_line is None:
        assert "resume" not in final
        assert cancelled["resume"] is None
    else:
        assert expected_line in final
        assert cancelled["resume"] == resume_token.value


async def test_1084_continue_cancelled_before_start_has_no_empty_resume_line() -> None:
    """The #1082 before-start path keeps the token it was given; a
    ``/continue`` token names no session, so no (empty) resume line."""
    transport = _PacedTransport()
    replies = ReplyTransport()
    runner = ScriptRunner([Return(answer="should not run")], engine="codex")
    running_tasks: dict = {}

    async with anyio.create_task_group() as tg:
        await _start(
            tg,
            transport,
            runner,
            running_tasks,
            progress_ref=PLACEHOLDER,
            resume_token=ResumeToken(engine="codex", value="", is_continue=True),
        )
        await handle_cancel(make_cfg(replies), _cancel_msg(), running_tasks)
        transport.release.set()

    assert runner.calls == []
    final = transport.edit_calls[-1]["message"].text
    assert "cancelled" in final
    assert "codex resume" not in final


def test_1082_running_task_defaults_have_no_edits() -> None:
    """A starting run carries no ``edits`` yet; consumers read it with
    ``getattr`` (drain PID scan, idle checks)."""
    from untether.runner_bridge import running_task_is_idle_after_result

    task = RunningTask(thread_id=3)
    assert task.edits is None
    assert running_task_is_idle_after_result(task) is False
