"""Token-bucket rate limiter for webhook requests."""

from __future__ import annotations

import time

from ..logging import get_logger

logger = get_logger(__name__)


class TokenBucketLimiter:
    """Simple token-bucket rate limiter.

    Each *key* (webhook ID or ``"__global__"``) gets its own bucket.
    Tokens refill at ``rate`` per ``window`` seconds.
    """

    def __init__(self, rate: int, window: float = 60.0) -> None:
        self._rate = rate
        self._window = window
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self._rate), now))
        elapsed = now - last
        tokens = min(self._rate, tokens + elapsed * (self._rate / self._window))
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        # Logged at debug to avoid flooding logs (and feeding the issue
        # watcher) on persistent burst attempts. Per-request denial visibility
        # is not actionable; the HTTP 429 response carries the signal that
        # matters. #309 CodeRabbit feedback.
        logger.debug("rate_limit.denied", key=key, tokens=tokens)
        return False
