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
