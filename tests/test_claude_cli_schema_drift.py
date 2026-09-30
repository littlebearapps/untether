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
from untether.runners.claude import _probe_cli_help as _real_probe_cli_help
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


def test_hook_event_flag_and_subtypes_present(cli_blob: mmap.mmap) -> None:
    """#812: ``--include-hook-events`` is still an option, the three hook
    lifecycle frames still carry the keys the runner pairs on, the
    ``outcome`` enum is unchanged, and a CLI-started turn's result origin is
    still ``task-notification`` (rewake retro-attribution)."""
    if cli_blob.find(b'.option("--include-hook-events"') < 0:
        pytest.fail(
            "--include-hook-events is no longer a CLI option — the #812 hold "
            "can't see background hooks; the --help probe will stop passing "
            f"it (last green on CLI {PROBED_CLI_VERSION})"
        )
    started = re.search(
        rb'subtype:"hook_started",hook_id:\w{1,4},hook_name:\w{1,4},'
        rb"hook_event:\w{1,4}",
        cli_blob,
    )
    progress = cli_blob.find(b'subtype:"hook_progress",hook_id:')
    # The emitter's object literal nests ``{exit_code:…}``, so take a
    # bounded window rather than matching braces.
    response = re.search(rb'subtype:"hook_response",hook_id:[^;]{0,300}', cli_blob)
    if started is None or progress < 0 or response is None:
        pytest.fail(
            "the hook_started / hook_progress / hook_response emitters moved "
            "— re-derive _apply_hook_event's pairing (last green on CLI "
            f"{PROBED_CLI_VERSION})"
        )
    body = response.group(0)
    for key in (b"hook_name:", b"hook_event:", b"exit_code:", b"outcome:"):
        assert key in body, f"hook_response lost {key!r}"
    outcome = _require(
        _zod_enum(cli_blob, rb'outcome:\w{1,4}\((\[[^\]]*"cancelled"[^\]]*\])\)'),
        "hook_response.outcome",
    )
    assert set(outcome) == {"success", "error", "cancelled"}, (
        f"hook_response.outcome is now {outcome} — review the rewake / "
        "cancelled branches in _apply_hook_event"
    )
    if re.search(rb'kind:\w{1,4}\("task-notification"\)', cli_blob) is None:
        pytest.fail(
            "result origin kind 'task-notification' moved — hook_rewake "
            f"retro-attribution is dead code (last green on CLI {PROBED_CLI_VERSION})"
        )


def test_help_probe_detects_hook_events_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """#812: the real ``claude --help`` probe (zero-token — no session) sees
    the flag, so ``_build_args`` passes it. conftest stubs the probe for every
    other test; this one restores the real one (bound at import)."""
    from untether.runners import claude as claude_mod

    monkeypatch.setattr(claude_mod, "_probe_cli_help", _real_probe_cli_help)
    claude_mod._HOOK_EVENTS_SUPPORT.clear()
    assert claude_mod.cli_supports_hook_events("claude") is True, (
        "`claude --help` no longer lists --include-hook-events "
        f"(last green on CLI {PROBED_CLI_VERSION})"
    )


def test_hook_started_precedes_a_detached_hook_spawn(cli_blob: mmap.mmap) -> None:
    """#812: the hook-process evidence rests on two CLI facts — the CLI
    emits ``hook_started`` *before* spawning the hook (so a hook's process
    never predates its frame: ``HOOK_START_SLACK_S``), and it spawns command
    hooks ``detached`` off Windows (own process group: the baseline and the
    MCP/LSP-name exemptions only apply to the CLI's own group). If either
    moves, those exemptions could release a running ``asyncRewake`` hook."""
    emitter = re.search(
        rb"function (\w+)\(\w+,\w+,\w+\)\{if\(!\w+\(\w+\)\)return;"
        rb'\w+\(\{type:"system",subtype:"hook_started"',
        cli_blob,
    )
    if emitter is None:
        pytest.skip(
            "hook_started emitter not found — re-derive the probe "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )
    name = re.escape(emitter.group(1))
    command_spawns: list[tuple[bytes, bytes]] = []
    for call in re.finditer(
        rb"[;,{}]" + name + rb"\(\w+,\w+,\w+\);let \w+=await (\w+)\(", cli_blob
    ):
        fn = re.search(
            rb"async function " + re.escape(call.group(1)) + rb"\(", cli_blob
        )
        if fn is None:
            continue
        window = cli_blob[fn.start() : fn.start() + 12000]
        command_spawns.extend(
            (detached, window)
            for detached in set(re.findall(rb"detached:(\w+)", window))
        )
    if not command_spawns:
        pytest.skip(
            "no emit-then-spawn hook call site with a spawn option found — "
            f"re-derive the probe (last green on CLI {PROBED_CLI_VERSION})"
        )
    for detached, window in command_spawns:
        assigned = re.search(rb"[,;]" + re.escape(detached) + rb"=!(\w+)[,;]", window)
        assert assigned is not None, (
            f"hook spawn detached:{detached.decode()} is no longer `!<windows>`"
        )
        windows = re.escape(assigned.group(1))
        assert re.search(
            windows + rb"\?\w+\(\):null;if\(" + windows + rb"&&!\w+\)throw Error\("
            rb'`Hook "\$\{\w+\.command\}" requires bash but Git Bash',
            window,
        ), "hook spawn detached flag no longer keyed on the Windows/Git Bash check"


