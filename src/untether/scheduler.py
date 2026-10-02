from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import anyio

from .context import RunContext
from .logging import get_logger
from .model import ResumeToken
from .transport import ChannelId, MessageId, MessageRef, ThreadId

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ThreadJob:
    chat_id: ChannelId
    user_msg_id: MessageId
    text: str
    resume_token: ResumeToken
    context: RunContext | None = None
    thread_id: ThreadId | None = None
    session_key: tuple[int, int | None] | None = None
    progress_ref: MessageRef | None = None
    image_paths: tuple[str, ...] = ()


RunJob = Callable[[ThreadJob], Awaitable[None]]
# #776: returns True when the job was delivered into a live engine process
# (so it must not also be run via --resume).
InjectJob = Callable[[ThreadJob], Awaitable[bool]]


class TaskGroup(Protocol):
    def start_soon(
        self, func: Callable[..., Awaitable[object]], *args: Any
    ) -> None: ...


class ThreadScheduler:
    def __init__(
        self,
        *,
        task_group: TaskGroup,
        run_job: RunJob,
        inject_job: InjectJob | None = None,
    ) -> None:
        self._task_group = task_group
        self._run_job = run_job
        self._inject_job = inject_job
        self._live_pump_interval_s = 0.25
        self._lock = anyio.Lock()
        self._pending_by_thread: dict[str, deque[ThreadJob]] = {}
        self._queued_by_progress: dict[tuple[ChannelId, MessageId], ThreadJob] = {}
        self._active_threads: set[str] = set()
        self._busy_until: dict[str, anyio.Event] = {}

    @staticmethod
    def thread_key(token: ResumeToken) -> str:
        return f"{token.engine}:{token.value}"

    async def note_thread_known(self, token: ResumeToken, done: anyio.Event) -> None:
        key = self.thread_key(token)
        async with self._lock:
            current = self._busy_until.get(key)
            if current is None or current.is_set():
                self._busy_until[key] = done
        self._task_group.start_soon(self._clear_busy, key, done)

    async def enqueue(self, job: ThreadJob) -> None:
        key = self.thread_key(job.resume_token)
        async with self._lock:
            queue = self._pending_by_thread.get(key)
            if queue is None:
                queue = deque()
                self._pending_by_thread[key] = queue
            queue.append(job)
            if job.progress_ref is not None:
                progress_key = (job.chat_id, job.progress_ref.message_id)
                self._queued_by_progress[progress_key] = job
            if key in self._active_threads:
                return
            self._active_threads.add(key)
        self._task_group.start_soon(self._thread_worker, key)

    async def enqueue_resume(
        self,
        chat_id: ChannelId,
        user_msg_id: MessageId,
        text: str,
        resume_token: ResumeToken,
        context: RunContext | None = None,
        thread_id: ThreadId | None = None,
        session_key: tuple[int, int | None] | None = None,
        progress_ref: MessageRef | None = None,
        image_paths: tuple[str, ...] = (),
    ) -> None:
        await self.enqueue(
            ThreadJob(
                chat_id=chat_id,
                user_msg_id=user_msg_id,
                text=text,
                resume_token=resume_token,
                context=context,
                thread_id=thread_id,
                session_key=session_key,
                progress_ref=progress_ref,
                image_paths=image_paths,
            )
        )

    def queued_for_chat(self, chat_id: ChannelId) -> list[ThreadJob]:
        """Return queued jobs for a specific chat (sync, for cancel fallback)."""
        return [
            job
            for job in self._queued_by_progress.values()
            if job.chat_id == chat_id and job.progress_ref is not None
        ]

    async def cancel_queued(
        self, chat_id: ChannelId, progress_msg_id: MessageId
    ) -> ThreadJob | None:
        progress_key = (chat_id, progress_msg_id)
        async with self._lock:
            job = self._queued_by_progress.pop(progress_key, None)
            if job is None:
                return None
            thread_key = self.thread_key(job.resume_token)
            queue = self._pending_by_thread.get(thread_key)
            if queue is None:
                return None
            try:
                queue.remove(job)
            except ValueError:
                return None
            if not queue:
                self._pending_by_thread.pop(thread_key, None)
            return job

    async def _clear_busy(self, key: str, done: anyio.Event) -> None:
        await done.wait()
        async with self._lock:
            if self._busy_until.get(key) is done:
                self._busy_until.pop(key, None)

    async def _run_job_with_live_pump(self, key: str, job: ThreadJob) -> None:
        """Run ``job``; meanwhile offer the thread's next queued jobs to the
        injector (#776).

        A live engine run lasts until its session closes, so awaiting it
        alone would hold every later follow-up for the same session behind it
        — exactly the jobs that should go *into* that live session.
        """
        if self._inject_job is None:
            await self._run_job(job)
            return
        async with anyio.create_task_group() as tg:
            finished = anyio.Event()

            async def run() -> None:
                try:
                    await self._run_job(job)
                finally:
                    finished.set()

            tg.start_soon(run)
            tg.start_soon(self._live_pump, key, finished)

    async def _live_pump(self, key: str, finished: anyio.Event) -> None:
        inject = self._inject_job
        assert inject is not None
        while not finished.is_set():
            with anyio.move_on_after(self._live_pump_interval_s):
                await finished.wait()
            if finished.is_set():
                return
            async with self._lock:
                queue = self._pending_by_thread.get(key)
                if not queue:
                    continue
                job = queue.popleft()
                if job.progress_ref is not None:
                    self._queued_by_progress.pop(
                        (job.chat_id, job.progress_ref.message_id), None
                    )
            # Shielded: once a job is taken off the queue it is either written
            # into the live session or put back — never lost, never run twice.
            with anyio.CancelScope(shield=True):
                try:
                    injected = await inject(job)
                except Exception:  # noqa: BLE001
                    logger.warning("scheduler.inject_failed", key=key, exc_info=True)
                    injected = False
                if not injected:
                    async with self._lock:
                        self._pending_by_thread.setdefault(key, deque()).appendleft(job)
                        if job.progress_ref is not None:
                            self._queued_by_progress[
                                (job.chat_id, job.progress_ref.message_id)
                            ] = job
                    # Not injectable right now (not live yet / closing):
                    # keep FIFO order and look again next tick.

    async def _thread_worker(self, key: str) -> None:
        try:
            while True:
                async with self._lock:
                    done = self._busy_until.get(key)
                    queue = self._pending_by_thread.get(key)
                    if not queue:
                        self._pending_by_thread.pop(key, None)
                        self._active_threads.discard(key)
                        return
                    job = queue.popleft()
                    if job.progress_ref is not None:
                        progress_key = (job.chat_id, job.progress_ref.message_id)
                        self._queued_by_progress.pop(progress_key, None)

                # #776: a follow-up for a session whose process is still
                # live goes into that process (queued until its turn ends)
                # instead of waiting for it to exit and resuming.
                if self._inject_job is not None:
                    try:
                        injected = await self._inject_job(job)
                    except Exception:  # noqa: BLE001 — fall back to resume
                        logger.warning(
                            "scheduler.inject_failed", key=key, exc_info=True
                        )
                        injected = False
                    if injected:
                        continue

                if done is not None and not done.is_set():
                    await done.wait()

                try:
                    await self._run_job_with_live_pump(key, job)
                except Exception as exc:
                    logger.exception(
                        "scheduler.job_failed",
                        key=key,
                        tag=job.resume_token.engine,
                        chat_id=job.chat_id,
                        user_msg_id=job.user_msg_id,
                        error=str(exc),
                        error_type=exc.__class__.__name__,
                    )
        finally:
            async with self._lock:
                self._active_threads.discard(key)
