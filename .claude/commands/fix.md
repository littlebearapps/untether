---
description: Action-authority defect sweep for Untether — run the 8-step /debug engine to a verified hand-off across open actionable defect issues, one at a time, rank-ordered. Branch off dev, minimal fix, tests+lint+changelog, PR to dev, needs-verification. Runaway-capped, batch-cohesion aware. Never master/tag/release.
argument-hint: "[] (full sweep) | [#NN #MM ...] scoped | [--dry-run] | [--resume-from #NN] | [--help]"
disable-model-invocation: true
allowed-tools: Edit Write Read Glob Grep Skill ToolSearch Bash(git:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh issue comment:*) Bash(gh issue edit:*) Bash(gh pr create:*) Bash(gh pr list:*) Bash(gh pr view:*) Bash(gh search:*) Bash(gh api:*) Bash(gh label list:*) Bash(journalctl:*) Bash(ssh:*) Bash(systemctl --user is-active:*) Bash(systemctl --user status:*) Bash(uv run pytest:*) Bash(uv run ruff:*) Bash(python3 scripts/validate_release.py:*) Bash(grep:*) Bash(jq:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(sort:*) Bash(uniq:*) Bash(awk:*) Bash(sed:*) Bash(ls:*) Bash(cat:*)
---

You are handling the user's `/fix` request. `/fix` is Untether's **action** half
of the debug engine: it runs the same 8-step protocol as `/debug`, but with
**ship authority** — it sweeps the open actionable-defect list to a *verified
hand-off* (PR to `dev` + `needs-verification`), one issue at a time,
rank-ordered.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` — the routing table + the 7
cross-cutting rules. In particular for `/fix`:

- **Release-guard obedience.** Branch off `dev`; PR **to `dev`**; never
  push/merge to `master`, never tag, never `gh release create`, never
  `--no-verify`. The GitHub branch ruleset + CODEOWNERS is the real gate.
- **Dev/staging separation.** Verify fixes on `untether-dev.service`; never
  restart `untether.service` (staging). Never `systemctl restart` from inside
  this session (hot-reload drain drops the final message).
- **Confirm-gated + idempotent.** Surface each drafted PR body / issue comment
  and wait for a tap; de-dupe against open **and** closed issues; a re-invoked
  `/fix` must not double-open a PR or double-comment.
- **Redaction.** Scrub tokens/keys/env/chat-content/fleet identifiers from any
  evidence before it lands in a PR or issue (see
  `.claude/commands/debug/step-evidence.md`).
- **Untether-mode.** `AskUserQuestion`/`ExitPlanMode` return empty under
  Telegram — state assumptions in text and STOP for a reply. Keep the run report
  brief (≈500–1500 chars).

## Boundary vs `/debug`

`/debug` = diagnose authority (pauses after hypothesis; ships only a *minimal*
≤3-file fix in `targeted`). `/fix` = the whole-list action sweep. **One
ship-path** (`.claude/commands/debug/step-fix.md`); two authority levels. If a
target needs investigation first, run `/debug` — don't guess a fix.

## Sub-commands

| Form | Action |
|---|---|
| `/fix` (no args) | Full sweep: rank the open actionable-defect list, process top-ranked to a hand-off |
| `/fix #NN [#MM ...]` | Scoped: only the named issues, still rank-ordered |
| `/fix --dry-run` | Propose-only: stage locally, print the would-be PR(s); open nothing |
| `/fix --resume-from #NN` | Resume a sweep from a specific issue (skip already-handled) |
| `/fix --help` | Print usage and stop |

## Reuse map (do not duplicate)

The 8-step engine and its detail live in the debug bundle — read the relevant
file before each step, never rederive:

- `.claude/commands/debug/{step-classify,step-evidence,step-research,systemic-patterns,step-fix,step-verify,output-template}.md`
- Rules: `.claude/rules/{runner-development,telegram-transport,control-channel,release-discipline,testing-conventions,dev-workflow,help-faq}.md`
- Severity/signal: `~/.claude/commands/monitor/{severity-rubric,signal-categories}.md`
- Superpowers (via the Skill tool): `superpowers:systematic-debugging`,
  `superpowers:test-driven-development`, `superpowers:verification-before-completion`.

## The sweep

### F-1. Build the actionable list

```bash
gh issue list --repo littlebearapps/untether --state open --limit 200 \
  --json number,title,labels,createdAt,updatedAt,comments
```

Actionable = a defect with a clear repro or evidence and no blocking question.
**Exclude**: `enhancement`-only, `needs-verification` (already handed off, awaiting
Nathan's close), by-design signals (cross-check `systemic-patterns.md`), and
anything already carrying an open PR. De-dupe against closed issues (regression
check) with a 90-day `closed:>=` search.

In scoped mode (`#NN ...`), the list is exactly those issues (still filtered for
by-design/already-handed-off).

### F-2. Rank

Order the actionable list:

1. **reopened / `regression-candidate`** first (a prior fix regressed).
2. `severity:critical` > `severity:major` > `severity:minor` (tie-break on
   `priority: high` > `medium` > `low`).
3. systemic / recurring (matches a `systemic-patterns.md` cluster).
4. occurrence count over the window (from fleet logs — see `step-evidence.md`).
5. age (older first, among equals).

### F-3. Runaway protection (tunable)

Count the actionable list **N**:

- **N ≤ 8** → process all, subject to batch-cohesion (F-4).
- **8 < N ≤ 15** → process the **top 5** by rank; mark the rest
  `queued-next-run` via a one-line issue comment (idempotent — skip if the
  marker comment already exists). Report the deferred set.
- **N > 15** → **STOP**. Do not code. Run a diagnosis-only pass
  (`pal debug` / `superpowers:systematic-debugging`) over the *cluster* to find
  a shared root cause; file or annotate **ONE** issue with the cluster analysis;
  zero code changes. A list this long usually means a systemic root, not 15
  independent bugs.

### F-4. Batch-cohesion rule (Untether-specific)

The sweep may fix many issues in one run, but **never co-batch fixes that touch
independent high-risk state machines**:

> session lifecycle / resume · signal-death handling · watchdog / stall ·
> hot-reload · rate-limit / cost

Each such fix gets **its own branch + its own PR** (matches Untether's real
practice — these are the areas where batch PRs obscure causality during fleet
regressions). **Safe batch candidates** (may share a branch/PR): schema/catalog
additions, telegram-formatting, docs, trivials. **Record the batching decision
in the run report** — which issues went together and why.

### F-5. Per-issue: run the 8-step engine to a hand-off

For each ranked issue (respecting F-3/F-4), follow the debug bundle:

1. **Classify** (`step-classify.md`) — pick the class.
2. **Evidence** (`step-evidence.md`) — journalctl/structlog/state/MCP, redacted + bounded.
3. **Research** (`step-research.md`) — docs + closed issues + upstream.
4. **Systemic cross-ref** (`systemic-patterns.md`) — by-design? regression? shared root?
5. **Hypothesis** — cause→effect, prior confidence, pre-mortem if <80%.
6. **Verify** — reproduce with a stub-subprocess test (or live dev repro).
7. **Fix** (`step-fix.md`) — minimal change on `fix/<issue-N>-<slug>` (or a
   cohesive shared branch per F-4); `uv run pytest tests/test_<area>.py -x` →
   `uv run pytest`; `uv run ruff check src/` + `uv run ruff format src/ tests/`;
   CHANGELOG entry (issue-linked; **rc versions skip** per `validate_release.py`).
8. **Verify** (`step-verify.md`) — confirm the target signature is absent on
   fresh `untether-dev` logs; run the integration tier the change scope requires.

Then open **ONE PR to `dev`** per branch (F-4) and apply `needs-verification` +
post the Debug Report comment on each issue. **Never close** — Nathan closes with
a live-verification comment.

### F-6. Hand-off product — the batch-PR body shape

Mirror PR #660's canonical shape. The PR body is a Markdown table plus a Tests
section:

```
| Issue | Root cause | Fix | Live verification |
|-------|-----------|-----|-------------------|
| #NN   | …         | …   | journalctl:event=… absent / tier N pass |

## Tests
- uv run pytest — <N> passed, <M>% coverage
- uv run ruff check src/ — clean
- integration tiers run: <list, or "pending /qa">
```

Surface the drafted PR body and wait for the operator's tap before
`gh pr create --base dev`. In `--dry-run`, print it and stop (stage locally, no
push).

### F-7. Run report + hand-off edges

End with a brief report: which issues were fixed, the batching decision (F-4),
what was deferred (`queued-next-run`), and the hand-off:

- `/debug` — if an issue needs deeper investigation first.
- `/qa` — validate a risk-bearing fix before merge (drives the dev-bot tiers).
- `/plan` — an "issue" that's really net-new capability.
- `/kaizen` — capture a process learning from the sweep.
- `/handover` — if the sweep is paused mid-flight.

## Hard rules / anti-patterns

- No push/merge to `master`; no `git tag`; no `gh release create`; no
  `--no-verify` / hook-skip.
- No `git add -A` — stage explicit paths only.
- No restarting staging (`untether.service`) to verify a fix — use `untether-dev`.
- No closing an unverified fix (`needs-verification` + Nathan's close only).
- No co-batching independent high-risk state machines (F-4).
- No fixing beyond the issue's scope — a discovered adjacent bug gets its own
  issue, not a silent ride-along.

`--help` prints the sub-command table above, then stops.

End of /fix command file. The 8-step detail is owned by `.claude/commands/debug/`.
