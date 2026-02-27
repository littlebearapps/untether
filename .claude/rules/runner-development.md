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
