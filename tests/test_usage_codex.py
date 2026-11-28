"""``/usage`` for Codex (#966): plan quota from a zero-token app-server read
(``runners/codex_reads``), then the #417 last-session block."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_command_engine_gates import (
    FakeCommandContext,
    FakeTransportRuntime,
    _codex_totals,
    _seed_session,
)
from untether.runners import codex_reads as reads
from untether.runners.codex import CodexRunner
from untether.telegram.commands import usage as usage_mod
from untether.telegram.commands.usage import UsageCommand

FIXTURE = (
    Path(__file__).parent / "fixtures" / "codex_app_server" / "reads-0.171.0.jsonl"
)


def _fixture_usage() -> reads.CodexUsage:
    """The Pro read from the 0.171.0 probe transcript."""
    by_id: dict[int, dict] = {}
    methods: dict[int, str] = {}
    for line in FIXTURE.read_text().splitlines():
        msg = json.loads(line)["msg"]
        if "method" in msg and "id" in msg:
            methods[msg["id"]] = msg["method"]
        elif "id" in msg and "result" in msg:
            by_id[msg["id"]] = msg["result"]
    account = next(by_id[i] for i, m in methods.items() if m == "account/read")
    limits = next(
        by_id[i] for i, m in methods.items() if m == "account/rateLimits/read"
    )
    return reads.parse_usage(account, limits).build()


@pytest.fixture(autouse=True)
def _history(monkeypatch: pytest.MonkeyPatch) -> None:
    from untether.telegram.commands import export as export_mod

    monkeypatch.setattr(export_mod, "_SESSION_HISTORY", {})


def _runner() -> CodexRunner:
    return CodexRunner(codex_cmd="codex", extra_args=[])


async def _reply(monkeypatch: pytest.MonkeyPatch, outcome: Any, args: str = "") -> str:
    calls: list[Any] = []

    async def get_usage(runner: CodexRunner) -> tuple[reads.CodexUsage, float]:
        calls.append(runner)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, 0.0

    monkeypatch.setattr(reads, "get_usage", get_usage)

    async def boom() -> None:
        raise AssertionError("Claude's usage API must not be called for codex")

    monkeypatch.setattr(usage_mod, "fetch_claude_usage", boom)
    ctx = FakeCommandContext(
        args_text=args,
        runtime=FakeTransportRuntime(default_engine="codex", runner=_runner()),
    )
    result = await UsageCommand().handle(ctx)  # type: ignore[arg-type]
    assert result is not None and result.parse_mode == "HTML"
    assert len(calls) == 1
    return result.text


@pytest.mark.anyio
async def test_pro_read_renders_plan_window_credits_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = _fixture_usage()
    text = await _reply(monkeypatch, usage)
    lines = text.splitlines()
    assert lines[0] == "📊 <b>Codex usage</b> · Pro plan"
    assert lines[1].startswith("7d: █░░░░░░░░░ 8%")
    assert "· resets in" in lines[1]
    assert lines[2] == "Credits: 62,500 · Rate-limit resets available: 3"
    assert "no quota spent" in lines[3]
    assert "🛑" not in text
    assert "No completed codex run in this chat" in text


@pytest.mark.anyio
async def test_last_session_block_still_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_session("sid-1", "codex", _codex_totals(50000, 900, 20000))
    text = await _reply(monkeypatch, _fixture_usage())
    assert "<b>Last session in this chat</b>" in text
    assert "50k in (20k cached) · 900 out" in text
    assert text.splitlines()[-1] == "Transcript: /export"


@pytest.mark.anyio
async def test_blocked_and_multi_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = reads.CodexUsage(
        auth_kind="chatgpt",
        plan="<team>",
        allowed=False,
        buckets=(
            reads.Bucket(
                "codex", (reads.Window("5h", 100, None),), "rate_limit_reached"
            ),
            reads.Bucket("<Sol>", (reads.Window("7d", 40, None),)),
        ),
        unlimited=True,
    )
    text = await _reply(monkeypatch, usage)
    assert "· &lt;team&gt; plan" in text
    assert (
        "🛑 Codex is blocking new turns until the limit resets (usage limit reached)."
        in text
    )
    assert "<b>codex</b>" in text and "<b>&lt;Sol&gt;</b>" in text
    assert "Credits: unlimited" in text
    assert "resets available" not in text


@pytest.mark.anyio
async def test_allowed_none_says_nothing_about_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = reads.CodexUsage(
        auth_kind="chatgpt",
        plan="plus",
        allowed=None,
        buckets=(reads.Bucket("codex", (reads.Window("5h", 100, None),)),),
    )
    text = await _reply(monkeypatch, usage)
    assert "🛑" not in text
    assert "Credits" not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("apiKey", "an API key"),
        ("amazonBedrock", "Amazon Bedrock"),
        ("gcp", "Google Cloud"),
    ],
)
async def test_non_chatgpt_auth(
    monkeypatch: pytest.MonkeyPatch, kind: str, words: str
) -> None:
    usage = reads.parse_usage({"account": {"type": kind}}, None).build()
    text = await _reply(monkeypatch, usage)
    assert f"Codex is using {words}, so there's no ChatGPT plan quota to show." in text
    assert "no quota spent" not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exc", "line"),
    [
        (
            reads.CodexNotSignedIn(),
            "Codex isn't signed in on this host. Run /auth codex, then try again.",
        ),
        (
            reads.CodexReadUnavailable("0.160.0"),
            "This Codex CLI can't report quota (needs codex-cli 0.169.0 or later).",
        ),
        (
            reads.CodexReadError("timeout"),
            "Codex didn't answer within 10 s — try again shortly.",
        ),
        (reads.CodexReadError("exited"), "Couldn't read Codex's quota (exited)."),
        (
            reads.CodexReadError("prespawn_blocked"),
            "Couldn't read Codex's quota (the host is short of memory or at its"
            " run limit right now; try /usage again in a minute).",
        ),
        (
            RuntimeError("x"),
            "Couldn't read Codex's quota (unexpected error) — try /usage again shortly.",
        ),
    ],
)
async def test_failure_lines(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, line: str
) -> None:
    _seed_session("sid-2", "codex", _codex_totals(10, 1))
    text = await _reply(monkeypatch, exc)
    assert text.splitlines()[1] == line
    assert "Session total:" in text


@pytest.mark.anyio
async def test_debug_section_has_no_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _reply(monkeypatch, _fixture_usage(), args="debug")
    assert "<b>🔧 debug</b>" in text
    assert "• codex version:" in text and "• auth:" in text
    assert "redacted" not in text and "@" not in text
