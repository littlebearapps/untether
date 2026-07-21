---
description: Weekly, human-gated, propose-only promotion of /kaizen bullets. Parse un-struck bullets on the [kaizen] collector, score/dedupe/cluster, present approval packets; on Accept mint a propose-only artefact (pytest/doc/rule draft + GH issue) and strike the source bullet. Never auto-edits rules, hooks.json, CLAUDE.md, or code. Monthly --monthly samples fix-outcome hold-rate.
argument-hint: "[] (weekly review) | [--monthly] | [--dry-run] | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Write Skill Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh issue comment:*) Bash(gh issue edit:*) Bash(gh issue create:*) Bash(gh label list:*) Bash(git log:*) Bash(date:*) Bash(grep:*) Bash(jq:*)
---

You are handling `/kaizen-review` — the weekly promotion pass over the process
learnings that `/kaizen` captured. It is **propose-only** and **human-gated**: it
recommends, drafts, and files proposals, but it **never** applies a change to any
authoritative surface itself.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `docs/kaizen/README.md` (the full policy: gate, hierarchy, scales,
approval-boundary table) and `.claude/rules/kaizen.md`. Also
`.claude/rules/workflow-commands.md`. Key points:

- **Authority: propose-only.** NEVER auto-edit `.claude/rules/`, `hooks.json`,
  `CLAUDE.md`, or code. The most it does to an *authoritative* file is draft a
  proposed diff into a **non-authoritative** location (`incoming/kaizen-runs/`,
  gitignored) and open a GH issue. Applying the draft is a later human/`/implement`
  step.
- **Two writes it MAY do**: (1) strike a source bullet by *editing the collector
  comment* (add `~~…~~` + a `→ #NN` / `→ dismissed: reason` marker); (2) open a
  GH issue for an accepted promotion. Both are confirm-gated under Untether-mode
  (state the recommended decision in text; `AskUserQuestion` returns empty).
- **Idempotent.** Re-running must not re-promote an already-struck bullet or
  double-file an issue. Resolve the collector by **exact title**, `--limit 200`.

## The promotion gate

Promote a bullet (or a cluster) only if it clears the gate:

> **Recurrence ≥ 2** OR **Severity ≥ S3** OR **trivial-high-leverage** OR
> **explicit operator request**.

Everything else defers (leave the bullet) or dismisses (strike with a reason).

## The promotion hierarchy (cheapest sufficient artefact)

Pick the **lowest** rung that would actually prevent recurrence:

1. **pytest** — a regression/guard test (best: makes the learning executable).
2. **doc** — a line in an existing doc / reference / FAQ.
3. **rule-draft** — a proposed edit to a `.claude/rules/*.md` (drafted, never applied).
4. **GH issue** — when the fix is real work, file it (labelled, severity-tagged).

## Flow (weekly, default)

### KR-1. Load un-struck bullets

Resolve the collector (exact title, `--limit 200`), read all comments, and
collect bullets **not** already struck (`~~…~~`) or marked `→`.

### KR-2. Score, dedupe, cluster

- Parse each bullet's tag + S/C/R.
- Merge duplicates and near-duplicates; a merged cluster's Recurrence is the sum.
- Apply the gate (above) to each bullet/cluster.

### KR-3. Build approval packets

For each gate-passing item, present a compact packet:

```
Bullet(s): <source line(s) + collector comment link>
Cluster:   <N bullets, tag, combined S/C/R>
Gate:      <which criterion it cleared>
Recommend: <Accept: mint <rung> | Dismiss: <reason> | Defer>
Draft:     <the exact pytest/doc/rule-draft text, or the issue title+body>
```

State the **recommended decision in text** (Untether-mode: buttons return
empty). Wait for the operator's choice per packet.

### KR-4. Execute the decision (confirm-gated)

- **Accept** → mint the propose-only artefact:
  - write the draft to `incoming/kaizen-runs/<YYYY-MM-DD>/<slug>.md` (gitignored),
    and/or open a GH issue (`gh issue create`, labelled; `kaizen-child` if it is a
    tracked follow-up of a bullet). **Do not** touch the real rule/hook/code.
  - **strike** the source bullet(s): `gh issue edit`/`comment` to wrap them in
    `~~…~~` and append `→ #NN` (the minted issue) or `→ incoming/…`.
- **Dismiss** → strike the bullet(s) with `→ dismissed: <reason>`.
- **Defer** → leave the bullet untouched.

In `--dry-run`, print the packets + would-be artefacts and change nothing.

### KR-5. Report

Brief summary: N reviewed, N promoted (with rungs + issue links), N dismissed, N
deferred. Note that all promotions are **drafts awaiting human/`/implement`
application** — nothing authoritative was edited.

## Monthly mode (`--monthly`)

In addition to KR-1..KR-5, sample recently-closed `needs-verification` / fixed
issues to compute a light health read (do not over-instrument — LOOPS.md is not
a metrics DSL):

```bash
SINCE=$(date -u -d '30 days ago' +%Y-%m-%d)
gh issue list --repo littlebearapps/untether --state closed --limit 200 \
  --search "closed:>=${SINCE}" --json number,title,labels,closedAt
```

Report a couple of numbers only: **fix-outcome hold-rate** (of fixes closed after
`needs-verification`, how many stayed closed vs reopened) and the **capture→promote
ratio** over the month. Flag a regression cluster if the hold-rate drops.

`--help` prints this summary and stops.

End of /kaizen-review. Capture is `/kaizen`; policy is `docs/kaizen/README.md`.
