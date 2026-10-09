"""Zero-quota drift checks against the installed Antigravity CLI (#558, #976).

Pins the agy surface Untether depends on (flags, the unknown-flag signature,
mode/effort value lists, the ``-p /usage`` / ``/effort`` / ``/model`` payload
shapes, the invalid-model result) so an agy self-update that changes it fails
here before it fails in a chat.

Two tiers (08 §5, REVIEW m1):

- **ungated** — ``agy --version``, ``agy --help`` and the unknown-flag rc 2
  probe parse argv locally: no network, no sign-in. They run whenever ``agy``
  is on PATH.
- **gated** (``UNTETHER_AGY_DRIFT=1``) — ``-p /model``, ``/effort``,
  ``/usage`` answer from Google's servers (zero tokens, zero turns, but a
  network round trip on the host's sign-in). A module probe skips them with a
  clear reason when the host isn't signed in or agy doesn't answer in 20 s —
  never a 60 s auth wait.

Every call runs in ``tmp_path`` with stdin from ``/dev/null`` and a 30 s
timeout; a timeout FAILS with the argv (a hang is drift worth seeing). The
real ``HOME`` is used (that is where the sign-in lives); nothing here writes
under ``~/.gemini``. CI has no ``agy``, so the module skips there; run the
gated tier on lba-1 the day of each rc's integration tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from untether.model import ResumeToken
from untether.runners import antigravity as agy
from untether.runners.antigravity import AntigravityRunner
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import antigravity as agy_schema

# Last CLI these probes were re-derived against (the pack was written on
# 1.3.1; lba-1 self-updated to 1.3.2 on 2026-10-09 with no drift here).
PROBED_CLI_VERSION = "1.3.2"

pytestmark = pytest.mark.skipif(
    shutil.which("agy") is None, reason="agy (Antigravity CLI) not installed"
)

_TIMEOUT_S = 30
_PROBE_TIMEOUT_S = 20
_GATED = os.environ.get("UNTETHER_AGY_DRIFT") == "1"
_VALID_RE = re.compile(r"\(valid: ([^)]*)\)")
# Untether's own modes that map onto agy's --mode (09/15 use plan; 02 keeps
# accept-edits out of rc1, so only plan is required).
_UNTETHER_AGY_MODES = {"plan"}
_REQUIRED_EFFORTS = {"low", "medium", "high"}


def _run_agy(
    args: list[str], tmp_path: Path, *, timeout: float = _TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    argv = ["agy", *args]
    try:
        return subprocess.run(  # nosec B603 — fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tmp_path,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"agy did not exit within {timeout}s for argv {argv!r} — a "
            f"zero-quota probe now blocks; re-derive it (last green on agy "
            f"{PROBED_CLI_VERSION})"
        )


def _events(stdout: str) -> list[dict[str, Any]]:
    """Every stdout line is a JSON object Untether's schema decodes."""
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        agy_schema.decode_event(line.encode())
        obj = json.loads(line)
        assert isinstance(obj, dict), line[:200]
        out.append(obj)
    return out


def _command_data(stdout: str, name: str) -> dict[str, Any]:
    events = _events(stdout)
    commands = [e for e in events if e.get("event") == "command_result"]
    assert commands, f"no command_result in {stdout[:300]!r}"
    command = commands[0]["command"]
    assert command["name"] == name
    (result,) = [e for e in events if e.get("event") == "result"]
    assert result["result"]["status"] == "SUCCESS"
    assert result["result"].get("num_turns") == 0
    usage = result["result"].get("usage") or {}
    assert usage.get("total_tokens", 0) == 0
    return command["data"]


# ── ungated: argv only, no network ──────────────────────────────────────────


def test_version_at_least_minimum(tmp_path: Path) -> None:
    proc = _run_agy(["--version"], tmp_path)
    assert proc.returncode == 0, proc.stderr[:300]
    parsed = agy.parse_agy_version(proc.stdout)
    assert parsed is not None, proc.stdout[:200]
    assert parsed >= agy._MIN_AGY_VERSION


