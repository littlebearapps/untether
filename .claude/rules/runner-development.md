---
applies_to: "src/untether/runners/**,src/untether/runner.py"
---

# Runner Development Rules

## 3-event contract

Every run MUST emit exactly this sequence:
1. `StartedEvent` — first, when session ID is known; additional `StartedEvent`s with the same resume but new `meta` are allowed for late-arriving metadata (e.g. pi.py ships the model from `message_end` via a supplementary event; #225). The base runner's `handle_started_event` emits duplicates through when `event.meta` is truthy; `ProgressTracker.note_event` merges meta idempotently. True duplicates (no meta) are still dropped.
2. `ActionEvent(s)` — zero or more, phase: started/updated/completed
3. `CompletedEvent` — exactly once per run, always the run's final event

After emitting `CompletedEvent`, drop all subsequent JSONL lines — **unless** the runner keeps its process live (#776): it sets `JsonlStreamState.followup_turns = True` and each later turn in the same process is a bracketed segment `TurnEvent(started) → ActionEvent* → TurnEvent(completed)`, built with `EventFactory.turn_started` / `turn_completed`. Never emit a second `CompletedEvent`. Only `ClaudeRunner` in control-channel mode opts in (kill switch `[watchdog] live_sessions`); every other runner keeps the drop-after-completed behaviour. A runner that reads past the result must keep the #505 inherited-fd protection — `_subprocess_watchdog` does it for opt-in runners (2 s post-exit drain, then close our stdout read end).

## Stream state tracking

`JsonlStreamState` (defined in `src/untether/runner.py`) captures subprocess lifecycle data including `proc_returncode`. Signal deaths (rc>128 or rc<0) are NOT auto-continued — see `_is_signal_death()` in `runner_bridge.py`.

Runner instances are **shared across chats**, so `runner.current_stream` / `runner.last_pid` mean "latest spawn in any chat" and are diagnostics only (#510). Each run's `run_impl` publishes its own stream + PID together via `publish_run_stream(stream, pid)` into the per-run `RunStreamHandle` that `runner_bridge.run_runner_with_cancel` binds through a ContextVar. Bridge code must read the handle, never the runner attributes. A new runner that spawns its own process outside the base `run_impl` must call `publish_run_stream` at spawn.

## Auto-continue

When Claude Code exits with `last_event_type=user` (tool results sent but never processed), `runner_bridge.py` auto-resumes the session. Suppressed on signal deaths (rc=143/137) to prevent death spirals. Configure via `[auto_continue]` in `untether.toml` (`enabled`, `max_retries`).

Live sessions (#776, Claude only): a run's process stays live after its result while background work or a follow-up keeps it busy; `_live_session_lifecycle` closes stdin (graceful — the CLI stops its tasks and exits rc=0) when idle, and a resumed run's replayed stopped-task 0-turn result is absorbed by the resume guard rather than treated as an empty resume. The quarantine paths below now mostly apply to `live_sessions = false` and to forced teardown after a close grace. A close that overruns its 15 s grace logs `claude.live_session.close_grace_expired` (proc snapshot) and escalates SIGINT → SIGTERM 5 s later; a clean idle close (turn closed, no live background work) is **not** quarantined even then (#791).

Empty-resume recovery (#631/#632, Claude only): a resume returning 0 turns/$0 quarantines the session (`session_quarantine.py`, persisted to `session_quarantine.json`) and auto-resends once on a fresh session; forced teardown after a result quarantines proactively (except a clean idle live-session close, #791) and the next message diverts fresh. Flags: `empty_resume_fresh`, `quarantine_on_forced_teardown` (both default true). Never retry the same poisoned session.

## Event creation

Use `EventFactory` (from `src/untether/events.py`) for all event construction:
```python
factory = EventFactory(engine=self.engine)
factory.started(token, title="myengine")
factory.action_started(action_id=..., kind=..., title=...)
factory.action_completed(action_id=..., kind=..., title=..., ok=True)
factory.completed_ok(answer=..., resume=token, usage=...)
```

Do NOT construct `StartedEvent`, `ActionEvent`, `CompletedEvent` dataclasses directly.

## RunContext trigger_source (#271)

`RunContext` has a `trigger_source: str | None` field. Dispatchers set it to `"cron:<id>"` or `"webhook:<id>"`; `runner_bridge.handle_message` seeds `progress_tracker.meta["trigger"] = "<icon> <source>"`. Engine `StartedEvent.meta` merges over (not replaces) the trigger key via `ProgressTracker.note_event`. Runners themselves should NOT set `meta["trigger"]`; that's reserved for dispatchers.

## Session locking

- `SessionLockMixin` provides `lock_for(token) -> anyio.Semaphore`
- Keys: `"engine:session_id"` in a `WeakValueDictionary` (auto-cleanup)
- Resume runs: acquire lock before spawning subprocess
- New runs: acquire lock when session ID first appears, before yielding `StartedEvent`

## Tool-result classification (engine-agnostic)

`runner.py:_classify_jsonl_event()` peeks at every raw JSONL dict and classifies it as `"tool_result"`, `"assistant"`, or `"other"` for the stuck-after-tool_result detector (#322). This happens inside `_handle_jsonl_line` before `translate()`, so runners DO NOT need to call it. If a new engine is added, extend `_classify_jsonl_event()` with the engine's tool_result-equivalent event shape and assistant-turn-start shape — keep runner-side code (`translate()`, schema) untouched. The classifier is conservative: unknown shapes return `"other"` and the detector stays silent.

## Adding a new engine

1. Create `src/untether/runners/myengine.py` extending `JsonlSubprocessRunner`
2. Create `src/untether/schemas/myengine.py` with msgspec structs
3. Override: `command()`, `build_args()`, `translate()`, `new_state()`
4. Export `BACKEND = EngineBackend(id="myengine", build_runner=..., cli_cmd="myengine")`
5. Register in `pyproject.toml` entry points: `myengine = "untether.runners.myengine:BACKEND"`
6. Add reference docs in `docs/reference/runners/myengine/`
7. Add tests mirroring the Codex suite's patterns (`tests/test_codex_runner_helpers.py`, `tests/test_codex_schema.py`, `tests/test_codex_tool_result_summary.py`)

## Deprecated engines — sweep exemption

`gemini` and `amp` are **deprecated** and targeted for removal in 0.36.0. Both
are non-functional on the maintainer's accounts today (Gemini: upstream EOL for
individual accounts 2026-06-18; AMP: remote `426` refusal of out-of-date
clients), and neither has observed Untether usage.

**The rule that matters for day-to-day work:** cross-engine sweeps mechanically
touch all six runners — `stream_end_events` threading (#565),
`manage_subprocess` (#599), `_classify_jsonl_event` (#322), `build_args`. When a
sweep breaks `gemini` or `amp`:

> **`xfail`/`skip` the affected test — do NOT fix the runner.**

Mark it with a comment pointing at the removal issue. This is the whole point of
the deprecation: without this rule the posture buys nothing, because a
required-green `test_amp_runner.py` drags the fix back onto the critical path.

What still applies to both:

- Security fixes (credential handling, command injection) — always
- Doc accuracy fixes
- Mechanical inclusion in a sweep is fine when it's free; only the *repair* is exempt

What does NOT apply:

- New features or parity catch-up
- Required integration testing (both dropped from the Tier 1 matrix — see `docs/reference/integration-testing.md`)
- Investigation of upstream protocol changes

Do not delete their tests before the removal release — they run against fake
CLIs, cost nothing, and deleting ~100 tests would flatter the 80% coverage gate
while reducing compatibility coverage.

Antigravity CLI ([#558](https://github.com/littlebearapps/untether/issues/558))
is a **new engine**, not a `gemini` rename — it must not reuse the `gemini`
engine id, because its auth, flags, and session semantics differ.

## After changes

```bash
uv run pytest tests/test_*_runner.py tests/test_claude_control.py -x
```

If this change will be released, also run integration tests U1-U4, U6, U7 (all engines) via `@untether_dev_bot`. See `docs/reference/integration-testing.md` — the "Changed area" table maps runner changes to required tests.
