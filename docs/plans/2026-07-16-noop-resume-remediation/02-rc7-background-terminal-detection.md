# W3 — Background Terminal-Detection (minimal) — rc7 (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. TDD throughout.

**Goal:** Make "no live background work" a more trustworthy signal by (a) recognising the `Task` tool (not only `Agent`) as a background-agent primitive for observability, and (b) not treating the first tool_result as terminal for background agents that are known to still be running — the minimal, low-risk slice of the #374 fix, WITHOUT the full v0.35.5 lifecycle refactor (which is #573 / rc8).

**Issue:** **#374** (UPDATE — "clear handle on terminal signal, not first tool_result"). Milestone v0.35.4, rc7.

## Global Constraints

Inherit `README.md`. Critically: do NOT introduce a rule that can leave a handle uncleared forever (that wedges the post-result idle watchdog into a permanent hang — the failure mode the existing conservative comment at `claude.py:653-659` warns about). Every new "keep the handle" branch MUST have a bounded age-out.

## File Structure

- Modify `src/untether/runners/claude.py`:
  - `_register_background_handle` (524-591) — add `Task` alongside `Agent`.
  - `_is_terminal_tool_result` (630-670) — extend the interim-vs-terminal decision to background agents with a bounded deadline (mirroring the Monitor pattern), NOT an unbounded keep.
  - `ClaudeStreamState` — add `live_bg_agents` deadline semantics if adopting the bounded-keep (change `set` → `dict[str, float]` OR add a parallel `bg_agent_deadlines`).
- Test: `tests/test_claude_control.py`, `tests/test_proc_diag.py` (if `has_live_background_work` touched).

## Design decision (bounded keep, not unbounded)

Today `live_bg_agents` is a `set` and every non-Monitor tool_result is terminal. The safe minimal change:

1. **Recognise `Task`** in `_register_background_handle` — a `Task`/`Agent` tool_use with `run_in_background` (or the background-agent notification shape) registers a handle. Observability only in rc7; it does not yet change teardown.
2. **Keep the background-agent handle until the earlier of** (a) an explicit terminal signal (`is_error`, or a `KillShell`/completion tool_result referencing it), or (b) a bounded deadline `BG_AGENT_MAX_KEEP_S` (e.g. the MCP-tool stall threshold, 15 min). This prevents the "cleared on first interim tool_result" premature drain WITHOUT risking a permanent hang.

This makes `has_live_background_work()` return True while a background subagent is genuinely in flight, which (a) suppresses spurious stall warnings and (b) gives W2 (quarantine) and W4 (serialisation) a truer "is this session drained?" input.

## Tasks (TDD)

### Task 1: Recognise `Task` as a background-agent primitive

- [ ] **Failing test** (`tests/test_claude_control.py`):

```python
def test_task_tool_registers_background_handle():
    state = ClaudeStreamState(...)
    block = make_tool_use("Task", {"run_in_background": True}, tool_id="t1")
    _register_background_handle(state, block)
    assert "t1" in state.live_bg_agents

def test_task_tool_foreground_not_registered():
    state = ClaudeStreamState(...)
    block = make_tool_use("Task", {}, tool_id="t2")
    _register_background_handle(state, block)
    assert "t2" not in state.live_bg_agents
```

- [ ] **Verify fail** → `AssertionError` (Task not recognised).
- [ ] **Implement:** in `_register_background_handle`, change the Agent branch to `elif tool_name in ("Agent", "Task") and bool(raw_input.get("run_in_background")):`.
- [ ] **Verify pass.**
- [ ] **Commit** `feat(claude): recognise Task as a background-agent primitive (#374)`.

### Task 2: Bounded-keep for background-agent handles

- [ ] **Failing test:**

```python
def test_bg_agent_handle_kept_until_deadline(monkeypatch):
    state = ClaudeStreamState(...)
    _register_background_handle(state, make_tool_use("Agent", {"run_in_background": True}, "a1"))
    # first (interim) tool_result should NOT clear it
    interim = make_tool_result("a1", is_error=False)
    assert _is_terminal_tool_result(interim, state, "a1") is False
    # an error result IS terminal
    err = make_tool_result("a1", is_error=True)
    assert _is_terminal_tool_result(err, state, "a1") is True

def test_bg_agent_handle_ages_out(monkeypatch):
    state = ClaudeStreamState(...)
    _register_background_handle(state, make_tool_use("Agent", {"run_in_background": True}, "a2"))
    # advance monotonic past BG_AGENT_MAX_KEEP_S
    monkeypatch.setattr("untether.runners.claude.time.monotonic", lambda: BASE + BG_AGENT_MAX_KEEP_S + 1)
    interim = make_tool_result("a2", is_error=False)
    assert _is_terminal_tool_result(interim, state, "a2") is True  # aged out → no permanent hang
```

- [ ] **Verify fail.**
- [ ] **Implement:** give background agents a deadline like monitors. Add `state.bg_agent_deadlines: dict[str, float]` set to `monotonic() + BG_AGENT_MAX_KEEP_S` on register; extend `_is_terminal_tool_result` so a tracked bg-agent id returns terminal only on `is_error` or past-deadline, else non-terminal; ensure `_clear_background_handle` pops `bg_agent_deadlines`; ensure `has_live_background_work` ages these out identically.
- [ ] **Verify pass.**
- [ ] **Commit** `fix(claude): keep background-agent handle until terminal or bounded deadline (#374)`.

### Task 3: Regression — Monitor + foreground unchanged

- [ ] Re-run the existing #374 Monitor suite (`tests/test_claude_control.py -k monitor`) and `tests/test_exec_bridge.py -k stall` — assert `stall_monitor_active_suppressed` and foreground clear-on-first-result behaviour are unchanged.
- [ ] **Commit** only if code changed.

## Acceptance criteria (#374)

- `Task` and `Agent` with `run_in_background` both register a handle.
- A background-agent handle is retained across interim tool_results and cleared on `is_error`/terminal OR a bounded deadline (no permanent-hang risk).
- Monitor behaviour and foreground clear-on-result behaviour are byte-for-byte unchanged.
- `has_live_background_work()` reflects genuinely in-flight background agents → consumed by W2/W4.

## Explicitly OUT of scope (→ #573 / rc8, doc 03)

KillShell-driven termination, subprocess-exit reconciliation, child-PID cleanup, ScheduleWakeup/RemoteTrigger true-terminal detection. rc7 keeps those on their current conservative behaviour.
