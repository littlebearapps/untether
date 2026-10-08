---
description: Everyday finalise → ONE merge-ready PR to dev (→ TestPyPI on merge). Take a feature/fix/chore branch from "code+tests done" to a PR with docs reconciliation folded in as a completion criterion (CHANGELOG, FAQ touch-up, `docs/reference/test-catalog.md`). Green locally first, apply the batch-cohesion rule, open the table-shaped PR, hand off needs-verification. May merge to dev only (--merge + confirm). Never master/tag/release/deploy.
argument-hint: "[] finalise current branch → dev PR | [--rc X.Y.ZrcN] | [--dry-run] | [--merge] | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Edit Write Skill ToolSearch Bash(git status:*) Bash(git branch:*) Bash(git rev-parse:*) Bash(git symbolic-ref:*) Bash(git log:*) Bash(git diff:*) Bash(git add:*) Bash(git commit:*) Bash(git push:*) Bash(gh pr create:*) Bash(gh pr view:*) Bash(gh pr list:*) Bash(gh pr merge:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh issue comment:*) Bash(gh issue edit:*) Bash(uv run pytest:*) Bash(uv run ruff:*) Bash(uv lock:*) Bash(python3 scripts/validate_release.py:*) Bash(grep:*) Bash(rg:*) Bash(jq:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(ls:*) Bash(cat:*)
---

You are handling `/pr-dev`. `/pr-dev` takes a feature/fix/chore branch from
"code + tests done" to **ONE merge-ready PR to `dev`** (→ TestPyPI on merge). Docs
reconciliation is a **completion criterion here**, not a separate `/docs` stage.
This is the everyday delivery command and Untether's batch-PR unit.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules) and
`.claude/rules/release-discipline.md` (changelog/version/FAQ discipline). Key
points:

