"""Tests for engine-gated commands: /usage and /planmode.

These commands must check the current engine and either refuse or adjust
behaviour for engines that don't support the feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from untether.telegram.commands._resolve_engine import resolve_effective_engine
from untether.telegram.commands.planmode import PlanModeCommand
from untether.telegram.commands.usage import UsageCommand


@dataclass
class FakeMessage:
    channel_id: int = 100
    message_id: int = 1


@dataclass
class FakeRunContext:
    project: str | None = "test"


class FakeTransportRuntime:
    def __init__(
        self, *, default_engine: str = "claude", project_engine: str | None = None
    ):
        self._default_engine = default_engine
        self._project_engine = project_engine

    @property
    def default_engine(self) -> str:
        return self._default_engine

    def default_context_for_chat(
        self, chat_id: int | str | None
    ) -> FakeRunContext | None:
        return FakeRunContext()

    def project_default_engine(self, context: FakeRunContext | None) -> str | None:
        return self._project_engine


@dataclass
class FakeCommandContext:
    command: str = ""
    text: str = ""
    args_text: str = ""
    args: tuple[str, ...] = ()
    message: FakeMessage | None = None
    reply_to: FakeMessage | None = None
    reply_text: str | None = None
    config_path: Path | None = None
    plugin_config: dict = None  # type: ignore[assignment]
    runtime: FakeTransportRuntime | None = None
    executor: object = None

    def __post_init__(self):
        if self.message is None:
            self.message = FakeMessage()
        if self.plugin_config is None:
            self.plugin_config = {}
        if self.runtime is None:
            self.runtime = FakeTransportRuntime()


# ---------------------------------------------------------------------------
# _resolve_engine helper
# ---------------------------------------------------------------------------


class TestResolveEffectiveEngine:
    @pytest.mark.anyio
    async def test_returns_global_default_when_no_overrides(self):
        ctx = FakeCommandContext(runtime=FakeTransportRuntime(default_engine="codex"))
        result = await resolve_effective_engine(ctx)  # type: ignore[arg-type]
        assert result == "codex"

    @pytest.mark.anyio
    async def test_returns_project_default_over_global(self):
        ctx = FakeCommandContext(
            runtime=FakeTransportRuntime(
                default_engine="claude", project_engine="codex"
            )
        )
        result = await resolve_effective_engine(ctx)  # type: ignore[arg-type]
        assert result == "codex"


# ---------------------------------------------------------------------------
# /usage engine gate
# ---------------------------------------------------------------------------


class TestUsageEngineGate:
    @pytest.mark.anyio
    async def test_usage_blocked_for_codex(self):
        ctx = FakeCommandContext(
            runtime=FakeTransportRuntime(default_engine="codex"),
        )
        cmd = UsageCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "not available" in result.text.lower()
        assert "codex" in result.text.lower()
        assert "/export" in result.text

    @pytest.mark.anyio
    async def test_usage_blocked_for_pi(self):
        ctx = FakeCommandContext(
            runtime=FakeTransportRuntime(default_engine="pi"),
        )
        cmd = UsageCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "not available" in result.text.lower()
        assert "pi" in result.text.lower()

    @pytest.mark.anyio
    async def test_usage_blocked_for_opencode(self):
        ctx = FakeCommandContext(
            runtime=FakeTransportRuntime(default_engine="opencode"),
        )
        cmd = UsageCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "not available" in result.text.lower()

    @pytest.mark.anyio
    async def test_usage_allowed_for_claude_attempts_fetch(self):
        """For Claude, /usage should attempt the actual fetch (may fail without
        credentials in test env, but shouldn't be blocked by engine gate)."""
        ctx = FakeCommandContext(
            runtime=FakeTransportRuntime(default_engine="claude"),
        )
        cmd = UsageCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        # Should get past the engine gate — either shows data or credential error
        assert "not available" not in result.text.lower()


# ---------------------------------------------------------------------------
# /usage for non-subscription engines (#417)
# ---------------------------------------------------------------------------


@pytest.fixture
def export_history(monkeypatch: pytest.MonkeyPatch) -> dict:
    from untether.telegram.commands import export as export_mod

    history: dict = {}
    monkeypatch.setattr(export_mod, "_SESSION_HISTORY", history)
    return history


def _seed_session(
    session_id: str, engine: str, usage: dict | None, *, channel_id: int = 100
) -> None:
    from untether.telegram.commands.export import (
        record_session_event,
        record_session_usage,
    )

    record_session_event(
        session_id, {"type": "started", "engine": engine}, channel_id=channel_id
    )
    if usage is not None:
        record_session_usage(session_id, usage, channel_id=channel_id)


async def _usage_reply(engine: str, args: str = "") -> str:
    ctx = FakeCommandContext(
        args_text=args, runtime=FakeTransportRuntime(default_engine=engine)
    )
    result = await UsageCommand().handle(ctx)  # type: ignore[arg-type]
    assert result is not None
    assert result.parse_mode == "HTML"
    return result.text


def _codex_totals(inp: int, out: int, cached: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "output_tokens": out,
    }


class TestUsageNonClaudeTokens:
    @pytest.mark.anyio
    async def test_usage_codex_shows_last_session_tokens(self, export_history):
        from untether.session_costs import get_session_cost_ledger

        sid = "019dc356-aaaa-bbbb"
        _seed_session(sid, "codex", _codex_totals(167508, 1583))
        ledger = get_session_cost_ledger()
        ledger.record_tokens(
            "codex",
            sid,
            _codex_totals(155000, 1200),
            scope="thread_cumulative",
            resumed=False,
        )
        ledger.record_tokens(
            "codex",
            sid,
            _codex_totals(167508, 1583),
            scope="thread_cumulative",
            resumed=True,
        )
        text = await _usage_reply("codex")
        assert text.startswith("📊 <b>codex</b> · last session in this chat")
        assert "Session total:" in text
        assert "168k in" in text
        assert "2 runs" in text
        assert "Last run:" in text
        assert "13k in" in text
        assert "/export" in text
        assert "Usage tracking is not available" not in text

    @pytest.mark.anyio
    async def test_usage_codex_falls_back_to_export_usage(self, export_history):
        _seed_session("sid-fallback", "codex", _codex_totals(50000, 900, 20000))
        text = await _usage_reply("codex")
        assert "<b>Session total:</b> 50k in (20k cached) · 900 out" in text
        assert "Last run:" not in text

    @pytest.mark.anyio
    async def test_usage_non_claude_never_fetches_claude_usage(
        self, export_history, monkeypatch
    ):
        from untether.telegram.commands import usage as usage_mod

        async def boom() -> None:
            raise AssertionError("fetch_claude_usage must not run for codex")

        monkeypatch.setattr(usage_mod, "fetch_claude_usage", boom)
        _seed_session("sid-x", "codex", _codex_totals(10, 1))
        text = await _usage_reply("codex", args="debug")
        assert "Session total:" in text

    @pytest.mark.anyio
    async def test_usage_opencode_shows_cost_and_tokens(self, export_history):
        from untether.session_costs import get_session_cost_ledger

        sid = "ses_oc"
        _seed_session(
            sid,
            "opencode",
            {
                "total_cost_usd": 0.0123,
                "usage": {"input_tokens": 2000, "output_tokens": 100},
            },
        )
        ledger = get_session_cost_ledger()
        for inp, out in ((5000, 300), (2000, 100)):
            ledger.record_tokens(
                "opencode",
                sid,
                {"input_tokens": inp, "output_tokens": out},
                scope="per_run",
                resumed=True,
            )
        text = await _usage_reply("opencode")
        assert "2 runs" in text
        assert "<b>Session total:</b> 7k in · 400 out" in text
        assert "Last run cost:</b> $0.0123" in text

    @pytest.mark.anyio
    async def test_usage_opencode_cache_read_is_additive(self, export_history):
        from untether.session_costs import get_session_cost_ledger

        _seed_session("ses_cr", "opencode", None)
        get_session_cost_ledger().record_tokens(
            "opencode",
            "ses_cr",
            {"input_tokens": 22443, "cache_read_tokens": 21415, "output_tokens": 118},
            scope="per_run",
            resumed=False,
        )
        text = await _usage_reply("opencode")
        assert "22k in + 21k cache read" in text
        assert "(21k cache read)" not in text

        _seed_session("sid-cx", "codex", _codex_totals(22443, 118, cached=21415))
        text = await _usage_reply("codex")
        assert "22k in (21k cached)" in text

    @pytest.mark.anyio
    async def test_usage_ignores_other_engine_sessions(self, export_history):
        _seed_session("claude-sess", "claude", {"total_cost_usd": 1.0})
        text = await _usage_reply("codex")
        assert "not available" in text
        assert "no completed codex run" in text

    @pytest.mark.anyio
    async def test_usage_html_escapes_session_id(self, export_history):
        _seed_session("<b>x</b>", "codex", _codex_totals(10, 1))
        text = await _usage_reply("codex")
        assert "&lt;b&gt;x&lt;/b&gt;" in text
        assert "<code><b>" not in text

    @pytest.mark.anyio
    async def test_usage_and_export_agree_for_codex(self, export_history):
        """The issue's ask: /usage's session total equals /export's header."""
        import re

        from untether.session_costs import get_session_cost_ledger
        from untether.telegram.commands.export import (
            _format_export_markdown,
            latest_session_for_chat,
        )

        sid = "sid-agree"
        _seed_session(sid, "codex", _codex_totals(167508, 1583))
        ledger = get_session_cost_ledger()
        ledger.record_tokens(
            "codex",
            sid,
            _codex_totals(155000, 1200),
            scope="thread_cumulative",
            resumed=False,
        )
        ledger.record_tokens(
            "codex",
            sid,
            _codex_totals(167508, 1583),
            scope="thread_cumulative",
            resumed=True,
        )
        sess = latest_session_for_chat(100, engine="codex")
        assert sess is not None
        md = _format_export_markdown(sid, sess.events, sess.usage)
        m = re.search(r"(\d+) in / (\d+) out tokens", md)
        assert m is not None
        tokens = ledger.session_tokens("codex", sid)
        assert tokens is not None
        assert int(m.group(1)) == tokens.totals["input_tokens"]
        assert int(m.group(2)) == tokens.totals["output_tokens"]

    @pytest.mark.anyio
    async def test_usage_baseline_unknown_label(self, export_history):
        from untether.session_costs import get_session_cost_ledger

        _seed_session("sid-cli", "codex", _codex_totals(90000, 800))
        get_session_cost_ledger().record_tokens(
            "codex",
            "sid-cli",
            _codex_totals(90000, 800),
            scope="thread_cumulative",
            resumed=True,
        )
        text = await _usage_reply("codex")
        assert "includes earlier runs outside Untether" in text


# ---------------------------------------------------------------------------
# /planmode engine gate
# ---------------------------------------------------------------------------


class TestPlanModeEngineGate:
    @pytest.mark.anyio
    async def test_planmode_blocked_for_codex(self):
        ctx = FakeCommandContext(
            args_text="on",
            config_path=Path("/tmp/fake.toml"),
            runtime=FakeTransportRuntime(default_engine="codex"),
        )
        cmd = PlanModeCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "only available for claude" in result.text.lower()
        assert "codex" in result.text.lower()

    @pytest.mark.anyio
    async def test_planmode_blocked_for_codex_with_config_hint(self):
        ctx = FakeCommandContext(
            args_text="on",
            config_path=Path("/tmp/fake.toml"),
            runtime=FakeTransportRuntime(default_engine="codex"),
        )
        cmd = PlanModeCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "approval policy" in result.text.lower()

    @pytest.mark.anyio
    async def test_planmode_blocked_for_gemini_with_config_hint(self):
        ctx = FakeCommandContext(
            args_text="on",
            config_path=Path("/tmp/fake.toml"),
            runtime=FakeTransportRuntime(default_engine="gemini"),
        )
        cmd = PlanModeCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "approval policy" in result.text.lower()

    @pytest.mark.anyio
    async def test_planmode_blocked_for_pi(self):
        ctx = FakeCommandContext(
            args_text="on",
            config_path=Path("/tmp/fake.toml"),
            runtime=FakeTransportRuntime(default_engine="pi"),
        )
        cmd = PlanModeCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "only available for claude" in result.text.lower()
        # Pi doesn't have approval policy either, so no hint
        assert "approval policy" not in result.text.lower()

    @pytest.mark.anyio
    async def test_planmode_blocked_for_project_engine_codex(self):
        """Even if global default is claude, project engine codex should block."""
        ctx = FakeCommandContext(
            args_text="on",
            config_path=Path("/tmp/fake.toml"),
            runtime=FakeTransportRuntime(
                default_engine="claude", project_engine="codex"
            ),
        )
        cmd = PlanModeCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "only available for claude" in result.text.lower()


class TestUsageDebugMode:
    """#410: ``/usage debug`` appends a debug section with cache + token info."""

    @pytest.mark.anyio
    async def test_debug_section_appended_on_success(self, monkeypatch):
        from untether.telegram.commands.usage import UsageCommand
        from untether.utils import usage_cache

        usage_cache.reset_cache()

        async def _fake_fetch(*a, **kw):
            return {
                "five_hour": {
                    "utilization": 12.0,
                    "resets_at": "2030-01-01T00:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 4.0,
                    "resets_at": "2030-01-08T00:00:00+00:00",
                },
            }

        monkeypatch.setattr(
            "untether.telegram.commands.usage.fetch_claude_usage", _fake_fetch
        )
        monkeypatch.setattr(
            "untether.telegram.commands.usage._read_token_expiry_ms",
            lambda: 9_999_999_999_000,  # year 2286 — never expired
        )

        ctx = FakeCommandContext(
            runtime=FakeTransportRuntime(default_engine="claude"),
            args_text="debug",
        )
        cmd = UsageCommand()
        result = await cmd.handle(ctx)  # type: ignore[arg-type]
        assert result is not None
        assert "debug" in result.text.lower()
        assert "OAuth token" in result.text
        assert "schema mismatches" in result.text
        # Default /usage (no args) should NOT include the debug block.
        ctx_plain = FakeCommandContext(
            runtime=FakeTransportRuntime(default_engine="claude"),
            args_text="",
        )
        result_plain = await cmd.handle(ctx_plain)  # type: ignore[arg-type]
        assert result_plain is not None
        assert "🔧 debug" not in result_plain.text
