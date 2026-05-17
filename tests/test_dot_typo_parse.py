"""#523: leading-dot typo recognition for slash commands.

`.new`, `.cancel`, etc. would previously be dispatched as fresh agent
prompts (full Claude cold-start cost paid before the user could cancel).
The parse helper short-circuits those at the route_message layer with a
"Did you mean /<cmd>?" hint.
"""

from __future__ import annotations

import pytest

from untether.telegram.commands.parse import parse_dot_typo

KNOWN: frozenset[str] = frozenset(
    {
        "new",
        "cancel",
        "continue",
        "ctx",
        "topic",
        "usage",
        "export",
        "browse",
        "restart",
        "verbose",
        "config",
        "planmode",
        "ping",
        "help",
        "file",
        "proc",
        "at",
        "listen",
        "agent",
        "model",
        "reasoning",
    }
)


@pytest.mark.parametrize(
    "text",
    [".new", ".cancel", ".usage", ".help", ".restart"],
)
def test_bare_dot_command_recognised(text: str) -> None:
    cmd = text.lstrip(".")
    assert parse_dot_typo(text, KNOWN) == cmd


@pytest.mark.parametrize(
    "text,expected",
    [
        (".new project idea", "new"),
        (".at 30m do something", "at"),
        (".topic switch  to-x", "topic"),
    ],
)
def test_dot_command_with_args_recognised(text: str, expected: str) -> None:
    assert parse_dot_typo(text, KNOWN) == expected


def test_returns_none_for_empty_string() -> None:
    assert parse_dot_typo("", KNOWN) is None


def test_returns_none_for_plain_text() -> None:
    assert parse_dot_typo("hello world", KNOWN) is None
    assert parse_dot_typo("please run the daily check", KNOWN) is None


def test_returns_none_for_slash_command() -> None:
    """``/new`` is a legitimate slash command and must not match here —
    that's the slash-command parser's job downstream."""
    assert parse_dot_typo("/new", KNOWN) is None


def test_returns_none_for_unknown_command_after_dot() -> None:
    """``.foo`` where ``foo`` isn't a registered command is just prose —
    don't false-trigger."""
    assert parse_dot_typo(".foo", KNOWN) is None
    assert parse_dot_typo(".asdf bar", KNOWN) is None


def test_returns_none_for_double_dot_ellipsis() -> None:
    """``..new`` (ellipsis-like, not a typo) shouldn't trigger."""
    assert parse_dot_typo("..new", KNOWN) is None
    assert parse_dot_typo("...new", KNOWN) is None


def test_returns_none_for_relative_path() -> None:
    """``./new`` is a literal path, never a command typo."""
    assert parse_dot_typo("./new", KNOWN) is None
    assert parse_dot_typo("./scripts/build.sh", KNOWN) is None


def test_returns_none_for_sentence_starting_with_dot() -> None:
    """``.well that didn't work`` — ``well`` not in KNOWN so no match."""
    assert parse_dot_typo(".well that didn't work", KNOWN) is None


def test_case_insensitive_match() -> None:
    assert parse_dot_typo(".New", KNOWN) == "new"
    assert parse_dot_typo(".CANCEL", KNOWN) == "cancel"


def test_leading_whitespace_preserved() -> None:
    """Whitespace-prefixed (rare on mobile but possible)."""
    assert parse_dot_typo("  .new", KNOWN) == "new"


def test_dot_command_followed_by_newline() -> None:
    assert parse_dot_typo(".new\nlonger prompt here", KNOWN) == "new"