- **Release-guard obedience.** PR **to `dev`** only. `gh pr merge --squash` is
  allowed **only** with base = `dev` (the only merge `/pr-dev` may do; the
  release merge is `/pr-main X.Y.Z --merge` only). Never push/merge to
  `master`, never `git tag`, never `gh release create`, never `--no-verify`.
  The ruleset blocks direct `master` pushes; the local guard (#915/#917) gates
  `master` merges — never work around a block.
- **Dev/staging separation.** Never `systemctl restart` staging or dev from
  inside this session (hot-reload drain drops the final message).
- **Confirm-gated + idempotent.** Surface the drafted PR body and wait for a tap;
  de-dupe against an existing open PR for this branch; a re-invoked `/pr-dev` must
  not double-open or double-merge.
- **Redaction.** Scrub tokens/keys/env/chat-content/fleet identifiers from any
  evidence quoted in the PR body.
- **Untether-mode.** `--merge` confirmation is stated in text and STOPS for a
  reply. Keep the report brief.

## Sub-commands

| Form | Action |
|---|---|
| `/pr-dev` (no args) | Finalise the current branch → open a PR to `dev` (stops merge-ready) |
| `/pr-dev --rc X.Y.ZrcN` | Bump `pyproject.toml` + `uv lock` to the rc (`chore(release): X.Y.ZrcN`, or fold it into the batch PR titled `rcN: … — X.Y.ZrcN (#…)`) → PR to `dev` |
| `/pr-dev --dry-run` | Gate + local checks + print what would happen; open/push nothing |
| `/pr-dev --merge` | Squash-merge the PR **to `dev` only** (confirm-gated) → TestPyPI CI |
| `/pr-dev --help` | Usage, then stop |

## Flow

### D-1. Assert a clean, valid starting point

- `git status` clean (or only the intended staged paths); resolve the branch.
- **Refuse** if the branch is `master`/`main` → STOP (a delivery branch is
  required). Confirm it is ahead of `dev`.

### D-2. Green locally (fix-loop before any push)

```bash
uv run pytest                          # full suite, 80% coverage gate
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
python3 scripts/validate_release.py    # only if pyproject.toml version changed
```

Fix-loop until green **before** push (`superpowers:verification-before-completion`
via the Skill tool — evidence before "done"). Never push red.

### D-3. Docs completion inline (the folded-in `/docs`)

This is the completion criterion that replaces AT's separate docs stage +
manifest:

- **CHANGELOG entry** — issue-linked (`[#N](…)`), under the current
  `## vX.Y.Z (unreleased)` heading (e.g. `v0.36.0`) in the right
  `### breaking/fixes/changes/docs/tests` subsection; rc numbers never get their own
  heading (`validate_release.py` skips rc versions). A `### breaking` entry needs a
  **Migration:** line.
- **FAQ touch-up** — scan the change against `docs/faq/faq.md` per
  `.claude/rules/help-faq.md`; if a user-visible surface changed (engine support,
  auth/billing, privacy/data flow, approval semantics, cost budgets, voice,
  install/update paths), edit the FAQ in this branch. Edit/Write only — never `rm`/`mv`/`>` it
  (the `help-faq-protect.sh` guard blocks that).
- **Context-doc reconciliation** — if a runner/schema/telegram surface changed,
  update `docs/reference/test-catalog.md` (and `feature-catalog.md` for a new
  feature) + the relevant `docs/reference/*` per
  `.claude/rules/runner-development.md` / `testing-conventions.md`.

### D-4. Classify docs-only vs code (mirror CI's predicate)

If the change is **docs-only**, skip blanket e2e — say so. If it touches code,
the full local gate (D-2) stands. Do not run integration tiers here (`/qa` owns
the live drive); note which tiers the change *will* need for `needs-verification`.

### D-5. Batch-cohesion rule (§5.2)

Never co-batch fixes that touch **independent high-risk state machines** (session
lifecycle/resume · signal-death · watchdog/stall · hot-reload · rate-limit/cost)
— one branch/PR per such fix. Schema/catalog/telegram-formatting/docs/trivials
are safe batch candidates. **Record the batching decision** in the PR body.

### D-6. Open ONE PR to `dev` (table-shaped body)

Stage **explicit paths** (never `git add -A`), commit with a conventional message,
`git push -u origin <branch>`. Draft the PR body in Untether's canonical shape:

```
| Issue | Root cause | Fix | Live verification |
|-------|-----------|-----|-------------------|
| #NN   | …         | …   | pending /qa tier N / journalctl:event=… absent |

## Tests
- uv run pytest — <N> passed, <M>% coverage
- uv run ruff check src/ tests/ — clean
- integration tiers to run: <list> (via /qa)

## Batching
- <which issues share this PR and why they're safe to co-batch>
```

Surface the body and wait for a tap, then `gh pr create --base dev`. In
`--dry-run`, print it and open nothing.

### D-7. Hand-off (and optional merge)

Apply `needs-verification` where the branch fixes tracked issues. Report the PR
URL and the hand-off:

- `/qa` — drive the required tiers against the dev bot before merge.
- `/pr-dev --merge` — squash-merge to `dev` (→ TestPyPI) once approved
  (confirm-gated; base = `dev` only).
- `/pr-main` — when `dev` is ready to cut a stable release.
- `/handover` — if delivery is paused.

**Deploy note:** the TestPyPI publish is **automatic CI** on the `dev` merge —
`/pr-dev` never deploys; it opens/merges the PR only.

## Anti-patterns

- No PR/merge to `master`; no `git tag`; no `gh release create`; no `--no-verify`.
- No readiness.json / manifest ceremony (dropped by design).
- No `git add -A` — explicit paths only.
- No blanket e2e on a docs-only change.
- No auto-merge without `--merge` + confirm; no merge to a base other than `dev`.
- No co-batching independent high-risk state machines (D-5).
- No restarting staging/dev to "verify" from inside this session.

`--help` prints the sub-command table, then stops.

End of /pr-dev. A stable release is `/pr-main`; validation is `/qa`.
