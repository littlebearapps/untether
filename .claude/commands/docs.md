---
description: Escape-hatch to reconcile Untether docs OUTSIDE a PR (CHANGELOG, docs/faq/faq.md, docs/reference/test-catalog.md, docs/reference/*). The normal path folds docs completion into /pr-dev — reach for /docs only when docs drift needs fixing on its own, with no code change to deliver. Docs-only authority; opens no PR, writes no code. Routes back to /pr-dev when a code branch is involved.
argument-hint: "[] (scan for doc drift) | [faq | changelog | tests | reference] | [--dry-run] | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Edit Write Skill Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(grep:*) Bash(rg:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(ls:*) Bash(cat:*)
---

You are handling `/docs`. `/docs` reconciles Untether's small doc surface
**outside a PR**. It is an **escape hatch**, not the default path — docs
completion is normally folded into `/pr-dev` (CHANGELOG + FAQ + `docs/reference/test-catalog.md`
as a completion criterion). Reach for `/docs` only when documentation
has drifted on its own and there is **no code change** to deliver alongside it.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules),
`.claude/rules/help-faq.md`, and `.claude/rules/context-quality.md`. Key points:

- **Authority: docs only.** Edit CHANGELOG / `docs/faq/faq.md` / `CLAUDE.md` (keep it
  under its ~200-line budget) / `docs/reference/*`. Never code, never a PR (if a code branch is involved →
  route to `/pr-dev`, which folds docs in), never master/tag/release.
- **If a code change is in flight → STOP and route to `/pr-dev`.** Do not split
  docs for a code change into a separate PR — that fragments delivery.
- **FAQ must stay put.** Edit/Write/append `docs/faq/faq.md` freely, but never
  `rm`/`mv`/`>` it (the `help-faq-protect.sh` hook blocks that).
- **Confirm-gated + idempotent.** Surface the drafted edits and wait for a tap;
  re-running must not duplicate a CHANGELOG entry or FAQ Q/A.

## When to use (vs `/pr-dev`)

| Situation | Command |
|---|---|
| Delivering code + its docs | `/pr-dev` (docs folded in) |
| A doc drifted with no code change (stale FAQ, missing CHANGELOG link, out-of-date `## Tests`) | `/docs` |
| Reconciling `docs/reference/*` after a runner/schema change already merged | `/docs` |

## Flow

### X-1. Scan for drift

- `git diff`/`git log` since the last doc touch; check the CHANGELOG against
  recent merges (issue links present? `### fixes/changes/…` correct?).
- FAQ: scan `docs/faq/faq.md` against current features per `help-faq.md` — any
  user-visible surface (engine support, auth/billing, privacy, approval
  semantics, cost, voice, install/update) answered wrongly?
- `docs/reference/test-catalog.md`: does it match `tests/` reality? Any new test file
  undocumented? Is `CLAUDE.md` still within budget (no feature/test lists re-grown)?
- `docs/reference/*`: any runner/schema/telegram doc lagging code?

Target a single surface if named (`faq`/`changelog`/`tests`/`reference`).

### X-2. Draft + confirm

Draft the minimal edits. Surface them and wait for a tap (Untether-mode: state
the change in text). In `--dry-run`, print and change nothing.

### X-3. Apply + verify

Apply via Edit/Write (explicit files). Then the after-changes checks from the
rules, e.g.:

```bash
grep -c '^## ' docs/faq/faq.md                     # ≥ 7 Q/A (help-faq)
grep -ciE 'TODO|\[placeholder\]|TBD' docs/faq/faq.md   # 0
```

### X-4. Report + hand-off

Brief report: which docs were reconciled and why. If any code change surfaced →
route to `/pr-dev`. If the drift implies a durable process gap → `/kaizen`.

## Anti-patterns

- No code, no PR (that's `/pr-dev`), no master/tag/release.
- No splitting docs for an in-flight code change into a separate PR.
- No deleting/moving `docs/faq/faq.md`.
- No duplicate CHANGELOG entries / FAQ Q/A on a re-run.

`--help` prints the when-to-use table, then stops.

End of /docs. The default path is `/pr-dev` (docs folded in).
