"""Zero-token drift checks against the installed Codex CLI (#830).

Codex parses argv (clap) before it reads the prompt, loads auth or touches the
network, so the exact argv Untether builds can be run against the installed
binary for free: with an empty stdin an *accepted* argv exits rc=1 with
``No prompt provided via stdin.``, a *rejected* one exits rc=2 with a clap
``error: …`` line. ``--help`` is NOT a valid argv probe for root-level values
(``codex -a untrusted exec --help`` prints help and exits 0), so value checks
use ``--version`` or a bogus value instead.

Every subprocess gets a fresh ``CODEX_HOME`` under ``tmp_path``, runs in
``tmp_path`` (never a trusted project) with stdin from ``/dev/null``, and is
bounded by ``timeout=30``. A timeout FAILS the test with the argv in the
message: a hang is drift worth seeing, never a skip. CI has no ``codex``, so
the module skips there; run it on lba-1 before every rc.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from untether.model import ResumeToken
from untether.runners.codex import CodexRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options

# Last CLI these probes were re-derived against.
PROBED_CLI_VERSION = "0.157.1"

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI not installed"
)

_TIMEOUT_S = 30
_NIL_THREAD = "00000000-0000-0000-0000-000000000000"
_POSSIBLE_VALUES_RE = re.compile(r"\[possible values: ([^\]]*)\]")


def _run_codex(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir(exist_ok=True)
    argv = ["codex", *args]
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            cwd=tmp_path,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"codex did not exit within {_TIMEOUT_S}s for argv {argv!r} — the "
            f"zero-token probe now blocks (network/auth before argv?); "
            f"re-derive it (last green on codex-cli {PROBED_CLI_VERSION})"
        )


def _clap_error(stderr: str) -> str:
    return next(
        (line for line in stderr.splitlines() if line.startswith("error:")),
        stderr.strip()[:300],
    )


def _possible_values(stderr: str) -> set[str]:
    m = _POSSIBLE_VALUES_RE.search(stderr)
    assert m is not None, f"no clap [possible values: …] in stderr: {stderr!r}"
    return {v.strip() for v in m.group(1).split(",") if v.strip()}


# --- D-C1: the exact argv Untether builds is accepted -----------------------


@pytest.mark.parametrize("permission_mode", [None, "safe"])
@pytest.mark.parametrize(
    "resume",
    [
        None,
        ResumeToken(engine="codex", value=_NIL_THREAD),
        ResumeToken(engine="codex", value="", is_continue=True),
    ],
    ids=["new", "resume", "continue"],
)
def test_build_args_argv_accepted_by_installed_codex(
    tmp_path: Path, permission_mode: str | None, resume: ResumeToken | None
) -> None:
    """The test that would have caught #830 the day codex-cli 0.149.0 landed."""
    runner = CodexRunner(codex_cmd="codex", extra_args=["-c", "notify=[]"])
    state = runner.new_state("probe", resume)
    with apply_run_options(EngineRunOptions(permission_mode=permission_mode)):
        args = runner.build_args("probe", resume, state=state)
    proc = _run_codex(args, tmp_path)
    assert proc.returncode != 2, (
        f"installed codex rejected Untether's argv {args!r}: "
        f"{_clap_error(proc.stderr)} (last green on codex-cli {PROBED_CLI_VERSION})"
    )
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-500:])
    assert "No prompt provided via stdin" in proc.stderr, proc.stderr[-500:]


# --- D-C2: why safe mode no longer passes -a untrusted ----------------------


def test_untrusted_rejected(tmp_path: Path) -> None:
    proc = _run_codex(["--ask-for-approval", "untrusted", "--version"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "invalid value 'untrusted'" in proc.stderr
    assert _possible_values(proc.stderr) == {"on-request", "never"}, (
        "codex's --ask-for-approval value list changed — revisit #830/#421 "
        "before relying on any approval policy"
    )


# --- D-C3: the exec-level sandbox value list (not precedence; see R15-CS5) ---


def test_exec_sandbox_values(tmp_path: Path) -> None:
    proc = _run_codex(["exec", "--sandbox", "__untether_probe__", "--help"], tmp_path)
    assert proc.returncode != 0
    values = _possible_values(proc.stderr)
    assert "read-only" in values, "safe mode pins `exec --sandbox read-only` (#830)"
    assert values == {"read-only", "workspace-write", "danger-full-access"}


# --- D-C4: -a is root-only (exec never reads it, #830 R2) --------------------


def test_exec_rejects_root_only_approval_flag(tmp_path: Path) -> None:
    proc = _run_codex(["exec", "-a", "never", "--help"], tmp_path)
    assert proc.returncode == 2
    assert "unexpected argument '-a'" in proc.stderr, (
        "`-a` became an exec-level flag — revisit #830 R2 (exec used to ignore it)"
    )


# --- D-C5: the config-route error string the error hint keys on -------------


def test_config_route_untrusted_rejected(tmp_path: Path) -> None:
    proc = _run_codex(
        ["-c", 'approval_policy="untrusted"', "features", "list"], tmp_path
    )
    if "unrecognized subcommand" in proc.stderr:
        pytest.skip("`codex features list` is gone; pick another config-loading probe")
    assert proc.returncode != 0
    assert "is no longer supported; remove this setting" in proc.stderr, proc.stderr


# --- #209: extra_args deny-list stays current with the CLI -------------------

# Flags #209 refuses that are hidden aliases or exec-level only; probed with
# `codex exec <flag> --help` (clap reports unknown args even with --help).
_209_EXEC_FLAGS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--approve-for-me",
    "--not-so-yolo",
    "--dangerously-bypass-hook-trust",
    "--worktree",
    "--ignore-rules",
    "--ignore-user-config",
    "--ephemeral",
)
# Inherited into exec from the ROOT position, where extra_args sit.
_209_ROOT_LIVE = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--approve-for-me",
    "--not-so-yolo",
    "--dangerously-bypass-hook-trust",
    "--worktree",
)
# Exec-only: at the root they already fail every run loudly.
_209_ROOT_REJECTED = ("--ignore-rules", "--ignore-user-config", "--ephemeral")

