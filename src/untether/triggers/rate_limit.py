"""Token-bucket rate limiter for webhook requests."""

from __future__ import annotations

import time

from ..logging import get_logger

logger = get_logger(__name__)

# At most one sweep for refilled buckets a second while the key table is full,
# so a flood of new keys can't make every request pay for a full scan.
_PRUNE_INTERVAL_S = 1.0


class TokenBucketLimiter:
    """Simple token-bucket rate limiter.

    Each *key* (webhook ID, ``"__global__"``, or a webhook + client address
    pair) gets its own bucket. Tokens refill at ``rate`` per ``window``
    seconds.

    ``max_keys`` caps how many buckets are tracked, for limiters whose keys a
    caller can choose (#906). A bucket that has refilled to full says the same
    thing as no bucket at all, so those are dropped to make room; if the table
    is still full, a key that isn't tracked has no tokens (``allow`` and
    ``available`` refuse it) rather than being let through uncounted. A
    caller that must tell "no room" from "spent its own tokens" asks
    ``tracked``. ``None`` means no cap.
    """

    def __init__(
        self, rate: int, window: float = 60.0, max_keys: int | None = None
    ) -> None:
        self._rate = rate
        self._window = window
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._next_prune = 0.0
        self._next_full_warning = 0.0

    def _tokens(self, key: str, now: float) -> float:
        """Tokens in *key*'s bucket at *now*, after refill. Changes nothing."""
        tokens, last = self._buckets.get(key, (float(self._rate), now))
        elapsed = now - last
        return min(self._rate, tokens + elapsed * (self._rate / self._window))

    def _has_room(self, keys: tuple[str, ...], now: float) -> bool:
        """Whether every key in *keys* is tracked or can be (#906)."""
        if self._max_keys is None:
            return True
        new = len({key for key in keys if key not in self._buckets})
        if not new or len(self._buckets) + new <= self._max_keys:
            return True
        if now >= self._next_prune:
            self._next_prune = now + _PRUNE_INTERVAL_S
            for key in [k for k in self._buckets if self._tokens(k, now) >= self._rate]:
                del self._buckets[key]
            if len(self._buckets) + new <= self._max_keys:
                return True
        if now >= self._next_full_warning:
            # Once a window: this is worth an operator's attention, but a
            # flood must not be able to write a line per request.
            self._next_full_warning = now + self._window
            logger.warning("rate_limit.key_table_full", max_keys=self._max_keys)
        return False

    def available(self, key: str) -> bool:
        """Whether *key* has a token, without spending it (#906)."""
        now = time.monotonic()
        return self._has_room((key,), now) and self._tokens(key, now) >= 1.0

    def tracked(self, key: str) -> bool:
        """Whether *key* has a bucket of its own, i.e. has spent a token that
        hasn't been pruned. An untracked key that ``available`` refuses was
        refused because the table is full, not for anything it did (#906)."""
        return key in self._buckets

    def allow_all(self, *keys: str) -> bool:
        """Spend one token from every key, or from none of them (#906).

        ``allow(a) and allow(b)`` spends ``a``'s token even when ``b`` then
        refuses, so a request that is turned away still costs budget.
        """
        now = time.monotonic()
        if not self._has_room(keys, now):
            return False
        levels = {key: self._tokens(key, now) for key in keys}
        for key, tokens in levels.items():
            if tokens < 1.0:
                logger.debug("rate_limit.denied", key=key, tokens=tokens)
                return False
        for key, tokens in levels.items():
            self._buckets[key] = (tokens - 1.0, now)
        return True

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if not self._has_room((key,), now):
            return False
        tokens = self._tokens(key, now)
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
