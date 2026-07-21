# Untether — Loop Registry

The visible index of Untether's repeatable workflows. This file **documents and
indexes** loops — it is **not** an orchestration DSL, and it must **not**
duplicate config values that drift (thresholds, chat IDs, versions live in code,
`untether.toml`, and the rules; this file links them, never restates them).

Two kinds of loop live here:

- **Agentic loops** (`L*`) — slash commands you (or Claude) drive. Each has a
  companion command file under `.claude/commands/` and, where relevant, a thin
  always-on rule under `.claude/rules/`.
- **Automated loops** (`A*`) — non-agentic machinery already running (daemons,
  cron, CI, fleet scripts). Listed for completeness so a human can see the whole
  system, not to re-implement it.

Each loop records four fields (AT's shape) plus a build **Status**:

- **Trigger** — what starts it.
- **Driver** — the command/skill/engine that runs it.
- **Output** — what it produces (the hand-off product).
- **Authority** — what it is allowed to do, and the hard boundary it must not cross.

> **Status legend:** `available` = built and usable now · `planned Pn` = specified
> in `docs/plans/agentic-loops-and-commands/` for phase *n*, not yet built.

---

## Index

```
Delivery loops (net-new capability)
  L1   /plan          idea → phased pack under docs/plans/<slug>/ (read-only)      [available]
  L2   /implement     approved phase → feature branch, TDD, stop before PR         [available]
  L3   /qa            validate a target; drive integ. tiers vs dev bot; attest     [available]
  L4a  /pr-dev        green + docs → ONE batch PR to dev (→ TestPyPI); may merge dev [available]
  L4b  /pr-main       release-prep → open dev→master PR → STOP (Nathan merges → PyPI) [available]

Production loops (defects)
  L5   /debug         8-step investigate (sweep / targeted) — fleet-aware          [available]
  L6   /fix           8-step act — sweep actionable defects → needs-verification   [available]

Continuous improvement
  L7   /kaizen        session-end capture → [kaizen] GitHub collector              [available]
  L8   /kaizen-review weekly propose-only promotion                                [available]

Continuity
  L9   /handover      H0–H3 interruption stop-state (state-derived)                [available]

Support commands + conventions
  /docs            escape-hatch: reconcile docs OUTSIDE a PR (default = folded into /pr-dev) [available]
  /research        reuse the global /research; findings land under docs/findings/  [available]
  agents           advisory non-authoring reviewers: debug-reviewer / delivery-reviewer / qa-reviewer [available]

Automated (non-agentic — already live)
  A1   untether-issue-watcher daemon (auto:error-report) — 5 hosts, host-tagged
  A2   /monitor cron (auto:monitor-audit) — per-host + untether-fleet meta-target
  A3   fleet-rollout.sh / fleet-rollback.sh / fleet-status.sh — operator, attestation-gated
  A4   run-integration-tests.sh — writes the per-VERSION attestation marker
  A5   CI (format/ruff/ty/pytest 3.12-3.14/build/lockfile/pip-audit/bandit/codeql/docs)
  A6   release pipeline (auto-tag-on-master.yml → release.yml, OIDC → PyPI) — OPERATOR gate

Intentionally NOT built
  /paid-run       — no billable CLI calls of Untether's own
  /dq-spot-check  — no warehouse / no DQ patterns
  /cost-watch     — cost lives in runtime budget config (cost_tracker.py + [watchdog]), not a command
  /issue-triage   — covered by A1 + A2
  /context-health — covered by the context hooks + the context-quality rule
```

The delivery model is **three boundaries, not five stages** —
`PREPARE (/plan→/implement OR /fix) → VALIDATE (/qa) → RELEASE (/pr-dev → dev; /pr-main → open master PR, STOP)`.
See `docs/plans/agentic-loops-and-commands/README.md` §7 for the diagram and rationale.

---

## Agentic loops

### L5 · `/debug` — investigate  ·  Status: **available**

- **Trigger:** a bug / regression / incident to understand (no code authority needed yet), or a periodic triage sweep.
- **Driver:** `.claude/commands/debug.md` + the `.claude/commands/debug/` 8-step bundle. Reuses `~/.claude/commands/monitor/{severity-rubric,signal-categories}.md`.
- **Output:** *sweep* → a printed ranked Triage Report (no filing). *targeted* → a per-issue Debug Report comment + `needs-verification` (posted only on explicit approval).
- **Authority:** diagnose only. Ships at most a *minimal* ≤3-file fix in `targeted`. Never files in sweep mode; never auto-closes; never pushes/merges to master; never restarts staging.

### L6 · `/fix` — act  ·  Status: **available**

- **Trigger:** one or more open actionable defect issues to ship fixes for.
- **Driver:** `/fix` (thin action layer over the same `.claude/commands/debug/` engine).
- **Output:** per-fix branch off `dev` → PR **to `dev`** with the batch-PR body shape (`Issue | Root cause | Fix | Live verification` + `## Tests`) + `needs-verification`.
- **Authority:** branch/fix/test/CHANGELOG/PR-to-`dev` only. Runaway-capped (>8 → top 5 + `queued-next-run`; >15 → STOP diagnosis-only). Never batches independent high-risk state machines; never master/tag/release; never closes an unverified fix.

### L1 · `/plan` — scope  ·  Status: **available**

- **Trigger:** an idea (`incoming/<file>.md` | `#issue` | inline) that is net-new capability, not a defect.
- **Driver:** `/plan` wrapping `superpowers:brainstorming` + Explore + `feature-dev:code-architect`.
- **Output:** a phased plan pack under `docs/plans/<slug>/` (index + phase files + reuse map + decisions + progress-tracker). Read-only on code.
- **Authority:** writes plan docs only, never code. Runs in plan mode by nature.

### L2 · `/implement` — build one phase  ·  Status: **available**

- **Trigger:** one approved phase of an existing plan pack.
- **Driver:** `/implement` composing `superpowers:executing-plans` + `test-driven-development`.
- **Output:** in-scope code + tests committed to `feature/<slug>-phase-N`, verified, **stopped before the PR**.
- **Authority:** code + tests in the approved phase scope only. Refuses multi-phase; never opens the PR (`/pr-dev` does); never widens scope; defect work routes to `/fix`.

### L3 · `/qa` — validate  ·  Status: **available**

- **Trigger:** "is this validated enough for its risk?" — before a merge, before a release, or a retest after `/fix`.
- **Driver:** `/qa` implementing `docs/reference/integration-testing.md` (tier definitions + chat IDs + Telegram MCP tools).
- **Output:** capped (≤5/run) findings routed to `/debug`→`/fix`; on a green release tier, the attestation marker `~/.untether-dev/integration-test-pass-<VERSION>.json`.
- **Authority:** read + safe local tests + **bounded** live drive of the **allowlisted dev bot only** (defaults to plan/dry-run; `--run` to drive). Never fixes code, merges, tags, releases, or rolls the fleet. Fails closed if the target can't be proven to be the dev bot.

### L4a · `/pr-dev` — finalise → PR to `dev`  ·  Status: **available**

- **Trigger:** a feature/fix/chore branch at "code + tests done".
- **Driver:** `/pr-dev` (docs reconciliation folded in as a completion criterion).
- **Output:** ONE merge-ready PR to `dev` with the table-shaped body; docs/CHANGELOG/FAQ/`## Tests` reconciled inline. Merge → TestPyPI (automatic CI).
- **Authority:** stage explicit paths; open a PR to `dev`; merge **only** with `--merge` + confirm + base = `dev` (the one merge Claude may do). Never master/tag/release/deploy.

### L4b · `/pr-main` — release-prep → open `dev`→`master` PR, STOP  ·  Status: **available**

- **Trigger:** `dev` is green + ahead of `master` and a stable `X.Y.Z` is decided.
- **Driver:** `/pr-main`.
- **Output:** stable version bump + `uv lock` + collapsed CHANGELOG + FAQ pass + the opened `dev`→`master` PR (release body), then **STOP**.
- **Authority:** everything Claude *may* do up to the operator boundary. Never merges to master, tags, `gh release create`, or runs `fleet-rollout.sh`. The master merge is Nathan's single release gate.

### L7 · `/kaizen` — capture a process learning  ·  Status: **available**

- **Trigger:** session end (self-invoked; a Stop-hook nudge is proposed to Nathan for wiring).
- **Driver:** `/kaizen` + `.claude/rules/kaizen.md` (thin slice) + `docs/kaizen/README.md` (policy).
- **Output:** 0–3 evidence-linked bullets appended to the permanent `[kaizen]` GitHub collector issue. **0 captures is valid.**
- **Authority:** read-only except ONE `gh issue comment`. Never edits rules/hooks/code.

### L8 · `/kaizen-review` — promote learnings  ·  Status: **available**

- **Trigger:** weekly (human-gated); monthly `--monthly` health sample.
- **Driver:** `/kaizen-review` (propose-only).
- **Output:** approval packets → on Accept, a propose-only artefact (pytest/doc/rule draft + GH issue) and the source bullet struck.
- **Authority:** propose only. Never auto-edits `.claude/rules/`, `hooks.json`, `CLAUDE.md`, or code.

### L9 · `/handover` — interruption stop-state  ·  Status: **available**

- **Trigger:** work genuinely paused/blocked/moving between sessions.
- **Driver:** `/handover` (state-derived).
- **Output:** H0 (none) / H1 (inline note) / H2 (`incoming/handovers/<date>-<slug>.md`, gitignored) / H3 (`docs/handovers/<date>-<slug>.md`, committed).
- **Authority:** derive `complete[]`/`decisions[]`/`next_action` from persisted state (git, test runs, `session_quarantine.json`, logs) — never from chat memory. Default DOWN between levels; routine ends are H0.

---

## Support commands + conventions

Not full loops — helpers the loops lean on.

### `/docs` — reconcile docs outside a PR  ·  Status: **available**

- **Trigger:** documentation drifted with **no code change** to deliver alongside it.
- **Driver:** `.claude/commands/docs.md`. The default path is `/pr-dev` (docs are folded in as a completion criterion); `/docs` is the escape hatch.
- **Output:** minimal edits to CHANGELOG / `docs/faq/faq.md` / `CLAUDE.md ## Tests` / `docs/reference/*`.
- **Authority:** docs only. No code, no PR (a code branch routes to `/pr-dev`), no master/tag/release. FAQ is gate-protected.

### `/research` + `docs/findings/` — current-truth convention  ·  Status: **available**

- **Trigger:** a provider/API/current-truth question that must not be answered from memory (the research gate in `/plan`, `/debug`, `/qa`).
- **Driver:** the **global** `/research` command (Untether ships no research loop of its own).
- **Output:** a cited note under `docs/findings/<date>-<slug>.md` (committed). Packs/reports **cite** it; they never restate it. See `docs/findings/README.md`.

### Advisory reviewer agents (non-authoring)  ·  Status: **available**

Read-only, verdict-returning reviewers under `.claude/agents/`, invoked via the Agent tool. They surface gaps; they author nothing (no edits, no filing, no merge).

| Agent | Reviews | Reject-on |
|---|---|---|
| `debug-reviewer` | a `/debug`/`/fix` hand-off (Debug Report + fix) | symptom-not-root-cause, un-falsifiable verification, co-batched high-risk state machines |
| `delivery-reviewer` | a `/pr-dev`/`/pr-main` hand-off | wrong PR base, red locally, missing docs completion, any master-merge/tag/release boundary crossed |
| `qa-reviewer` | a `/qa` run | non-dev-bot target, unbounded drive, hand-written/non-green marker, authority escalation |

---

## Automated loops (already live — do not re-implement)

| ID | What | Where |
|---|---|---|
| A1 | `untether-issue-watcher` daemon — files `auto:error-report` from error-log patterns, host-tagged | 5 hosts (lba-1, nsd, channelo, sl, mac); `contrib/untether-issue-watcher.*` |
| A2 | `/monitor` cron — files `auto:monitor-audit` (bugs + enhancements) | per-host configs + `untether-fleet` meta-target |
| A3 | `fleet-rollout.sh` / `fleet-rollback.sh` / `fleet-status.sh` — parallel upgrade/rollback/status, attestation-gated | `scripts/` (operator-run) |
| A4 | `run-integration-tests.sh` — writes the per-VERSION attestation marker | `scripts/` |
| A5 | CI — format / ruff / ty / pytest 3.12–3.14 / build / lockfile / pip-audit / bandit / codeql / docs | `.github/workflows/` |
| A6 | Release pipeline — `auto-tag-on-master.yml` → `release.yml` (OIDC → PyPI) | OPERATOR gate: the `dev`→`master` PR merge |

---

## Cross-cutting rules

Every agentic loop obeys `.claude/rules/workflow-commands.md` (the routing table +
the 7 cross-cutting rules) and the release-guard boundary. See that rule and the
plan (`docs/plans/agentic-loops-and-commands/README.md`) for the full design.
