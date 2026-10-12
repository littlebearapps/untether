"""Context windows for Antigravity's ``~N% ctx`` (#979).

Custom, because agy has no native source: its ``contextWindow`` is a user
setting (empty for every model on 1.3.3), no line of the run stream names a
window, and ``/context`` is refused in print mode. See the register in
``docs/reference/runners/antigravity/runner.md``; retire this table when agy
reports a window in the stream.

The figures are the input limits on Google's own model pages, read
2026-10-12. They are the **published** windows: no Google page states the
window Antigravity itself applies, and agy compacts on its own schedule, so
a percentage of these can read low.
"""

from __future__ import annotations

import re

_GEMINI_WINDOW = 1_048_576

# agy's base model id (``agy models``, ``-p /model``) → published input
# limit in tokens. Only verified figures: a model missing here shows no
# percentage at all.
AGY_CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-3.8-flash": _GEMINI_WINDOW,
    "gemini-3.7-flash": _GEMINI_WINDOW,
    "gemini-3.6-flash": _GEMINI_WINDOW,
    # Assumption: Google's page is for the API model
    # ``gemini-3.1-pro-preview``; that agy's ``gemini-3.1-pro`` is that model
    # is taken from the name, not stated anywhere.
    "gemini-3.1-pro": _GEMINI_WINDOW,
    # Google Cloud's hosted partner models.
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "gpt-oss-120b": 131_072,
}

# agy's ids can carry the effort as a suffix (``gemini-3.8-flash-low``).
EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$")


def context_window_for(model: str | None) -> int | None:
    """The published window for agy's *model* id, or None.

    Exact match on the id with one effort suffix removed. No fuzzy match,
    no label match and no default: an unknown model has no window."""
    if not model or not isinstance(model, str):
        return None
    return AGY_CONTEXT_WINDOWS.get(EFFORT_SUFFIX_RE.sub("", model, count=1))


def context_pct(used: int, window: int) -> int:
    """*used* of *window* in whole percent, rounded half up, 0 to 100."""
    if window <= 0 or used <= 0:
        return 0
    return min(100, (used * 200 + window) // (2 * window))