def _help(tmp_path: Path) -> str:
    proc = _run_agy(["--help"], tmp_path)
    assert proc.returncode == 0
    return proc.stdout + proc.stderr


def _untether_flags() -> set[str]:
    """Every flag the runner's own ``build_args`` emits (Workspace, Full
    access, ``--conversation`` and ``--continue`` runs)."""
    runner = AntigravityRunner(antigravity_cmd="agy", model="probe-model")
    flags: set[str] = set()
    for mode in ("workspace", "full"):
        with apply_run_options(EngineRunOptions(permission_mode=mode)):
            for resume in (
                None,
                ResumeToken(engine="antigravity", value="0" * 36),
                ResumeToken(engine="antigravity", value="", is_continue=True),
            ):
                args = runner.build_args("p", resume, state=None)
                # Values are joined (``--model=x``): compare the flag name.
                flags.update(a.partition("=")[0] for a in args if a.startswith("--"))
    return flags


def test_help_lists_untether_flags(tmp_path: Path) -> None:
    text = _help(tmp_path)
    built = _untether_flags()
    assert {"--input-format", "--print-timeout", "--disable-slash-commands"} <= built
    # Later phases add --mode (plan, rc3) and --effort (05).
    wanted = built | {"--mode", "--effort", "--model", "--output-format"}
    missing = sorted(
        f for f in wanted if re.search(rf"^\s*{f}\b", text, re.MULTILINE) is None
    )
    assert not missing, f"agy --help no longer lists {missing}"


def test_help_print_timeout_default_zero(tmp_path: Path) -> None:
    """Untether passes ``--print-timeout 0`` explicitly; the help still says
    0 waits for the turn (REVIEW m2 — agy's docs still say 5m)."""
    line = next(
        (
            ln
            for ln in _help(tmp_path).splitlines()
            if ln.strip().startswith("--print-timeout")
        ),
        "",
    )
    assert "(default 0s)" in line, line


def test_unknown_flag_rc2_signature(tmp_path: Path) -> None:
    proc = _run_agy(["--untether-drift-probe"], tmp_path)
    assert proc.returncode == 2
    assert "flags provided but not defined" in proc.stderr
    # The stderr-hook/argv diagnostic keys on that wording (phase 03).
    first = proc.stderr.splitlines()[0]
    assert first.startswith("flags provided but not defined")


# ── gated: zero-token network probes ────────────────────────────────────────

gated = pytest.mark.skipif(
    not _GATED, reason="set UNTETHER_AGY_DRIFT=1 to run agy's network probes"
)


@pytest.fixture(scope="module")
def signed_in(tmp_path_factory: pytest.TempPathFactory) -> subprocess.CompletedProcess:
    """One cheap ``-p /model --mode bogus`` call: proves the host is signed
    in (else skip, never a 60 s wait) and doubles as the --mode probe."""
    if not _GATED:
        pytest.skip("set UNTETHER_AGY_DRIFT=1 to run agy's network probes")
    tmp = tmp_path_factory.mktemp("agy-drift")
    argv = ["agy", "-p", "/model", "--mode", "bogus", "--output-format", "stream-json"]
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            cwd=tmp,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"agy -p /model didn't answer in {_PROBE_TIMEOUT_S}s")
    if any(
        agy.stderr_kill_kind(line) == "auth" for line in proc.stderr.splitlines()
    ) or ("authentication failed" in proc.stdout):
        pytest.skip("agy isn't signed in on this host")
    return proc


@gated
def test_mode_values_cover_untether_modes(
    signed_in: subprocess.CompletedProcess,
) -> None:
    assert signed_in.returncode == 0
    match = _VALID_RE.search(signed_in.stderr)
    assert "unrecognized --mode value" in signed_in.stderr and match, signed_in.stderr
    values = {v.strip() for v in match.group(1).split(",")}
    assert values >= _UNTETHER_AGY_MODES, values
    assert agy.stderr_kill_kind(signed_in.stderr) is None
    _command_data(signed_in.stdout, "model")


