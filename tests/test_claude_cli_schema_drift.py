"""Zero-token drift checks against the installed Claude Code CLI (#790).

The CLI bundles the zod schemas for its stream-json SDK messages as plain
text, so the enum lists Untether keys behaviour off can be read straight out
of the installed binary (native build) or ``cli.js`` (npm build) — no model
turn, no tokens. Same posture as the #741 permission-mode drift test in
``test_claude_permission_modes.py``: skip when the CLI is absent or the
minified shape stops matching, fail loudly when the values move.
"""

from __future__ import annotations

import mmap
import os
import re
import shutil
from collections.abc import Iterator

import pytest

from untether.schemas.claude import (
    CLAUDE_OVERAGE_STATUSES,
    CLAUDE_RATE_LIMIT_STATUSES,
    CLAUDE_RATE_LIMIT_TYPES,
)

# Last CLI these constants were re-derived against.
PROBED_CLI_VERSION = "2.1.283"

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None, reason="claude CLI not installed"
)


@pytest.fixture(scope="module")
def cli_blob() -> Iterator[mmap.mmap]:
    """The installed CLI, memory-mapped (the native build is ~240 MB, so it
    is searched in place rather than copied)."""
    claude = shutil.which("claude")
    assert claude is not None
    path = os.path.realpath(claude)
    try:
        fh = open(path, "rb")  # noqa: SIM115 - closed in the finally below
    except OSError as exc:
        pytest.skip(f"cannot read installed CLI at {path}: {exc}")
    try:
        if os.fstat(fh.fileno()).st_size == 0:
            pytest.skip(f"empty CLI file at {path}")
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            yield mm
    finally:
        fh.close()


def _zod_enum(blob: mmap.mmap, pattern: bytes) -> tuple[str, ...] | None:
    """Values of the first ``<key>:<fn>([...])`` zod enum matching *pattern*.

    Minified function names change every release, so the call name is
    matched loosely (``\\w{1,4}``); the key and the literal list are what
    matter.
    """
    m = re.search(pattern, blob)
    if m is None:
        return None
    return tuple(v.decode() for v in re.findall(rb'"([^"]+)"', m.group(1)))


def _require(values: tuple[str, ...] | None, what: str) -> tuple[str, ...]:
    if values is None:
        pytest.skip(
            f"could not locate the {what} enum in the installed CLI — the "
            f"minified shape moved; re-derive the probe "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )
    return values


def test_rate_limit_status_enum_matches(cli_blob: mmap.mmap) -> None:
    declared = _require(
        _zod_enum(cli_blob, rb"status:\w{1,4}\(\[([^\]]*)\]\),resetsAt:"),
        "rate_limit_info.status",
    )
    assert set(declared) == set(CLAUDE_RATE_LIMIT_STATUSES), (
        f"installed CLI declares rate_limit_info.status {declared}; "
        f"CLAUDE_RATE_LIMIT_STATUSES holds {CLAUDE_RATE_LIMIT_STATUSES}. "
        "A new status needs a handling decision in translate_claude_event "
        "(#790) before the constant is widened."
    )


def test_rate_limit_type_enum_matches(cli_blob: mmap.mmap) -> None:
    declared = _require(
        _zod_enum(cli_blob, rb"rateLimitType:\w{1,4}\(\[([^\]]*)\]\)"),
        "rate_limit_info.rateLimitType",
    )
    assert set(declared) == set(CLAUDE_RATE_LIMIT_TYPES), (
        f"installed CLI declares rateLimitType {declared}; "
        f"CLAUDE_RATE_LIMIT_TYPES holds {CLAUDE_RATE_LIMIT_TYPES}"
    )


def test_overage_status_enum_matches(cli_blob: mmap.mmap) -> None:
    declared = _require(
        _zod_enum(cli_blob, rb"overageStatus:\w{1,4}\(\[([^\]]*)\]\)"),
        "rate_limit_info.overageStatus",
    )
    assert set(declared) == set(CLAUDE_OVERAGE_STATUSES)


def test_rate_limit_snapshot_keys_present(cli_blob: mmap.mmap) -> None:
    """The keys the #790 schema decodes must still exist upstream."""
    for key in (
        b"unifiedWindows",
        b"resetsAt",
        b"isUsingOverage",
        b"credits_required",
        b'"rate_limit_event"',
    ):
        assert cli_blob.find(key) != -1, f"{key!r} missing from the installed CLI"


# ---------------------------------------------------------------------------
# #792 — system/api_retry
# ---------------------------------------------------------------------------

API_RETRY_KEYS = (
    "attempt",
    "max_retries",
    "retry_delay_ms",
    "error_status",
    "error",
    "no_response",
    "waited_ms",
    "retry_wait_ms",
)


def test_api_retry_subtype_and_counter_keys_present(cli_blob: mmap.mmap) -> None:
    """The keys `_translate_api_retry` reads must still be declared on the
    CLI's ``system/api_retry`` schema."""
    m = re.search(rb'subtype:\w{1,4}\("api_retry"\)', cli_blob)
    if m is None:
        pytest.fail(
            'installed CLI no longer declares subtype "api_retry" — #792 '
            f"handling is dead code (last green on CLI {PROBED_CLI_VERSION})"
        )
    # The schema object follows the subtype literal; its describe() strings
    # make it long, so take a generous window and strip them.
    window = re.sub(
        rb'\.describe\("(?:[^"\\]|\\.)*"\)', b"", cli_blob[m.end() : m.end() + 4000]
    )
    window = window.split(b"session_id:", 1)[0]
    missing = [k for k in API_RETRY_KEYS if f"{k}:".encode() not in window]
    assert not missing, (
        f"system/api_retry schema lost keys {missing} "
        f"(last green on CLI {PROBED_CLI_VERSION})"
    )
