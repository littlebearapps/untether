"""Mutable holder for trigger configuration, supporting hot-reload.

The ``TriggerManager`` is shared between the cron scheduler and webhook
server.  On config reload, the manager's state is atomically replaced
so that subsequent ticks/requests see the new configuration immediately.
"""

from __future__ import annotations

from pathlib import Path

from ..logging import get_logger
from .run_once_state import (
    iso_now,
    load_fired_state,
    resolve_state_path,
    save_fired_state,
)
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

    __slots__ = (
        "_crons",
        "_default_timezone",
        "_fired_run_once",
        "_run_once_state_path",
        "_webhooks_by_path",
    )

    def __init__(
        self,
        settings: TriggersSettings | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        self._crons: list[CronConfig] = []
        self._webhooks_by_path: dict[str, WebhookConfig] = {}
        self._default_timezone: str | None = None
        # #317: persistent fired-state for ``run_once`` crons so restarts
        # and config hot-reloads don't re-fire already-completed one-shots.
        # ``config_path=None`` keeps the old in-memory-only behaviour (used
        # by the large existing test suite where persistence is irrelevant).
        self._run_once_state_path: Path | None = (
            resolve_state_path(config_path) if config_path is not None else None
        )
        self._fired_run_once: dict[str, str] = (
            load_fired_state(self._run_once_state_path)
            if self._run_once_state_path is not None
            else {}
        )
        if settings is not None:
            self.update(settings)

    def update(self, settings: TriggersSettings) -> None:
        """Replace cron and webhook configuration.

        Creates new container objects so that in-flight iterations over
        the previous ``crons`` list are unaffected.
        """
        old_cron_ids = {c.id for c in self._crons}
        old_webhook_ids = {wh.id for wh in self._webhooks_by_path.values()}

        # #317: filter out crons whose id is in the fired-once set so
        # reloads don't re-activate one-shots.
        incoming_cron_ids = {c.id for c in settings.crons}
        self._crons = [c for c in settings.crons if c.id not in self._fired_run_once]
        # Clean fired-state entries for crons that are no longer in the
        # TOML at all — lets the user re-add the same id later under a
        # fresh schedule.
        stale_fired = set(self._fired_run_once) - incoming_cron_ids
        if stale_fired:
            for cron_id in stale_fired:
                self._fired_run_once.pop(cron_id, None)
            self._persist_fired_state()
            logger.info(
                "triggers.cron.run_once_state_cleaned",
                dropped=sorted(stale_fired),
            )

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

    def cron_ids(self) -> list[str]:
        """Return a snapshot list of all configured cron ids."""
        return [c.id for c in self._crons]

    def webhook_ids(self) -> list[str]:
        """Return a snapshot list of all configured webhook ids."""
        return [wh.id for wh in self._webhooks_by_path.values()]

    def crons_for_chat(
        self, chat_id: int, default_chat_id: int | None = None
    ) -> list[CronConfig]:
        """Return crons that target the given chat.

        A cron with ``chat_id=None`` falls back to ``default_chat_id``; when
        ``default_chat_id`` is also ``None``, such crons are excluded.
        """
        return [
            c
            for c in self._crons
            if (c.chat_id if c.chat_id is not None else default_chat_id) == chat_id
        ]

    def webhooks_for_chat(
        self, chat_id: int, default_chat_id: int | None = None
    ) -> list[WebhookConfig]:
        """Return webhooks that target the given chat (same fallback as ``crons_for_chat``)."""
        return [
            wh
            for wh in self._webhooks_by_path.values()
            if (wh.chat_id if wh.chat_id is not None else default_chat_id) == chat_id
        ]

    def remove_cron(self, cron_id: str) -> bool:
        """Atomically remove a cron by id; returns ``True`` if found.

        Used by the ``run_once`` flag to disable a cron after its first fire.
        Replaces ``self._crons`` with a new list so that in-flight iterations
        see a consistent snapshot (same pattern as ``update()``).

        #317: also records ``cron_id`` in the persistent fired-state so the
        one-shot doesn't re-fire on the next config reload or restart.
        """
        for i, c in enumerate(self._crons):
            if c.id == cron_id:
                self._crons = [*self._crons[:i], *self._crons[i + 1 :]]
                self._fired_run_once[cron_id] = iso_now()
                self._persist_fired_state()
                logger.info(
                    "triggers.cron.run_once_completed",
                    cron_id=cron_id,
                    remaining_crons=len(self._crons),
                )
                return True
        return False

    def _persist_fired_state(self) -> None:
        """Write the fired-once set to disk if a state path is configured."""
        if self._run_once_state_path is not None:
            save_fired_state(self._run_once_state_path, self._fired_run_once)

    def fired_run_once_ids(self) -> list[str]:
        """Return a snapshot of cron ids that have already fired (#317)."""
        return sorted(self._fired_run_once)
