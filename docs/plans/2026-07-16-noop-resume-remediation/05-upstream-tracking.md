# W7 — Upstream Tracking & Filing (Plan)

**Goal:** Track the upstream Claude Code / Agent SDK defect that causes the poisoned session, file a well-evidenced report, and record the version window so we can retire the Untether mitigations if/when it is fixed.

**Issue:** **#569** (UPDATE — "codify post-result/stuck-after-tool_result watchdog as permanent" already tracks upstream). Add the specific no-op-resume upstream references. Milestone v0.35.4, rc7.

## What to file / watch (anthropics/claude-code + claude-agent-sdk-python)

Best-fit existing upstream issues (from the 2026-07-16 research pass):

| Upstream | Repo | Fit |
|----------|------|-----|
| **#75658** | anthropics/claude-code | Task subagents end a turn with zero final text → empty result; resume recovers. **Best match** for the dangling-tool_use shape. |
| **#55893** | anthropics/claude-code | `run_in_background` bash stuck post-completion, persists across session boundaries. |
| **#1030** | anthropics/claude-agent-sdk-python | background children reaped at turn boundary; signature `session_id empty / tokens_used==0`. |
| **#36811** | anthropics/claude-code | `-p --resume` omits `result` when a `last-prompt` entry exists (secondary — our transcripts DO carry `last-prompt`). |

## Filing content (attach to #75658 or a new report if none fits after re-check)

Include the reproduction evidence Untether already has:
- Session transcript tail showing the last turn ending on an assistant `tool_use` with no `tool_result` / no `result` record, plus `queue-operation` enqueue/dequeue of `<task-notification>` messages and `last-prompt`.
- The `num_turns=0 / total_cost_usd=0 / duration_api_ms=0 / is_error=false / rc=0` result on the next `--resume`.
- Repro shape: real turn spawning a background subagent → process lingers on MCP/background children → force-killed after ~390s → next `--resume` returns empty.
- Note it persists on **2.1.211** (i.e. the 2.1.208 "No completion record on resume — orphaned background tasks" and 2.1.211 anti-fabrication fixes do NOT cover this variant).

## Tasks

1. Re-check upstream at filing time (issues move fast) — confirm #75658 is still the closest and not already fixed in a newer 2.1.x than the fleet runs.
2. Comment on #569 with: this root cause, the four upstream references, and the 2.1.211-persists fact.
3. If no upstream issue matches after re-check, file a new one against anthropics/claude-code with the evidence above; link it back into #569 and `project_claude_noop_resume_upstream.md`.
4. Add an upstream-watch note: when an upstream release claims to fix background-subagent resume, run the B-RESUME integration scenario (doc 04) against that CLI version on `@untether_dev_bot`; if it no longer reproduces, open a follow-up to relax/retire W1/W2/W4 (behind their flags first, then remove).

## Acceptance

- #569 updated with the four upstream references + persists-on-2.1.211 fact.
- Upstream issue filed or the existing one endorsed with our evidence.
- A documented "retire the mitigation" trigger (B-RESUME passes on a future CLI) so we don't carry the workaround forever.

## Note on billing/Fable-5 (ruled out — do NOT file against these)

The metered Agent-SDK billing split was announced 2026-05-14, **paused 2026-06-15, never took effect**; and quota exhaustion surfaces as an ERROR (`is_error=true`), never a clean `rc=0` empty. Fable 5 is a temporal coincidence. See `project_claude_noop_resume_upstream.md` and the corrected `project_anthropic_agent_sdk_billing_split.md`.
