"""Dispatch trigger events into the Untether run pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.abc import TaskGroup

from ..context import RunContext
from ..logging import get_logger
from ..transport import RenderedMessage, SendOptions, Transport
from .settings import CronConfig, WebhookConfig

logger = get_logger(__name__)

# Type alias matching the run_job() closure signature in loop.py.
RunJobFn = Callable[..., Awaitable[None]]

# Bounded retry schedule (seconds) for the trigger announce send. A transient
# resolver/network blip at cron-fire time must not cost the whole dispatch —
# the session is the payload, the announce is decoration. One DNS hiccup in the
# 06:45 second silently killed a whole scheduled run (auditor-toolkit#2535);
# two retries ride out a blip while a real outage still fails loudly within
# ~35s of the trigger firing.
SEND_RETRY_DELAYS: tuple[float, ...] = (5.0, 30.0)


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
        # #271 Tier 3: record last-fired-at for the /config:tg page. Recorded
        # after dispatch so a transport-send failure (logged inside _dispatch)
        # doesn't pollute the history with a phantom entry.
        from . import history

        history.record_fired(webhook.id)

    async def dispatch_cron(
        self,
        cron: CronConfig,
        *,
        retry_delays: tuple[float, ...] | None = None,
    ) -> bool:
        """Dispatch *cron*; return ``False`` only when nothing ran.

        ``False`` means the announce send failed after its retries, so no run
        was started and the caller may safely try again (#893: a ``run_once``
        cron stays pending instead of being consumed). Every other outcome —
        a started run, or a fetch ``on_failure = "abort"`` that the user was
        notified about — returns ``True``. ``retry_delays`` overrides
        :data:`SEND_RETRY_DELAYS` (the scheduler's retry ticks pass ``()``).
        """
        chat_id = cron.chat_id or self.default_chat_id
        context = RunContext(
            project=cron.project,
            trigger_source=f"cron:{cron.id}",
            permission_mode=cron.permission_mode,
            model=cron.model,
            reasoning=cron.reasoning,
        )
        engine_override = cron.engine
        label = f"\N{ALARM CLOCK} Scheduled: cron:{cron.id}"

        # If cron has a fetch step, execute it before rendering the prompt.
        if cron.fetch is not None:
            prompt = await self._fetch_and_render(cron)
            if prompt is None:
                return True  # fetch failed with on_failure=abort (handled)
        elif cron.prompt_template:
            # prompt_template without fetch — render with empty payload.
            from .templating import render_template_fields

            prompt = render_template_fields(cron.prompt_template, {})
        else:
            prompt = cron.prompt or ""

        return await self._dispatch(
            chat_id,
            label,
            prompt,
            context,
            engine_override,
            retry_delays=retry_delays,
        )

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
        *,
        retry_delays: tuple[float, ...] | None = None,
    ) -> bool:
        """Announce and start the run; ``False`` if the announce never landed.

        ``False`` is returned only before ``run_job`` is scheduled, so a caller
        retrying on ``False`` can never double-dispatch (#893).
        """
        # Send a notification message so run_job has a message_id to reply to.
        # Retried on failure per SEND_RETRY_DELAYS before giving up.
        delays = SEND_RETRY_DELAYS if retry_delays is None else retry_delays
        notify_ref = await self.transport.send(
            channel_id=chat_id,
            message=RenderedMessage(text=label),
            options=SendOptions(notify=False),
        )
        for delay in delays:
            if notify_ref is not None:
                break
            logger.warning("triggers.dispatch.send_retry", label=label, retry_in=delay)
            await anyio.sleep(delay)
            notify_ref = await self.transport.send(
                channel_id=chat_id,
                message=RenderedMessage(text=label),
                options=SendOptions(notify=False),
            )
        if notify_ref is None:
            logger.error("triggers.dispatch.send_failed", label=label, chat_id=chat_id)
            return False

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
        return True

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
        # #271 Tier 3: record last-fired-at for non-agent actions too — the
        # webhook still fired even if it didn't spawn a run.
        from . import history

        history.record_fired(webhook.id)
