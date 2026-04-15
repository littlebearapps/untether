"""Dispatch trigger events into the Untether run pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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

    async def dispatch_webhook(self, webhook: WebhookConfig, prompt: str) -> None:
        chat_id = webhook.chat_id or self.default_chat_id
        # rc4 (#271): always set trigger_source so the meta footer can render
        # provenance even when no project is configured.
        context = RunContext(
            project=webhook.project,
            trigger_source=f"webhook:{webhook.id}",
        )
        engine_override = webhook.engine
        label = f"\N{HIGH VOLTAGE SIGN} Trigger: webhook:{webhook.id}"

        await self._dispatch(chat_id, label, prompt, context, engine_override)

    async def dispatch_cron(self, cron: CronConfig) -> None:
        chat_id = cron.chat_id or self.default_chat_id
        context = RunContext(
            project=cron.project,
            trigger_source=f"cron:{cron.id}",
        )
        engine_override = cron.engine
        label = f"\N{ALARM CLOCK} Scheduled: cron:{cron.id}"

        # If cron has a fetch step, execute it before rendering the prompt.
        if cron.fetch is not None:
            prompt = await self._fetch_and_render(cron)
            if prompt is None:
                return  # fetch failed with on_failure=abort
        elif cron.prompt_template:
            # prompt_template without fetch — render with empty payload.
            from .templating import render_template_fields

            prompt = render_template_fields(cron.prompt_template, {})
        else:
            prompt = cron.prompt or ""

        await self._dispatch(chat_id, label, prompt, context, engine_override)

    async def _fetch_and_render(self, cron: CronConfig) -> str | None:
        """Execute cron fetch step and build the prompt.

        Returns the rendered prompt, or ``None`` if fetch failed and
        ``on_failure`` is ``"abort"``.
        """
        from .fetch import build_fetch_prompt, execute_fetch

        assert cron.fetch is not None
        chat_id = cron.chat_id or self.default_chat_id

        ok, error_msg, data = await execute_fetch(cron.fetch)

        if not ok:
            logger.warning(
                "triggers.cron.fetch_failed",
                cron_id=cron.id,
                error=error_msg,
            )
            if cron.fetch.on_failure == "abort":
                # Notify user of the failure.
                fail_label = f"\u274c cron:{cron.id} fetch failed: {error_msg}"
                await self.transport.send(
                    channel_id=chat_id,
                    message=RenderedMessage(text=fail_label),
                    options=SendOptions(notify=True),
                )
                return None
            # on_failure=run_with_error — inject error into prompt.
            data = f"[FETCH ERROR: {error_msg}]"

        return build_fetch_prompt(
            cron.prompt,
            cron.prompt_template,
            data,
            cron.fetch.store_as,
        )

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

    async def dispatch_action(
        self,
        webhook: WebhookConfig,
        payload: dict[str, Any],
        raw_body: bytes,
    ) -> None:
        """Execute a non-agent webhook action (file_write, http_forward, notify_only)."""
        from .actions import (
            execute_file_write,
            execute_http_forward,
            execute_notify_message,
        )

        chat_id = webhook.chat_id or self.default_chat_id
        action = webhook.action

        logger.info(
            "triggers.action.start",
            webhook_id=webhook.id,
            action=action,
        )

        if action == "file_write":
            ok, msg = await execute_file_write(webhook, payload, raw_body)
        elif action == "http_forward":
            ok, msg = await execute_http_forward(webhook, payload, raw_body)
        elif action == "notify_only":
            msg = execute_notify_message(webhook, payload)
            ok = True
        else:
            logger.error(
                "triggers.action.unknown", action=action, webhook_id=webhook.id
            )
            return

        # Send notification to Telegram if configured.
        should_notify = (ok and webhook.notify_on_success) or (
            not ok and webhook.notify_on_failure
        )

        if action == "notify_only":
            # notify_only always sends the message.
            await self.transport.send(
                channel_id=chat_id,
                message=RenderedMessage(text=msg),
                options=SendOptions(notify=True),
            )
        elif should_notify:
            icon = "\u2705" if ok else "\u274c"
            label = f"{icon} webhook:{webhook.id} ({action}): {msg}"
            await self.transport.send(
                channel_id=chat_id,
                message=RenderedMessage(text=label),
                options=SendOptions(notify=not ok),
            )

        logger.info(
            "triggers.action.done",
            webhook_id=webhook.id,
            action=action,
            ok=ok,
            message=msg,
        )
