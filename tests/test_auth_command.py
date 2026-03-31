"""Tests for /auth command backend."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from untether.telegram.commands.auth import (
    AuthCommand,
    parse_device_code,
    strip_ansi,
)

# ── ANSI stripping ─────────────────────────────────────────────────────────


def test_strip_ansi_removes_sequences() -> None:
    assert strip_ansi("\x1b[32mhello\x1b[0m") == "hello"


def test_strip_ansi_preserves_clean_text() -> None:
    assert strip_ansi("no ansi here") == "no ansi here"


def test_strip_ansi_multiple_codes() -> None:
    assert strip_ansi("\x1b[1;34mblue\x1b[0m \x1b[31mred\x1b[0m") == "blue red"


# ── Device code parsing ───────────────────────────────────────────────────


def test_parse_device_code_both() -> None:
    text = "Visit https://auth.openai.com/codex/device and enter code: ABCD-1234"
    url, code = parse_device_code(text)
    assert url == "https://auth.openai.com/codex/device"
    assert code == "ABCD-1234"


def test_parse_device_code_with_ansi() -> None:
    text = "\x1b[1mVisit https://example.com/auth Code: WXYZ-5678\x1b[0m"
    url, code = parse_device_code(text)
    assert url == "https://example.com/auth"
    assert code == "WXYZ-5678"


def test_parse_device_code_real_codex_format() -> None:
    """Real codex output has 4-5 char codes on a separate line."""
    text = "   CS33-V5YT6"
    url, code = parse_device_code(text)
    assert code == "CS33-V5YT6"


def test_parse_device_code_no_match() -> None:
    url, code = parse_device_code("Logging in...")
    assert url is None
    assert code is None


def test_parse_device_code_url_only() -> None:
    url, code = parse_device_code("Visit https://auth.example.com/device")
    assert url == "https://auth.example.com/device"
    assert code is None


# ── Command backend ───────────────────────────────────────────────────────


def test_auth_command_id() -> None:
    cmd = AuthCommand()
    assert cmd.id == "auth"
    assert cmd.description


@pytest.mark.anyio
async def test_auth_no_args_shows_codex_info() -> None:
    from dataclasses import dataclass, field

    @dataclass
    class FakeCtx:
        command: str = "auth"
        text: str = "/auth"
        args_text: str = ""
        args: tuple = ()
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = field(default_factory=dict)
        runtime = None
        executor = None

    cmd = AuthCommand()
    result = await cmd.handle(FakeCtx())
    assert "/auth codex" in result.text
    assert "Only Codex" in result.text
    assert "terminal" in result.text


@pytest.mark.anyio
async def test_auth_non_codex_engine_shows_info() -> None:
    from dataclasses import dataclass, field

    @dataclass
    class FakeCtx:
        command: str = "auth"
        text: str = "/auth foobar"
        args_text: str = "foobar"
        args: tuple = ("foobar",)
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = field(default_factory=dict)
        runtime = None
        executor = None

    cmd = AuthCommand()
    result = await cmd.handle(FakeCtx())
    assert "Only Codex" in result.text


@pytest.mark.anyio
async def test_auth_cli_not_found() -> None:
    from dataclasses import dataclass, field

    @dataclass
    class FakeCtx:
        command: str = "auth"
        text: str = "/auth codex"
        args_text: str = "codex"
        args: tuple = ("codex",)
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = field(default_factory=dict)
        runtime = None
        executor = None

    cmd = AuthCommand()
    with patch("untether.telegram.commands.auth.shutil.which", return_value=None):
        result = await cmd.handle(FakeCtx())
    assert "not found" in result.text


@pytest.mark.anyio
async def test_auth_concurrent_guard() -> None:
    from dataclasses import dataclass, field

    import untether.telegram.commands.auth as auth_mod

    @dataclass
    class FakeCtx:
        command: str = "auth"
        text: str = "/auth codex"
        args_text: str = "codex"
        args: tuple = ("codex",)
        message = None
        reply_to = None
        reply_text = None
        config_path = None
        plugin_config: dict = field(default_factory=dict)
        runtime = None
        executor = None

    cmd = AuthCommand()
    old_value = auth_mod._auth_running
    auth_mod._auth_running = True
    try:
        with patch(
            "untether.telegram.commands.auth.shutil.which",
            return_value="/usr/bin/codex",
        ):
            result = await cmd.handle(FakeCtx())
        assert "already in progress" in result.text
    finally:
        auth_mod._auth_running = old_value
