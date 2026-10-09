"""Antigravity ``/usage`` quota (#558 phase 04): the zero-token
``agy -p /usage`` probe, its parser and formatter, and the private 60 s cache.

The probe runs the fake agy (``tests/fake_clis/fake_agy.py``); the payload is
the agy 1.3.x capture in ``tests/fixtures/antigravity/usage.script``.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from untether.runners.antigravity import AntigravityRunner
from untether.utils import antigravity_quota as quota
from untether.utils import usage_cache

FAKE_AGY = Path(__file__).parent / "fake_clis" / "fake_agy.py"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


def _usage_data() -> dict[str, Any]:
    first = (FIXTURES / "usage.script").read_text().splitlines()[0]
    assert first.startswith("out:")
    return json.loads(first[4:])["command"]["data"]


def _runner() -> AntigravityRunner:
    assert os.access(FAKE_AGY, os.X_OK), f"chmod +x {FAKE_AGY}"
    return AntigravityRunner(antigravity_cmd=str(FAKE_AGY))


@pytest.fixture
def usage_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SLASH_DATA", json.dumps(_usage_data()))
    return record


# ── parse ───────────────────────────────────────────────────────────────────


def test_parse_usage_fixture_groups_per_group_no_merge() -> None:
    groups = quota.parse_quota(_usage_data())
    assert [g.name for g in groups] == ["Gemini Models", "Claude and GPT models"]
    gemini, third_party = groups
    # 5-hour first, then weekly; each group keeps its own buckets (audit R5)
    assert [b.window for b in gemini.buckets] == ["5h", "weekly"]
    assert [b.window for b in third_party.buckets] == ["5h", "weekly"]
    weekly = gemini.buckets[1]
    assert weekly.utilisation == pytest.approx(0.278, abs=0.01)
    assert weekly.resets_at == "2026-10-09T03:14:27Z"
    assert all(b.utilisation == 0 for b in third_party.buckets)
    # no cross-group "worst bucket" field anywhere
    assert not hasattr(quota, "worst_bucket")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"groups": "nope"},
        {"groups": [None, 3, {"name": 5}]},
        {"groups": [{"name": "G", "buckets": "x"}]},
    ],
)
def test_parse_quota_tolerates_garbage(data: Any) -> None:
    groups = quota.parse_quota(data)
    assert all(isinstance(g, quota.QuotaGroup) for g in groups)
    assert all(not g.buckets for g in groups)


def test_parse_quota_caps_and_clamps() -> None:
    bucket = {
        "id": "b",
        "name": "N" * 500,
        "window": "5h",
        "remaining_fraction": -3,
        "reset_time": "x" * 500,
    }
    data = {
        "groups": [
            {"name": "G" * 500, "buckets": [bucket] * 50},
        ]
        * 50
    }
    groups = quota.parse_quota(data)
    assert len(groups) == quota.MAX_GROUPS
    assert len(groups[0].buckets) == quota.MAX_BUCKETS
    assert len(groups[0].name) <= quota.MAX_NAME_CHARS
    b = groups[0].buckets[0]
    assert b.utilisation == 100.0
    assert b.resets_at is None  # unparseable / oversized reset dropped
    assert len(b.name) <= quota.MAX_NAME_CHARS


@pytest.mark.parametrize("bad", [True, "0.5", float("nan"), float("inf"), None])
def test_parse_quota_skips_non_numeric_fraction(bad: Any) -> None:
    data = {
        "groups": [
            {
                "name": "G",
                "buckets": [{"id": "b", "window": "5h", "remaining_fraction": bad}],
            }
        ]
    }
    assert quota.parse_quota(data)[0].buckets == ()


# ── format ──────────────────────────────────────────────────────────────────


def test_format_quota_html_layout() -> None:
    now = datetime(2026, 10, 8, 7, 33, 0, tzinfo=UTC)
    lines = quota.format_quota_html(quota.parse_quota(_usage_data()), now=now)
    text = "\n".join(lines)
    assert lines[0] == "<b>Gemini Models</b>"
    assert "• 5-hour: ░░░░░░░░░░ 0% used" in text
    # 0.28 % used → shown as 0 %, but it has started, so the reset shows
    assert "• Weekly: ░░░░░░░░░░ 0% used · resets in 19h 41m" in text
    assert "<b>Claude and GPT models</b>" in text
    # untouched buckets never claim a reset
    assert text.count("resets in") == 1


def test_render_escapes_html_in_group_names() -> None:
    data = {
        "groups": [
            {
                "name": "<b>Gemini</b> & <script>",
                "buckets": [
                    {
                        "id": "x",
                        "name": "<i>odd</i>",
                        "window": "monthly<br>",
                        "remaining_fraction": 0.5,
                    }
                ],
            }
        ]
    }
    text = "\n".join(quota.format_quota_html(quota.parse_quota(data)))
    assert "<script>" not in text
    assert "&lt;b&gt;Gemini&lt;/b&gt; &amp; &lt;script&gt;" in text
    assert "&lt;i&gt;odd&lt;/i&gt;" in text
    assert "<i>" not in text and "<br>" not in text


def test_format_quota_html_empty() -> None:
    assert quota.format_quota_html([]) == ["agy returned no quota groups."]


# ── run_agy_slash ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_agy_slash_uses_guard_filtered_env_temp_cwd_devnull_no_conversation(
    usage_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-leak")
    data = await quota.run_agy_slash(_runner(), "/usage")
    assert [g["name"] for g in data["groups"]] == [
        "Gemini Models",
        "Claude and GPT models",
    ]
    rec = json.loads(usage_env.read_text())
    assert rec["argv"] == ["-p", "/usage", "--output-format", "stream-json"]
    assert rec["stdin"] == ""
    assert Path(rec["cwd"]).name.startswith("untether-agy-")
    assert not Path(rec["cwd"]).exists()
    assert "UNRELATED_SECRET" not in rec["env_keys"]


@pytest.mark.anyio
async def test_run_agy_slash_explicit_cwd_and_no_gate_env(
    usage_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    # REVIEW-2 m10: a gate variable in Untether's own env (the UNTETHER_
    # prefix passes the filter) must never reach a slash probe.
    monkeypatch.setenv("UNTETHER_AGY_GATE_SOCKET", "/tmp/gate.sock")
    monkeypatch.setenv("UNTETHER_AGY_GATE_TOKEN", "secret")
    await quota.run_agy_slash(_runner(), "/usage", cwd=project)
    rec = json.loads(usage_env.read_text())
    assert Path(rec["cwd"]) == project
    assert project.exists()  # an explicit cwd is never removed
    assert not [k for k in rec["env_keys"] if k.startswith("UNTETHER_AGY_GATE")]
    assert "UNTETHER_FAKE_AGY_RECORD" in rec["env_keys"]


@pytest.mark.anyio
async def test_fetch_not_signed_in_raises_fast(
    usage_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SLASH_UNAUTH", "1")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    start = time.monotonic()
    with pytest.raises(quota.AntigravityNotSignedIn):
        await quota.run_agy_slash(_runner(), "/usage", timeout_s=30)
    assert time.monotonic() - start < 5


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0)
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.anyio
async def test_fetch_timeout_raises_timeout_and_kills_descendants(
    usage_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_HOLD_S", "60")
    monkeypatch.setenv("UNTETHER_FAKE_AGY_CHILD_PIDFILE", str(pidfile))
    start = time.monotonic()
    with pytest.raises(quota.AgySlashError) as exc:
        await quota.run_agy_slash(_runner(), "/usage", timeout_s=1.5)
    assert exc.value.kind == "timeout"
    assert time.monotonic() - start < 12
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(child):  # don't leak it if the assertion fails
        os.kill(child, signal.SIGKILL)
        pytest.fail("descendant survived the timeout kill")


# ── cache ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_quota_cache_ttl_and_own_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    claude_lock = usage_cache._lock

    async def fake_slash(runner: Any, command: str, **kwargs: Any) -> dict:
        calls.append(command)
        # Claude's usage lock is never held while agy is probed
        if claude_lock is not None:
            assert not claude_lock.locked()
        return _usage_data()

    monkeypatch.setattr(quota, "run_agy_slash", fake_slash)
    clock = {"t": 1000.0}
    monkeypatch.setattr(quota.time, "monotonic", lambda: clock["t"])
    runner = _runner()
    with structlog.testing.capture_logs() as logs:
        first = await quota.get_quota(runner)
        clock["t"] += 59
        second = await quota.get_quota(runner)
        clock["t"] += 2
        third = await quota.get_quota(runner)
    assert calls == ["/usage", "/usage"]
    assert first.groups == second.groups == third.groups
    assert second.age_s == pytest.approx(59)
    fetched = [e for e in logs if e["event"] == "antigravity.quota.fetched"]
    assert len(fetched) == 2
    assert "duration_ms" in fetched[0]
    assert usage_cache._lock is claude_lock  # Claude's cache untouched
    assert quota.quota_cache_stats().last_success_wall is not None


@pytest.mark.anyio
async def test_quota_cache_reset_and_error_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(runner: Any, command: str, **kwargs: Any) -> dict:
        raise quota.AgySlashError("timeout")

    monkeypatch.setattr(quota, "run_agy_slash", failing)
    with pytest.raises(quota.AgySlashError):
        await quota.get_quota(_runner())
    assert quota.quota_cache_stats().last_error_kind == "timeout"
    quota.reset_quota_cache()
    stats = quota.quota_cache_stats()
    assert stats.last_error_kind is None and stats.last_success_wall is None


@pytest.mark.anyio
async def test_quota_failure_log_is_sanitised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(runner: Any, command: str, **kwargs: Any) -> dict:
        raise quota.AgySlashError(
            "unparseable",
            "see https://accounts.google.com/o/oauth2?code=abc /home/nathan/x",
        )

    monkeypatch.setattr(quota, "run_agy_slash", failing)
    with structlog.testing.capture_logs() as logs, pytest.raises(quota.AgySlashError):
        await quota.get_quota(_runner())
    failed = [e for e in logs if e["event"] == "antigravity.quota.failed"]
    assert len(failed) == 1
    assert failed[0]["kind"] == "unparseable"
    assert "[url]" in failed[0]["detail"]
    assert "accounts.google.com" not in failed[0]["detail"]
    assert "code=abc" not in failed[0]["detail"]
    assert "/home/nathan" not in failed[0]["detail"]
    assert failed[0]["log_level"] == "warning"


@pytest.mark.anyio
async def test_quota_not_signed_in_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def unauth(runner: Any, command: str, **kwargs: Any) -> dict:
        calls.append(1)
        raise quota.AntigravityNotSignedIn()

    monkeypatch.setattr(quota, "run_agy_slash", unauth)
    for _ in range(2):
        with pytest.raises(quota.AntigravityNotSignedIn):
            await quota.get_quota(_runner())
    assert len(calls) == 2  # signing in takes effect on the next /usage


def test_time_until_formats() -> None:
    now = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    fmt = quota._time_until
    assert fmt((now + timedelta(minutes=5)).isoformat(), now) == "5m"
    assert fmt((now + timedelta(hours=4, minutes=58)).isoformat(), now) == "4h 58m"
    assert fmt((now + timedelta(days=6, hours=23)).isoformat(), now) == "6d 23h"
    assert fmt("2026-10-07T00:00:00Z", now) == "0m"
