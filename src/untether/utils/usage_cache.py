"""60-second TTL cache for the Claude Code subscription usage fetch.

The Anthropic OAuth usage endpoint is called once per run completion by the
footer code. A short cache smooths transient errors (429, network blips) and
avoids beating the API during bursts of completions. On fetch failure the
cache falls back to the last successful response if one is still held in
memory (stale-while-error); otherwise the underlying exception propagates so
callers can handle it like before.
"""

from __future__ import annotations

import time
from typing import Any

import anyio

from ..logging import get_logger

logger = get_logger(__name__)

_TTL_SECONDS = 60.0

_cache: tuple[float, dict[str, Any]] | None = None
_lock: anyio.Lock | None = None


def _get_lock() -> anyio.Lock:
    global _lock
    if _lock is None:
        _lock = anyio.Lock()
    return _lock


def reset_cache() -> None:
    """Clear the cache and lock. Intended for tests."""
    global _cache, _lock
    _cache = None
    _lock = None


async def fetch_claude_usage_cached() -> dict[str, Any]:
    """Return Claude usage data, using a 60s TTL cache with stale-while-error.

    On cache hit within TTL, returns the cached dict without calling the API.
    On miss, calls `fetch_claude_usage()` and stores the result. If the
    underlying fetch raises, returns the stale cached value if present;
    otherwise re-raises so the caller's existing error handling still fires.
    """
    global _cache
    from ..telegram.commands.usage import fetch_claude_usage

    now = time.monotonic()
    async with _get_lock():
        if _cache is not None:
            cached_at, cached_data = _cache
            if now - cached_at < _TTL_SECONDS:
                return cached_data

        try:
            data = await fetch_claude_usage()
        except Exception:
            if _cache is not None:
                logger.debug("claude_usage.cache.stale_on_error")
                return _cache[1]
            raise

        _cache = (now, data)
        return data
