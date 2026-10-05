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
import subprocess
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
PROBED_CLI_VERSION = "2.1.289"

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
        rb"`\$\{[\w$]{1,4}\([\w$]{1,4}\)\}('s safeguards stopped the response above"
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
        # The flag is declared either inside a longer ``let`` list
        # (``…,_n=!Qe,`` up to 2.1.286) or as the head of its own ``let``
        # (``let Jn=!Ze,`` on 2.1.287, where a new hook-cwd early return —
        # ``startsOutsideProject`` → ``spawnFailed`` — split the list).
        assigned = re.search(
            rb"(?:[,;]|let )" + re.escape(detached) + rb"=!([\w$]+)[,;]", window
        )
        assert assigned is not None, (
            f"hook spawn detached:{detached.decode()} is no longer `!<windows>`"
        )
        windows = re.escape(assigned.group(1))
        assert re.search(
            rb"[,;\s]" + windows + rb'=[\w$]+\(\)==="windows"[,;]', window
        ), "the hook spawn's detached flag is no longer `!(platform === windows)`"
        assert re.search(
            windows + rb"\?\w+\(\):null;if\(" + windows + rb"&&!\w+\)throw Error\("
            rb'`Hook "\$\{\w+\.command\}" requires bash but Git Bash',
            window,
        ), "hook spawn detached flag no longer keyed on the Windows/Git Bash check"


# --- #209: extra_args deny-list stays current with the CLI -------------------

# Long flags the #209 deny-list refuses in `[claude] extra_args`.
_209_BLOCKED = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-prompts",
    "--allowedTools",
    "--allowed-tools",
)
_209_MANAGED = (
    "--print",
    "--output-format",
    "--input-format",
    "--resume",
    "--continue",
    "--permission-mode",
    "--permission-prompt-tool",
)

# Every long flag `claude --help` lists on 2.1.285, classified for #209.
# blocked  — refused in extra_args (bypass / approval wiring)
# managed  — Untether sets it; refused in extra_args
# allowed  — passes through; documented in docs/how-to/security.md
# d10      — allowed today but replaces/breaks the stream-json run (interactive,
#            cloud, background, TUI); candidates for the v0.36.2 follow-up #851
CLAUDE_FLAGS_CLASSIFIED_2_1_285: dict[str, str] = {
    **dict.fromkeys(_209_BLOCKED, "blocked"),
    **dict.fromkeys(_209_MANAGED, "managed"),
    # allowed (documented; some can't be fully denylisted — security.md)
    "--add-dir": "allowed",  # D4: widening, not a bypass
    "--agent": "allowed",
    "--agents": "allowed",
    "--append-system-prompt": "allowed",
    "--autocompact": "allowed",  # rc15 #819 R15-19e relies on it
    "--bare": "allowed",
    "--betas": "allowed",
    "--brief": "allowed",
    "--chrome": "allowed",
    "--client-data-url": "allowed",
    "--debug": "allowed",
    "--debug-file": "allowed",
    "--disable-slash-commands": "allowed",
    "--disallowed-tools": "allowed",
    "--disallowedTools": "allowed",
    "--effort": "allowed",
    "--exclude-dynamic-system-prompt-sections": "allowed",
    "--fallback-model": "allowed",
    "--file": "allowed",
    "--forward-subagent-text": "allowed",
    "--include-hook-events": "allowed",  # #812: deduped, never reserved
    "--include-partial-messages": "allowed",
    "--json-schema": "allowed",
    "--max-budget-usd": "allowed",
    "--mcp-config": "allowed",
    "--model": "allowed",
    "--name": "allowed",
    "--no-chrome": "allowed",
    "--plugin-dir": "allowed",
    "--plugin-url": "allowed",
    "--prompt-suggestions": "allowed",
    "--restricted": "allowed",
    "--safe-mode": "allowed",
    "--setting-sources": "allowed",
    "--settings": "allowed",
    "--strict-mcp-config": "allowed",
    "--system-prompt": "allowed",
    "--system-prompt-snapshot": "allowed",
    "--tools": "allowed",
    "--verbose": "allowed",
    "--ax-screen-reader": "allowed",
    "--help": "allowed",
    "--version": "allowed",
    "--worktree": "allowed",
    # d10: protocol-breaking, not a security bypass (follow-up #851)
    "--background": "d10",
    "--bg": "d10",
    "--cloud": "d10",
    "--desktop": "d10",
    "--environment": "d10",
    "--fork-session": "d10",
    "--from-pr": "d10",
    "--ide": "d10",
    "--no-session-persistence": "d10",
    "--remote-control": "d10",
    "--remote-control-session-name-prefix": "d10",
    "--replay-user-messages": "d10",
    "--session-id": "d10",
    "--teleport": "d10",
    "--tmux": "d10",
}

