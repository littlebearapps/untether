# W1 + W2 + Diagnostics — rc7 Recovery & Quarantine (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit.

**Goal:** On the strict empty-0-turn resume anomaly, clear the poisoned session and re-run the original prompt as a FRESH session; and proactively quarantine any session Untether had to SIGTERM after a valid result so the next message never resumes it.

**Issues:** W1 → **#631** (UPDATE, umbrella). W2 → **NEW** ("Forced-teardown session quarantine"). Diagnostics folded into #631 acceptance criteria.

## Global Constraints

Inherit `README.md` §Global Constraints. Additionally: recovery is single-shot per message; the fresh-retry preserves the ORIGINAL user text; every branch logs a structured reason.

## File Structure

- Modify `src/untether/runner_bridge.py` — anomaly detection (~3259-3309), auto-resend block (~3635-3676), `handle_message` threading of a new `_empty_resent_count`-parallel guard is NOT needed (reuse `_empty_resent_count`).
- Modify `src/untether/runners/claude.py` — `_post_result_subcountdown` (records forced-teardown), and the settings loader `_load_auto_continue_settings` / its settings dataclass for the two flags.
- Create `src/untether/session_quarantine.py` — persisted quarantine store (JSON, debounced), modelled on `offset_persistence.py`.
- Test: `tests/test_exec_bridge.py` (recovery), `tests/test_session_quarantine.py` (new store), `tests/test_claude_control.py` (forced-teardown record).

**Interfaces produced (later workstreams rely on these):**
- `session_quarantine.QuarantineStore` with `quarantine(engine: str, session_id: str, reason: str) -> None`, `is_quarantined(engine: str, session_id: str) -> bool`, `clear(engine: str, session_id: str) -> None`, `load(path: Path) -> QuarantineStore`, `flush() -> None`.
- Settings fields on the auto-continue settings object: `resend_empty_resume: bool` (exists), `empty_resume_fresh: bool` (new, default True), `quarantine_on_forced_teardown: bool` (new, default True).

---

## Task 1: Config flags for fresh-retry and quarantine

**Files:**
- Modify: `src/untether/runners/claude.py` (the `_AutoContinueSettings`-style dataclass returned by `_load_auto_continue_settings`)
- Test: `tests/test_exec_bridge.py`

**Interfaces — Produces:** `settings.empty_resume_fresh: bool`, `settings.quarantine_on_forced_teardown: bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exec_bridge.py
def test_auto_continue_settings_new_flags_default_on(monkeypatch, tmp_path):
    from untether.runner_bridge import _load_auto_continue_settings
    monkeypatch.setenv("UNTETHER_CONFIG", str(tmp_path / "untether.toml"))
    (tmp_path / "untether.toml").write_text("[auto_continue]\nenabled = true\n")
    s = _load_auto_continue_settings()
    assert s.empty_resume_fresh is True
    assert s.quarantine_on_forced_teardown is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exec_bridge.py::test_auto_continue_settings_new_flags_default_on -v`
Expected: FAIL — `AttributeError: ... has no attribute 'empty_resume_fresh'`

- [ ] **Step 3: Add the fields to the settings dataclass + parser**

```python
# in the auto-continue settings dataclass (msgspec.Struct or dataclass)
class _AutoContinueSettings(...):
    enabled: bool = True
    max_retries: int = 1
    resend_empty_resume: bool = True
    empty_resume_fresh: bool = True          # W1: retry as a FRESH session, not same-session
    quarantine_on_forced_teardown: bool = True  # W2: mark force-killed-after-result sessions unsafe
```
And read them in `_load_auto_continue_settings` from the `[auto_continue]` table with the same `.get(..., default)` pattern already used for `resend_empty_resume`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_exec_bridge.py::test_auto_continue_settings_new_flags_default_on -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/untether/runners/claude.py tests/test_exec_bridge.py
git commit -m "feat(claude): add empty_resume_fresh + quarantine_on_forced_teardown flags (#631)"
```

---

## Task 2: Persisted quarantine store (new module)

**Files:**
- Create: `src/untether/session_quarantine.py`
- Test: `tests/test_session_quarantine.py`

**Interfaces — Produces:** `QuarantineStore` (see interfaces block above).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_quarantine.py
from pathlib import Path
from untether.session_quarantine import QuarantineStore

def test_quarantine_roundtrip(tmp_path: Path):
    p = tmp_path / "quarantine.json"
    store = QuarantineStore.load(p)
    assert store.is_quarantined("claude", "sid-1") is False
    store.quarantine("claude", "sid-1", reason="forced_teardown_after_result")
    assert store.is_quarantined("claude", "sid-1") is True
    store.flush()
    # reload from disk → survives restart
    store2 = QuarantineStore.load(p)
    assert store2.is_quarantined("claude", "sid-1") is True
    store2.clear("claude", "sid-1")
    assert store2.is_quarantined("claude", "sid-1") is False

def test_quarantine_isolated_by_engine(tmp_path: Path):
    store = QuarantineStore.load(tmp_path / "q.json")
    store.quarantine("claude", "sid", reason="x")
    assert store.is_quarantined("pi", "sid") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_quarantine.py -v`
