---
description: Execute ONE approved plan-pack phase to a verified hand-off — ground-truth the pack, compose superpowers executing-plans + test-driven-development, build in-scope only, verify (phase tests + pytest + ruff), commit to feature/<slug>-phase-N, STOP before the PR. Refuses multi-phase. Never opens the PR (/pr-dev does) or widens scope.
argument-hint: "<slug> <phase-N> | <slug> (read-only next-up preview) | <slug> <phase-N> --dry-run | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Write Edit Skill ToolSearch Bash(git:*) Bash(uv run pytest:*) Bash(uv run ruff:*) Bash(python3 scripts/validate_release.py:*) Bash(grep:*) Bash(rg:*) Bash(ls:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*)
---

You are handling `/implement`. `/implement` executes **exactly one approved
phase** of a `docs/plans/<slug>/` pack to a verified hand-off, then **STOPS before
the PR** (`/pr-dev` opens it). It is the *build* half of delivery.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules). For
`/implement`:

- **Authority:** code + tests **in the approved phase scope only**, plus the one
  `progress-tracker.md` 🟡 mark. Never opens the PR. Never widens scope
  (→ STOP + ask). Never takes defect work (→ `/fix`). Never runs paid work (N/A).
- **Release-guard.** Commit to `feature/<slug>-phase-N` (branch off `dev`). Never
  master/tag/release. Never `--no-verify`.
- **Dev/staging.** Verify on `untether-dev`; never restart staging. Never
  `systemctl restart` from inside this session.
- **Refuse multi-phase.** One phase per invocation, always.

## Reuse (compose, don't re-describe)

Via the Skill tool: `superpowers:executing-plans` (phase discipline) +
`superpowers:test-driven-development` (red→green→refactor) +
`superpowers:verification-before-completion` (evidence before "done"). If the
phase touches a runner/schema/telegram surface, also load the matching
`.claude/rules/*.md` and the relevant `.claude/skills/*`.

## Sub-commands

| Form | Action |
|---|---|
| `/implement <slug> <phase-N>` | Execute that phase (TDD), commit, stop before PR |
| `/implement <slug>` | Read-only: print the next-up phase + its pre-flight status |
| `/implement <slug> <phase-N> --dry-run` | Write the failing tests + stage locally; no branch push |
| `/implement --help` | Usage, then stop |

## Flow

### I-1. Gate check

- Resolve `docs/plans/<slug>/` and the named phase file. If the pack or phase is
  missing → STOP (route to `/plan`).
- Confirm the phase is **approved** and its dependencies (earlier phases) are ✅
  in `progress-tracker.md`. If a dependency is unmet → STOP and say which.
- If more than one phase is named → **refuse** (one phase only).

### I-2. Pre-flight — ground-truth the pack (the #1200 analogue)

Plan packs over-count "missing" work and can cite recipes that have since
changed. Before building:

- **grep the phase's named deliverables against real code** — many "to create"
  items already exist. Adjust scope to reality.
- **re-verify the phase's premises live** (a file it says to edit still exists;
  a signature it targets is still emitted).
- **run any CLI/command recipe the pack cites** to confirm it actually works
  before depending on it.

Report any drift found and adjust the phase scope (do not silently widen).

### I-3. Build in-scope (TDD)

- Write the phase's named tests **first** (red), then the minimal code to green
  them (`superpowers:test-driven-development`).
- Stay strictly inside the phase's declared file scope. A needed change outside
  scope → **STOP + ask** (or split into a follow-up phase); never silently widen.

### I-4. Whole-class sweep (the #1201 analogue)

If the change touches a **chokepoint / guard / shared literal** (a constant, a
helper every runner calls, a renamed basename), grep it to **every** sibling
site — all call sites, every consumer **including `tests/`**, and for renamed
basenames the **whole repo, not just `*.py`**: `pyproject.toml`,
`.github/workflows/*`, `contrib/*`. A half-applied chokepoint change is a
regression.

### I-5. Verify

Run, in order:

```bash
uv run pytest tests/test_<phase-named>.py -x      # the phase's tests, targeted
uv run pytest                                     # full suite (80% coverage gate)
uv run ruff check src/
uv run ruff format --check src/ tests/
```

If the phase touched a **runner / schema / telegram** surface, also run the
rule-mandated suite and update docs:

```bash
uv run pytest tests/test_*_runner.py tests/test_claude_control.py -x
```

- update `CLAUDE.md`'s `## Tests` list (test counts + new file description) and
  the relevant `docs/reference/*` per `.claude/rules/runner-development.md` /
  `testing-conventions.md`.

All must be green before commit. Fix-loop until green (`superpowers:
verification-before-completion` — evidence before claiming done).

### I-6. Commit + mark progress

- Stage **explicit paths** (never `git add -A`) and commit to
  `feature/<slug>-phase-N` with a conventional message (`feat(area): … (phase N)`).
- Flip the phase to 🟡 (or ✅ if fully verified) in `progress-tracker.md` — the
  one plan-doc write `/implement` may make.

### I-7. STOP + hand-off

Do **not** open the PR. Print a brief report (what was built, tests green, files
touched) and the hand-off:

- `/qa` — validate a risk-bearing phase against the dev bot before delivery.
- `/pr-dev` — open the PR to `dev` when the branch is delivery-ready.
- `/handover` — if the phase is paused mid-build.

## Anti-patterns

- No multi-phase in one invocation.
- No scope-widening — STOP + ask, or a follow-up phase.
- No defect work — route to `/fix`.
- No PR (that's `/pr-dev`), no master/tag/release, no `--no-verify`.
- No `git add -A`; no restarting staging to verify.
- No half-applied chokepoint change (I-4 whole-class sweep is mandatory).

`--help` prints the sub-command table, then stops.

End of /implement. Delivery of the finished branch is `/pr-dev`.
