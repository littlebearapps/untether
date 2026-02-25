"""Tests for token-bucket rate limiter."""

from __future__ import annotations

import time

from untether.triggers.rate_limit import TokenBucketLimiter


class TestTokenBucketLimiter:
    def test_allows_within_rate(self):
        limiter = TokenBucketLimiter(rate=5, window=60.0)
        for _ in range(5):
            assert limiter.allow("key") is True

    def test_denies_over_rate(self):
        limiter = TokenBucketLimiter(rate=2, window=60.0)
        assert limiter.allow("key") is True
        assert limiter.allow("key") is True
        assert limiter.allow("key") is False

    def test_separate_keys_independent(self):
        limiter = TokenBucketLimiter(rate=1, window=60.0)
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        assert limiter.allow("a") is False
        assert limiter.allow("b") is False

    def test_tokens_refill_over_time(self):
        limiter = TokenBucketLimiter(rate=1, window=1.0)
        assert limiter.allow("key") is True
        assert limiter.allow("key") is False
        # Simulate time passing by manipulating the bucket directly
        now = time.monotonic()
        limiter._buckets["key"] = (0.0, now - 2.0)  # 2 seconds ago
        assert limiter.allow("key") is True
