from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

from ..logging import get_logger
from .client_api import RetryAfter

logger = get_logger(__name__)

SEND_PRIORITY = 0
DELETE_PRIORITY = 1
EDIT_PRIORITY = 2


@dataclass(slots=True)
class OutboxOp:
    execute: Callable[[], Awaitable[Any]]
    priority: int
    queued_at: float
    chat_id: int | None
    label: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None

    def set_result(self, result: Any) -> None:
        if self.done.is_set():
            return
        self.result = result
        self.done.set()


class TelegramOutbox:
    def __init__(
        self,
        *,
        interval_for_chat: Callable[[int | None], float],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        on_error: Callable[[OutboxOp, Exception], None] | None = None,
        on_outbox_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._interval_for_chat = interval_for_chat
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error
        self._on_outbox_error = on_outbox_error
        self._pending: dict[Hashable, OutboxOp] = {}
        self._cond = anyio.Condition()
        self._start_lock = anyio.Lock()
        self._closed = False
        self._tg: TaskGroup | None = None
        self._next_at: dict[int | None, float] = {}
        self.retry_at = 0.0

    async def ensure_worker(self) -> None:
        async with self._start_lock:
            if self._tg is not None or self._closed:
                return
            self._tg = await anyio.create_task_group().__aenter__()
            self._tg.start_soon(self.run)

    async def enqueue(self, *, key: Hashable, op: OutboxOp, wait: bool = True) -> Any:
        await self.ensure_worker()
        async with self._cond:
            if self._closed:
                logger.warning(
                    "outbox.enqueue.closed", label=op.label, chat_id=op.chat_id
                )
                op.set_result(None)
                return op.result
            previous = self._pending.get(key)
            if previous is not None:
                logger.debug(
                    "outbox.enqueue.superseded",
                    label=op.label,
                    chat_id=op.chat_id,
                    prev_label=previous.label,
                )
                op.queued_at = previous.queued_at
                previous.set_result(None)
            self._pending[key] = op
            self._cond.notify()
        if not wait:
            return None
        await op.done.wait()
        return op.result

    async def drop_pending(self, *, key: Hashable) -> None:
        async with self._cond:
            pending = self._pending.pop(key, None)
            if pending is not None:
                pending.set_result(None)
            self._cond.notify()

    async def close(self) -> None:
        pending_count = len(self._pending)
        logger.info("outbox.closing", pending_count=pending_count)
        async with self._cond:
            self._closed = True
            self.fail_pending()
            self._cond.notify_all()
        if self._tg is not None:
            await self._tg.__aexit__(None, None, None)
            self._tg = None
        logger.info("outbox.closed")

    def fail_pending(self) -> None:
        count = len(self._pending)
        if count > 0:
            logger.warning("outbox.fail_pending", count=count)
        for pending in list(self._pending.values()):
            pending.set_result(None)
        self._pending.clear()

    def _pick_ready(self, now: float) -> tuple[Hashable, OutboxOp] | None:
        """Pick the highest-priority, oldest-queued op whose chat is not blocked."""
        best: tuple[Hashable, OutboxOp] | None = None
        best_key: tuple[int, float] | None = None
        for key, op in self._pending.items():
            if self._next_at.get(op.chat_id, 0.0) > now:
                continue
            candidate = (op.priority, op.queued_at)
            if best_key is None or candidate < best_key:
                best = (key, op)
                best_key = candidate
        return best

    def _earliest_unblock(self) -> float | None:
        """Earliest time any pending op's chat becomes ready."""
        earliest: float | None = None
        for op in self._pending.values():
            t = self._next_at.get(op.chat_id, 0.0)
            if earliest is None or t < earliest:
                earliest = t
        return earliest

    async def execute_op(self, op: OutboxOp) -> Any:
        try:
            return await op.execute()
        except Exception as exc:
            if isinstance(exc, RetryAfter):
                logger.info(
                    "outbox.op.retry_after",
                    label=op.label,
                    chat_id=op.chat_id,
                    retry_after=exc.retry_after,
                )
                raise
            logger.error(
                "outbox.op.failed",
                label=op.label,
                chat_id=op.chat_id,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            if self._on_error is not None:
                self._on_error(op, exc)
            return None

    async def sleep_until(self, deadline: float) -> None:
        delay = deadline - self._clock()
        if delay > 0:
            await self._sleep(delay)

    async def run(self) -> None:
        logger.info("outbox.worker.started")
        cancel_exc = anyio.get_cancelled_exc_class()
        try:
            while True:
                async with self._cond:
                    while not self._pending and not self._closed:
                        await self._cond.wait()
                    if self._closed and not self._pending:
                        return
                if self._clock() < self.retry_at:
                    await self.sleep_until(self.retry_at)
                    continue
                async with self._cond:
                    if self._closed and not self._pending:
                        return
                    now = self._clock()
                    picked = self._pick_ready(now)
                    if picked is not None:
                        key, op = picked
                        self._pending.pop(key, None)
                if picked is None:
                    earliest = self._earliest_unblock()
                    if earliest is not None and earliest > self._clock():
                        await self.sleep_until(earliest)
                    continue
                started_at = self._clock()
                try:
                    result = await self.execute_op(op)
                except RetryAfter as exc:
                    self.retry_at = max(self.retry_at, self._clock() + exc.retry_after)
                    async with self._cond:
                        if self._closed:
                            logger.warning(
                                "outbox.retry_after.closed",
                                label=op.label,
                                chat_id=op.chat_id,
                            )
                            op.set_result(None)
                        elif key not in self._pending:
                            self._pending[key] = op
                            self._cond.notify()
                        else:
                            logger.debug(
                                "outbox.retry_after.superseded",
                                label=op.label,
                                chat_id=op.chat_id,
                            )
                            op.set_result(None)
                    continue
                logger.debug(
                    "outbox.op.completed",
                    label=op.label,
                    chat_id=op.chat_id,
                    elapsed_ms=round((self._clock() - op.queued_at) * 1000, 1),
                )
                self._next_at[op.chat_id] = started_at + self._interval_for_chat(
                    op.chat_id
                )
                op.set_result(result)
        except cancel_exc:
            logger.debug("outbox.worker.cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "outbox.worker.fatal",
                error=str(exc),
                error_type=exc.__class__.__name__,
                pending_count=len(self._pending),
            )
            async with self._cond:
                self._closed = True
                self.fail_pending()
                self._cond.notify_all()
            if self._on_outbox_error is not None:
                self._on_outbox_error(exc)
            return


if TYPE_CHECKING:
    from anyio.abc import TaskGroup
else:
    TaskGroup = object
