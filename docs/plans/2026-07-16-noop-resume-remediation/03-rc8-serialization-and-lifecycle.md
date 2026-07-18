# W4 + W5 — rc8 Serialisation & Background Lifecycle v2 (Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Expand each task to full TDD micro-steps (failing test → fail → implement → pass → commit) at execution time; the design, files, and acceptance criteria below are fixed.

**Goal:** Prevent the poisoned-session state from arising in the first place (proactive), by (W4) never resuming a session whose prior subprocess is still alive, and (W5) making background-task teardown reliable so limbo/SIGTERM happens far less often.

**Issues:** W4 → **NEW** ("One-owner-per-session serialisation"). W5 → **#573** (UPDATE — background-task lifecycle v2, stays v0.35.5 but its child-PID/terminal work is scheduled with rc8).

## Global Constraints

Inherit `README.md`. W4 must be deadlock-free (bounded waits, then fall back to quarantine-and-fresh). W5 must not create an uncleared-handle permanent hang (same constraint as W3).

---

## W4 — One-owner-per-session serialisation

**Problem:** Today a follow-up message can spawn `--resume <sid>` while the prior subprocess for `sid` is still alive in limbo (the reproductions show a resume 6s after the prior process was SIGTERM'd; the timing is race-prone). Concurrent/near-concurrent ownership of one session id is exactly what corrupts the queue/turn state.

**Design:** Extend `SessionLockMixin` (already present — per-`engine:session_id` semaphore) with an *ownership + liveness* gate:

1. Before spawning a `--resume` run for `sid`, acquire `lock_for(token)` AND verify no live subprocess is still registered for `sid` (reuse the runner's PID/limbo registry).
2. If a prior subprocess for `sid` is still alive: wait (bounded, `SESSION_HANDOFF_TIMEOUT_S`, e.g. 30s) for it to exit cleanly. Poll on the existing post-result-idle exit signal (condition-based wait, not a fixed sleep).
3. If it exits cleanly within the window → proceed with `--resume`.
4. If it must be force-killed (or the window elapses) → quarantine `sid` (W2) and start FRESH — do not resume a session we just had to kill.

**Files:**
- `src/untether/runners/claude.py` — the run-spawn path; the PID/limbo registry; `SessionLockMixin` usage.
- `src/untether/runner.py` — `SessionLockMixin` if the liveness predicate belongs there.
- Test: `tests/test_claude_control.py`, `tests/test_exec_runner.py`.

**Tasks:**
1. Add a `session_has_live_subprocess(engine, sid) -> bool` predicate backed by the existing PID registry (the one that logs `mcp_child_pids`/`limbo_detected`).
2. Add `await wait_for_session_handoff(token, timeout_s)` — condition-based poll of the exit signal; returns `"exited" | "timed_out"`.
3. In the spawn path, gate `--resume` on the handoff; on `"timed_out"` → quarantine + fresh (reuse W2 helpers).
4. Config: `[auto_continue] serialize_session_owner = true` (default on in rc8), `session_handoff_timeout_s = 30`.

**Acceptance:**
- Never two live subprocesses for one session id (asserted via a harness that keeps the first process alive).
- A follow-up while the prior process lingers waits ≤ timeout then either resumes cleanly (prior exited) or goes fresh+quarantine (prior killed).
- No deadlock: the wait is bounded and always resolves.
- Feature-flagged; off → exact pre-rc8 behaviour.

**Risks:** adds latency to a follow-up when the prior process lingers. Mitigation: the wait is condition-based (resolves the instant the prior exits) and bounded; the alternative (racing a resume) is what corrupts the session. Rollback: flip `serialize_session_owner` off.

---

## W5 — Background-task lifecycle v2 (#573)

**Problem:** `_is_terminal_tool_result` only handles Monitor interim results (rc7/W3 adds bounded-keep for bg-agents). Full reliability needs true-terminal detection so a background primitive's handle clears exactly when the underlying work ends — reducing how often the process lingers into limbo (the trigger for the whole bug).

**Scope (from #573):** terminal detection for Bash-bg / Agent-bg / Task-bg / ScheduleWakeup / RemoteTrigger + child-PID cleanup.

**Design:**
1. **Bash-bg:** clear on the `KillShell` tool_result for its shell id, or on observing the background shell's completion line, or on subprocess-exit reconciliation (the PID left the process tree). Age-out backstop retained.
2. **Agent/Task-bg:** clear on the subagent-completion notification (`<task-notification>` / "the agent … has completed") matched by task id, else bounded deadline (W3's `BG_AGENT_MAX_KEEP_S`).
3. **ScheduleWakeup/RemoteTrigger:** already membership-only; add deadline-based age-out so they cannot pin `has_live_background_work` forever.
4. **Child-PID cleanup:** reconcile `mcp_child_pids` / descendant snapshot at true-terminal so #590's orphan sweep has an accurate live-set (ties into #590/#592).

**Files:** `src/untether/runners/claude.py` (lifecycle), `src/untether/utils/proc_diag.py` (descendant reconciliation), tests in `tests/test_claude_control.py` + `tests/test_proc_diag.py`.

**Acceptance (#573):**
- Each background primitive's handle clears on a real terminal signal, with a bounded age-out backstop (no permanent hang).
- `has_live_background_work()` transitions to False promptly after the last real background task ends → the post-result idle watchdog SIGTERMs far less often → fewer poisoned sessions.
- Child-PID set reflects genuinely-live descendants (supports #590 orphan reaping / #592 dead-zone).

**Note on milestone:** #573 is filed under v0.35.5. Landing its Bash-bg/Agent-bg terminal detection in 0.35.4rc8 is a scope pull-forward justified by its direct causal link to this bug; keep the remaining #573 scope (ScheduleWakeup/RemoteTrigger polish, full child-PID reconciliation) in v0.35.5 if rc8 gets tight.

## Sequencing

W4 depends on W2 (quarantine helpers) and benefits from W3 (truer liveness). W5 depends on W3 (bounded-keep foundation). Land W4 first in rc8 (higher user impact — it stops the race), then W5 as capacity allows.
