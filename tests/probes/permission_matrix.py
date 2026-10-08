#!/usr/bin/env python3
"""Black-box permission-pipeline probe matrix (#749, phase 02 exit gate).

**Not collected by pytest** — the filename has no ``test_`` prefix on purpose.
Every row spawns a real ``claude`` process and spends real tokens, so this is
run deliberately, never in CI.

    uv run python tests/probes/permission_matrix.py --list
    uv run python tests/probes/permission_matrix.py --subset d3
    uv run python tests/probes/permission_matrix.py --subset all   # expensive

Why it exists
-------------
``decisions.md`` D-3 ("``plan``/``plan-auto`` keep ``--allowedTools``") is
marked **provisional**. It rests on probe G, which covered exactly one shape:
a ``Write`` to ``/tmp``. What that proved is *"this write shape doesn't
leak"*, not *"plan mode doesn't leak"*. If any mutating shape is found
reaching stage 5 under ``plan``, D-3 flips and ``plan``/``plan-auto`` join the
allowlist drop list.

The load-bearing design rule
----------------------------
**Every row asserts two things: did the parent get a callback, and did the
operation actually happen?** Callback-only assertions are how the original
too-narrow design nearly shipped — a tool can execute without ever raising a
``can_use_tool`` (that is precisely what stage 5 does).

So the probe never answers a control request — the CLI blocks, stdin closes,
and the operation does not proceed — then looks at the filesystem. Three
outcomes, each meaning something different:

===================  ==========  ============================================
callback seen?       side-effect  interpretation
===================  ==========  ============================================
no                   **yes**      ``LEAKED-STAGE5`` — leaked through stage 5;
                                  the allowlist pre-approved it and Untether
                                  never got a say
no                   no           ``NO-EXECUTE`` — the operation did not
                                  happen. See the caveat below: this does NOT
                                  distinguish "blocked at stage 4" from
                                  "reached stage 6"
yes                  no           ``REACHED-STAGE6`` — the request surfaced
yes                  yes          ``UNANSWERED-RAN`` — **broken**: it ran
                                  despite never being approved
===================  ==========  ============================================

Only ``LEAKED-STAGE5`` under ``plan`` would flip D-3.

Caveat: ``NO-EXECUTE`` is deliberately coarse
---------------------------------------------
``subprocess.run(input=...)`` closes stdin as soon as the payload is written,
so the CLI cannot complete a stage-6 round-trip and may abandon the request
rather than emit it. A ``NO-EXECUTE`` row therefore means only *"the operation
did not happen"* — it cannot tell you *why*. That is sufficient for the
question this probe exists to answer (does the allowlist let a mutating
operation through without Untether being asked?) but it is **not** evidence
about where the block occurred. Proving a request reaches Telegram needs the
real runner with a persistent stdin — that is integration-test territory
(`/qa`, Tier 2 C1-C6), not this harness.

Settings isolation is mandatory
------------------------------
Stage 5 is ``permissions.allow`` **plus** ``--allowedTools``, and
``permissions.allow`` is read from the operator's ``~/.claude/settings.json``.
The first run of this probe on lba-1 reported every prompting-mode row as
``LEAKED-STAGE5`` *even with the allowlist absent* — because that machine had
528 user-level allow rules. The probe was measuring the operator's settings,
not the CLI's mode semantics.

Every row therefore runs with ``CLAUDE_CONFIG_DIR`` pointed at an empty
directory. ``--settings`` is **not** sufficient: it loads *additional*
settings and cannot remove an existing allow rule. Project-level settings are
already excluded because each row runs in a fresh temp cwd.

If you change this file, do not remove the isolation — without it a green run
means nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Untether's real default — mirrored rather than imported so the probe stays a
# genuine black box over the CLI, not a test of our own constant.
DEFAULT_ALLOWED_TOOLS = "Bash,Read,Edit,Write"

TIMEOUT_S = 120


@dataclass
class Row:
    """One cell of the matrix."""

    name: str
    mode: str
    prompt: str
    # Relative to the scratch dir; the probe checks whether it exists after.
    side_effect_path: str
    allowlist: bool = True
    subsets: tuple[str, ...] = ()
    # The verdict this row is SUPPOSED to produce.  Several rows are expected
    # to leak — they exist to demonstrate the pre-rc9 bug, or they encode
    # correct upstream behaviour (acceptEdits auto-approves in-scope
    # filesystem commands at stage 4).  Without this the script is a report;
    # with it, it is a gate.
    expect: str = "NO-EXECUTE"


@dataclass
class Result:
    row: Row
    callbacks: list[str] = field(default_factory=list)
    side_effect: bool = False
    returncode: int | None = None
    error: str | None = None

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        if not self.callbacks and self.side_effect:
            return "LEAKED-STAGE5"
        if not self.callbacks and not self.side_effect:
            # Deliberately does not claim *where* it was blocked — see the
            # module docstring's caveat about stdin closing.
            return "NO-EXECUTE"
        if self.callbacks and not self.side_effect:
            return "REACHED-STAGE6"
        return "UNANSWERED-RAN"


def _rows() -> list[Row]:
    """The matrix.

    ``d3`` is the decisive subset: the mutating shapes under ``plan`` that
    probe G did not cover. Run that before trusting D-3.
    """
    rows: list[Row] = []

    # --- D-3: does any mutating shape leak through plan mode? -----------
    # `plan-auto` is deliberately absent: it maps to CLI `plan` and its argv
    # is byte-identical (the sugar is a parent-side ExitPlanMode rubber stamp,
    # invisible to the CLI). Probing it would double the token cost for an
    # identical spawn. Probe G already covered Write-to-/tmp; these are the
    # shapes it did not.
    rows.extend(
        Row(
            name=f"plan/{tool}/allowlist",
            mode="plan",
            prompt=prompt,
            side_effect_path=target,
            allowlist=True,
            subsets=("d3", "all"),
            expect="NO-EXECUTE",
        )
        for tool, prompt, target in (
            (
                "bash-touch",
                "Run exactly this bash command and nothing else: touch probe_bash.txt",
                "probe_bash.txt",
            ),
            (
                "write",
                "Create a file named probe_write.txt containing the word ok. "
                "Use the Write tool.",
                "probe_write.txt",
            ),
            (
                "edit",
                "Edit the existing file probe_seed.txt, replacing the word "
                "seed with edited. Use the Edit tool.",
                "probe_seed.edited",
            ),
        )
    )

    # --- The rc9 change itself: prompting modes must now reach stage 6 --
    for mode in ("default", "manual", "acceptEdits"):
        rows.append(
            Row(
                name=f"{mode}/bash-touch/no-allowlist",
                mode=mode,
                prompt=(
                    "Run exactly this bash command and nothing else: "
                    "touch probe_bash.txt"
                ),
                side_effect_path="probe_bash.txt",
                allowlist=False,
                # acceptEdits auto-approves an IN-SCOPE filesystem command at
                # stage 4 regardless of the allowlist — correct upstream
                # behaviour, and why the out-of-scope pair below exists.
                expect="LEAKED-STAGE5" if mode == "acceptEdits" else "NO-EXECUTE",
                subsets=("rc9", "all"),
            )
        )
        # Control: the same row WITH the allowlist is the pre-rc9 behaviour.
        # The pair is the point — the ONLY difference is the allowlist, so a
        # LEAKED/NO-EXECUTE split across the two is phase 02's whole claim.
        rows.append(
            Row(
                name=f"{mode}/bash-touch/allowlist",
                mode=mode,
                prompt=(
                    "Run exactly this bash command and nothing else: "
                    "touch probe_bash.txt"
                ),
                side_effect_path="probe_bash.txt",
                allowlist=True,
                # The pre-rc9 behaviour this release removes.
                expect="LEAKED-STAGE5",
                subsets=("rc9", "all"),
            )
        )

    # --- D-4: acceptEdits must still gate an OUT-OF-SCOPE write ---------
    # An in-scope `touch` is auto-approved by acceptEdits' own stage-4 logic
    # (correct upstream behaviour), so an in-scope row proves nothing about
    # D-4. Only an out-of-scope target isolates the allowlist's effect.
    rows.extend(
        Row(
            name=f"acceptEdits/write-out-of-scope/{'allowlist' if allow else 'no-allowlist'}",
            mode="acceptEdits",
            prompt=(
                "Create a file at the absolute path /tmp/ut_probe_oos.txt "
                "containing the word ok. Use the Write tool."
            ),
            side_effect_path="/tmp/ut_probe_oos.txt",
            allowlist=allow,
            # D-4 in one line: the allowlist is the whole difference between
            # an out-of-scope write running silently and being gated.
            expect="LEAKED-STAGE5" if allow else "NO-EXECUTE",
            subsets=("rc9", "all"),
        )
        for allow in (False, True)
    )

    # --- Autonomous modes stay quiet ------------------------------------
    rows.extend(
        Row(
            name=f"{mode}/bash-touch/allowlist",
            mode=mode,
            prompt=(
                "Run exactly this bash command and nothing else: touch probe_bash.txt"
            ),
            side_effect_path="probe_bash.txt",
            allowlist=True,
            expect="LEAKED-STAGE5",
            subsets=("autonomous", "all"),
        )
        for mode in ("auto", "dontAsk", "bypassPermissions")
    )

    return rows


def _build_argv(claude: str, row: Row) -> list[str]:
    """Untether's exact argv shape — see ClaudeRunner.build_args."""
    argv = [
        claude,
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--verbose",
    ]
    if row.allowlist:
        argv += ["--allowedTools", DEFAULT_ALLOWED_TOOLS]
    argv += ["--permission-mode", row.mode]
    argv += ["--permission-prompt-tool", "stdio"]
    return argv


