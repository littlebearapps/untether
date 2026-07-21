# Research findings — convention

Durable, cited research notes that back planning and validation decisions.
Untether does **not** ship its own research loop — it reuses the global
`/research` command. This directory is the **convention** for where that
command's output lands so `/plan`, `/debug`, and `/qa` can cite it instead of
re-deriving provider/current-truth facts from memory.

## When to write a finding

Whenever a decision depends on **external or current truth** that drifts and must
not be answered from model memory:

- an engine CLI's actual behaviour (Claude/Codex/OpenCode/Pi/Gemini/AMP flags,
  resume semantics, event shapes),
- a provider's billing / API / rate-limit model (the research gate in `/plan`),
- a library's current API (before planning against it),
- an upstream bug's real status (before treating it as a shared root in `/debug`).

## The research gate (who routes here)

- **`/plan`** — its research gate (P-4) routes provider/API-sensitive work to the
  global `/research` and requires the plan to **cite** the finding.
- **`/debug`** — when provider/current-truth is uncertain during investigation.
- **`/qa`** — when testing surfaces a provider-truth question it can't resolve
  from the repo.

Do not proceed on memory for provider/API-sensitive work — write (or cite) a
finding first.

## Naming + shape

One file per question: `docs/findings/<YYYY-MM-DD>-<slug>.md`.

```markdown
---
question: <the specific question researched>
date: <YYYY-MM-DD>
sources: [<url>, <url>, …]
confidence: low | med | high
---

## Answer
<the finding, stated plainly>

## Evidence
- <source> — <what it established> — <date accessed>

## Implications for Untether
<what this changes for the plan / fix / test — cite the loop that consumes it>
```

## Discipline

- **Date every claim** — findings are current-truth snapshots; stamp them.
- **Cite, don't restate** — packs and reports link the finding, they don't copy it.
- **Redaction** — scrub any tokens/keys/env/chat-content/fleet identifiers that a
  source or a log leaked before committing.
- Findings are **committed** (durable reference) — unlike `docs/plans/` (local)
  and `incoming/handovers/` (gitignored).

See `docs/plans/agentic-loops-and-commands/README.md` §5.10 for the design
rationale, and `.claude/rules/workflow-commands.md` for the routing table.
