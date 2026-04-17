"""Tests for the subscription-usage fetch cache.

The cache must:
* Serve cached values within the 60-second TTL without re-calling the fetch.
* Fetch on miss and store the result.
* Return stale cached data when the underlying fetch raises, and propagate the
  exception when no cache has yet been populated.
"""

from __future__ import annotations

import pytest

from untether.utils import usage_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    usage_cache.reset_cache()
    yield
    usage_cache.reset_cache()


@pytest.mark.anyio
async def test_cache_miss_calls_fetch_and_stores(monkeypatch):
    calls = 0
    payload = {"five_hour": {"utilization": 42.0, "resets_at": "x"}}

    async def _fake_fetch():
        nonlocal calls
        calls += 1
        return payload

    monkeypatch.setattr(
        "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
    )

    result = await usage_cache.fetch_claude_usage_cached()
    assert result is payload
    assert calls == 1


@pytest.mark.anyio
async def test_cache_hit_does_not_call_fetch(monkeypatch):
    calls = 0
    payload = {"five_hour": {"utilization": 42.0, "resets_at": "x"}}

    async def _fake_fetch():
        nonlocal calls
        calls += 1
        return payload

    monkeypatch.setattr(
        "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
    )

    first = await usage_cache.fetch_claude_usage_cached()
    second = await usage_cache.fetch_claude_usage_cached()
    assert first is second
    assert calls == 1


@pytest.mark.anyio
async def test_ttl_miss_refetches(monkeypatch):
    calls = 0
    payload_a = {"five_hour": {"utilization": 10.0, "resets_at": "a"}}
    payload_b = {"five_hour": {"utilization": 20.0, "resets_at": "b"}}
    responses = [payload_a, payload_b]

    async def _fake_fetch():
        nonlocal calls
        calls += 1
        return responses.pop(0)

    monkeypatch.setattr(
        "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
    )

    # Freeze time via monkeypatching time.monotonic inside the cache module.
    now = 1000.0

    def _fake_monotonic():
        return now

    monkeypatch.setattr("untether.utils.usage_cache.time.monotonic", _fake_monotonic)

    first = await usage_cache.fetch_claude_usage_cached()
    assert first is payload_a
    now = 1000.0 + usage_cache._TTL_SECONDS + 1.0
    second = await usage_cache.fetch_claude_usage_cached()
    assert second is payload_b
    assert calls == 2


@pytest.mark.anyio
async def test_stale_while_error(monkeypatch):
    """If the fetch raises and we have a cached value, return it."""
    payload = {"five_hour": {"utilization": 42.0, "resets_at": "x"}}
    raise_next = {"flag": False}

    async def _fake_fetch():
        if raise_next["flag"]:
            raise RuntimeError("boom")
        return payload

    monkeypatch.setattr(
        "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
    )

    now = 1000.0

    def _fake_monotonic():
        return now

    monkeypatch.setattr("untether.utils.usage_cache.time.monotonic", _fake_monotonic)

    first = await usage_cache.fetch_claude_usage_cached()
    assert first is payload

    # Advance past the TTL and switch the fetch to raise.
    now = 1000.0 + usage_cache._TTL_SECONDS + 1.0
    raise_next["flag"] = True

    second = await usage_cache.fetch_claude_usage_cached()
    assert second is payload  # stale fallback


@pytest.mark.anyio
async def test_error_without_cache_propagates(monkeypatch):
    """When the cache is empty and fetch raises, the caller sees the error."""

    async def _fake_fetch():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
    )

    with pytest.raises(RuntimeError, match="boom"):
        await usage_cache.fetch_claude_usage_cached()
