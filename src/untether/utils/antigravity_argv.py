"""The one place a user-influenced value becomes part of agy's argv (#558).

agy's flag parser reads a dash-leading token after a value-taking flag as
another flag: ``agy --model --version`` prints the version, while
``--model=--version`` is treated as a (bad) model id (agy 1.3.2). A model
from ``/model set``, ``[antigravity] model`` or a cron, or a conversation id
from a resume line or a saved session, could therefore have switched on
``--dangerously-skip-permissions``, ``--mode``, ``--continue`` or
``--project`` on a run labelled Workspace.

Two defences, both here:

1. ``agy_flag`` validates the value against an allow-list for that flag and
   raises ``AgyArgvError`` otherwise. Callers refuse the run (or the probe)
   before anything spawns.
2. It returns the joined ``--flag=value`` form, which agy never re-parses
   as a flag.

Nothing else in Untether may put a value-taking agy flag into an argv
(pinned by ``tests/test_antigravity_argv.py``).
"""

from __future__ import annotations

import re
from typing import Any

MAX_VALUE_CHARS = 128
PREVIEW_CHARS = 40

# Model ids as agy lists them (``gemini-3.8-flash-high``, ``claude-opus-5-5``,
# ``gpt-oss-120b``) plus ``/ : @ +`` for provider-qualified ids. ASCII only:
# no whitespace, control characters or NUL, never a leading dash.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}\Z")
# Conversation ids are UUIDs; the resume-line regex allows ``[0-9A-Za-z_-]``.
_CONVERSATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh", "max"})
_SLASH_COMMAND_RE = re.compile(r"/[a-z][a-z-]{0,31}\Z")

_PREVIEW_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


class AgyArgvError(ValueError):
    """A value isn't safe to pass to agy. Never carries the raw value."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"invalid agy {field or 'argument'}: {reason}")
        self.field = field
        self.reason = reason


def safe_preview(value: Any) -> str:
    """A short, inert form of a rejected value for a log line: at most
    ``PREVIEW_CHARS`` characters, anything outside ``[A-Za-z0-9._-]`` and
    every leading dash → ``?`` (so it never reads as a flag either)."""
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    cut = value[:PREVIEW_CHARS]
    preview = _PREVIEW_UNSAFE_RE.sub("?", cut)
    body = preview.lstrip("-")
    preview = "?" * (len(preview) - len(body)) + body
    return preview + ("…" if len(value) > PREVIEW_CHARS else "")


def _reason(value: Any) -> str | None:
    """Why *value* can't be an argv value at all (None when it could be)."""
    if not isinstance(value, str):
        return "not_a_string"
    if not value:
        return "empty"
    if len(value) > MAX_VALUE_CHARS:
        return "too_long"
    if value.startswith("-"):
        return "leading_dash"
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        return "whitespace_or_control"
    return None


def check_model(value: Any) -> str:
    reason = _reason(value)
    if reason is None and _MODEL_RE.match(value) is None:
        reason = "unsupported_character"
    if reason is not None:
        raise AgyArgvError("model", reason)
    return value


def check_conversation(value: Any) -> str:
    reason = _reason(value)
    if reason is None and _CONVERSATION_RE.match(value) is None:
        reason = "unsupported_character"
    if reason is not None:
        raise AgyArgvError("conversation", reason)
    return value


def check_effort(value: Any) -> str:
    reason = _reason(value)
    if reason is None and value not in _EFFORT_VALUES:
        reason = "unknown_level"
    if reason is not None:
        raise AgyArgvError("effort", reason)
    return value


def check_slash_command(value: Any) -> str:
    """``/usage``, ``/config``, ``/effort`` …: one lower-case slash word."""
    if not isinstance(value, str) or _SLASH_COMMAND_RE.match(value) is None:
        raise AgyArgvError("command", "not_a_slash_command")
    return value


_CHECKS = {
    "--model": check_model,
    "--conversation": check_conversation,
    "--effort": check_effort,
}


def agy_flag(flag: str, value: Any) -> str:
    """``--flag=value`` for agy's argv, or ``AgyArgvError``.

    Only the flags listed in ``_CHECKS`` exist; a new value-taking flag needs
    its own allow-list here first.
    """
    check = _CHECKS.get(flag) if isinstance(flag, str) else None
    if check is None:
        raise AgyArgvError("flag", "unknown_flag")
    return f"{flag}={check(value)}"
