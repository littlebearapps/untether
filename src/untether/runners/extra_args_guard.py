"""Shared ``extra_args`` tokeniser for the engine deny-lists (#209).

``[engines.<id>] extra_args`` is a free-form passthrough to the engine CLI.
Both Claude (commander) and Codex (clap) accept ``--flag=value``, short
clusters (``-pc``) and attached short values (``-sVALUE`` / ``-s=VALUE``), so a
plain ``arg in BLOCKED`` check misses most spellings. This module turns an
``extra_args`` list into canonical option tokens; each runner applies its own
policy table (``claude.find_blocked_claude_args`` /
``codex.find_blocked_codex_args``) and raises :class:`BlockedExtraArgsError`.

Pure functions, no logging. Error text names the **flag only, never its
value** — a ``-c key=value`` can carry a secret.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import ConfigError

__all__ = [
    "SECURITY_DOC_REF",
    "BlockedArg",
    "BlockedExtraArgsError",
    "OptionToken",
    "dedupe_hits",
    "format_blocked",
    "iter_option_tokens",
    "normalise_value",
    "option_value",
]

BlockedCategory = Literal["managed", "bypass", "workspace", "separator", "subcommand"]

# Where the refusal hints point. The heading "Engine CLI flags (`extra_args`)"
# in docs/how-to/security.md slugs to this anchor.
SECURITY_DOC_REF = "docs/how-to/security.md#engine-cli-flags-extra_args"


class BlockedExtraArgsError(ConfigError):
    """``extra_args`` carries a flag Untether refuses (#209).

    A subclass of ``ConfigError`` so every existing handler still applies;
    ``runtime_loader.build_router`` treats it specially for non-default
    engines (D15): the engine is disabled (``load_error``) instead of being
    rebuilt from ``{}``, which would also drop restrictive settings.
    """


@dataclass(frozen=True, slots=True)
class OptionToken:
    index: int  # position in extra_args
    flag: str  # canonical long name ("--sandbox"), "--", or "-X" if unknown
    attached: str | None  # value given via "=" or inside a short cluster


@dataclass(frozen=True, slots=True)
class BlockedArg:
    flag: str  # canonical flag name only — NEVER the value
    category: BlockedCategory
    hint: str


def iter_option_tokens(
    args: Sequence[str],
    *,
    short_aliases: Mapping[str, str],
    short_value_flags: frozenset[str],
) -> Iterator[OptionToken]:
    """Yield every option-shaped token in *args*, canonicalised.

    Rules: a bare ``--`` is its own token and scanning continues past it;
    ``--name=value`` splits at the first ``=``; a single-dash token walks its
    letters through *short_aliases* — a value-taking letter (in
    *short_value_flags*) swallows the rest of the token (minus one leading
    ``=``) and stops the walk, an unknown letter is emitted as ``-X`` and also
    stops it (the remainder is treated as a value, never silently skipped as
    flags). Case-sensitive (``-c`` ≠ ``-C``). Non-option tokens are skipped,
    so a *value* that spells a blocked flag is still reported — a deliberate,
    documented false positive.
    """
    for index, arg in enumerate(args):
        if arg == "--":
            yield OptionToken(index, "--", None)
            continue
        if arg.startswith("--"):
            name, sep, value = arg.partition("=")
            yield OptionToken(index, name, value if sep else None)
            continue
        if not arg.startswith("-") or len(arg) < 2:
            continue
        pos = 1
        while pos < len(arg):
            letter = arg[pos]
            canonical = short_aliases.get(letter)
            if canonical is None:
                yield OptionToken(index, f"-{letter}", None)
                break
            if letter in short_value_flags:
                rest = arg[pos + 1 :]
                rest = rest.removeprefix("=")
                yield OptionToken(index, canonical, rest or None)
                break
            yield OptionToken(index, canonical, None)
            pos += 1


def option_value(args: Sequence[str], tok: OptionToken) -> str | None:
    """The token's value: the attached one, else the next argument."""
    if tok.attached is not None:
        return tok.attached
    nxt = tok.index + 1
    return args[nxt] if nxt < len(args) else None


def normalise_value(value: str) -> str:
    """Strip whitespace and one pair of matching quotes, lower-case."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v.lower()


def dedupe_hits(hits: Sequence[BlockedArg]) -> list[BlockedArg]:
    """First hit per flag, order kept."""
    seen: set[str] = set()
    out: list[BlockedArg] = []
    for hit in hits:
        if hit.flag in seen:
            continue
        seen.add(hit.flag)
        out.append(hit)
    return out


def format_blocked(engine: str, config_path: Path, hits: Sequence[BlockedArg]) -> str:
    """One ``ConfigError`` message listing every hit (flag names only)."""
    parts = [f"flag {hit.flag!r} {hit.hint}" for hit in hits]
    return f"Invalid `{engine}.extra_args` in {config_path}; " + "; ".join(parts) + "."
