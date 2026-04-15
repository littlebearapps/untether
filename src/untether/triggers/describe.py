"""Human-friendly cron schedule rendering (issue 271).

Converts a 5-field cron expression plus optional timezone into a short,
natural-language description suitable for the Telegram ping indicator,
the config trigger page, and dispatch notifications. Complex patterns
(stepped, specific day-of-month, multi-month) fall back to the raw
expression; the goal is a clear default for common patterns, not a
full cron-to-English translator.

Examples (rendered output shown in quotes):
- ``0 9 * * *`` + ``Australia/Melbourne`` -> ``9:00 AM daily (Melbourne)``
- ``0 8 * * 1-5`` + ``Australia/Melbourne`` -> ``8:00 AM Mon-Fri (Melbourne)``
- ``30 14 * * 0,6`` + ``None`` -> ``2:30 PM Sat,Sun``
- ``0 0 * * *`` + ``None`` -> ``12:00 AM daily``
- ``*/15 * * * *`` + ``None`` -> ``*/15 * * * *`` (fallback)
"""

from __future__ import annotations

__all__ = ["describe_cron"]

_DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _normalise_dow(value: int) -> int | None:
    """Cron day-of-week 0 and 7 both mean Sunday. Anything else (including
    out-of-range like 8) is invalid and the caller should fall back to raw."""
    if value == 7:
        return 0
    if 0 <= value <= 6:
        return value
    return None


def _format_dow(dow: str) -> str | None:
    """Turn a day-of-week field into a label like 'Mon-Fri' or 'Sat,Sun'.

    Returns ``None`` if the field uses an unsupported pattern or
    out-of-range day. Caller falls back to the raw schedule string. (#309)
    """
    if dow == "*":
        return ""
    # Range, e.g. "1-5"
    if "-" in dow and "," not in dow and "/" not in dow:
        try:
            start_s, end_s = dow.split("-", 1)
            start = _normalise_dow(int(start_s))
            end = _normalise_dow(int(end_s))
            if start is None or end is None:
                return None
            return f"{_DAY_NAMES[start]}\u2013{_DAY_NAMES[end]}"
        except ValueError:
            return None
    # Comma list, e.g. "0,6"
    if "," in dow and "-" not in dow and "/" not in dow:
        try:
            normalised = [_normalise_dow(int(p)) for p in dow.split(",")]
            if any(n is None for n in normalised):
                return None
            return ",".join(_DAY_NAMES[n] for n in normalised)  # type: ignore[index]
        except ValueError:
            return None
    # Single day
    if dow.isdigit():
        try:
            n = _normalise_dow(int(dow))
            if n is None:
                return None
            return _DAY_NAMES[n]
        except ValueError:
            return None
    return None


def _format_timezone_suffix(timezone: str | None) -> str:
    """Turn 'Australia/Melbourne' into ' (Melbourne)'; '' if no tz."""
    if not timezone:
        return ""
    leaf = timezone.split("/")[-1].replace("_", " ")
    return f" ({leaf})"


def _format_time_12h(hour: int, minute: int) -> str:
    """Turn (9, 0) into '9:00 AM', (14, 30) into '2:30 PM', (0, 0) into '12:00 AM'."""
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def describe_cron(schedule: str, timezone: str | None = None) -> str:
    """Render a cron expression + timezone in a human-friendly form.

    Returns ``schedule`` unchanged if the expression uses features outside
    the supported common-case grammar (stepped minutes, specific day-of-month,
    specific months, multi-hour, multi-minute). The goal is a helpful default
    for daily/weekly schedules, not a universal translator.
    """
    fields = schedule.split()
    if len(fields) != 5:
        return schedule
    minute, hour, dom, mon, dow = fields

    # Bail out on patterns we don't try to translate.
    # `*` must be exact: `*/2` is a stepped wildcard and should NOT render as
    # "daily" (#309). Use exact equality, not substring.
    if mon != "*":
        return schedule
    if dom != "*":
        return schedule
    if "/" in minute or "," in minute or "-" in minute:
        return schedule
    if "/" in hour or "," in hour or "-" in hour:
        return schedule

    try:
        h = int(hour)
        m = int(minute)
    except ValueError:
        return schedule
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return schedule

    time_part = _format_time_12h(h, m)
    dow_part = _format_dow(dow)
    if dow_part is None:
        # Unsupported / out-of-range day-of-week — fall back to raw.
        return schedule
    if dow_part == "":
        # Every day
        suffix_dow = " daily"
    elif "," in dow_part or "\u2013" in dow_part or "-" in dow_part:
        suffix_dow = f" {dow_part}"
    else:
        # Single day
        suffix_dow = f" {dow_part}"

    tz_part = _format_timezone_suffix(timezone)
    return f"{time_part}{suffix_dow}{tz_part}".rstrip()
