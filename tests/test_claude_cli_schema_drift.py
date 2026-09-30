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

from untether.background_status import COLLECTION_TOOLS
from untether.runners.claude import _SAFEGUARD_NOTICE_MODEL_RE, _SAFEGUARD_NOTICE_RE
from untether.schemas.claude import (
    CLAUDE_ABORTED_TERMINAL_REASONS,
    CLAUDE_OVERAGE_STATUSES,
    CLAUDE_RATE_LIMIT_STATUSES,
    CLAUDE_RATE_LIMIT_TYPES,
)

# Last CLI these constants were re-derived against.
PROBED_CLI_VERSION = "2.1.285"

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


# ---------------------------------------------------------------------------
# #814 — safeguard stops (findings 2026-09-29 §B)
# ---------------------------------------------------------------------------


def _schema_window(blob: mmap.mmap, subtype: str, size: int = 4000) -> bytes:
    """The zod object following ``subtype:<fn>("<subtype>")``, with its
    ``.describe("…")`` strings stripped, up to its ``session_id`` key."""
    m = re.search(rb'subtype:\w{1,4}\("' + subtype.encode() + rb'"\)', blob)
    if m is None:
        pytest.fail(
            f'installed CLI no longer declares system subtype "{subtype}" '
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )
    window = re.sub(
        rb'\.describe\((?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')\)',
        b"",
        blob[m.end() : m.end() + size],
    )
    return window.split(b"session_id:", 1)[0]


def test_informational_notice_present(cli_blob: mmap.mmap) -> None:
    """``system/informational`` keeps ``content`` + the ``level`` enum the
    runner branches on, and the safeguard notice text still matches the
    runner's patterns."""
    window = _schema_window(cli_blob, "informational")
    assert b"content:" in window
    level = _require(
        _zod_enum(window, rb"level:\w{1,4}\(\[([^\]]*)\]\)"), "informational.level"
    )
    assert set(level) == {"info", "notice", "suggestion", "warning"}, (
        f"informational.level is now {level} — review the row/log-only split "
        "in _translate_informational (D-15)"
    )
    m = re.search(
        rb"`\$\{\w{1,4}\(\w{1,4}\)\}('s safeguards stopped the response above"
        rb"[^`]{0,80})`",
        cli_blob,
    )
    if m is None:
        pytest.fail(
            "the safeguard notice text moved — _SAFEGUARD_NOTICE_RE no longer "
            f"sees it (last green on CLI {PROBED_CLI_VERSION})"
        )
    notice = "Opus 5.5" + m.group(1).decode("utf-8", "replace")
    assert "continuing once" in notice
    assert _SAFEGUARD_NOTICE_RE.search(notice)
    model = _SAFEGUARD_NOTICE_MODEL_RE.match(notice)
    assert model is not None and model.group(1) == "Opus 5.5"


def test_refusal_stop_details_present(cli_blob: mmap.mmap) -> None:
    """The CLI still reads ``stop_reason === "refusal"`` and
    ``stop_details.category`` off the API message it passes through."""
    assert re.search(rb'\.stop_reason==="refusal"', cli_blob), (
        'no `stop_reason==="refusal"` check left in the installed CLI '
        f"(last green on CLI {PROBED_CLI_VERSION})"
    )
    assert re.search(rb"stop_details\??\.category", cli_blob), (
        "stop_details.category no longer read by the installed CLI"
    )


@pytest.mark.parametrize(
    ("subtype", "keys"),
    [
        (
            "model_refusal_fallback",
            ("original_model", "fallback_model", "api_refusal_category", "content"),
        ),
        (
            "model_refusal_no_fallback",
            ("original_model", "api_refusal_category", "content"),
        ),
        ("model_fallback", ("original_model", "fallback_model", "trigger")),
    ],
)
def test_model_refusal_subtypes_present(
    cli_blob: mmap.mmap, subtype: str, keys: tuple[str, ...]
) -> None:
    """The undocumented refusal/fallback subtypes keep the keys the #814
    handlers read."""
    window = _schema_window(cli_blob, subtype)
    missing = [k for k in keys if f"{k}:".encode() not in window]
    assert not missing, (
        f"system/{subtype} schema lost keys {missing} "
        f"(last green on CLI {PROBED_CLI_VERSION})"
    )


def test_terminal_reason_aborted_values_present(cli_blob: mmap.mmap) -> None:
    """#806 (phase 03): the CLI's own "was this turn aborted?" predicate
    names exactly CLAUDE_ABORTED_TERMINAL_REASONS."""
    m = re.search(
        rb'function \w{1,4}\((\w)\)\{return ((?:\1==="aborted_[a-z_]+"(?:\|\|)?)+)\}',
        cli_blob,
    )
    if m is None:
        pytest.fail(
            "the CLI's aborted-terminal predicate moved — re-derive the probe "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )
    declared = {v.decode() for v in re.findall(rb'"([^"]+)"', m.group(2))}
    assert declared == set(CLAUDE_ABORTED_TERMINAL_REASONS), (
        f"installed CLI treats {sorted(declared)} as aborted; "
        f"CLAUDE_ABORTED_TERMINAL_REASONS holds "
        f"{sorted(CLAUDE_ABORTED_TERMINAL_REASONS)}"
    )


def test_taskoutput_in_removed_tools(cli_blob: mmap.mmap) -> None:
    """#813 (phase 06): ``TaskOutput`` is still in the CLI's removed-tools
    list, so a wake turn collects a task's output with ``Read`` — which
    COLLECTION_TOOLS must keep treating as read-only."""
    m = re.search(rb'\[((?:"[A-Za-z]+",)*"TaskOutput"(?:,"[A-Za-z]+")*)\]', cli_blob)
    if m is None:
        pytest.fail(
            "TaskOutput is no longer in a removed-tools list in the installed "
            f"CLI (last green on CLI {PROBED_CLI_VERSION}) — if it came back, "
            "re-check COLLECTION_TOOLS"
        )
    removed = {v.decode() for v in re.findall(rb'"([^"]+)"', m.group(1))}
    assert {"TaskOutput", "BashOutput", "AgentOutput"} <= removed
    assert "Read" in COLLECTION_TOOLS
