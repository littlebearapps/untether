"""5-field cron expression grammar (#1144).

The one parser behind both the cron matcher (``triggers/cron.py``) and the
``[[triggers.crons]] schedule`` config validator (``triggers/settings.py``),
so a schedule that loads is always one the matcher understands.

Supported per field: ``*``, a number, a range ``lo-hi``, a step on either
(``*/15``, ``0-30/10``) and comma lists of those. Names (``MON``, ``JAN``)
are not supported. A step on a single number (``5/10``) matches just that
number, as the matcher always has.

Kept free of other Untether imports: ``settings.py`` imports it, and
``cron.py`` imports ``settings.py`` via the dispatcher.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = [
    "CRON_FIELDS",
    "CronExpressionError",
    "parse_cron_expression",
    "parse_cron_field",
]

#: (name, min, max) per field, in expression order. Day of week: 0 and 7
#: are both Sunday.
CRON_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)

_NUMBER_RE = re.compile(r"[0-9]+", re.ASCII)


class CronExpressionError(ValueError):
    """A cron expression that doesn't fit the grammar. The message names the
    bad field in plain words, for logs and the Telegram trigger notice."""


def _is_number(text: str) -> bool:
    return _NUMBER_RE.fullmatch(text) is not None


def _in_range(value: int, name: str, min_val: int, max_val: int) -> int:
    if not min_val <= value <= max_val:
        raise CronExpressionError(
            f"{name} field: {value} is out of range ({min_val}–{max_val})"
        )
    return value


def parse_cron_field(
    field: str, name: str, min_val: int, max_val: int
) -> frozenset[int]:
    """Parse one cron field into the set of values it matches.

    Raises :class:`CronExpressionError` naming *name* when the field is
    malformed or a value falls outside ``min_val`` to ``max_val``.
    """
    values: set[int] = set()
    for part in field.split(","):
        if not part:
            raise CronExpressionError(f"{name} field: empty item in list {field!r}")
        base, has_step, step_text = part.partition("/")
        step = 1
        if has_step:
            if not _is_number(step_text) or int(step_text) < 1:
                raise CronExpressionError(
                    f"{name} field: step must be a whole number of 1 or more, "
                    f"got {step_text!r}"
                )
            step = int(step_text)
        if base == "*":
            lo, hi = min_val, max_val
        elif "-" in base:
            lo_text, _, hi_text = base.partition("-")
            if not (_is_number(lo_text) and _is_number(hi_text)):
                raise CronExpressionError(
                    f"{name} field: {base!r} isn't a valid range "
                    "(use two numbers like 1-5)"
                )
            lo = _in_range(int(lo_text), name, min_val, max_val)
            hi = _in_range(int(hi_text), name, min_val, max_val)
            if lo > hi:
                raise CronExpressionError(
                    f"{name} field: range {base} runs backwards "
                    "(the start is after the end)"
                )
        elif _is_number(base):
            lo = hi = _in_range(int(base), name, min_val, max_val)
        else:
            raise CronExpressionError(
                f"{name} field: {base!r} isn't a number "
                "(names like MON or JAN aren't supported)"
            )
        values.update(range(lo, hi + 1, step))
    return frozenset(values)


@lru_cache(maxsize=256)
def parse_cron_expression(expression: str) -> tuple[frozenset[int], ...]:
    """Parse a 5-field cron expression into one value set per field.

    Order: minute, hour, day-of-month, month, day-of-week. Cached, since
    the scheduler re-reads each schedule every minute and the loop
    scheduler walks up to a year of minutes. Raises
    :class:`CronExpressionError` (a ``ValueError``) when invalid.
    """
    fields = expression.split()
    if len(fields) != len(CRON_FIELDS):
        raise CronExpressionError(
            "needs 5 fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)}"
        )
    return tuple(
        parse_cron_field(field, name, min_val, max_val)
        for field, (name, min_val, max_val) in zip(fields, CRON_FIELDS, strict=True)
    )
