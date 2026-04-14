---
applies_to: "src/untether/runners/**,src/untether/runner.py"
---

# Runner Development Rules

## 3-event contract

Every run MUST emit exactly this sequence:
1. `StartedEvent` — once, when session ID is known
2. `ActionEvent(s)` — zero or more, phase: started/updated/completed
3. `CompletedEvent` — exactly once, always the final event

After emitting `CompletedEvent`, drop all subsequent JSONL lines.

## Stream state tracking

`JsonlStreamState` (defined in `src/untether/runner.py`) captures subprocess lifecycle data including `proc_returncode`. Signal deaths (rc>128 or rc<0) are NOT auto-continued — see `_is_signal_death()` in `runner_bridge.py`.

## Auto-continue

When Claude Code exits with `last_event_type=user` (tool results sent but never processed), `runner_bridge.py` auto-resumes the session. Suppressed on signal deaths (rc=143/137) to prevent death spirals. Configure via `[auto_continue]` in `untether.toml` (`enabled`, `max_retries`).

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

## Adding a new engine

1. Create `src/untether/runners/myengine.py` extending `JsonlSubprocessRunner`
2. Create `src/untether/schemas/myengine.py` with msgspec structs
3. Override: `command()`, `build_args()`, `translate()`, `new_state()`
4. Export `BACKEND = EngineBackend(id="myengine", build_runner=..., cli_cmd="myengine")`
5. Register in `pyproject.toml` entry points: `myengine = "untether.runners.myengine:BACKEND"`
6. Add reference docs in `docs/reference/runners/myengine/`
7. Add tests mirroring `tests/test_codex_runner.py` patterns

## After changes

```bash
uv run pytest tests/test_*_runner.py tests/test_claude_control.py -x
```

If this change will be released, also run integration tests U1-U4, U6, U7 (all engines) via `@untether_dev_bot`. See `docs/reference/integration-testing.md` — the "Changed area" table maps runner changes to required tests.
