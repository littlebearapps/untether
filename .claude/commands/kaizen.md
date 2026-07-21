---
description: Session-end continuous-improvement capture for Untether. Record 0-3 evidence-linked process-learnings as bullets on the permanent [kaizen] GitHub collector issue. Read-only except ONE gh issue comment. Zero captures is a valid outcome. Self-invokes at session end; propose-only downstream via /kaizen-review.
argument-hint: "[] (capture from this session) | [--dry-run] | [--help]"
allowed-tools: Read Glob Grep Skill Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh issue comment:*) Bash(gh issue create:*) Bash(gh label list:*) Bash(gh label create:*) Bash(git log:*) Bash(git diff:*) Bash(date:*) Bash(grep:*) Bash(jq:*)
---

You are handling `/kaizen`. **Always begin your output with the literal heading
`## /kaizen`** (the Stop-hook and future runs detect a completed capture by that
marker — do not omit or reword it).

`/kaizen` captures *process* learnings — how the work went, not what the code
does — as durable bullets on a single permanent GitHub collector issue. It is
the cheap, high-frequency half; `/kaizen-review` is the weekly, human-gated
promotion half.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/kaizen.md` (the thin capture rule) and, for the full rubric,
`docs/kaizen/README.md`. Also `.claude/rules/workflow-commands.md` for the
cross-cutting rules. Key points:

- **Authority: read-only except ONE `gh issue comment`.** Never edit code,
  `.claude/rules/*`, `hooks.json`, or `CLAUDE.md`. Promotion to any of those is
  `/kaizen-review`'s job, and even then only as a *proposal*.
- **Self-invokes** at session end (a Stop-hook nudge is proposed to Nathan for
  wiring, but is not required — this command runs itself). Under Untether/cron
  it still self-invokes; the single collector comment is the one sanctioned
  autonomous external write in the suite (bounded, idempotent, one comment).
- **Idempotent.** Re-running must not duplicate a bullet already on the collector
  this session. Resolve the collector by **exact title** with `--limit 200`.
- **Redaction.** Scrub tokens/keys/env/chat-content/fleet identifiers from every
  evidence ref before it lands in the comment.

## What counts as a capture

A capture is a **process** learning with **evidence**, in one of these tag
buckets (Untether-tuned):

`[timing]` `[tooling]` `[issue-quality]` `[novel-pattern]` `[guardrail-block]`
`[engine-quirk]` `[cost]` `[meta]`

Every bullet MUST carry:

1. a **tag** from the list above,
2. an **evidence ref** — a trace, a log signature, a `file:line`, an issue `#N`,
   a PR `#N`, or an event signature (never vague "I noticed…"),
3. an **S/C/R score** — Severity `S0–S4` / Confidence `low|med|high` /
   Recurrence `×N` (see `docs/kaizen/README.md` for the scales).

**Do NOT capture:**

- anything already tracked in an **open issue** (link it instead, no new bullet),
- anything already encoded in a `.claude/rules/*` trap or `docs/` policy (link it),
- product bugs — those are `/fix`/`/debug` work, not process learnings,
- filler. **0 captures is a valid, common, correct outcome.** Never manufacture
  noise to look productive.

## Flow

### K-1. Gather session signal (read-only)

Derive candidates from **evidence**, not vibes:

- `git diff`/`git log` for this session's edits (churn, reverts, scope creep).
- Guardrail blocks / tool errors encountered (a `BLOCKED:` from a release-guard
  hook, a denied permission, a failed command retried).
- Timing/tooling friction (a wrong command recipe in a doc, a slow path, a
  missing test fixture, an MCP catalog stale event).
- Engine quirks observed (an upstream CLI behaviour, not an Untether bug).

### K-2. Filter to real captures

For each candidate, apply "What counts" above. Drop anything already in an open
issue or a rule (link it in your report, but do not add a bullet). Cap at **3**
bullets — pick the highest-leverage. If nothing survives, that's 0 captures.

### K-3. Resolve or lazily create the collector

```bash
gh issue list --repo littlebearapps/untether --label kaizen --state open --limit 200 \
  --json number,title | jq -r '.[] | select(.title == "[kaizen] untether — process improvement log") | .number'
```

- Match by **exact title** `"[kaizen] untether — process improvement log"` —
  **never** a prefix match (promoted children keep a `[kaizen]`-ish look; they
  carry label `kaizen-child`, never `kaizen`).
- If not found **and** there is ≥1 real capture, create it (lazy — never create
  an empty collector):
  ```bash
  gh label create kaizen --repo littlebearapps/untether --color 5319e7 --description "Kaizen process-improvement collector" 2>/dev/null || true
  gh label create kaizen-child --repo littlebearapps/untether --color b19cd9 --description "Artefact promoted from a kaizen bullet" 2>/dev/null || true
  gh issue create --repo littlebearapps/untether --label kaizen \
    --title "[kaizen] untether — process improvement log" \
    --body "Permanent collector for /kaizen process learnings. Bullets appended by /kaizen; promoted/struck by /kaizen-review. Do not close."
  ```
  (Collector title/label scheme is a propose-to-Nathan item — see the plan §9.)

### K-4. Draft the bullets

Each bullet, one line where possible:

```
- [tag] <the learning, stated as a reusable rule of thumb> — evidence: <ref> — S<0-4>/C<low|med|high>/R×<n> — <YYYY-MM-DD>
```

Under Untether-mode, print the drafted bullets in text (state the assumption
that you'll post them). De-dupe against the collector's existing open bullets.

### K-5. Post the ONE comment

```bash
gh issue comment <collector#> --repo littlebearapps/untether --body "<the bullets>"
```

One comment per session. In `--dry-run`, print the comment body and **do not
post**. If 0 captures, post nothing and say so.

### K-6. Report

Under the `## /kaizen` heading, print: how many captured (0–3), the bullets (or
"0 captures — nothing durable this session"), anything linked-not-captured
(already-in-an-issue/rule), and the collector issue number. Keep it brief.

`--help` prints this sub-command summary and stops (still under the `## /kaizen`
heading).

End of /kaizen. Weekly promotion is `/kaizen-review`; policy is `docs/kaizen/README.md`.
