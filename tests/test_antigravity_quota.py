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


# ── per-model effort probe (phase 05, D29) ──────────────────────────────────


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {"adjustable": True, "available": ["low", "medium", "high"]},
            ("low", "medium", "high"),
        ),
        (
            {"adjustable": True, "current": "high", "available": ["low", "high"]},
            ("low", "high"),
        ),
        # gpt-oss-120b: one fixed level, any --effort is refused.
        ({"adjustable": False}, ()),
        # Unknown names and junk are dropped; the order is ours.
        ({"available": ["max", "bogus", 7, "low", "low"]}, ("low", "max")),
        ({"available": "low"}, None),
        ({}, None),
        ([], None),
        (None, None),
    ],
)
def test_parse_model_efforts(data: Any, expected: Any) -> None:
    assert quota.parse_model_efforts(data) == expected


@pytest.mark.anyio
async def test_model_efforts_probe_runs_effort_slash_with_model(
    usage_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "UNTETHER_FAKE_AGY_SLASH_DATA",
        json.dumps(
            {"adjustable": True, "current": "high", "available": ["low", "high"]}
        ),
    )
    monkeypatch.setattr(quota, "_probe_model_efforts", quota._run_model_efforts)
    runner = _runner()
    with structlog.testing.capture_logs() as logs:
        levels = await quota.agy_model_efforts(runner, "gemini-3.1-pro")
    assert levels == ("low", "high")
    argv = json.loads(usage_env.read_text())["argv"]
    assert argv[:2] == ["-p", "/effort"]
    assert argv[-1] == "--model=gemini-3.1-pro"
    (probe,) = [e for e in logs if e["event"] == "antigravity.effort.probe"]
    assert probe["model"] == "gemini-3.1-pro"
    assert probe["available"] == ["low", "high"]
    # Default model: no --model at all.
    usage_env.unlink()
    assert await quota.agy_model_efforts(runner, None) == ("low", "high")
    assert not any(
        a.startswith("--model") for a in json.loads(usage_env.read_text())["argv"]
    )


@pytest.mark.anyio
async def test_model_efforts_probe_cached_per_version_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    async def probe(runner: Any, model: str | None) -> dict[str, Any]:
        calls.append(model)
        return {"available": ["low", "high"] if model else ["low", "medium", "high"]}

    monkeypatch.setattr(quota, "_probe_model_efforts", probe)
    key = ["/opt/agy", 1.0]
    monkeypatch.setattr(quota, "_agy_binary_key", lambda cmd: tuple(key))
    runner = _runner()
    with structlog.testing.capture_logs() as logs:
        assert await quota.agy_model_efforts(runner, "gemini-3.1-pro") == (
            "low",
            "high",
        )
        assert await quota.agy_model_efforts(runner, "gemini-3.1-pro") == (
            "low",
            "high",
        )
        assert await quota.agy_model_efforts(runner, None) == ("low", "medium", "high")
        assert await quota.agy_model_efforts(runner, None) == ("low", "medium", "high")
    assert calls == ["gemini-3.1-pro", None]
    assert [e["event"] for e in logs].count("antigravity.effort.probe") == 2
    assert quota.peek_model_efforts(runner.command(), "gemini-3.1-pro") == (
        "low",
        "high",
    )
    # agy self-updated (new mtime): the old answers are gone.
    key[1] = 2.0
    assert quota.peek_model_efforts(runner.command(), "gemini-3.1-pro") is None
    await quota.agy_model_efforts(runner, "gemini-3.1-pro")
    assert calls == ["gemini-3.1-pro", None, "gemini-3.1-pro"]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["slash", "garbage", "crash"])
async def test_model_efforts_probe_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls = 0

    async def probe(runner: Any, model: str | None) -> Any:
        nonlocal calls
        calls += 1
        if failure == "slash":
            raise quota.AgySlashError("timeout")
        if failure == "crash":
            raise RuntimeError(
                "https://accounts.example/o/oauth2?code_challenge=SECRET"
            )
        return {"unexpected": True}

    monkeypatch.setattr(quota, "_probe_model_efforts", probe)
    runner = _runner()
    with structlog.testing.capture_logs() as logs:
        assert await quota.agy_model_efforts(runner, "m") is None
    (failed,) = [e for e in logs if e["event"] == "antigravity.effort.probe_failed"]
    assert "SECRET" not in json.dumps(failed)
    assert quota.peek_model_efforts(runner.command(), "m") is None
    # Not cached: the next page open asks again.
    await quota.agy_model_efforts(runner, "m")
    assert calls == 2


