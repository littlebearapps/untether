"""Lightweight cron scheduler with 5-field expression matching."""

from __future__ import annotations

import datetime

import anyio

from ..logging import get_logger
from .dispatcher import TriggerDispatcher
from .settings import CronConfig

logger = get_logger(__name__)


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


async def run_cron_scheduler(
    crons: list[CronConfig],
    dispatcher: TriggerDispatcher,
) -> None:
    """Tick every minute and dispatch crons whose schedule matches."""
    logger.info("triggers.cron.started", crons=len(crons))
    last_fired: dict[str, tuple[int, int]] = {}  # cron_id -> (hour, minute)

    while True:
        now = datetime.datetime.now()
        for cron in crons:
            try:
                matched = cron_matches(cron.schedule, now)
            except Exception:
                logger.exception("triggers.cron.match_failed", cron_id=cron.id)
                continue
            if matched:
                key = (now.hour, now.minute)
                if last_fired.get(cron.id) == key:
                    continue  # already fired this minute
                last_fired[cron.id] = key
                logger.info("triggers.cron.firing", cron_id=cron.id)
                await dispatcher.dispatch_cron(cron)

        # Sleep until next minute boundary (+ small buffer).
        now = datetime.datetime.now()
        sleep_s = 60 - now.second + 0.1
        await anyio.sleep(sleep_s)
