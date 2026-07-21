# Kaizen policy — Untether continuous improvement

The full rubric for Untether's continuous-improvement loop. The commands
(`/kaizen`, `/kaizen-review`) and the thin rule (`.claude/rules/kaizen.md`) point
here for detail. This file is policy, not code — it explains and links; it does
not restate thresholds that live in the command files.

Kaizen has two halves, deliberately split by frequency and authority:

| Half | Command | Cadence | Authority |
|---|---|---|---|
| **Capture** | `/kaizen` | every substantive session end | read-only except ONE collector comment |
| **Promote** | `/kaizen-review` | weekly (human-gated); `--monthly` health | propose-only; never auto-applies |

The point of the split: capture is cheap and frequent so nothing is lost;
promotion is rare, deliberate, and human-approved so the ruleset/hooks/docs never
drift under an unattended agent.

## Capture vs promote

- **Capture** = record a *process* learning with evidence, as a bullet. Fast,
  low-stakes, reversible (a bullet can be struck). 0–3 per session; **0 is a
  valid, common outcome**.
- **Promote** = turn a recurring/high-severity bullet into a durable artefact
  (test/doc/rule-draft/issue). Slow, gated, always a *proposal* first.

A learning is worth capturing only if it is:

- about **how the work went** (friction, a wrong recipe, a guardrail block, an
  engine quirk, a timing/cost surprise) — **not** a product bug (that's
  `/fix`/`/debug`), and **not** already tracked in an open issue or a rule.
- backed by **evidence** you can point at.

## The 8 tags

Untether-tuned (AT's `[data-quality]` → `[engine-quirk]`):

| Tag | Use for |
|---|---|
| `[timing]` | latency/ordering surprises — drain timeouts, stall thresholds, race windows |
| `[tooling]` | a command/recipe/skill that was wrong, slow, or missing |
| `[issue-quality]` | issue/PR hygiene — missing repro, weak labels, unlinked changelog |
| `[novel-pattern]` | a reusable technique discovered this session |
| `[guardrail-block]` | a release-guard/permission block hit (and whether it was right) |
| `[engine-quirk]` | upstream CLI behaviour (Claude/Codex/OpenCode/Pi/Gemini/AMP) — not an Untether bug |
| `[cost]` | spend/rate-limit surprises (billing split, budget hit, throttle) |
| `[meta]` | about the kaizen/loop system itself |

## S/C/R scales

Every bullet scores three axes:

- **S — Severity / impact** of the underlying friction:
  `S0` cosmetic · `S1` minor annoyance · `S2` slows work · `S3` causes rework or
  a wrong result · `S4` release/fleet risk.
- **C — Confidence** the learning is real and correctly diagnosed:
  `low` (a hunch) · `med` (some evidence) · `high` (reproduced / cited).
- **R — Recurrence**: `×N` = how many times this (or a near-duplicate) has been
  seen, counting prior collector bullets.

## The promotion gate

`/kaizen-review` promotes a bullet or cluster only if it clears:

> **Recurrence ≥ 2** OR **Severity ≥ S3** OR **trivial-high-leverage**
> (a one-line fix that removes real friction) OR **explicit operator request**.

Below the gate → **defer** (leave) or **dismiss** (strike with a reason).

## The promotion hierarchy (cheapest sufficient rung)

Pick the **lowest** rung that would actually prevent recurrence:

1. **pytest** — a regression/guard test. Best rung: makes the learning
   executable and CI-enforced.
2. **doc** — a line in an existing doc / reference / FAQ.
3. **rule-draft** — a proposed edit to a `.claude/rules/*.md`, drafted into
   `incoming/kaizen-runs/` — **never applied** by the command.
4. **GH issue** — when the fix is real work; file it (labelled, severity-tagged;
   `kaizen-child` if it's a tracked follow-up of a bullet).

## Human-approval boundary

| Action | `/kaizen` | `/kaizen-review` | Human / `/implement` |
|---|---|---|---|
| Append a bullet (one comment) | ✅ | — | — |
| Strike a bullet | — | ✅ (confirm-gated) | — |
| Draft a test/doc/rule into `incoming/` | — | ✅ | — |
| Open a GH issue | — | ✅ (confirm-gated) | — |
| **Apply** a rule/hook/CLAUDE.md/code change | ❌ | ❌ | ✅ only |

The invariant: **no autonomous edit ever reaches an authoritative surface.**
Capture is one comment; promotion is a proposal; application is human.

## Collector lifecycle

- **Title (exact):** `[kaizen] untether — process improvement log`. Resolve by
  exact-title match, **never** a prefix (promoted children look `[kaizen]`-ish).
- **Labels:** the collector carries `kaizen`; promoted children carry
  `kaizen-child` (never `kaizen`).
- **Resolver:** always `gh issue list … --limit 200` (avoids the 30-item default
  cap and the child-collision trap).
- **Lazy-create:** only on the first real capture; never create an empty
  collector. Never close it.

## Stale / expiry, caps, health

- **Stale:** a bullet un-promoted for **>90 days** that has not recurred is
  dismissed on the next `/kaizen-review` with `→ dismissed: stale`.
- **Over-capture cap:** ≤3 bullets per session; if a single session wants more,
  it's a signal to `/plan` or `/fix`, not to flood the collector.
- **Health numbers** (`/kaizen-review --monthly`, report only — do not
  over-instrument): **fix-outcome hold-rate** (fixes closed after
  `needs-verification` that stayed closed vs reopened) and the
  **capture→promote ratio**. A dropping hold-rate flags a regression cluster.

## Relationship to the rest of the suite

Kaizen is `L7`/`L8` in `docs/LOOPS.md`. It is orthogonal to delivery (`/plan`,
`/implement`, `/pr-dev`, `/pr-main`) and defects (`/debug`, `/fix`): it watches
*how* those loops run and proposes small improvements to the process, never to
the product directly.
