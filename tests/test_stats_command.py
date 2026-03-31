"""Tests for /stats command backend."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from untether.session_stats import AggregatedStats
from untether.telegram.commands.stats import (
    StatsCommand,
    _format_duration,
    _format_last_run,
    format_stats_message,
)

# ── Duration formatting ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (0, "0s"),
        (30_000, "30s"),
        (90_000, "1m 30s"),
        (3_661_000, "1h 1m"),
    ],
)
def test_format_duration(ms: int, expected: str) -> None:
    assert _format_duration(ms) == expected


# ── Last run formatting ────────────────────────────────────────────────────


def test_format_last_run_never() -> None:
    assert _format_last_run(0) == "never"


def test_format_last_run_just_now() -> None:
    assert _format_last_run(time.time() - 10) == "just now"


def test_format_last_run_minutes() -> None:
    assert "m ago" in _format_last_run(time.time() - 300)


def test_format_last_run_hours() -> None:
    assert "h ago" in _format_last_run(time.time() - 7200)


def test_format_last_run_days() -> None:
    assert "d ago" in _format_last_run(time.time() - 172800)


# ── Stats message formatting ──────────────────────────────────────────────


def test_format_stats_empty() -> None:
    with patch("untether.telegram.commands.stats.get_stats", return_value=[]):
        msg = format_stats_message(engine=None, period="today")
    assert "No sessions recorded" in msg
    assert "Today" in msg


def test_format_stats_single_engine() -> None:
    stats = [
        AggregatedStats(
            engine="claude",
            run_count=3,
            action_count=15,
            duration_ms=60_000,
            last_run_ts=time.time(),
        )
    ]
    with patch("untether.telegram.commands.stats.get_stats", return_value=stats):
        msg = format_stats_message(engine=None, period="today")
    assert "<b>claude</b>" in msg
    assert "3 runs" in msg
    assert "15 actions" in msg


def test_format_stats_multiple_engines_shows_total() -> None:
    stats = [
        AggregatedStats(
            engine="claude",
            run_count=3,
            action_count=15,
            duration_ms=60_000,
            last_run_ts=time.time(),
        ),
        AggregatedStats(
            engine="codex",
            run_count=1,
            action_count=5,
            duration_ms=30_000,
            last_run_ts=time.time(),
        ),
    ]
    with patch("untether.telegram.commands.stats.get_stats", return_value=stats):
        msg = format_stats_message(engine=None, period="today")
    assert "<b>Total</b>" in msg
    assert "4 runs" in msg


def test_format_stats_week_label() -> None:
    with patch("untether.telegram.commands.stats.get_stats", return_value=[]):
        msg = format_stats_message(engine=None, period="week")
    assert "This Week" in msg


def test_format_stats_all_label() -> None:
    with patch("untether.telegram.commands.stats.get_stats", return_value=[]):
        msg = format_stats_message(engine=None, period="all")
    assert "All Time" in msg


# ── Command handle ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_stats_command_default() -> None:
    from dataclasses import dataclass

    @dataclass
    class FakeCtx:
        command: str = "stats"
        text: str = "/stats"
        args_text: str = ""
        args: tuple = ()
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = None
        runtime = None
        executor = None

        def __post_init__(self):
            if self.plugin_config is None:
                self.plugin_config = {}

    cmd = StatsCommand()
    with patch("untether.telegram.commands.stats.get_stats", return_value=[]):
        result = await cmd.handle(FakeCtx())
    assert result is not None
    assert result.parse_mode == "HTML"


@pytest.mark.anyio
async def test_stats_command_with_engine_and_period() -> None:
    from dataclasses import dataclass

    @dataclass
    class FakeCtx:
        command: str = "stats"
        text: str = "/stats claude week"
        args_text: str = "claude week"
        args: tuple = ("claude", "week")
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = None
        runtime = None
        executor = None

        def __post_init__(self):
            if self.plugin_config is None:
                self.plugin_config = {}

    cmd = StatsCommand()
    with patch(
        "untether.telegram.commands.stats.get_stats", return_value=[]
    ) as mock_get:
        result = await cmd.handle(FakeCtx())
    mock_get.assert_called_once_with(engine="claude", period="week")
    assert "This Week" in result.text


def test_stats_command_id() -> None:
    cmd = StatsCommand()
    assert cmd.id == "stats"
    assert cmd.description


# ── Auth status subcommand ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_stats_auth_subcommand() -> None:
    from dataclasses import dataclass

    @dataclass
    class FakeCtx:
        command: str = "stats"
        text: str = "/stats auth"
        args_text: str = "auth"
        args: tuple = ("auth",)
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = None
        runtime = None
        executor = None

        def __post_init__(self):
            if self.plugin_config is None:
                self.plugin_config = {}

    cmd = StatsCommand()
    with patch(
        "untether.telegram.commands.stats.get_auth_status",
        return_value=["<b>claude</b>: \u2705 api_key"],
    ):
        result = await cmd.handle(FakeCtx())
    assert "Auth Status" in result.text
    assert "claude" in result.text
    assert result.parse_mode == "HTML"


@pytest.mark.anyio
async def test_stats_auth_no_engines() -> None:
    from dataclasses import dataclass

    @dataclass
    class FakeCtx:
        command: str = "stats"
        text: str = "/stats auth"
        args_text: str = "auth"
        args: tuple = ("auth",)
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = None
        runtime = None
        executor = None

        def __post_init__(self):
            if self.plugin_config is None:
                self.plugin_config = {}

    cmd = StatsCommand()
    with patch(
        "untether.telegram.commands.stats.get_auth_status",
        return_value=[],
    ):
        result = await cmd.handle(FakeCtx())
    assert "No engines found" in result.text