Expected: FAIL — `ModuleNotFoundError: untether.session_quarantine`

- [ ] **Step 3: Implement the store**

```python
# src/untether/session_quarantine.py
"""Persisted per-session quarantine markers (#631/W2).

A session id is quarantined when Untether had to forcibly terminate its
subprocess after a valid result (post-result limbo SIGTERM/SIGKILL), or when
a strict empty-0-turn resume anomaly is observed. A quarantined session is
never resumed again — the next message on it starts a FRESH session.

Persisted to JSON (sibling to untether.toml) so a service restart cannot
re-enable a poisoned token. Writes are debounced; markers are pruned by age.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Prune markers older than this (a session id is only resumable within
# Claude's ~24h transcript retention anyway; 7d is a safe generous ceiling).
_MAX_AGE_SECONDS = 7 * 24 * 3600


def _key(engine: str, session_id: str) -> str:
    return f"{engine}:{session_id}"


@dataclass
class QuarantineStore:
    path: Path
    _entries: dict[str, dict[str, object]] = field(default_factory=dict)
    _dirty: bool = False

    @classmethod
    def load(cls, path: Path) -> "QuarantineStore":
        entries: dict[str, dict[str, object]] = {}
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                entries = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            logger.warning("quarantine.load_failed", path=str(path), exc_info=True)
        store = cls(path=path, _entries=entries)
        store._prune()
        return store

    def is_quarantined(self, engine: str, session_id: str) -> bool:
        return _key(engine, session_id) in self._entries

    def quarantine(self, engine: str, session_id: str, reason: str) -> None:
        k = _key(engine, session_id)
        if k in self._entries:
            return
        self._entries[k] = {"reason": reason, "ts": time.time()}
        self._dirty = True
        logger.warning("session.quarantined", engine=engine,
                       session_id=session_id, reason=reason)
        self.flush()

    def clear(self, engine: str, session_id: str) -> None:
        if self._entries.pop(_key(engine, session_id), None) is not None:
            self._dirty = True
            self.flush()

    def _prune(self) -> None:
        cutoff = time.time() - _MAX_AGE_SECONDS
        stale = [k for k, v in self._entries.items()
                 if float(v.get("ts", 0) or 0) < cutoff]
        for k in stale:
            del self._entries[k]
            self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self._entries))
            tmp.replace(self.path)  # atomic
            self._dirty = False
        except OSError:
            logger.warning("quarantine.flush_failed", path=str(self.path),
                           exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_quarantine.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/untether/session_quarantine.py tests/test_session_quarantine.py
git commit -m "feat: persisted session quarantine store (#631, new W2 issue)"
```

---

## Task 3: W1 — quarantine-and-fresh recovery (replace same-session resend)

**Files:**
- Modify: `src/untether/runner_bridge.py:3635-3676` (the `#596` auto-resend block)
- Test: `tests/test_exec_bridge.py`

