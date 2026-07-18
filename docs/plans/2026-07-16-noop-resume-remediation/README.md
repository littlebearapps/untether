# No-op Empty Resume — Remediation Plan (0.35.4rc7 / rc8)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each per-workstream doc task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the user-facing silent failure where a resumed Claude session returns an empty 0-turn result, by making Untether detect, recover from, and proactively avoid the upstream "poisoned session" state — without patching the CLI.

**Architecture:** A layered defence. (rc7) Reactive recovery — on the strict empty-0-turn anomaly, *quarantine* the poisoned session token and re-run the original prompt as a *fresh* session, plus mark any session forcibly SIGTERM'd after a result as unsafe-to-resume. (rc8) Proactive avoidance — serialise per-session ownership so we never resume a session whose prior subprocess is still alive, and land the background-task lifecycle v2 so "no live background work" is trustworthy. Throughout: structured diagnostics and a deterministic fault-injection test harness.

**Tech Stack:** Python 3.12+, anyio, msgspec, structlog, pytest + anyio; Telegram MCP + Bash for integration tests against `@untether_dev_bot`.

## Global Constraints

- Target milestone: **v0.35.4** (rc7/rc8 are staging rc's toward v0.35.4 stable). #573 stays **v0.35.5**.
- Branch model: `feature/*` → PR → `dev` (TestPyPI). NEVER push master/tag/merge-to-master.
- Every fix references a GitHub issue in the commit + CHANGELOG (rc's do NOT need changelog entries; the stable v0.35.4 will).
- `uv run ruff format src/ tests/` before every commit; keep 80% coverage.
- Do NOT patch or depend on a specific Claude CLI version — the bug spans 2.1.206–2.1.211; treat it as permanent upstream behaviour.
- Do NOT use `--continue` as recovery (selects the global/current session in a multi-session bridge).
- Do NOT remove the post-result limbo SIGTERM timeout (it bounds 100s-MB + child-process leaks — #590/#592).
- Recovery must stay **single-shot** per message (no retry storms); every automatic recovery logs a structured reason.
- Test via `@untether_dev_bot` (dev), never `@hetz_lba1_bot` (staging), before any rc rollout.

---

## 1. Root cause (recap)

Confirmed upstream Claude Code defect in the background-subagent + in-CLI message-queue feature set (rolled out ~early-mid May 2026; persists on 2.1.211). Full investigation: memory `project_claude_noop_resume_upstream.md`.

```
turn spawns background subagent (Agent/Task) / run_in_background bash
        │
        ▼
model emits result, but claude process does NOT exit
  (MCP children + background agents hold it open → "limbo")
        │
        ▼
Untether SIGTERMs the lingering process after ~390s
  → last assistant turn left DANGLING on an unresolved tool_use
    (no tool_result, no serialized result)
        │
        ▼
next message --resume's that session
  → CLI cannot cleanly continue a dangling turn
  → returns num_turns=0, cost=0, duration_api_ms=0, empty, is_error=false, rc=0
        │
        ▼
Untether #596 auto-resend re-runs against the SAME (still poisoned) session → can no-op again
```

The `num_turns=0 / duration_api_ms=0 / is_error=false` signature = zero API work: local resume-state short-circuit, not a model/billing/quota failure.

## 2. Why the current #596 mitigation is insufficient

- It re-sends against the **same** session (`runner_bridge.py:3646`), which is the poisoned one → can return empty again.
- The `on_resume_failed` session-clear hook only fires when `run_ok is False` (`runner_bridge.py:3203-3222`); the anomaly is `run_ok is True`, so the token is **never cleared**.
- Nothing prevents resuming a session whose prior subprocess is still alive or was force-killed.
- "No live background work" is not trustworthy: `_is_terminal_tool_result` clears `live_bg_agents` on the first tool_result (claude.py:630-670; true-terminal detection deferred to #374/#573).

## 3. Workstreams and issue map

| WS | Workstream | rc | GitHub issue | Action |
|----|-----------|----|--------------|--------|
| W1 | Quarantine-and-fresh recovery (+ anomaly diagnostics) | rc7 | **#631** (umbrella) | UPDATE |
| W2 | Forced-teardown session quarantine (persisted) | rc7 | **#632** | CREATED |
| W3 | Background terminal-detection (minimal): clear on terminal signal not first tool_result; recognise Task | rc7 | **#374** | UPDATE |
| W4 | One-owner-per-session serialisation (no double-resume) | rc8 | **#633** | CREATED |
| W5 | Background-task lifecycle v2 (child-PID cleanup, deadline sweeps) | rc8 → v0.35.5 | **#573** | UPDATE |
| W6 | Deterministic fault-injection test harness + integration coverage | rc7/rc8 | **#634** | CREATED |
| W7 | Upstream tracking + filing (claude-code #75658, SDK #1030) | rc7 | **#569** | UPDATE |
| — | Cross-reference shared root cause | — | **#591**, **#592** | COMMENT |

Per-workstream detail docs:
- `01-rc7-recovery-and-quarantine.md` — W1 + W2 + W5-diagnostics (full TDD)
- `02-rc7-background-terminal-detection.md` — W3 (full TDD)
- `03-rc8-serialization-and-lifecycle.md` — W4 + W5 (design + tasks)
- `04-test-strategy.md` — W6 (fake-claude harness, unit, integration, fleet correlation)
- `05-upstream-tracking.md` — W7

## 4. Sequencing and dependencies

```
rc7 (stop the bleeding — low risk, high impact)
  ├─ W1 quarantine-and-fresh recovery ───┐ (independent; supersedes #596 resend)
  ├─ W2 forced-teardown quarantine ──────┤ (independent; shares SessionState marker with W1)
  ├─ W3 background terminal detection ────┘ (independent; makes W2's "was it drained?" signal better)
  ├─ W5-diag anomaly diagnostics ──────── (prereq observability for verifying W1/W2 in the field)
  └─ W6a fake-claude harness ──────────── (prereq for W1/W2/W3 unit tests)
        │  all land together in 0.35.4rc7
        ▼
rc8 (prevent the poison — deeper, higher risk)
  ├─ W4 one-owner-per-session serialisation (depends on W2 quarantine markers)
  ├─ W5 background-task lifecycle v2 (#573 — depends on W3)
  └─ W6b integration tiers + fleet-correlation query
```

W6a (the fake-claude harness) is built first because W1/W2/W3 unit tests all consume it.

## 5. Test strategy (summary — full detail in `04-test-strategy.md`)

1. **Fake-claude CLI harness** — a scripted fake that deterministically emits: (a) a real turn with a dangling tool_use, (b) lingers past the limbo timeout so Untether SIGTERMs it, (c) on the next `--resume` emits the empty 0-turn result. This reproduces the production shape with zero flakiness.
2. **Unit tests** — W1 clears the token + re-runs fresh; W2 persists+honours the quarantine marker; W3 keeps the handle for interim results and clears on terminal; recovery is single-shot; diagnostics fields present.
3. **Integration (`@untether_dev_bot`)** — Tier 1 (Claude) + Tier 2 (interactive/plan) + a bespoke background-subagent-then-resume scenario in the Claude chat; assert the follow-up gets a real answer, footer shows a *new* session id, and no "empty result" notice.
4. **Fleet correlation** — post-deploy query joining `runner.empty_result`, `session.quarantined`, `session.auto_resend_fresh`, and the next-run outcome across all 5 hosts to confirm recovery rate.

## 6. Risks and rollback

| Risk | Mitigation | Rollback |
|------|-----------|----------|
| Fresh-session retry loses conversational context the user expected | Only triggers on the strict `num_turns==0 && duration_api_ms==0` anomaly (session already produced nothing to lose); single-shot; log old→new session id | Config flag `resend_empty_resume` already gates #596; W1 adds `empty_resume_fresh` (default on) — flip off to fall back to notice |
| Over-eager quarantine discards healthy sessions | Quarantine only on forced SIGTERM/SIGKILL *after a valid result*, or the strict anomaly — never on clean exit / real answer / API error | Marker is per-session; clearable; `quarantine_on_forced_teardown` flag (default on) |
| Serialisation (W4) deadlocks a chat if a prior process never exits | Bounded wait, then quarantine + fresh; reuse SessionLockMixin semantics | `serialize_session_owner` flag (default on in rc8) |
| W3 background-handle change wedges the watchdog (handle never clears) | Keep the #374 conservative rule (only Monitor is interim today); Task recognised for observability only in rc7 | Revert W3 commit; W3 is isolated |
| Recovery masks a genuinely different failure | W5-diag logs raw result subtype/is_error/rc so real errors stay visible; anomaly branch requires is_error=false | — |

Each rc is independently revertible via `scripts/fleet-rollback.sh <prev> --only <host>`; markers are additive and ignored by older builds.

## 7. Definition of done

- rc7: W1+W2+W3+W5-diag+W6a merged to `dev`; unit suite green (harness-driven); integration Tier 7 + Tier 1 (Claude) + the background-resume scenario pass on `@untether_dev_bot`; attestation marker written; fleet-rollout of 0.35.4rc7; 48h fleet-correlation shows empty-resume events now recover to a real answer.
- rc8: W4+W5+W6b merged; integration Tier 1 (all engines) + Tier 2; 0.35.4rc8 rolled; fleet-correlation shows a drop in `runner.empty_result` frequency (proactive avoidance working).
- v0.35.4 stable: CHANGELOG updated referencing #631/#374/#573/#569 + the new issues; FAQ untouched (no user-facing surface change beyond fewer silent failures).