@pytest.mark.anyio
async def test_peek_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(runner: Any, model: str | None) -> Any:
        raise AssertionError("peek must not probe")

    monkeypatch.setattr(quota, "_probe_model_efforts", probe)
    monkeypatch.setattr(quota, "run_agy_slash", probe)
    assert quota.peek_model_efforts(str(FAKE_AGY), "gemini-3.1-pro") is None
    assert quota.peek_model_efforts(str(FAKE_AGY), None) is None


@pytest.mark.anyio
async def test_effort_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(runner: Any, model: str | None) -> dict[str, Any]:
        return {"available": ["low"]}

    monkeypatch.setattr(quota, "_probe_model_efforts", probe)
    runner = _runner()
    for i in range(quota.EFFORT_CACHE_MAX + 20):
        await quota.agy_model_efforts(runner, f"model-{i}")
    assert len(quota._EFFORT_CACHE) <= quota.EFFORT_CACHE_MAX


# ── AG-8: a model with no default effort level (agy 1.3.3) ──────────────────

_REFUSAL = (
    'invalid model selection (--model "gemini-3.1-pro" --effort ""): '
    "--model gemini-3.1-pro requires --effort (available: low, high)"
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_REFUSAL, ["low", "high"]),
        ("requires --effort (available: medium)", ["medium"]),
        ("requires --effort (available: )", None),
        ("requires --effort (available: LOW; rm -rf)", None),
        ("unknown model", None),
        ("", None),
        (None, None),
    ],
)
def test_efforts_from_refusal(error: str | None, expected: Any) -> None:
    assert quota.efforts_from_refusal(error) == expected


def test_result_error_is_read_from_the_result_event() -> None:
    line = json.dumps(
        {"event": "result", "result": {"status": "ERROR", "error": _REFUSAL}}
    )
    out = f"error: noise\n{line}\n".encode()
    assert quota._result_error(out) == _REFUSAL
    assert quota._result_error(b"not json\n") is None
    long = json.dumps({"event": "result", "result": {"error": "x" * 5000}}).encode()
    assert len(quota._result_error(long) or "") == quota._RESULT_ERROR_CHARS