def _stdin_payload(prompt: str) -> bytes:
    init = {
        "type": "control_request",
        "request_id": "init_probe",
        "request": {"subtype": "initialize", "hooks": None},
    }
    user = {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }
    return (json.dumps(init) + "\n" + json.dumps(user) + "\n").encode()


def _isolated_env(config_dir: Path) -> dict[str, str]:
    """Env that hides the operator's `permissions.allow` from stage 5.

    OAuth credentials live beside `settings.json`, so copy just that one file
    across — enough to authenticate, not enough to carry any permission rule.
    """
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    real = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    creds = real / ".credentials.json"
    if creds.exists():
        shutil.copy2(creds, config_dir / ".credentials.json")
    return env


def run_row(claude: str, row: Row) -> Result:
    result = Result(row=row)
    with tempfile.TemporaryDirectory(prefix="ut-probe-") as tmp:
        scratch = Path(tmp) / "work"
        scratch.mkdir()
        config_dir = Path(tmp) / "config"
        config_dir.mkdir()
        # Seed file for the Edit shape; its *absence* of change is the signal.
        (scratch / "probe_seed.txt").write_text("seed\n")
        # Out-of-scope rows target a fixed absolute path, which unlike the
        # temp cwd is NOT fresh per run — a leftover from a previous row would
        # read as a side effect this row did not cause.
        if row.side_effect_path.startswith("/"):
            Path(row.side_effect_path).unlink(missing_ok=True)
        try:
            proc = subprocess.run(
                _build_argv(claude, row),
                input=_stdin_payload(row.prompt),
                capture_output=True,
                cwd=scratch,
                timeout=TIMEOUT_S,
                env=_isolated_env(config_dir),
            )
        except subprocess.TimeoutExpired:
            result.error = f"timeout after {TIMEOUT_S}s"
            return result
        except (OSError, subprocess.SubprocessError) as exc:
            result.error = f"spawn failed: {exc}"
            return result

        result.returncode = proc.returncode
        for line in proc.stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "control_request":
                continue
            request = event.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                result.callbacks.append(str(request.get("tool_name", "?")))

        target = scratch / row.side_effect_path
        if row.side_effect_path.endswith(".edited"):
            seed = scratch / "probe_seed.txt"
            result.side_effect = seed.read_text().strip() != "seed"
        else:
            result.side_effect = target.exists()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subset",
        default="d3",
        help="d3 (default, decisive for the provisional decision) | rc9 | "
        "autonomous | all",
    )
    parser.add_argument("--list", action="store_true", help="print rows, run nothing")
    args = parser.parse_args()

    rows = [r for r in _rows() if args.subset in r.subsets]
    if not rows:
        print(f"no rows match subset {args.subset!r}", file=sys.stderr)
        return 2

    if args.list:
        for row in rows:
            allow = "allowlist" if row.allowlist else "no-allowlist"
            print(f"{row.name:<44} mode={row.mode:<18} {allow}")
        print(f"\n{len(rows)} rows — each spawns a live claude run.")
        return 0

    claude = shutil.which("claude")
    if claude is None:
        print("claude CLI not installed", file=sys.stderr)
        return 2

    print(f"running {len(rows)} live rows (subset={args.subset})\n")
    results = [run_row(claude, row) for row in rows]

    width = max(len(r.row.name) for r in results)
    print(f"{'row':<{width}}  {'effect':<7}  {'verdict':<14}  expected")
    print("-" * (width + 40))
    for res in results:
        flag = "" if res.verdict == res.row.expect else "   <-- MISMATCH"
        print(
            f"{res.row.name:<{width}}  {res.side_effect!s:<7}  "
            f"{res.verdict:<14}  {res.row.expect}{flag}"
        )

    mismatched = [r for r in results if r.verdict != r.row.expect]
    print()
    if not mismatched:
        print(f"all {len(results)} rows matched expectations.")
        return 0

    print(f"{len(mismatched)} row(s) did not match:")
    for res in mismatched:
        detail = res.error or f"got {res.verdict}, expected {res.row.expect}"
        print(f"  - {res.row.name}: {detail}")

    plan_leaks = [
        r for r in mismatched if r.row.mode == "plan" and r.verdict == "LEAKED-STAGE5"
    ]
    if plan_leaks:
        print(
            "\n  A plan-mode leak FLIPS decisions.md D-3: plan/plan-auto must "
            "join the allowlist drop list in _build_args."
        )
    if any(r.verdict == "UNANSWERED-RAN" for r in mismatched):
        print("\n  UNANSWERED-RAN: an operation ran without ever being approved.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