# ── #383: permission-mode edges and the plan re-arm ──────────────────────────


def test_permission_mode_status_frame_present(cli_blob: mmap.mmap) -> None:
    """#383 tracks the CLI's effective mode from the `system/status` frame the
    CLI emits on every mode change (plan exit, set_permission_mode)."""
    if cli_blob.find(b'subtype:"status",status:null,permissionMode:') == -1:
        pytest.fail(
            "the system/status permissionMode frame literal is gone from the "
            "installed CLI — #383's effective-mode tracking falls back to the "
            f"approval stamps (last green on CLI {PROBED_CLI_VERSION})"
        )


def test_set_permission_mode_subtype_handled(cli_blob: mmap.mmap) -> None:
    """#383 re-arms plan mode with the parent-initiated set_permission_mode."""
    for literal in (
        b'subtype==="set_permission_mode"',
        b'subtype:"set_permission_mode"',
    ):
        if cli_blob.find(literal) == -1:
            pytest.fail(
                f"{literal.decode()} is gone from the installed CLI — the #383 "
                "plan re-arm would go unanswered; re-derive (last green on CLI "
                f"{PROBED_CLI_VERSION})"
            )


def test_exit_plan_mode_restores_pre_plan_mode(cli_blob: mmap.mmap) -> None:
    """Approving ExitPlanMode moves the CLI to ``prePlanMode ?? "default"`` —
    the reason a live session stays out of plan mode (#383)."""
    for literal in (b'prePlanMode??"default"', b'trigger:"exit_plan_mode"'):
        if cli_blob.find(literal) == -1:
            pytest.fail(
                f"{literal.decode()} is gone from the installed CLI — re-check "
                "what an approved plan leaves the session in (#383; last green "
                f"on CLI {PROBED_CLI_VERSION})"
            )


_SET_MODE_VALIDATOR_RE = re.compile(
    rb"function \w{1,4}\(\w,\w\)\{let \w=\w{1,4}\(\w\);if\(\w===void 0\)"
    rb'return\{ok:!1,error:\w{1,4},code:"invalid_mode"\}(.{0,1500}?)'
    rb"return\{ok:!0,mode:\w\}\}"
)


def test_set_permission_mode_refusal_codes(cli_blob: mmap.mmap) -> None:
    """Guards #383's no-wait FIFO decision (plan §4 alt. 7): the follow-up is
    written right behind the re-arm without waiting for its ack, which is only
    safe while the CLI can't refuse a ``plan`` request."""
    hint = (
        "re-derive; if plan can now be refused, revisit §4 alternative 7 of the "
        f"#383 plan (last green on CLI {PROBED_CLI_VERSION})"
    )
    match = _SET_MODE_VALIDATOR_RE.search(cli_blob)
    if match is None:
        pytest.fail(f"set_permission_mode validator not found — {hint}")
    body = match.group(1)
    codes = {"invalid_mode"}
    codes.update(c.decode() for c in re.findall(rb'code:"([a-z_]+)"', body))
    codes.update(c.decode() for c in re.findall(rb'"(auto_mode_[a-z_]+)"', body))
    auto_map = re.search(rb'\{settings:"auto_mode_settings",[^}]*\}', cli_blob)
    if auto_map is None:
        pytest.fail(f"auto-mode refusal-code map not found — {hint}")
    codes.update(
        c.decode() for c in re.findall(rb'"(auto_mode_[a-z_]+)"', auto_map.group(0))
    )
    expected = {
        "invalid_mode",
        "bypass_restricted",
        "bypass_disabled",
        "bypass_not_launched",
        "auto_mode_settings",
        "auto_mode_circuit_breaker",
        "auto_mode_fast_mode",
        "auto_mode_model",
        "auto_mode_unavailable",
    }
    if codes != expected:
        pytest.fail(f"refusal codes {sorted(codes)} != {sorted(expected)} — {hint}")
    guarded = {m.decode() for m in re.findall(rb'if\(\w==="(\w+)"', body)}
    if guarded != {"bypassPermissions", "auto"}:
        pytest.fail(f"guarded target modes {sorted(guarded)} — {hint}")
