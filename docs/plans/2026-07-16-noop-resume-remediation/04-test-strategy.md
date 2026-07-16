# W6 — Comprehensive Test Strategy (Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development for unit work; the integration playbook follows `docs/reference/integration-testing.md`.

**Goal:** A deterministic, non-flaky reproduction of the dangling-tool_use → empty-resume shape so W1–W5 can be unit-tested; plus integration coverage on `@untether_dev_bot` and a post-deploy fleet-correlation check.

**Issue:** **NEW** ("Deterministic fault-injection harness + integration coverage for no-op resume recovery"). Milestone v0.35.4; W6a (harness+unit) rc7, W6b (integration+fleet) rc7→rc8.

## Layer 1 — Fake-claude reproduction harness (W6a, rc7, PREREQUISITE)

A scripted fake CLI that speaks just enough stream-json to reproduce each phase. Reused by W1/W2/W3 tests. Follows the existing stub pattern in `.claude/rules/testing-conventions.md` (a fake CLI emitting known JSONL), extended with resume-aware, multi-invocation behaviour keyed off an on-disk state file.

**File:** `tests/fake_clis/fake_claude_noop_resume.py` (+ a `tests/conftest.py` fixture `fake_claude_env`).

**Scenarios (selected via env var `FAKE_CLAUDE_SCENARIO`):**

| Scenario | Behaviour |
|----------|-----------|
| `dangling_then_empty_resume` | Invocation 1 (no `--resume`): emit `system.init` (session_id=S), an assistant turn ending on a `tool_use` block with NO tool_result, then a real `result` (num_turns=4, cost>0), then **stay alive** (sleep) to force limbo. Invocation 2 (`--resume S`): emit `system.init` then an immediate empty `result` (num_turns=0, cost=0, duration_api_ms=0, is_error=false, rc=0). Fresh invocation (no `--resume`): emit a normal real answer. |
| `linger_then_sigterm_after_result` | Emit a real `result`, then sleep past the limbo timeout (never exit) so Untether SIGTERMs — exercises W2's forced-teardown record. |
| `healthy_resume` | Normal `--resume` that produces a real answer — negative control (no quarantine, no recovery). |

**Sketch:**

```python
#!/usr/bin/env python3
# tests/fake_clis/fake_claude_noop_resume.py
import json, os, sys, time

def emit(obj): print(json.dumps(obj), flush=True)

def main() -> int:
    argv = sys.argv[1:]
    resume = None
    if "--resume" in argv:
        resume = argv[argv.index("--resume") + 1]
    scenario = os.environ["FAKE_CLAUDE_SCENARIO"]
    sid = resume or "S-fresh-" + str(os.getpid())
    emit({"type": "system", "subtype": "init", "session_id": sid})

    if scenario == "dangling_then_empty_resume":
        if resume is None:
            emit({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "spawning a background agent"},
                {"type": "tool_use", "id": "bg1", "name": "Task",
                 "input": {"run_in_background": True}}]}})
            emit({"type": "result", "subtype": "success", "is_error": False,
                  "num_turns": 4, "total_cost_usd": 0.12, "duration_api_ms": 8000,
                  "session_id": sid, "result": "started"})
            time.sleep(float(os.environ.get("FAKE_CLAUDE_LINGER_S", "0")))  # limbo
            return 0
        else:  # the poisoned resume
            emit({"type": "result", "subtype": "success", "is_error": False,
                  "num_turns": 0, "total_cost_usd": 0.0, "duration_api_ms": 0,
                  "session_id": sid, "result": ""})
            return 0
    # ... linger_then_sigterm_after_result / healthy_resume analogous ...
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

The `fake_claude_env` fixture builds a `ClaudeRunner` pointed at this script, exposes `env.runs` (each spawn's `resume_token`/`text`), `env.quarantine` (the `QuarantineStore`), `env.advance_clock`, and `env.last_final_text`, so bridge-level tests read like the W1/W2 tests in doc 01.

**Acceptance (W6a):** the three scenarios drive `runner.empty_result`, `session.quarantined`, and a healthy control deterministically; no `sleep`-based flakiness (limbo uses the injected clock / a short `FAKE_CLAUDE_LINGER_S` with a shortened test-only limbo timeout).

## Layer 2 — Unit tests (rc7)

Enumerated in docs 01/02. Coverage targets:
- W1: token cleared + fresh re-run + single-shot + no notice on success + legacy flag path.
- W2: forced-teardown quarantines; next message on a quarantined token goes fresh; marker cleared after a healthy run; survives reload.
- W3: Task recognised; bg-agent handle kept across interim, cleared on terminal/deadline; Monitor + foreground unchanged.
- Diagnostics: every empty-result branch logs `resend_eligible_reason`.
- Keep suite ≥ 80% coverage; add the new files to the `## Tests` list in `CLAUDE.md`.

## Layer 3 — Integration on `@untether_dev_bot` (rc7 minimum, rc8 full)

Per `docs/reference/integration-testing.md`. Use the Claude chat (`5284581592`).

**Bespoke scenario B-RESUME (new, add to the playbook):**
1. Send a prompt that spawns a background subagent AND keeps the model busy (e.g. "Launch a background agent to summarise the README, then wait for it"). Let it run so the process lingers post-result.
2. Send a follow-up ("what did it find?") that resumes.
3. **Assert:** the follow-up returns a REAL answer (num_turns>0), the run footer shows a **different** session id than the first (fresh recovery) OR the same id with real work (healthy resume), and NO "engine returned an empty result" notice appears.
4. Negative control: a normal two-message conversation with no background agent still resumes the SAME session (no spurious quarantine).

**Required tiers:** rc7 → Tier 7 (command smoke) + Tier 1 (Claude) + B-RESUME. rc8 → add Tier 1 (all 6 engines, confirm no cross-engine regression from the quarantine store) + Tier 2 (interactive/plan).

Automate via Telegram MCP (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`) + Bash (`journalctl --user -u untether-dev`) to read back `session.auto_resend_fresh` / `session.quarantined`.

## Layer 4 — Fleet-correlation verification (post-deploy, rc7 & rc8)

After rolling each rc, run a 24–48h correlation across all 5 hosts:

```bash
# per host: did every empty_result recover to a real answer?
journalctl --user -u untether --since "24 hours ago" -o short-iso \
 | grep -E "runner.empty_result|session.auto_resend_fresh|session.quarantined|session.resume_diverted_fresh" \
 | ...  # join by session_id, assert each empty_result is followed by auto_resend_fresh + a later real runner.completed
```
Add a helper `scripts/audit-noop-resume.sh <host…>` that emits, per host: count of `runner.empty_result`, recovery rate (`auto_resend_fresh` → subsequent `num_turns>0`), quarantine count, and any empty_result WITHOUT a recovery (regression signal).

**rc7 success:** every `runner.empty_result` is followed by a fresh recovery that yields a real answer.
**rc8 success:** the *frequency* of `runner.empty_result` drops (serialisation + lifecycle v2 prevent poisoning), and `limbo_detected`/`sigterm_after_timeout` counts fall.

## Definition of done (W6)

- `fake_claude_noop_resume.py` + fixture merged; all W1–W3 unit tests green against it.
- B-RESUME added to `docs/reference/integration-testing.md` and passing on `@untether_dev_bot`.
- `scripts/audit-noop-resume.sh` merged; clean rc7 correlation on all 5 hosts.
