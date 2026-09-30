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
