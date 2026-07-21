---
description: Capture resumable stop-state when work is genuinely paused/blocked/moving between sessions — H0 (none) / H1 (inline note) / H2 (incoming/handovers/<date>-<slug>.md, gitignored) / H3 (docs/handovers/<date>-<slug>.md, committed). Derive complete[]/decisions[]/next_action from persisted state + evidence (git, test runs, session_quarantine.json, logs), never from chat memory. Git-fingerprinted. Defaults DOWN — routine ends are H0.
argument-hint: "[] (auto-pick level) | [--level H0|H1|H2|H3] | [<slug>] | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Write Edit Skill Bash(git status:*) Bash(git branch:*) Bash(git rev-parse:*) Bash(git log:*) Bash(git diff:*) Bash(git stash list:*) Bash(git hash-object:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh pr list:*) Bash(gh pr view:*) Bash(journalctl:*) Bash(ls:*) Bash(cat:*) Bash(jq:*) Bash(date:*) Bash(grep:*) Bash(wc:*) Bash(head:*) Bash(tail:*)
---

You are handling `/handover`. `/handover` captures **resumable stop-state** so a
paused/blocked session can be picked up cleanly later — critical for Telegram
sessions that may resume on a different host or after a gap.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules). Key
points:

- **Derive from persisted state, never chat memory.** `complete[]`,
  `decisions[]`, and `next_action` come from **evidence** — `git` (branch, HEAD,
  diff, stashes), test runs, `session_quarantine.json`, `active_progress.json`,
  and logs — not from what you "remember" doing. Chat memory is unreliable across
  a resume; the repo is ground truth.
- **Default DOWN between levels.** A routine session end is **H0** (no handover).
  Only escalate when work is genuinely paused mid-flight with an unobvious next
  action. Never manufacture an H2/H3 for a clean stopping point.
- **No `auditor continuity` validator** — Untether uses a plain frontmatter
  template + a `git`-based fingerprint. No manifest CLI.
- **Authority: writes the handover doc only.** Never code, never a PR, never a
  release action. H2 is gitignored; H3 is committed (docs only).
- **Redaction.** Scrub tokens/keys/env/chat-content/fleet identifiers from any
  log/state evidence before it lands in a committed (H3) doc.

## Levels (default DOWN)

| Level | When | Where |
|---|---|---|
| **H0** | routine end, nothing paused | nothing written (the common, correct case) |
| **H1** | a one-line "next time, do X" | an inline note in the final message |
| **H2** | paused mid-work, private/local | `incoming/handovers/<YYYY-MM-DD>-<slug>.md` (gitignored) |
| **H3** | a durable, shareable handover worth committing | `docs/handovers/<YYYY-MM-DD>-<slug>.md` (committed) |

`--level HN` forces a level; otherwise auto-pick, biasing DOWN.

## Flow

### V-1. Decide the level

Is work genuinely paused with a non-obvious next action? If **no** → **H0**, say
so, stop. If a single line suffices → **H1**. Escalate to H2/H3 only for real
mid-flight interruption. When unsure, go one level DOWN.

### V-2. Gather stop-state from evidence (H2/H3)

Derive, do not recall:

```bash
git rev-parse HEAD                       # exact commit
git status --short && git branch --show-current
git diff --stat                          # what's uncommitted
git stash list                           # anything stashed
git log --oneline -5                     # recent trajectory
```

Add, where relevant: last `uv run pytest` outcome (re-run if cheap), open
issues/PRs for this work (`gh issue list`/`gh pr list`), and state files
(`session_quarantine.json`, `active_progress.json`) if the pause touched
session/resume lifecycle.

### V-3. Fingerprint (git — everywhere on the fleet)

Bind the handover to the exact tree so a resume can detect drift:

```bash
echo "head=$(git rev-parse HEAD)"
echo "worktree=$(git diff | git hash-object --stdin)"
```

Use `git rev-parse` / `git diff | git hash-object --stdin` — **not**
`sha256sum`/`shasum` (portability across the 5 hosts; git is everywhere).

### V-4. Write the doc (H2/H3)

Frontmatter template:

```markdown
---
slug: <slug>
date: <YYYY-MM-DD>
level: H2 | H3
head: <git rev-parse HEAD>
worktree_fingerprint: <git diff | git hash-object --stdin>
branch: <branch>
---

## What's complete (from evidence)
- <complete[] — each item cites its evidence: commit / test / issue>

## Decisions made
- <decisions[] — with the why>

## Next action (single, concrete)
<the one next step — resolvable from state, not memory>

## Resume check
- verify head/worktree fingerprint matches before continuing
- open issues/PRs: <#NN …>
```

H2 → `incoming/handovers/<date>-<slug>.md` (gitignored). H3 →
`docs/handovers/<date>-<slug>.md` (committed — docs only; no code).

### V-5. Report + hand-off

Brief report: the level chosen (and why DOWN if applicable), the doc path (H2/H3)
or the inline note (H1) or "H0 — clean stop, no handover", and the resume pointer.
Related loops: `/plan`/`/implement` (resume delivery), `/fix` (resume a sweep),
`/qa` (resume a drive).

## Anti-patterns

- No H2/H3 for a clean stopping point (default DOWN — most ends are H0).
- No `complete[]`/`next_action` from chat memory — derive from state.
- No `sha256sum`/`shasum` fingerprint (use git).
- No code, no PR, no release action — `/handover` writes a doc only.
- No unredacted secrets/chat-content in a committed (H3) doc.

`--help` prints the level table, then stops.

End of /handover. Resume the work with its owning loop (`/implement`, `/fix`, `/qa`).
