---
description: Turn an idea (incoming/<file>.md | #issue | inline) into a grounded, phased plan pack under docs/plans/<slug>/. Read-only on code — brainstorm, map current state, gate research, blueprint with a reuse map, write the pack. Never writes code. Runs in plan mode by nature.
argument-hint: "[incoming/<file>.md] | [#NN] | [inline idea text] | [<slug> --status] | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Write Edit Skill ToolSearch Agent Bash(gh issue view:*) Bash(gh issue list:*) Bash(gh issue create:*) Bash(git log:*) Bash(git diff:*) Bash(ls:*) Bash(grep:*) Bash(date:*) Bash(wc:*)
---

You are handling `/plan`. `/plan` turns an idea into a **grounded, phased plan
pack** under `docs/plans/<slug>/`. It is the *scope* half of delivery — read-only
on code, it produces the artefact that `/implement` later executes one phase at a
time.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules). For
`/plan`:

- **Authority: writes plan docs only, never code.** Runs in plan mode by nature.
- **Reuse, don't duplicate.** The blueprint MUST include a **reuse map** citing
  existing Untether files/skills to reuse *before* proposing new code. A plan
  with no reuse map is incomplete.
- **Research gate.** Never plan provider/API-sensitive or current-truth work from
  memory — route it to the global `/research` and capture findings under
  `docs/findings/<YYYY-MM-DD>-<slug>.md`, then cite them.
- **Date every current-state claim.** State-of-the-world facts drift; stamp them.
- **Note on location:** `docs/plans/` is gitignored — plan packs are **local
  by design** (this very plan lives there untracked). `/implement` reads them
  locally; only committed code lands in git.

## Sub-commands

| Form | Action |
|---|---|
| `/plan incoming/<file>.md` | Plan from a captured idea file |
| `/plan #NN` | Plan from a GitHub issue |
| `/plan <inline idea text>` | Plan from a one-line idea |
| `/plan <slug> --status` | Read-only: print an existing pack's phase table + progress |
| `/plan --help` | Usage, then stop |

## Flow

### P-1. Redirect check (don't plan the wrong thing)

Classify the idea first:

- A **defect / regression** → STOP, route to `/fix` (or `/debug` to understand).
  A plan pack is for net-new capability, not bug work.
- An **existing pack** (a `docs/plans/<slug>/` already covers this) → STOP, don't
  re-plan; offer `--status` or route to `/implement` for the next phase.

### P-2. Brainstorm intent (superpowers)

Invoke `superpowers:brainstorming` via the Skill tool to explore intent,
requirements, and design *before* mapping code. Capture the sharpened problem
statement and success criteria.

### P-3. Map current state (read-only, Explore)

- **Overlap search first:** `ls docs/plans/` and grep existing packs so you don't
  duplicate or contradict an in-flight plan.
- Launch the **Explore** agent (read-only) to map the relevant subsystems: which
  files, events, rules, and runners the idea touches. Prefer a subagent so the
  file dumps stay out of context — keep the conclusion.
- Write findings into `00-current-state.md`, **dating** each claim.

### P-4. Research gate

If the idea depends on external/current truth (an engine's CLI behaviour, a
provider's billing/API, a library's current API), route to the global
`/research`, save `docs/findings/<date>-<slug>.md`, and cite it. Do **not**
proceed on memory for provider/API-sensitive work.

### P-5. Blueprint + reuse map (feature-dev:code-architect)

Launch `feature-dev:code-architect` (read-only) for an implementation blueprint.
Require, as part of it, a **reuse map**: for each planned capability, name the
existing Untether file/skill/rule to reuse or extend *before* any new file.
Phase the work into the smallest independently-shippable units, each with a real
**exit gate**.

### P-6. Write the pack

Create `docs/plans/<slug>/`:

| File | Contents |
|---|---|
| `README.md` | index + phase table + build order + dependencies |
| `00-current-state.md` | dated current-state map (from P-3) + reuse map (from P-5) |
| `01-*.md` … `NN-*.md` | one per phase: **goal · scope · files · tests (pytest-shaped) · exit-gate · rollback · risk** |
| `decisions.md` | key decisions + rejected alternatives + why |
| `progress-tracker.md` | per-phase status (⬜ todo / 🟡 in-progress / ✅ done) — `/implement` flips 🟡 |

**Exit-gate shape (Untether):** a phase's exit gate is *"the event/log signature
this phase produces is actually emitted by a real code path, not a fixture"* —
the read-model-wired-feed analogue. Name the concrete signature (an `event=…`
structlog line, a passing `tests/test_<x>.py::test_<y>`, an attestation marker).

**Dropped from AT (do not add):** no `coverage.md` / quality-coverage inventory
sweep — Untether has no DQ/coverage matrix. Use the reuse-map + whole-class grep
discipline instead (the latter is enforced in `/implement`).

### P-7. Optional tracking issue

Offer (confirm-gated) a GitHub tracking issue labelled `idea` (Untether's Trello
analogue). De-dupe against open issues first. Do not create silently.

### P-8. Report + hand-off

Brief report: the pack path, the phase table, and the hand-off:

- `/implement <slug> <phase-1>` — build the first approved phase.
- `/research` — if the research gate flagged an open provider/API question.
- `/handover` — if planning is paused mid-flight.

## Anti-patterns

- No reuse map → the plan is incomplete; do not finish without it.
- Don't re-plan an existing pack (`--status` instead).
- Don't plan provider/API-sensitive work from memory (research gate).
- Never write code — `/plan` is read-only on code.
- Don't over-phase: a phase must be independently shippable with a real exit gate.

`--help` prints the sub-command table, then stops.

End of /plan. Execution of an approved phase is `/implement`.
