"""Lightweight cron scheduler with 5-field expression matching."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import anyio

from ..logging import get_logger
from .dispatcher import DISPATCH_REFUSED, TriggerDispatcher
from .manager import TriggerManager

logger = get_logger(__name__)

# #893: how long a ``run_once`` cron whose announce send failed keeps being
# retried (one attempt per minute tick) before it is given up as
# ``triggers.cron.run_once_lost``. Rides out a Telegram/network outage or a
# host blip around the fire time, without running a one-shot so late that it
# would surprise the user. Measured from the first failed fire.
RUN_ONCE_RETRY_WINDOW_S: float = 15 * 60
_RUN_ONCE_RETRY_WINDOW = datetime.timedelta(seconds=RUN_ONCE_RETRY_WINDOW_S)


def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of matching integers."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                return set()

        if part == "*":
            values.update(range(min_val, max_val + 1, step))
        elif "-" in part:
            lo, hi = part.split("-", 1)
            values.update(range(int(lo), int(hi) + 1, step))
        else:
            values.add(int(part))
    return values


def cron_matches(expression: str, now: datetime.datetime) -> bool:
    """Check if a 5-field cron expression matches the given datetime.

    Fields: minute hour day-of-month month day-of-week (0=Sun or 7=Sun).
    """
    fields = expression.split()
    if len(fields) != 5:
        return False

    minutes = _parse_field(fields[0], 0, 59)
    hours = _parse_field(fields[1], 0, 23)
    days = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12)
    weekdays = _parse_field(fields[4], 0, 7)
    # Normalise Sunday: both 0 and 7 map to Sunday (isoweekday()=7, weekday()=6)
    dow = now.weekday()  # Monday=0 .. Sunday=6
    # Convert to cron convention: Sunday=0, Monday=1 .. Saturday=6
    cron_dow = (dow + 1) % 7

    return (
        now.minute in minutes
        and now.hour in hours
        and now.day in days
        and now.month in months
        and (cron_dow in weekdays or (7 in weekdays and cron_dow == 0))
    )


def _resolve_now(
    utc_now: datetime.datetime,
    cron_tz: str | None,
    default_tz: str | None,
) -> datetime.datetime:
    """Return the wall-clock datetime for cron matching.

    If a timezone is configured (per-cron or global default), converts UTC *now*
    to that timezone.  Otherwise falls back to system local time (backward compat).
    """
    tz_name = cron_tz or default_tz
    if tz_name is not None:
        return utc_now.astimezone(ZoneInfo(tz_name))
    # No timezone configured — use system local time (strip tzinfo for compat).
    return utc_now.astimezone().replace(tzinfo=None)


async def run_cron_scheduler(
    manager: TriggerManager,
    dispatcher: TriggerDispatcher,
) -> None:
    """Tick every minute and dispatch crons whose schedule matches.

    Reads ``manager.crons`` and ``manager.default_timezone`` on each tick
    so that config hot-reloads take effect immediately.
    """
    logger.info("triggers.cron.started", crons=len(manager.crons))
    # Key the minute fully (year, month, day, hour, minute). A bare (hour, minute)
    # key would suppress every subsequent day's run because tomorrow's 09:00 looks
    # identical to today's. See #309 CodeRabbit feedback (Critical).
    last_fired: dict[str, tuple[int, int, int, int, int]] = {}
    # #893: the run_once crons whose announce send failed (first failed fire)
    # live on the manager, persisted next to untether.toml, so a restart in
    # the retry window resumes them on the first tick — or gives up a one-shot
    # whose window passed meanwhile. ``owed_retry``: pending one-shots that
    # sat out a /pause; each gets one attempt after resume even if the window
    # closed during the pause.
    owed_retry: set[str] = set()

    while True:
        utc_now = datetime.datetime.now(datetime.UTC)
        # #294: master pause flag — skip every cron's tick when set.
        # `run_once` crons that would have fired during the pause are NOT
        # consumed; they fire on the next matching tick after resume.
        if manager.is_paused:
            owed_retry.update(
                c.id
                for c in manager.crons
                if manager.run_once_pending_since(c.id) is not None
            )
            await anyio.sleep(60 - utc_now.second + 0.1)
            continue
        # Snapshot the cron list for this tick — safe even if update()
        # replaces manager._crons mid-iteration (new list, old ref valid).
        crons = manager.crons
        default_timezone = manager.default_timezone
        for cron in crons:
            try:
                local_now = _resolve_now(utc_now, cron.timezone, default_timezone)
                matched = cron_matches(cron.schedule, local_now)
            except Exception:
                logger.exception("triggers.cron.match_failed", cron_id=cron.id)
                continue
            retry_since = (
                manager.run_once_pending_since(cron.id) if cron.run_once else None
            )
            if retry_since is not None:
                # #893: a one-shot whose announce send failed earlier. Retry
                # every tick (whether or not the schedule matches) until the
                # window closes, then give up loudly.
                if (
                    utc_now - retry_since >= _RUN_ONCE_RETRY_WINDOW
                    and cron.id not in owed_retry
                ):
                    manager.abandon_run_once(
                        cron.id, pending_since=retry_since.isoformat()
                    )
                    continue
                owed_retry.discard(cron.id)
                logger.info(
                    "triggers.cron.run_once_retry",
                    cron_id=cron.id,
                    pending_since=retry_since.isoformat(),
                )
                # Single attempt: the in-dispatch 5 s + 30 s backoff already
                # ran on the first fire, and repeating it every tick would
                # stall the scheduler loop past other crons' minutes.
                dispatched = await dispatcher.dispatch_cron(cron, retry_delays=())
            elif matched:
                key = (
                    local_now.year,
                    local_now.month,
                    local_now.day,
                    local_now.hour,
                    local_now.minute,
                )
                if last_fired.get(cron.id) == key:
                    continue  # already fired this minute
                last_fired[cron.id] = key
                logger.info("triggers.cron.firing", cron_id=cron.id)
                dispatched = await dispatcher.dispatch_cron(cron)
            else:
                continue
            if dispatched == DISPATCH_REFUSED:
                # #896: the daily cost budget refused it before anything ran.
                # Not recorded as fired, and a one-shot is not consumed: like
                # a one-shot skipped by /pause, it stays active and fires at
                # its next schedule match. Not retried every tick (the gate
                # stays shut until midnight, and a one-shot run that late
                # would surprise the user) — so any #893 retry is dropped.
                if cron.run_once:
                    manager.clear_run_once_pending(cron.id)
                    logger.warning(
                        "triggers.cron.run_once_refused",
                        cron_id=cron.id,
                        hint=(
                            "The daily cost budget refused this one-shot; it "
                            "stays scheduled and fires at its next schedule "
                            "match."
                        ),
                    )
                continue
            # #893: ``False`` means the announce send failed and no run was
            # started (anything else, incl. a legacy ``None``, is a dispatch).
            # A one-shot is NOT consumed — it stays active and is retried on
            # later ticks; recurring crons simply wait for their next match.
            if dispatched is False:
                if cron.run_once and manager.mark_run_once_pending(cron.id, utc_now):
                    logger.warning(
                        "triggers.cron.run_once_pending",
                        cron_id=cron.id,
                        retry_window_s=RUN_ONCE_RETRY_WINDOW_S,
                    )
                continue
            manager.clear_run_once_pending(cron.id)
            # #271 Tier 3: record last-fired-at after dispatch returns.
            # `dispatch_cron` only blocks until the notification is
            # queued, not run completion — recording here means the
            # `/config:tg` page reflects every dispatched cron, even if
            # the run later fails.
            from . import history

            history.record_fired(cron.id)
            # #288: one-shot crons are removed from the active list
            # after firing; they stay in the TOML and re-activate on
            # the next config reload or restart.
            if cron.run_once:
                manager.remove_cron(cron.id)

        # Sleep until next minute boundary (+ small buffer).
        utc_now = datetime.datetime.now(datetime.UTC)
        sleep_s = 60 - utc_now.second + 0.1
        await anyio.sleep(sleep_s)
