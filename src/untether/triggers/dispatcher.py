"""Dispatch trigger events into the Untether run pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from anyio.abc import TaskGroup

from ..context import RunContext
from ..logging import get_logger
from ..transport import RenderedMessage, SendOptions, Transport
from .settings import CronConfig, WebhookConfig

logger = get_logger(__name__)

# Type alias matching the run_job() closure signature in loop.py.
RunJobFn = Callable[..., Awaitable[None]]


@dataclass(slots=True)
class TriggerDispatcher:
    """Bridge between trigger sources (webhooks/crons) and ``run_job()``."""

    run_job: RunJobFn
    transport: Transport
    default_chat_id: int
    task_group: TaskGroup

    async def dispatch_webhook(
        self, webhook: WebhookConfig, prompt: str
    ) -> None:
        chat_id = webhook.chat_id or self.default_chat_id
        context = RunContext(project=webhook.project) if webhook.project else None
        engine_override = webhook.engine
        label = f"\N{HIGH VOLTAGE SIGN} Trigger: webhook:{webhook.id}"

        await self._dispatch(chat_id, label, prompt, context, engine_override)

    async def dispatch_cron(self, cron: CronConfig) -> None:
        chat_id = cron.chat_id or self.default_chat_id
        context = RunContext(project=cron.project) if cron.project else None
        engine_override = cron.engine
        label = f"\N{ALARM CLOCK} Scheduled: cron:{cron.id}"

        await self._dispatch(chat_id, label, cron.prompt, context, engine_override)

    async def _dispatch(
        self,
        chat_id: int,
        label: str,
        prompt: str,
        context: RunContext | None,
        engine_override: str | None,
    ) -> None:
        # Send a notification message so run_job has a message_id to reply to.
        notify_ref = await self.transport.send(
            channel_id=chat_id,
            message=RenderedMessage(text=label),
            options=SendOptions(notify=False),
        )
        if notify_ref is None:
            logger.error("triggers.dispatch.send_failed", label=label)
            return

        logger.info(
            "triggers.dispatch.starting",
            label=label,
            chat_id=chat_id,
            project=context.project if context else None,
            engine=engine_override,
        )

        self.task_group.start_soon(
            self.run_job,
            chat_id,
            notify_ref.message_id,
            prompt,
            None,  # resume_token
            context,
            None,  # thread_id
            None,  # chat_session_key
            None,  # reply_ref
            None,  # on_thread_known
            engine_override,
            None,  # progress_ref
        )