_HELP_FLAG_RE = re.compile(
    r"^  (?:-\w, )?(--[A-Za-z][\w-]*)(?:, (--[A-Za-z][\w-]*))?", re.MULTILINE
)


def _claude_help() -> str:
    text = _real_probe_cli_help(shutil.which("claude") or "claude")
    if not text:
        pytest.skip("`claude --help` could not be run")
    return text


def _probe_env() -> dict[str, str]:
    # Unroutable API base + no nonessential traffic: argv errors only, never a turn.
    return {
        **os.environ,
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def test_209_blocked_flags_listed_in_help() -> None:
    text = _claude_help()
    for flag in (*_209_BLOCKED, "--print", "--output-format", "--input-format"):
        assert flag in text, (
            f"`claude --help` no longer lists {flag} — renamed or removed upstream; "
            f"update the #209 deny-list (last green on CLI {PROBED_CLI_VERSION})"
        )


@pytest.mark.parametrize(
    "flag",
    [
        *_209_BLOCKED,
        "--print",
        "--output-format",
        "--input-format",
        "--permission-mode",
        "--permission-prompt-tool",
    ],
)
def test_209_blocked_flags_recognised_by_parser(flag: str, tmp_path) -> None:
    """Known boolean flag → the `-p` input error; known value flag → commander's
    `argument missing`; gone → `unknown option`. Never a prompt (that bills).
    `--resume`/`--continue` are excluded: without a value they look up a
    session instead of failing at parse time."""
    claude = shutil.which("claude")
    assert claude is not None
    try:
        proc = subprocess.run(
            [claude, "-p", flag],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            cwd=tmp_path,
            env=_probe_env(),
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"`claude -p {flag}` did not exit within 60 s")
    blob = f"{proc.stderr}\n{proc.stdout}"
    if f"unknown option '{flag}'" in blob:
        pytest.fail(
            f"flag {flag} is gone upstream — drop it from the #209 deny-list or "
            f"it was renamed (last green on CLI {PROBED_CLI_VERSION})"
        )
    if "Input must be provided" in blob or "argument missing" in blob:
        return
    pytest.skip(
        f"unexpected wording for {flag}: {blob.strip()[:200]!r} — re-derive the "
        f"probe (last green on CLI {PROBED_CLI_VERSION})"
    )


def test_209_commander_expands_short_clusters(tmp_path) -> None:
    """Pins extra_args_guard rule 3: `-pv` is `--print --version`, so clusters
    must be walked (`-pc` would otherwise slip a managed flag through)."""
    claude = shutil.which("claude")
    assert claude is not None
    proc = subprocess.run(
        [claude, "-pv"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        cwd=tmp_path,
        env=_probe_env(),
    )
    assert re.search(r"\d+\.\d+\.\d+", proc.stdout), (proc.stdout, proc.stderr)


def test_209_bypass_launch_gate_literal_present(cli_blob: mmap.mmap) -> None:
    """Why `--allow-dangerously-skip-permissions` is blocked too: without it the
    CLI refuses to enter bypassPermissions later in the session."""
    literal = (
        b"Cannot set permission mode to bypassPermissions because the session"
        b" was not launched with --dangerously-skip-permissions"
    )
    if cli_blob.find(literal) == -1:
        pytest.skip(
            "bypass launch-gate literal moved — re-derive (last green on CLI "
            f"{PROBED_CLI_VERSION})"
        )


def test_209_claude_flag_snapshot() -> None:
    """Every long flag in `claude --help` must be classified for #209.

    A new flag fails here until someone decides block / allow (+ document);
    a keyword filter would miss run-replacing flags like `--remote-control`.
    Removals only warn."""
    text = _claude_help().split("\nCommands:")[0]
    listed: set[str] = set()
    for m in _HELP_FLAG_RE.finditer(text):
        listed.update(f for f in m.groups() if f)
    assert listed, "parsed no flags from `claude --help` — the help layout moved"
    unclassified = sorted(listed - CLAUDE_FLAGS_CLASSIFIED_2_1_285.keys())
    assert not unclassified, (
        f"new Claude flag(s) {unclassified} on the installed CLI — classify each "
        "for #209 (block / allow + document in docs/how-to/security.md) and add "
        f"it to CLAUDE_FLAGS_CLASSIFIED_2_1_285 (last green on CLI "
        f"{PROBED_CLI_VERSION})"
    )
    removed = sorted(CLAUDE_FLAGS_CLASSIFIED_2_1_285.keys() - listed)
    hidden_ok = {"--permission-prompt-tool"}  # hidden since before 2.1.228 (#750)
    if set(removed) - hidden_ok:
        import warnings

        warnings.warn(
            f"Claude flags no longer in --help: {sorted(set(removed) - hidden_ok)}",
            stacklevel=1,
        )


def test_209_snapshot_matches_the_deny_list() -> None:
    """The snapshot's blocked/managed rows are exactly what the guard refuses."""
    from untether.runners.claude import find_blocked_claude_args

    for flag, cls in CLAUDE_FLAGS_CLASSIFIED_2_1_285.items():
        refused = bool(find_blocked_claude_args([flag]))
        assert refused is (cls in {"blocked", "managed"}), (flag, cls)


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


# ── #819: context-window use and compaction ──────────────────────────────────


def test_compaction_frames_present(cli_blob: mmap.mmap) -> None:
    """The compaction frames #819's rows and ``% ctx`` reset are built on:
    ``system/status`` (``compacting`` → null + ``compact_result``) and
    ``system/compact_boundary`` + ``compact_metadata``."""
    for literal in (
        b'subtype:"compact_boundary"',
        b"compact_metadata",
        b"pre_tokens:",
        b"post_tokens",
        b"cumulative_dropped_tokens:",
        b'type:"sdk_status",status:"compacting"',
        b"compact_result",
        b"compact_error:",
        b"logical_parent_uuid",
        b"isCompactSummary",
    ):
        if cli_blob.find(literal) == -1:
            pytest.fail(
                f"{literal.decode()} is gone from the installed CLI — the #819 "
                "compaction handling reads it; re-derive (last green on CLI "
                f"{PROBED_CLI_VERSION})"
            )
    status = _schema_window(cli_blob, "status")
    result = _require(
        _zod_enum(status, rb"compact_result:\w{1,4}\(\[([^\]]*)\]\)"),
        "system/status compact_result",
    )
    assert set(result) == {"success", "failed"}, (
        f"system/status compact_result is now {result} — review the #819 row "
        "outcomes (success / failed / skipped)"
    )
    assert b"permissionMode:" in status, "system/status lost permissionMode (#383)"
    boundary = _schema_window(cli_blob, "compact_boundary")
    trigger = _require(
        _zod_enum(boundary, rb"trigger:\w{1,4}\(\[([^\]]*)\]\)"),
        "compact_metadata.trigger",
    )
    assert set(trigger) == {"manual", "auto"}, (
        f"compact_metadata.trigger is now {trigger} — the #819 manual-only "
        "0-turn exemption keys off 'manual'"
    )
    for key in ("pre_tokens:", "post_tokens:", "duration_ms:"):
        assert key.encode() in boundary, (
            f"compact_metadata lost {key!r} (last green on CLI {PROBED_CLI_VERSION})"
        )


def test_context_window_literals_present(cli_blob: mmap.mmap) -> None:
    """#819's ``% ctx`` denominator is ``result.modelUsage[<model>]
    .contextWindow``; its numerator is the assistant ``usage`` input side —
    the same parts the CLI's own ``/context`` reads."""
    for literal in (
        b"contextWindow",
        b"rawMaxTokens",
        b"autoCompactThreshold",
        b"% context used",
        b"cache_creation_input_tokens",
        b"cache_read_input_tokens",
    ):
        if cli_blob.find(literal) == -1:
            pytest.fail(
                f"{literal.decode()} is gone from the installed CLI — re-check "
                f"the #819 context maths (last green on CLI {PROBED_CLI_VERSION})"
            )
    if not re.search(
        rb"costUSD:\w{1,4}\(\),contextWindow:\w{1,4}\(\)\.int\(\)", cli_blob
    ):
        pytest.fail(
            "result.modelUsage no longer declares an int contextWindow — the "
            f"#819 window cache would never learn (last green on CLI "
            f"{PROBED_CLI_VERSION})"
        )


def test_get_context_usage_present(cli_blob: mmap.mmap) -> None:
    """Pins the contract of the #833 follow-up (exact ``/context`` parity via
    the ``get_context_usage`` control request)."""
    for literal in (
        b"get_context_usage",
        b"'summary' answers from the last response",
    ):
        if cli_blob.find(literal) == -1:
            pytest.fail(
                f"{literal.decode()} is gone from the installed CLI — revisit "
                f"#833 (last green on CLI {PROBED_CLI_VERSION})"
            )


def test_compact_heartbeat_interval_present(cli_blob: mmap.mmap) -> None:
    """The CLI re-sends ``status: "compacting"`` on a 30 s interval while a
    compaction runs — the #819 liveness latch is sized from it (four missed
    heartbeats)."""
    m = re.search(
        rb"(\w{1,4})=(\d+);function \w{1,4}\(\w\)\{let \w=setInterval\("
        rb"\w{1,4},\1,\w\);return\(\)=>clearInterval\(\w\)\}"
        rb'function \w{1,4}\(\w\)\{[^}]{0,80}type:"sdk_status",status:"compacting"',
        cli_blob,
    )
    if m is None:
        pytest.skip(
            "the compacting heartbeat's minified shape moved — re-derive the "
            f"probe (last green on CLI {PROBED_CLI_VERSION})"
        )
    assert int(m.group(2)) == 30000, (
        f"compacting heartbeat is now {m.group(2)} ms — resize the #819 "
        "compaction latch"
    )


# ---------------------------------------------------------------------------
# #684: the CLI withdrawing a pending control request
# ---------------------------------------------------------------------------


def test_684_control_cancel_request_frame_present(cli_blob: mmap.mmap) -> None:
    """#684 retires a request on the CLI's ``control_cancel_request`` frame
    (findings 2026-09-30 Q1 §3, probe Z4). If the frame is gone, withdrawn
    requests would hold a live session until the 4 h cap again."""
    for literal in (
        b'type:"control_cancel_request",request_id:',
        b"enqueueCancelRequest(",
    ):
        if cli_blob.find(literal) == -1:
            pytest.fail(
                f"{literal.decode()} is gone from the installed CLI — re-check "
                "how the CLI withdraws a pending permission request before "
                f"trusting #684 (last green on CLI {PROBED_CLI_VERSION})"
            )


def test_684_pending_permission_requests_present(cli_blob: mmap.mmap) -> None:
    """The ``initialize`` re-send cross-check deferred to rc16 (#684 D2,
    #837) reads ``pending_permission_requests`` off the success envelope."""
    if cli_blob.find(b"pending_permission_requests") == -1:
        pytest.fail(
            "pending_permission_requests is gone from the installed CLI — "
            "re-check findings Q1 §7 before building the rc16 cross-check "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )


def test_684_stdin_close_rejection_text_present(cli_blob: mmap.mmap) -> None:
    """Closing stdin with a request pending rejects it with this text and
    sends no cancel frame (findings Q1 §6, probe Z5) — so registries are
    cleaned at process end, not by a cancel."""
    if cli_blob.find(b"Tool permission stream closed before response received") == -1:
        pytest.fail(
            "the stdin-close permission rejection text is gone from the "
            "installed CLI — re-check Q1 §6 (last green on CLI "
            f"{PROBED_CLI_VERSION})"
        )


def test_init_frame_carries_permission_mode(cli_blob: mmap.mmap) -> None:
    """#751: ``system/init`` still declares ``permissionMode`` — the only
    signal of the mode the CLI actually runs (``auto`` on Haiku silently
    runs as ``default``, findings Q3). The runtime mismatch check and its
    stage-6 re-arm depend on it."""
    window = _schema_window(cli_blob, "init")
    assert b"permissionMode:" in window, (
        "system/init no longer declares permissionMode "
        f"(last green on CLI {PROBED_CLI_VERSION})"
    )


# --- #872: declared background waits ------------------------------------------


def test_872_background_bash_time_limit_present(cli_blob: mmap.mmap) -> None:
    """#872 rests on the CLI enforcing a background command's ``timeout``
    itself: 30 min default, 2 h maximum, then "stopped after reaching its
    background time limit". If the defaults move, the hold's grace and the
    docs (FAQ, troubleshooting) need re-checking."""
    limits = re.search(
        rb"var [\w$]{1,4}=(\d+);function [\w$]{1,4}\(\)\{return "
        rb"Math\.min\(Math\.max\((\d+),",
        cli_blob,
    )
    if limits is None:
        pytest.skip(
            "background Bash time-limit constants not found — re-derive the "
            f"probe (last green on CLI {PROBED_CLI_VERSION})"
        )
    assert (limits.group(1), limits.group(2)) == (b"1800000", b"7200000"), (
        "the CLI's background-command default / max time limit moved to "
        f"{limits.group(1).decode()} / {limits.group(2).decode()} ms — review "
        "#872's declared-wait docs"
    )
    if cli_blob.find(b"stopped after reaching its background time limit") < 0:
        pytest.fail(
            "the CLI no longer stops background commands at their time limit "
            "(notice text gone) — a declared wait now ends only at Untether's "
            f"grace or live_session_max_s (last green on CLI {PROBED_CLI_VERSION})"
        )


def test_872_schedule_wakeup_clamp_present(cli_blob: mmap.mmap) -> None:
    """ScheduleWakeup stays within [60, 3600] s, so a wake-up the hold waits
    for is bounded well inside ``live_session_max_s``."""
    if cli_blob.find(b"outside [60, 3600]") < 0:
        pytest.fail(
            "ScheduleWakeup's [60, 3600] s clamp moved — a pending wake-up may "
            "now hold a live session longer than an hour (#872; last green on "
            f"CLI {PROBED_CLI_VERSION})"
        )


# --- #828: hook frames carry no async / subagent marker ------------------------

_HOOK_MARKER_KEYS = (
    b"agent_id:",
    b"agent_type:",
    b"parent_tool_use_id:",
    b"async:",
    b"asyncRewake",
    b"rewake:",
)


def test_828_hook_frames_still_carry_no_async_or_agent_marker(
    cli_blob: mmap.mmap,
) -> None:
    """#828 tells a background subagent's sync hook from an asyncRewake one
    by *when it started* (it must outlive its turn), because neither frame
    says which it is. If the CLI starts marking hook frames natively, switch
    the heuristic to the marker (native first)."""
    started = re.search(
        rb'subtype:"hook_started",hook_id:[\w$]{1,4},hook_name:[\w$]{1,4},'
        rb"hook_event:[\w$]{1,4}[^}]{0,200}\}",
        cli_blob,
    )
    response = re.search(
        rb'subtype:"hook_response",[^;]{0,600}?outcome:[\w$.]+\}\)', cli_blob
    )
    if started is None or response is None:
        pytest.skip(
            "hook_started / hook_response emitter literals not found — re-derive "
            f"the probe (last green on CLI {PROBED_CLI_VERSION})"
        )
    for frame, literal in (
        ("hook_started", started.group(0)),
        ("hook_response", response.group(0)),
    ):
        for key in _HOOK_MARKER_KEYS:
            assert key not in literal, (
                f"{frame} now carries {key.decode()!r} — the CLI marks hook "
                "frames natively; switch #828's outlived-turn heuristic to it "
                f"(last green on CLI {PROBED_CLI_VERSION})"
            )


# --- #825 / #876: foreground -> background transitions -------------------------


def test_825_task_updated_patch_reports_is_backgrounded(cli_blob: mmap.mmap) -> None:
    """The ``task_updated`` patch differ emits ``is_backgrounded`` when the CLI
    moves a running foreground task to the background — #876's primary
    signal (the snapshot listing and the idle-notification fallback remain)."""
    differ = re.search(
        rb'"isBackgrounded"in [\w$]{1,4}\?[\w$]{1,4}\.isBackgrounded:void 0;'
        rb"if\([\w$]{1,4}!==[\w$]{1,4}&&[\w$]{1,4}!==void 0\)"
        rb"[\w$]{1,4}\.is_backgrounded=[\w$]{1,4}",
        cli_blob,
    )
    if differ is None:
        if re.search(rb"[\w$]\.is_backgrounded=[\w$]", cli_blob) is None:
            pytest.fail(
                "foreground→background transitions are no longer reported on "
                "task_updated — #876's snapshot / idle-notification fallback is "
                f"the only path (last green on CLI {PROBED_CLI_VERSION})"
            )
        pytest.skip(
            "task_updated patch differ moved — re-derive the probe "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )


_EFFORT_HELP_RE = re.compile(r"--effort <level>[^(]*\(([^)]*)\)", re.DOTALL)


def test_743_effort_choices_match_untether_levels() -> None:
    """#743: a cron's `reasoning` is validated against Untether's Claude
    levels, which must match the CLI's `--effort` choices (#416 rule: every
    allowed level has a /config button). Zero-token: `claude --help` only."""
    from untether.telegram.engine_overrides import allowed_reasoning_levels

    text = _claude_help()
    match = _EFFORT_HELP_RE.search(text)
    if match is None:
        pytest.skip("`--effort` choices not found in `claude --help`")
    choices = {c.strip() for c in match.group(1).replace("\n", " ").split(",")}
    assert choices == set(allowed_reasoning_levels("claude")), (
        "CLI effort levels changed — update `telegram/engine_overrides.py` and "
        f"the /config reasoning buttons (#416 rule): CLI {sorted(choices)}"
    )


# --- #922 (rc20) ---------------------------------------------------------------
# The #701 action-required cap latch reads the result's `api_error*` fields
# first and the headless cap wording second. Re-derived against CLI 2.1.289.

# The headless builder's templates: `<cap sentence> Switch to another model${K}
# to continue.` — the cap sentence is a literal or a `${g}` / `${h}` placeholder.
_922_HEADLESS_TEMPLATE_RE = re.compile(
    rb"`([^`]{0,90}?) Switch to another model\$\{\w{1,4}\} to continue\.`"
)
_922_PLACEHOLDER_RE = re.compile(r"\$\{\w{1,4}\}")
# What the builder's `${g}` / `${h}` expand to (each literal is asserted present).
_922_CAP_SENTENCES = (
    "You've reached your Fable limit.",
    "Opus 5.5 requires usage credits.",
    "You've hit your monthly spend limit.",
    "You've hit your channel's monthly spend limit.",
)
_922_CAP_LITERALS = (
    b"You've reached your Fable limit.",
    b" requires usage credits.",
    b"You've hit your monthly spend limit.",
    b"You've hit your channel's monthly spend limit.",
)
# `${K}`: empty when the account can't buy credits, else the personal or the
# Team/Enterprise usage URL.
_922_K_VARIANTS = (
    "",
    ", or manage usage credits at claude.ai/settings/usage?from=cc_cli_limit_message,",
    ", or manage usage credits at claude.ai/admin-settings/usage,",
)


def _922_render(prefix: str) -> list[str]:
    if _922_PLACEHOLDER_RE.fullmatch(prefix):
        heads = list(_922_CAP_SENTENCES)
    else:
        heads = [_922_PLACEHOLDER_RE.sub("Opus 5.5", prefix)]
    return [
        f"{head} Switch to another model{k} to continue."
        for head in heads
        for k in _922_K_VARIANTS
    ]


def test_922_headless_cap_wording_matches_parser(cli_blob: mmap.mmap) -> None:
    """Every headless cap message the CLI can emit must arm the latch. This
    probe FAILS (never skips) when the wording moves — that is the drift it
    exists to catch (#922 review amendment 1)."""
    from untether.runners.claude import _parse_action_required_cap

    for literal in _922_CAP_LITERALS:
        assert cli_blob.find(literal) != -1, (
            f"cap sentence {literal!r} missing from the installed CLI — the "
            "cap wording moved; re-derive _ACTION_CAP_RE / _ACTION_CAP_CLAUSES"
        )
    templates = [
        m.group(1).decode("utf-8", "replace")
        for m in _922_HEADLESS_TEMPLATE_RE.finditer(cli_blob)
    ]
    assert templates, (
        "no `… Switch to another model${K} to continue.` template in the "
        "installed CLI — the headless remedy wording moved; re-derive "
        "_ACTION_REMEDY_RE (#922)"
    )
    unparsed = [
        text
        for prefix in templates
        for text in _922_render(prefix)
        if _parse_action_required_cap(text) is None
    ]
    assert not unparsed, f"cap wording the latch no longer recognises: {unparsed}"


def test_922_result_schema_carries_api_error_fields(cli_blob: mmap.mmap) -> None:
    for key in (b"api_error_status:", b"api_error_code:", b"api_error:"):
        assert cli_blob.find(key) != -1, f"{key!r} missing from the installed CLI"
    if (
        re.search(
            rb"api_error_status:\w{1,4}\(\)\.int\(\)\.nullable\(\)\.optional\(\),"
            rb"api_error_code:",
            cli_blob,
        )
        is None
        or re.search(rb"\{api_error:\w{1,4}\.api_error\}", cli_blob) is None
    ):
        pytest.skip(
            "result api_error_* schema / spread shape moved; re-derive the probe "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )


def test_922_api_error_enum_keeps_credit_kinds(cli_blob: mmap.mmap) -> None:
    from untether.runners.claude import (
        _ACTION_REQUIRED_API_ERRORS,
        _NOT_ACTION_API_ERRORS,
    )

    declared = _require(
        _zod_enum(cli_blob, rb"api_error:\w{1,4}\(\[([^\]]*)\]\)"), "api_error"
    )
    missing = (_ACTION_REQUIRED_API_ERRORS | _NOT_ACTION_API_ERRORS) - set(declared)
    assert not missing, (
        f"api_error no longer declares {sorted(missing)} — the #922 structured "
        "cap classification needs re-deriving"
    )


def test_922_cap_builder_tags_api_error(cli_blob: mmap.mmap) -> None:
    for literal in (
        b'apiError:"model_requires_usage_credits"',
        b'apiError:"long_context_credits_required"',
    ):
        assert cli_blob.find(literal) != -1, f"{literal!r} missing from the CLI"


# --- #929 (rc20) ---------------------------------------------------------------

# Review amendment 1: ``[^}]`` can't cross the ``...r.mcpInfo&&{mcp_server:…}``
# spread between the subtype and the reason fields, so the window is
# ``[\s\S]``. The CLI has several ``can_use_tool`` builders (the sandbox
# network ask carries no ``decision_reason_type``); any occurrence matching is
# enough.
_929_REASON_RE = re.compile(
    rb'subtype:"can_use_tool"[\s\S]{0,1500}?decision_reason_type:'
)
_929_IDS_RE = re.compile(
    rb'subtype:"can_use_tool"[\s\S]{0,1500}?tool_use_id:[\w$]+,agent_id:'
)


def test_929_can_use_tool_carries_agent_and_tool_use_id(cli_blob: mmap.mmap) -> None:
    """#929: the stage-6 ``can_use_tool`` request names its own tool call and
    agent (the bridge maps the approval by ``tool_use_id`` and labels the
    standalone approval message by ``agent_id``) and why it asks."""
    assert _929_REASON_RE.search(cli_blob) is not None, (
        "can_use_tool no longer sends decision_reason_type — the #929 surface "
        "loses its hook-reason line (last green on CLI "
        f"{PROBED_CLI_VERSION})"
    )
    assert _929_IDS_RE.search(cli_blob) is not None, (
        "can_use_tool no longer sends tool_use_id + agent_id together — #929's "
        "per-request mapping falls back to the newest tool_use and the "
        f"surface to its generic copy (last green on CLI {PROBED_CLI_VERSION})"
    )


def test_929_local_agent_task_id_is_agent_id(cli_blob: mmap.mmap) -> None:
    """#929 (best-effort): a background agent's task id is its ``agentId``,
    so ``can_use_tool.agent_id`` looks up ``ClaudeStreamState.tasks``. If this
    drifts the surface falls back to the generic label and the latest-turn
    reply anchor."""
    if re.search(rb'type:"local_agent",status:"running",agentId:', cli_blob) is None:
        pytest.skip(
            "local_agent task registration shape moved — re-derive the probe "
            f"(last green on CLI {PROBED_CLI_VERSION})"
        )


# --- #923 (rc20) ---


def test_923_async_rewake_exit_2_enqueues_a_next_priority_wake(
    cli_blob: mmap.mmap,
) -> None:
    """#923's open-time idle candidate assumes an asyncRewake hook's exit 2
    enqueues the wake as a ``next``-priority task-notification, so the
    parent's turn opens right after the ``hook_response``. ``"Stop hook
    feedback"`` occurs several times (schema ``describe`` strings first), so
    every occurrence is scanned."""
    hits = [m.start() for m in re.finditer(rb'"Stop hook feedback"', cli_blob)]
    assert any(
        re.search(rb'priority:"next",stopHookActive:!0', cli_blob[pos : pos + 400])
        for pos in hits
    ), (
        "asyncRewake wake delivery changed — re-verify #923's open-time idle "
        f"candidate (last green on CLI 2.1.289; {len(hits)} occurrence(s) scanned)"
    )
    assert cli_blob.find(b'mode:"task-notification",agentId:') != -1, (
        "asyncRewake wake delivery changed (no task-notification enqueue with "
        "agentId) — re-verify #923's open-time idle candidate (last green on "
        "CLI 2.1.289)"
    )


# --- #928 (rc20) ---


def test_928_no_query_dispatch_paths_present(cli_blob: mmap.mmap) -> None:
    """#928 absorbs the CLI's empty ``num_turns: 0`` task-notification results.
    They come from the CLI's ``shouldQuery: false`` dispatch paths:
    notification coalescing, the agent hand-back pointer notice and the
    generic route. If those move, the result shape may have too."""
    for literal in (
        b"queryHeldForNextTurn",
        b"print_task_notification_coalesce",
        b"agent_handback_pointer_notice",
    ):
        assert cli_blob.find(literal) != -1, (
            f"{literal.decode()!r} is gone — the CLI changed its no-query "
            "dispatch paths; re-verify #928's `_is_no_query_result` (last green "
            "on CLI 2.1.289)"
        )


# --- #925 (rc20) ---
# Untether owns Loop-mode schedules through SDK PreToolUse hook callbacks
# (`docs/findings/2026-10-04-claude-session-cron-resume-and-host-controls.md`
# F3/F8; live probe G1 on CLI 2.1.289). Zero-token string probes.


def _require_bytes(blob: mmap.mmap, needles: tuple[bytes, ...], why: str) -> None:
    missing = [n.decode() for n in needles if blob.find(n) == -1]
    if missing:
        pytest.fail(
            f"installed CLI lost {missing} — {why} "
            "(last green on CLI 2.1.289; see the 2026-10-04 cron findings note)"
        )


def test_925_hook_callback_protocol_present(cli_blob: mmap.mmap) -> None:
    """``initialize.hooks`` callback registration, the ``hook_callback``
    request, the PreToolUse deny output and the ``hook error:`` text the
    denied tool_result carries (CLI 2.1.289)."""
    _require_bytes(
        cli_blob,
        (
            b"hookCallbackIds",
            b'subtype:"hook_callback"',
            b"permissionDecision",
            b"hook error: ",
        ),
        "#925's CronCreate/CronDelete hooks can no longer decline the job",
    )


def test_925_cron_tools_and_bind_text_present(cli_blob: mmap.mmap) -> None:
    """The tools the hooks match by exact name, and the result text
    ``_LOOP_CRON_ID_RE`` binds (CLI 2.1.289)."""
    _require_bytes(
        cli_blob,
        (b'"CronCreate"', b'"CronDelete"', b"Scheduled recurring job"),
        "the hook matchers / upstream id binding no longer match the CLI",
    )


def test_925_session_cron_resurrection_present(cli_blob: mmap.mmap) -> None:
    """F3: ``--resume`` resurrects session crons unless a CronDelete marker
    exists. If this disappears the resume behaviour changed — re-evaluate
    #925 D-E and #926's suppression (CLI 2.1.289)."""
    _require_bytes(
        cli_blob,
        (b"resume: resurrected", b"deletedCronIds"),
        "session-cron resurrection on --resume changed",
    )


# --- #926 (rc20) ---


def test_926_disable_cron_env_present(cli_blob: mmap.mmap) -> None:
    """F6: the env var suppressed sessions are resumed with (CLI 2.1.289)."""
    _require_bytes(
        cli_blob,
        (b"CLAUDE_CODE_DISABLE_CRON",),
        "#926's resume suppression no longer switches the CLI scheduler off",
    )


def test_926_recurring_max_age_default(cli_blob: mmap.mmap) -> None:
    """The 7-day resurrect window ``loop_scheduler.CLI_CRON_MAX_AGE_S``
    mirrors (CLI 2.1.289). Fails loudly if the default moves."""
    from untether.loop_scheduler import CLI_CRON_MAX_AGE_S

    assert CLI_CRON_MAX_AGE_S * 1000 == 604800000
    _require_bytes(
        cli_blob,
        (b"recurringMaxAgeMs:604800000",),
        "the CLI's session-cron max age moved — update CLI_CRON_MAX_AGE_S",
    )