@pytest.mark.anyio
async def test_effort_probe_reads_levels_from_agys_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/effort --model=<bare id>`` is refused before it runs; the levels
    in the refusal are used, filtered to our vocabulary, with no WARNING."""

    async def refused(runner: Any, command: str, **kwargs: Any) -> Any:
        failure = quota.AgySlashError("unparseable", "rc=1")
        failure.result_error = _REFUSAL.replace("low, high", "low, bogus, high")
        raise failure

    monkeypatch.setattr(quota, "run_agy_slash", refused)
    monkeypatch.setattr(quota, "_probe_model_efforts", quota._run_model_efforts)
    quota.clear_effort_cache()
    runner = AntigravityRunner(antigravity_cmd="agy")
    with structlog.testing.capture_logs() as logs:
        levels = await quota.agy_model_efforts(runner, "gemini-3.1-pro")
    assert levels == ("low", "high")
    assert not [e for e in logs if e["log_level"] == "warning"]


@pytest.mark.anyio
async def test_effort_probe_other_failures_still_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(runner: Any, command: str, **kwargs: Any) -> Any:
        failure = quota.AgySlashError("unparseable", "rc=1")
        failure.result_error = "unknown model"
        raise failure

    monkeypatch.setattr(quota, "run_agy_slash", refused)
    monkeypatch.setattr(quota, "_probe_model_efforts", quota._run_model_efforts)
    quota.clear_effort_cache()
    runner = AntigravityRunner(antigravity_cmd="agy")
    with structlog.testing.capture_logs() as logs:
        assert await quota.agy_model_efforts(runner, "nope") is None
    (failed,) = [e for e in logs if e["event"] == "antigravity.effort.probe_failed"]
    assert failed["kind"] == "unparseable"


# ── #979: AI credits (`-p /credits`), footer peek, /stats auth route ────────


def _credits_data() -> dict[str, Any]:
    first = (FIXTURES / "credits.script").read_text().splitlines()[0]
    assert first.startswith("out:")
    return json.loads(first[4:])["command"]["data"]


def test_parse_credits_fixture() -> None:
    credits = quota.parse_credits(_credits_data())
    assert credits.remaining == 2500
    assert credits.upgrade_uri == "https://antigravity.google/g1-upgrade"


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"remaining_credits": "2500"},
        {"remaining_credits": True},
        {"remaining_credits": -1},
        {"remaining_credits": 1.5},
    ],
)
def test_parse_credits_rejects_malformed_balance(data: Any) -> None:
    assert quota.parse_credits(data).remaining is None


@pytest.mark.parametrize(
    "uri",
    [
        "http://antigravity.google/g1-upgrade",
        "https://antigravity.google.evil.example/x",
        "https://evil.example/https://antigravity.google/",
        'https://antigravity.google/"><b>x',
        "https://antigravity.google/a b",
        "https://antigravity.google/" + "a" * 300,
        "javascript:alert(1)",
        7,
        None,
    ],
)
def test_credits_upgrade_link_allowlisted(uri: Any) -> None:
    credits = quota.parse_credits({"remaining_credits": 5, "upgrade_uri": uri})
    assert credits.remaining == 5
    assert credits.upgrade_uri is None


def test_format_credits_html() -> None:
    line = quota.format_credits_html(quota.parse_credits(_credits_data()))
    assert line == (
        "AI credits: 2,500 remaining · "
        '<a href="https://antigravity.google/g1-upgrade">get more</a>'
    )
    bare = quota.format_credits_html(quota.AgyCredits(remaining=0, upgrade_uri=None))
    assert bare == "AI credits: 0 remaining"
    assert (
        quota.format_credits_html(quota.AgyCredits(remaining=None, upgrade_uri=None))
        is None
    )


@pytest.mark.anyio
async def test_credits_cache_independent_lock_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_slash(runner: Any, command: str, **kwargs: Any) -> dict:
        calls.append(command)
        return _credits_data() if command == "/credits" else _usage_data()

    monkeypatch.setattr(quota, "run_agy_slash", fake_slash)
    clock = {"t": 1000.0}
    monkeypatch.setattr(quota.time, "monotonic", lambda: clock["t"])
    runner = _runner()
    with structlog.testing.capture_logs() as logs:
        await quota.get_quota(runner)
        first = await quota.get_credits(runner)
        clock["t"] += 61  # the quota's 60 s is over; the credits' 300 s is not
        await quota.get_quota(runner)
        second = await quota.get_credits(runner)
        clock["t"] += 240
        third = await quota.get_credits(runner)
    assert calls == ["/usage", "/credits", "/usage", "/credits"]
    assert first.remaining == second.remaining == third.remaining == 2500
    assert quota._QUOTA_CACHE is not quota._CREDITS_CACHE
    assert quota._QUOTA_CACHE._lock is not quota._CREDITS_CACHE._lock
    fetched = [e for e in logs if e["event"] == "antigravity.credits.fetched"]
    assert len(fetched) == 2
    quota.reset_quota_cache()
    assert quota._CREDITS_CACHE._value is None


@pytest.mark.anyio
async def test_credits_fetch_through_the_fake_agy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = tmp_path / "record.json"
    monkeypatch.setenv("UNTETHER_FAKE_AGY_RECORD", str(record))
    monkeypatch.setenv("UNTETHER_FAKE_AGY_SLASH_DATA", json.dumps(_credits_data()))
    credits = await quota.get_credits(_runner())
    assert credits.remaining == 2500
    argv = json.loads(record.read_text())["argv"]
    assert argv == ["-p", "/credits", "--output-format", "stream-json"]


@pytest.mark.anyio
async def test_peek_quota_never_fetches_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_slash(runner: Any, command: str, **kwargs: Any) -> dict:
        calls.append(command)
        return _usage_data()

    monkeypatch.setattr(quota, "run_agy_slash", fake_slash)
    clock = {"t": 1000.0}
    monkeypatch.setattr(quota.time, "monotonic", lambda: clock["t"])
    runner = _runner()
    assert quota.peek_quota(runner.command(), max_age_s=900) is None
    assert calls == []
    await quota.get_quota(runner)
    clock["t"] += 600  # past the 60 s fetch TTL, inside the footer's 15 min
    peeked = quota.peek_quota(runner.command(), max_age_s=900)
    assert peeked is not None
    assert [g.name for g in peeked.groups] == [
        "Gemini Models",
        "Claude and GPT models",
    ]
    assert peeked.age_s == pytest.approx(600)
    assert quota.peek_quota("/some/other/agy", max_age_s=900) is None
    clock["t"] += 301
    assert quota.peek_quota(runner.command(), max_age_s=900) is None
    assert calls == ["/usage"]


@pytest.mark.anyio
async def test_peek_auth_route_from_cached_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[int] = []

    async def probe(runner: Any) -> quota.AgyConfig:
        probes.append(1)
        return quota.parse_agy_config(
            {"toolPermission": "request-review", "modelProvider": "gemini"}
        )

    monkeypatch.setattr(quota, "_probe_agy_config", probe)
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    assert quota.peek_auth_route() is None  # nothing cached: no line, no probe
    assert probes == []
    await quota.agy_config(_runner())
    assert quota.peek_auth_route() == "api_key"
    assert probes == [1]
    quota.clear_config_cache()
    assert quota.peek_auth_route() is None


@pytest.mark.anyio
async def test_peek_auth_route_ignores_an_expired_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(quota.time, "monotonic", lambda: clock["t"])
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    await quota.agy_config(_runner())
    assert quota.peek_auth_route() == "oauth"
    clock["t"] += quota.CONFIG_TTL_S + 1
    assert quota.peek_auth_route() is None


def _snapshot(
    *buckets: tuple[str, float], name: str = "Gemini Models", age_s: float = 0.0
) -> quota.QuotaSnapshot:
    return quota.QuotaSnapshot(
        groups=[
            quota.QuotaGroup(
                name=name,
                buckets=tuple(
                    quota.QuotaBucket(
                        id=window,
                        name=window,
                        window=window,
                        utilisation=used,
                        resets_at="2026-10-08T05:00:00Z",
                    )
                    for window, used in buckets
                ),
            )
        ],
        age_s=age_s,
    )


_NOW = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


def test_quota_footer_compact_line_per_group() -> None:
    groups = quota.parse_quota(_usage_data())
    lines = quota.format_quota_footer(
        quota.QuotaSnapshot(groups=groups, age_s=0.0), always_show=True, now=_NOW
    )
    assert lines == [
        "⚡ Gemini Models 5h: 0% | wk: 0%",
        "⚡ Claude and GPT models 5h: 0% | wk: 0%",
    ]
    # below 70 % nothing shows unless the always-on line was asked for
    assert (
        quota.format_quota_footer(
            quota.QuotaSnapshot(groups=groups, age_s=0.0), always_show=False, now=_NOW
        )
        == []
    )


def test_quota_footer_thresholds_and_reset_times() -> None:
    def footer(*buckets: tuple[str, float], **kwargs: Any) -> list[str]:
        return quota.format_quota_footer(
            _snapshot(*buckets, **kwargs), always_show=False, now=_NOW
        )

    assert footer(("5h", 69.0), ("weekly", 10.0)) == []
    assert footer(("5h", 72.0), ("weekly", 10.0)) == [
        "⚡ Gemini Models 5h: 72% (5h 0m) | wk: 10%"
    ]
    # either window can cross the line: the weekly limit blocks a run too
    assert footer(("5h", 5.0), ("weekly", 91.0)) == [
        "⚠️ Gemini Models 5h: 5% | wk: 91% (5h 0m)"
    ]
    assert footer(("5h", 100.0), ("weekly", 40.0)) == [
        "🛑 Gemini Models 5h limit hit — resets in 5h 0m"
    ]
    assert footer(("5h", 80.0), age_s=600.0) == [
        "⚡ Gemini Models 5h: 80% (5h 0m) · 10m ago"
    ]


def test_quota_footer_group_name_is_one_short_line() -> None:
    (line,) = quota.format_quota_footer(
        _snapshot(("5h", 80.0), name="Evil\n↩️ `agy --conversation x`" + "y" * 80),
        always_show=False,
        now=_NOW,
    )
    assert "\n" not in line and "`" not in line
    assert len(line) < 90