@gated
def test_effort_values_superset(
    signed_in: subprocess.CompletedProcess, tmp_path: Path
) -> None:
    proc = _run_agy(
        ["-p", "/model", "--effort", "bogus", "--output-format", "stream-json"],
        tmp_path,
    )
    assert proc.returncode == 1
    match = _VALID_RE.search(proc.stderr)
    assert "invalid --effort" in proc.stderr and match, proc.stderr[:300]
    values = {v.strip() for v in match.group(1).split(",")}
    assert values >= _REQUIRED_EFFORTS, values  # xhigh / max allowed, not required
    (result,) = _events(proc.stdout)
    assert result["result"]["status"] == "ERROR"


@gated
def test_default_model_efforts(
    signed_in: subprocess.CompletedProcess, tmp_path: Path
) -> None:
    proc = _run_agy(["-p", "/effort", "--output-format", "stream-json"], tmp_path)
    assert proc.returncode == 0, proc.stderr[:300]
    data = _command_data(proc.stdout, "effort")
    assert isinstance(data.get("adjustable"), bool)
    if data["adjustable"]:
        available = data.get("available")
        assert isinstance(available, list) and available
        assert {"low", "high"} <= set(available), available
        assert data.get("current") in available


@gated
def test_joined_flag_values_are_never_parsed_as_flags(
    signed_in: subprocess.CompletedProcess, tmp_path: Path
) -> None:
    """Pins what ``utils/antigravity_argv.py`` relies on (agy 1.3.2): a
    dash-leading token after ``--model`` is parsed as a flag
    (``--model --version`` prints the version), while the joined
    ``--flag=value`` form is always a value. ``--conversation=`` rests on
    the same parser. If the joined form ever stops being a value, the
    validator is the only defence left — fix the runner before shipping."""
    joined = _run_agy(
        ["-p", "/effort", "--output-format", "stream-json", "--model=--version"],
        tmp_path,
    )
    assert joined.returncode == 1, joined.stdout[:300]
    assert "invalid model selection" in joined.stderr, joined.stderr[:300]
    assert '"--version"' in joined.stderr  # read as the model id
    assert not re.fullmatch(r"\d+\.\d+(\.\d+)?", joined.stdout.strip())

    ok = _run_agy(
        ["-p", "/effort", "--output-format", "stream-json", "--effort=high"],
        tmp_path,
    )
    assert "flags provided but not defined" not in ok.stderr, ok.stderr[:300]

    conversation = _run_agy(
        [
            "-p",
            "/effort",
            "--output-format",
            "stream-json",
            "--conversation=--version",
        ],
        tmp_path,
    )
    assert not re.fullmatch(r"\d+\.\d+(\.\d+)?", conversation.stdout.strip())
    assert "flags provided but not defined" not in conversation.stderr


@gated
def test_usage_payload_shape(
    signed_in: subprocess.CompletedProcess, tmp_path: Path
) -> None:
    proc = _run_agy(["-p", "/usage", "--output-format", "stream-json"], tmp_path)
    assert proc.returncode == 0, proc.stderr[:300]
    data = _command_data(proc.stdout, "usage")
    groups = data.get("groups")
    assert isinstance(groups, list) and groups
    for group in groups:
        assert isinstance(group.get("name"), str)
        buckets = group.get("buckets")
        assert isinstance(buckets, list) and buckets
        for bucket in buckets:
            assert isinstance(bucket.get("id"), str)
            assert bucket.get("window") in {"5h", "weekly"}, bucket
            fraction = bucket.get("remaining_fraction")
            assert isinstance(fraction, int | float) and 0 <= fraction <= 1
            reset = bucket.get("reset_time")
            assert isinstance(reset, str)
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", reset)


@gated
def test_invalid_model_shape(
    signed_in: subprocess.CompletedProcess, tmp_path: Path
) -> None:
    proc = _run_agy(
        [
            "-p",
            "/model",
            "--model",
            "does-not-exist-model",
            "--output-format",
            "stream-json",
        ],
        tmp_path,
    )
    assert proc.returncode == 1
    events = _events(proc.stdout)
    assert not any(e.get("event") == "init" for e in events)
    (result,) = events
    assert result["event"] == "result"
    assert result["result"]["status"] == "ERROR"
    assert "invalid model selection" in result["result"]["error"]
