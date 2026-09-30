"""Tokeniser behind the #209 `extra_args` deny-lists (`runners/extra_args_guard.py`)."""

from __future__ import annotations

from pathlib import Path

from untether.runners.claude import (
    _CLAUDE_SHORT_ALIASES,
    _CLAUDE_SHORT_VALUE_FLAGS,
)
from untether.runners.codex import (
    _CODEX_SHORT_ALIASES,
    _CODEX_SHORT_VALUE_FLAGS,
    find_blocked_codex_args,
)
from untether.runners.extra_args_guard import (
    BlockedArg,
    BlockedExtraArgsError,
    OptionToken,
    dedupe_hits,
    format_blocked,
    iter_option_tokens,
    normalise_value,
    option_value,
)


def _claude(args: list[str]) -> list[OptionToken]:
    return list(
        iter_option_tokens(
            args,
            short_aliases=_CLAUDE_SHORT_ALIASES,
            short_value_flags=_CLAUDE_SHORT_VALUE_FLAGS,
        )
    )


def _codex(args: list[str]) -> list[OptionToken]:
    return list(
        iter_option_tokens(
            args,
            short_aliases=_CODEX_SHORT_ALIASES,
            short_value_flags=_CODEX_SHORT_VALUE_FLAGS,
        )
    )


def test_long_equals_form_split() -> None:
    assert _codex(["--sandbox=danger-full-access"]) == [
        OptionToken(0, "--sandbox", "danger-full-access")
    ]


def test_short_cluster_expands_booleans() -> None:
    assert [t.flag for t in _claude(["-pv"])] == ["--print", "--version"]


def test_short_cluster_stops_at_value_flag() -> None:
    assert _claude(["-rabc"]) == [OptionToken(0, "--resume", "abc")]
    # -d takes an optional value, so `-dp` is debug filter "p", not --print.
    assert _claude(["-dp"]) == [OptionToken(0, "--debug", "p")]


def test_short_attached_and_equals_value() -> None:
    assert _codex(["-sX"]) == [OptionToken(0, "--sandbox", "X")]
    assert _codex(["-s=X"]) == [OptionToken(0, "--sandbox", "X")]
    assert _codex(["-s"]) == [OptionToken(0, "--sandbox", None)]


def test_case_sensitive_short_flags() -> None:
    assert [t.flag for t in _codex(["-C"])] == ["--cd"]
    assert [t.flag for t in _codex(["-c"])] == ["--config"]


def test_unknown_short_letter_stops_the_walk() -> None:
    # `x` is unknown: emitted as -x, and the rest is never read as flags.
    assert [t.flag for t in _claude(["-xp"])] == ["-x"]


def test_double_dash_is_reported_and_scan_continues() -> None:
    assert [t.flag for t in _codex(["--", "--yolo"])] == ["--", "--yolo"]


def test_values_are_not_options() -> None:
    assert _claude(["--mcp-config", "x.json"]) == [OptionToken(0, "--mcp-config", None)]


def test_option_value_prefers_attached_then_next() -> None:
    args = ["-s", "read-only", "--sandbox=workspace-write"]
    toks = _codex(args)
    assert option_value(args, toks[0]) == "read-only"
    assert option_value(args, toks[1]) == "workspace-write"


def test_option_value_missing_at_end_is_none() -> None:
    args = ["-s"]
    assert option_value(args, _codex(args)[0]) is None


def test_normalise_value_strips_whitespace_quotes_and_case() -> None:
    assert normalise_value(' "DANGER-full-access" ') == "danger-full-access"
    assert normalise_value("'read-only'") == "read-only"
    assert normalise_value('"x') == '"x'


def test_dedupe_hits_keeps_first_per_flag() -> None:
    a = BlockedArg("--yolo", "bypass", "h1")
    b = BlockedArg("--yolo", "bypass", "h2")
    c = BlockedArg("--", "separator", "h3")
    assert dedupe_hits([a, b, c]) == [a, c]


def test_format_blocked_never_contains_values() -> None:
    hits = find_blocked_codex_args(["-c", "bypass_hook_trust=SECRETVALUE"])
    msg = format_blocked("codex", Path("/tmp/untether.toml"), hits)
    assert "'--config'" in msg
    assert "SECRETVALUE" not in msg
    assert msg.startswith("Invalid `codex.extra_args` in /tmp/untether.toml;")


def test_blocked_error_is_a_config_error() -> None:
    from untether.config import ConfigError

    assert issubclass(BlockedExtraArgsError, ConfigError)
