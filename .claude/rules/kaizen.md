# Kaizen — capture rule (thin slice)

The always-on slice for Untether's continuous-improvement loop. Loads whenever
`/kaizen` or `/kaizen-review` runs (and when a Stop-hook nudges `/kaizen`). The
full rubric lives in `docs/kaizen/README.md`; this rule is the boundary + shape.

## When `/kaizen` fires (session end)

Capture **0–3** *process* learnings — how the work went, not what the code does
— as durable bullets on the permanent collector issue
`[kaizen] untether — process improvement log` (label `kaizen`).

- **0 captures is valid and common.** Never manufacture noise. Do not capture
  anything already in an **open issue** or a `.claude/rules/*` trap — link it
  instead.
- Every bullet needs: a **tag** `[timing] [tooling] [issue-quality]
  [novel-pattern] [guardrail-block] [engine-quirk] [cost] [meta]`, an **evidence
  ref** (trace / log signature / `file:line` / `#issue` / `#PR`), and an
  **S/C/R** score (Severity `S0–S4` / Confidence `low|med|high` / Recurrence `×N`).
- Always emit the literal `## /kaizen` heading (the Stop-hook detects a
  completed capture by it).

## Authority boundary

| Command | May write | May NEVER write |
|---|---|---|
| `/kaizen` | exactly ONE `gh issue comment` on the collector (idempotent) | code · `.claude/rules/*` · `hooks.json` · `CLAUDE.md` |
| `/kaizen-review` | strike a bullet (edit collector comment) · draft into `incoming/kaizen-runs/` · open a GH issue | apply any edit to `.claude/rules/*` · `hooks.json` · `CLAUDE.md` · code |

Promotion is **propose-only**: `/kaizen-review` drafts a pytest/doc/rule-draft +
files an issue; a human (or `/implement`) applies it later. Nothing authoritative
is auto-edited.

## Collector hygiene

- Resolve by **exact title**, never a prefix match. Promoted children carry
  label `kaizen-child`, never `kaizen`.
- Always `gh issue list … --limit 200` on the resolver (avoids the 30-item cap +
  child-collision traps).
- Lazy-create the collector only on the first real capture — never an empty one.

See `docs/kaizen/README.md` for the gate, hierarchy, scales, stale/expiry, caps,
and health numbers.