# Every long flag in `codex --help` plus `codex exec --help` on 0.157.1, classified
# for #209: blocked (bypass/workspace), managed (Untether sets it / exec-only),
# allowed (documented passthrough), d16 (non-security, deferred to #851), and
# root-only (TUI/top-level; rejected or ignored by exec).
CODEX_FLAGS_CLASSIFIED_0_157_1: dict[str, str] = {
    "--dangerously-bypass-approvals-and-sandbox": "blocked",
    "--approve-for-me": "blocked",
    "--dangerously-bypass-hook-trust": "blocked",
    "--cd": "blocked",
    "--worktree": "blocked",
    "--sandbox": "allowed",  # only the danger-full-access value is blocked (D3)
    "--config": "allowed",  # substring rule on the value (D5)
    "--ask-for-approval": "managed",
    "--ignore-rules": "managed",
    "--ignore-user-config": "managed",
    "--skip-git-repo-check": "managed",
    "--json": "managed",
    "--output-schema": "managed",
    "--output-last-message": "managed",
    "--color": "managed",
    "--add-dir": "allowed",  # D4
    "--enable": "allowed",
    "--disable": "allowed",
    "--strict-config": "allowed",
    "--image": "allowed",
    "--model": "allowed",
    "--oss": "allowed",
    "--local-provider": "allowed",
    "--profile": "allowed",
    "--search": "allowed",
    "--help": "allowed",
    "--version": "allowed",
    "--ephemeral": "d16",
    "--thread-source": "d16",
    "--remote": "root-only",
    "--remote-auth-token-env": "root-only",
    "--no-alt-screen": "root-only",
    "--no-daemon": "root-only",
}

_CODEX_HELP_FLAG_RE = re.compile(r"^  (?:-\w, |    )(--[A-Za-z][\w-]*)", re.MULTILINE)


@pytest.mark.parametrize("flag", _209_EXEC_FLAGS)
def test_209_exec_bypass_flags_exist(tmp_path: Path, flag: str) -> None:
    proc = _run_codex(["exec", flag, "--help"], tmp_path)
    assert proc.returncode == 0, (
        f"`codex exec {flag}` is no longer accepted — renamed/removed upstream; "
        f"update the #209 deny-list: {_clap_error(proc.stderr)} "
        f"(last green on codex-cli {PROBED_CLI_VERSION})"
    )


@pytest.mark.parametrize("flag", [*_209_ROOT_LIVE, *_209_ROOT_REJECTED])
def test_209_root_placement_semantics(tmp_path: Path, flag: str) -> None:
    proc = _run_codex([flag, "exec", "--help"], tmp_path)
    if flag in _209_ROOT_LIVE:
        assert proc.returncode == 0, (
            f"root-level {flag} is no longer accepted: {_clap_error(proc.stderr)}"
        )
    else:
        assert proc.returncode == 2, (
            f"root-level {flag} is now accepted — the #209 'exec-only, codex "
            "rejects it before exec' hint is wrong; reword it"
        )
        assert f"unexpected argument '{flag}'" in proc.stderr


def test_209_sandbox_value_set_includes_danger(tmp_path: Path) -> None:
    proc = _run_codex(["exec", "--sandbox", "__untether_probe__", "--help"], tmp_path)
    assert "danger-full-access" in _possible_values(proc.stderr)


def test_209_root_and_exec_sandbox_coexist(tmp_path: Path) -> None:
    """A root `-s` (where extra_args sit) coexists with #830's exec-level
    `--sandbox read-only`; a second exec-level `-s` is a clap error."""
    ok = _run_codex(
        ["-s", "workspace-write", "exec", "-s", "read-only", "--help"], tmp_path
    )
    assert ok.returncode == 0, _clap_error(ok.stderr)
    dup = _run_codex(["exec", "-s", "read-only", "-s", "read-only", "--help"], tmp_path)
    assert dup.returncode == 2
    assert "cannot be used multiple times" in dup.stderr


def test_209_codex_flag_snapshot(tmp_path: Path) -> None:
    listed: set[str] = set()
    for args in (["--help"], ["exec", "--help"]):
        proc = _run_codex(args, tmp_path)
        assert proc.returncode == 0, _clap_error(proc.stderr)
        listed.update(_CODEX_HELP_FLAG_RE.findall(proc.stdout))
    assert listed, "parsed no flags from `codex --help` — the help layout moved"
    unclassified = sorted(listed - CODEX_FLAGS_CLASSIFIED_0_157_1.keys())
    assert not unclassified, (
        f"new Codex flag(s) {unclassified} on the installed CLI — classify each "
        "for #209 (block / allow + document in docs/how-to/security.md) and add "
        "it to CODEX_FLAGS_CLASSIFIED_0_157_1 (last green on codex-cli "
        f"{PROBED_CLI_VERSION})"
    )
    removed = sorted(CODEX_FLAGS_CLASSIFIED_0_157_1.keys() - listed)
    if removed:
        import warnings

        warnings.warn(f"Codex flags no longer in --help: {removed}", stacklevel=1)


def test_209_snapshot_matches_the_deny_list() -> None:
    from untether.runners.codex import find_blocked_codex_args

    for flag, cls in CODEX_FLAGS_CLASSIFIED_0_157_1.items():
        refused = bool(find_blocked_codex_args([flag]))
        assert refused is (cls in {"blocked", "managed"}), (flag, cls)
