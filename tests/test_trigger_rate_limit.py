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

    # ── #906: non-consuming check + all-or-nothing consumption ──────────

    def test_available_does_not_consume(self):
        limiter = TokenBucketLimiter(rate=1, window=60.0)
        for _ in range(5):
            assert limiter.available("key") is True
        assert limiter.allow("key") is True
        assert limiter.available("key") is False

    def test_allow_all_consumes_from_every_key(self):
        limiter = TokenBucketLimiter(rate=1, window=60.0)
        assert limiter.allow_all("a", "b") is True
        assert limiter.allow("a") is False
        assert limiter.allow("b") is False

    def test_allow_all_refusal_consumes_nothing(self):
        # #906: a request the global bucket refuses must not also spend the
        # per-webhook token.
        limiter = TokenBucketLimiter(rate=1, window=60.0)
        assert limiter.allow("global") is True
        assert limiter.allow_all("hook", "global") is False
        assert limiter.allow("hook") is True

    # ── #906 follow-up: bounded key table ───────────────────────────────

    def test_key_count_stays_bounded(self):
        # Keys that include a client address are attacker-chosen; the table
        # must not grow with them.
        limiter = TokenBucketLimiter(rate=3, window=60.0, max_keys=50)
        for i in range(1000):
            limiter.allow(f"addr-{i}")
        assert len(limiter._buckets) <= 50

    def test_full_table_fails_closed_for_untracked_keys(self):
        limiter = TokenBucketLimiter(rate=3, window=60.0, max_keys=2)
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        # No room to track "c": refuse it instead of letting it run uncounted.
        assert limiter.available("c") is False
        assert limiter.allow("c") is False
        assert "c" not in limiter._buckets
        # Keys that are already tracked keep working.
        assert limiter.allow("a") is True

    def test_tracked_tells_no_room_from_spent(self):
        # #906: both "b" (spent its token) and "c" (no room) are refused; only
        # "b" was refused for something it did.
        limiter = TokenBucketLimiter(rate=1, window=60.0, max_keys=2)
        limiter.allow("a")
        limiter.allow("b")
        assert limiter.available("b") is False
        assert limiter.available("c") is False
        assert limiter.tracked("b") is True
        assert limiter.tracked("c") is False

    def test_refilled_buckets_are_pruned_to_make_room(self):
        limiter = TokenBucketLimiter(rate=3, window=60.0, max_keys=2)
        limiter.allow("a")
        limiter.allow("b")
        assert limiter.allow("c") is False
        # "a" refills completely: it carries no information any more.
        limiter._buckets["a"] = (2.0, time.monotonic() - 120.0)
        limiter._next_prune = 0.0
        assert limiter.allow("c") is True
        assert set(limiter._buckets) == {"b", "c"}

    def test_allow_all_respects_the_key_cap(self):
        limiter = TokenBucketLimiter(rate=3, window=60.0, max_keys=2)
        assert limiter.allow("a") is True
        assert limiter.allow_all("b", "c") is False
        assert set(limiter._buckets) == {"a"}

    def test_unbounded_by_default(self):
        limiter = TokenBucketLimiter(rate=1, window=60.0)
        for i in range(200):
            assert limiter.allow(f"k{i}") is True
        assert len(limiter._buckets) == 200
