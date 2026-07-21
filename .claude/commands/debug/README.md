# /debug companion bundle

This directory is the canonical source for each step of Untether's 8-step deep
debugging protocol. The parent command file at `../debug.md` orchestrates the
steps; each file here owns the detail for one step (or one shared output).

Edit a file here to update the rule for that step everywhere — the parent
command references these by path and does not duplicate their content.

## Files

| File | Owns | Read before |
|---|---|---|
| [`step-classify.md`](./step-classify.md) | Step 1 — 15 Untether issue classes + diagnostic hints | Picking a class |
| [`step-evidence.md`](./step-evidence.md) | Step 2 — data-source catalogue (journalctl, structlog, fleet SSH, MCP, state files) | Running any evidence command |
| [`step-research.md`](./step-research.md) | Step 3 — docs, closed issues, upstream engine repos, library docs | Researching prior art |
| [`systemic-patterns.md`](./systemic-patterns.md) | Step 4 — known Untether patterns + memory-aware exceptions | Scoring any signal in sweep, OR filing/escalating in targeted |
| [`step-fix.md`](./step-fix.md) | Step 7 — implementation checklist + branch model + release guard | Writing any code change |
| [`step-verify.md`](./step-verify.md) | Step 8 — post-fix verification (dev restart, integration tests, attestation, fleet rollout) | Declaring an issue verified |
| [`output-template.md`](./output-template.md) | Output formats — Debug Report (targeted), Triage Report (sweep), Verification Spec | Emitting any user-facing report |

## External references reused (not duplicated)

| Path | Purpose |
|---|---|
| `~/.claude/commands/monitor/severity-rubric.md` | Severity buckets and label routing |
| `~/.claude/commands/monitor/signal-categories.md` | Bug + enhancement signal taxonomy |
| `../../rules/runner-development.md` | Runner contract: 3-event sequence, session locking, signal-death handling |
| `../../rules/telegram-transport.md` | Outbox model, callback_data limits, ephemeral cleanup |
| `../../rules/control-channel.md` | PTY lifecycle, session registries, cooldown, ask-question flow |
| `../../rules/dev-workflow.md` | Dev/staging separation — NEVER restart staging to test |
| `../../rules/release-discipline.md` | Branch model, integration test gate, fleet rollout |
| `../../rules/testing-conventions.md` | pytest patterns, stub subprocess scripts |
| `../../rules/help-faq.md` | FAQ touch-up checklist on releases |
| `../../rules/context-quality.md` | Cross-file consistency rules |
| `../../../docs/reference/integration-testing.md` | Per-tier integration test playbook |
| `../../../docs/reference/dev-instance.md` | dev vs staging service quickref |

## Two modes — at a glance

| | Sweep (`/debug` or `/debug sweep [hours]`) | Targeted (`/debug <issue#>...`) |
|---|---|---|
| **Goal** | Ranked triage of open issues + recent fleet errors | Full 8-step on one or more named issues |
| **Output** | Triage Report markdown (printed) | Debug Report comment per issue (printed, posted only on explicit approval) |
| **Auto-file?** | No — watcher daemon + `/monitor` cover that | No — adds `needs-verification` only |
| **Fleet probe?** | All 5 hosts by default | Only relevant hosts |
| **Steps walked** | Steps 1, 4 (classify + cross-ref patterns) on each finding | All 8 steps per issue |

See `../debug.md` for invocation syntax and orchestration logic.