**Interfaces — Consumes:** `settings.empty_resume_fresh`, `on_resume_failed(resume_token)`, `QuarantineStore` (via the bridge's store handle).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exec_bridge.py — uses the fake-claude harness from 04-test-strategy (W6a)
async def test_empty_resume_recovers_as_fresh_session(bridge_env):
    """On empty-0-turn resume, the token is cleared and the prompt re-runs fresh."""
    env = bridge_env(scenario="dangling_then_empty_resume")  # harness scenario
    cleared: list[str] = []
    env.on_resume_failed = lambda tok: cleared.append(tok.value)

    await env.send("please continue", resume_token=env.token("poisoned-sid"))

    # 1) the poisoned token was cleared
    assert "poisoned-sid" in cleared
    # 2) the recovery run used resume=None (fresh session), same original text
    fresh = env.runs[-1]
    assert fresh.resume_token is None
    assert fresh.text == "please continue"
    # 3) single-shot: only one recovery run
    assert sum(1 for r in env.runs if r.resume_token is None) == 1
    # 4) user got a real answer, not the "empty result" notice
    assert "engine returned an empty result" not in env.last_final_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exec_bridge.py::test_empty_resume_recovers_as_fresh_session -v`
Expected: FAIL — recovery still resumes the same token (`fresh.resume_token == poisoned-sid`).

- [ ] **Step 3: Implement quarantine-and-fresh**

Replace the auto-resend body (`runner_bridge.py:3643-3676`). Key change: clear the token, quarantine it, and re-run with `resume_token=None` when `empty_resume_fresh` is on.

```python
if empty_resume["pending"] and _empty_resent_count < 1:
    _er_settings = _load_auto_continue_settings()
    _poison = completed.resume or outcome.resume or resume_token
    if _er_settings.empty_resume_fresh and _poison is not None:
        # W1: the resumed session is poisoned (dangling upstream turn). Clear
        # the stored token and quarantine it so it is never resumed again,
        # then re-run the ORIGINAL prompt as a FRESH session.
        if on_resume_failed is not None:
            try:
                await on_resume_failed(_poison)
            except Exception:  # noqa: BLE001
                logger.debug("session.clear_failed", exc_info=True)
        quarantine_store.quarantine(runner.engine, _poison.value,
                                    reason="empty_zero_turn_resume")
        logger.warning("session.auto_resend_fresh",
                       old_session_id=_poison.value, engine=runner.engine,
                       attempt=_empty_resent_count + 1)
        _retry_resume = None                      # FRESH session
    else:
        # legacy same-session path (flag off): preserve #596 behaviour
        _retry_resume = _poison
        logger.warning("session.auto_resend_empty",
                       session_id=_poison.value if _poison else None,
                       engine=runner.engine, attempt=_empty_resent_count + 1)
    await handle_message(
        cfg, runner=runner,
        incoming=IncomingMessage(
            channel_id=incoming.channel_id, message_id=incoming.message_id,
            text=incoming.text, reply_to=incoming.reply_to,
            thread_id=incoming.thread_id),
        resume_token=_retry_resume,
        context=context, context_line=context_line,
        strip_resume_line=strip_resume_line, running_tasks=running_tasks,
        on_thread_known=on_thread_known, on_resume_failed=on_resume_failed,
        clock=clock,
        _auto_continued_count=_auto_continued_count,
        _empty_resent_count=_empty_resent_count + 1,
    )
    return
```

`quarantine_store` is the bridge-scoped `QuarantineStore` (Task 5 wires it in).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_exec_bridge.py::test_empty_resume_recovers_as_fresh_session -v`
Expected: PASS

- [ ] **Step 5: Regression — legacy flag still works**

```python
async def test_empty_resume_legacy_same_session_when_flag_off(bridge_env, monkeypatch):
    monkeypatch.setenv_toml("[auto_continue]\nempty_resume_fresh = false\n")
    env = bridge_env(scenario="dangling_then_empty_resume")
    await env.send("continue", resume_token=env.token("sid"))
    assert env.runs[-1].resume_token is not None  # same-session (#596 behaviour)
```
Run both; Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/untether/runner_bridge.py tests/test_exec_bridge.py
git commit -m "fix(bridge): quarantine-and-fresh recovery for empty 0-turn resume (#631)"
```

---

## Task 4: W2 — record forced teardown → quarantine + honour on next message

**Files:**
- Modify: `src/untether/runners/claude.py` (`_post_result_subcountdown`, where SIGTERM/SIGKILL is sent after a result)
- Modify: `src/untether/runner_bridge.py` (`handle_message`, near where `resume_token` is consumed) to divert quarantined tokens to fresh
- Test: `tests/test_claude_control.py`, `tests/test_exec_bridge.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_control.py — forced teardown records the marker
async def test_forced_teardown_after_result_quarantines(fake_claude_env):
    env = fake_claude_env(scenario="linger_then_sigterm_after_result")
    await env.run_to_completion(resume=None)  # produces a result, then lingers
    # limbo timeout elapses in the harness → Untether SIGTERMs
    env.advance_clock(env.limbo_timeout_s + 1)
    await env.drain()
    assert env.quarantine.is_quarantined("claude", env.session_id) is True

# tests/test_exec_bridge.py — next message on a quarantined token starts fresh
async def test_next_message_on_quarantined_session_starts_fresh(bridge_env):
    env = bridge_env()
    env.quarantine.quarantine("claude", "sid-q", reason="forced_teardown_after_result")
    await env.send("hi", resume_token=env.token("sid-q"))
    assert env.runs[-1].resume_token is None            # started fresh
    env.quarantine.clear.assert_not_called_or_noop()    # cleared only after a good run
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_claude_control.py::test_forced_teardown_after_result_quarantines tests/test_exec_bridge.py::test_next_message_on_quarantined_session_starts_fresh -v`
Expected: FAIL — no quarantine on teardown; quarantined token still resumed.

- [ ] **Step 3a: Record forced teardown in `_post_result_subcountdown`**

At the point Untether sends SIGTERM/SIGKILL to a process that had already emitted a result (the `sigterm_after_timeout` log site), record the marker. The runner needs a `QuarantineStore` handle (inject via the runner ctor / a module-level accessor consistent with how other cross-cutting state is shared). Pseudocode at the SIGTERM site:

```python
if stream.did_emit_completed and session_id is not None:
    # W2: the process produced a result but had to be force-killed while
    # lingering on background children → its last upstream turn may be left
    # dangling → the session is unsafe to resume.
    if _load_auto_continue_settings().quarantine_on_forced_teardown:
        get_quarantine_store().quarantine(
            self.engine, session_id, reason="forced_teardown_after_result")
    logger.warning("claude.post_result_idle.sigterm_after_timeout",
                   session_id=session_id, quarantined=True, ...)
```

- [ ] **Step 3b: Honour quarantine in `handle_message`**

Where `resume_token` is about to be used to spawn the run, divert quarantined tokens to fresh:

```python
if (
    resume_token is not None
    and quarantine_store.is_quarantined(runner.engine, resume_token.value)
):
    logger.info("session.resume_diverted_fresh",
                engine=runner.engine, session_id=resume_token.value,
                reason="quarantined")
    if on_resume_failed is not None:
        await on_resume_failed(resume_token)   # clear stored token
    resume_token = None                        # start fresh proactively
```

- [ ] **Step 3c: Clear the marker after a clean run**

Where a run completes with real work (`run_ok and num_turns > 0`), clear any quarantine for the NEW session id so a healthy session is never stuck fresh-only:

```python
if run_ok and (completed.usage or {}).get("num_turns", 0):
    fresh_sid = (completed.resume or outcome.resume)
    if fresh_sid is not None:
        quarantine_store.clear(runner.engine, fresh_sid.value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claude_control.py tests/test_exec_bridge.py -k "quarantin or teardown or fresh" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/untether/runners/claude.py src/untether/runner_bridge.py tests/
git commit -m "feat: quarantine sessions force-killed after result; divert next resume to fresh (#631 + new W2)"
```

---

## Task 5: Wire the QuarantineStore into the bridge/runner lifecycle

**Files:**
- Modify: `src/untether/runner_bridge.py` (construct `QuarantineStore.load(...)` once at startup; path sibling to config, e.g. `session_quarantine.json`)
- Modify: `src/untether/telegram/loop.py` (or wherever run state is assembled) to pass the store into `handle_message` / the runner
- Test: `tests/test_exec_bridge.py`

- [ ] **Step 1: Failing test** — `handle_message` uses an injected store; default path resolves next to config.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Add a module-level `get_quarantine_store()` accessor** (mirrors how `_load_auto_continue_settings` is reached from the runner) initialised from the config dir; inject into `handle_message` as a defaulted kwarg `quarantine_store: QuarantineStore | None = None` resolving to the singleton.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** `chore: wire QuarantineStore into bridge + runner (#631)`.

---

## Task 6: Diagnostics enrichment (W5-diag)

**Files:** Modify `src/untether/runner_bridge.py` (the `runner.empty_result` warning + the manual-notice branch).

- [ ] **Step 1: Failing test** — assert `runner.empty_result` log carries: `raw_subtype`, `is_error`, `proc_returncode`, `sigterm_sent`, `background_observed`, `resend_eligible_reason`.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Enrich the log** at `runner_bridge.py:3277`:

```python
logger.warning(
    "runner.empty_result",
    engine=runner.engine,
    resume=(completed.resume or run_outcome.resume).value if ... else None,
    was_resume=resume_token is not None,
    raw_subtype=(completed.usage or {}).get("subtype"),
    is_error=run_ok is False,
    proc_returncode=edits.stream.proc_returncode if edits.stream else None,
    sigterm_sent=edits.stream.sigterm_sent if edits.stream else None,
    background_observed=bool(edits.stream and edits.stream.background_observed),
    resend_eligible_reason=_resend_reason,  # "fresh" | "disabled" | "counter_exhausted" | "blank_input" | "no_token"
)
```
Set `_resend_reason` in the eligibility branch so the manual-notice path always records WHY it did not auto-recover (this closes the "auto-resend didn't fire" gap observed in the screenshot). Add `sigterm_sent` / `background_observed` bools to `JsonlStreamState` if absent (default False), set at the SIGTERM site and on `_register_background_handle`.

- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** `feat(bridge): structured diagnostics for empty 0-turn resume + resend eligibility reason (#631)`.

---

## Self-Review checklist

- [ ] `empty_resume_fresh` default True; legacy same-session path preserved behind the flag.
- [ ] Quarantine only on forced-teardown-after-result OR strict `num_turns==0 && duration_api_ms==0` anomaly — never on clean exit / real answer / API error.
- [ ] Recovery single-shot (`_empty_resent_count < 1`), preserves original user text.
- [ ] Quarantine marker cleared after a healthy run so a reused session id is never permanently fresh-only.
- [ ] Every non-recovery branch logs `resend_eligible_reason`.
- [ ] `uv run ruff format` clean; `uv run pytest tests/test_exec_bridge.py tests/test_session_quarantine.py tests/test_claude_control.py` green.
