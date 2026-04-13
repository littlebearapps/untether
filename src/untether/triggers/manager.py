"""Mutable holder for trigger configuration, supporting hot-reload.

The ``TriggerManager`` is shared between the cron scheduler and webhook
server.  On config reload, the manager's state is atomically replaced
so that subsequent ticks/requests see the new configuration immediately.
"""

from __future__ import annotations

from ..logging import get_logger
from .settings import CronConfig, TriggersSettings, WebhookConfig

logger = get_logger(__name__)

__all__ = ["TriggerManager"]


class TriggerManager:
    """Thread-safe (single-event-loop) mutable trigger configuration holder.

    The cron scheduler reads ``crons`` and ``default_timezone`` each tick.
    The webhook server calls ``webhook_for_path()`` on each request.
    ``update()`` replaces both atomically via simple attribute assignment —
    safe in a single-threaded asyncio loop because coroutines only yield
    at ``await`` points.
    """

    __slots__ = ("_crons", "_default_timezone", "_webhooks_by_path")

    def __init__(self, settings: TriggersSettings | None = None) -> None:
        self._crons: list[CronConfig] = []
        self._webhooks_by_path: dict[str, WebhookConfig] = {}
        self._default_timezone: str | None = None
        if settings is not None:
            self.update(settings)

    def update(self, settings: TriggersSettings) -> None:
        """Replace cron and webhook configuration.

        Creates new container objects so that in-flight iterations over
        the previous ``crons`` list are unaffected.
        """
        old_cron_ids = {c.id for c in self._crons}
        old_webhook_ids = {wh.id for wh in self._webhooks_by_path.values()}

        self._crons = list(settings.crons)
        self._webhooks_by_path = {wh.path: wh for wh in settings.webhooks}
        self._default_timezone = settings.default_timezone

        new_cron_ids = {c.id for c in self._crons}
        new_webhook_ids = {wh.id for wh in self._webhooks_by_path.values()}

        # Log changes for observability.
        added_crons = new_cron_ids - old_cron_ids
        removed_crons = old_cron_ids - new_cron_ids
        added_webhooks = new_webhook_ids - old_webhook_ids
        removed_webhooks = old_webhook_ids - new_webhook_ids

        if added_crons or removed_crons or added_webhooks or removed_webhooks:
            logger.info(
                "triggers.manager.updated",
                crons_added=sorted(added_crons) if added_crons else None,
                crons_removed=sorted(removed_crons) if removed_crons else None,
                webhooks_added=sorted(added_webhooks) if added_webhooks else None,
                webhooks_removed=sorted(removed_webhooks) if removed_webhooks else None,
                total_crons=len(self._crons),
                total_webhooks=len(self._webhooks_by_path),
            )

        # Warn about unauthenticated webhooks.
        for wh in settings.webhooks:
            if wh.auth == "none" and wh.id in added_webhooks:
                logger.warning(
                    "triggers.webhook.no_auth",
                    webhook_id=wh.id,
                    path=wh.path,
                )

    @property
    def crons(self) -> list[CronConfig]:
        """Current cron list — the scheduler iterates this each tick."""
        return self._crons

    @property
    def default_timezone(self) -> str | None:
        return self._default_timezone

    def webhook_for_path(self, path: str) -> WebhookConfig | None:
        """Look up a webhook by its HTTP path."""
        return self._webhooks_by_path.get(path)

    @property
    def webhook_count(self) -> int:
        return len(self._webhooks_by_path)
