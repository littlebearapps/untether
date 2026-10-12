---
paths:
  - "src/untether/runners/**"
  - "src/untether/runner.py"
  - "src/untether/runner_bridge.py"
  - "src/untether/schemas/**"
  - "docs/reference/runners/**"
---

# Runner Development Rules

## Native-first (STRICT — every supported and future engine)

Applies to every fix, update, enhancement and new feature for Claude Code, Codex, OpenCode, Pi, Antigravity and any
engine added later. Not Gemini CLI / AMP: they get no feature work at all (see "Deprecated engines" below).

**Build on what the engine ships natively; Untether maps, passes through and renders it.** Custom Untether
functionality for an engine is allowed **only** when the engine does not provide that tool or feature. "Native is
harder to wire up" and "we already built it for another engine" are not exceptions.

### What counts as native (in order of preference)

1. Documented CLI flags, config/settings keys, env vars and hooks.
2. Documented protocol surface: stream events and their fields, `system/init` capabilities, and **mid-session
   requests** (Claude: `interrupt`, `set_model`, `set_permission_mode`, `stop_task`, `get_context_usage`, MCP
   status/reconnect). The engine's SDK reference is the documentation for these — read it even though Untether talks to
   the CLI directly.
3. Never, for new work: undocumented HTTP endpoints, reading the engine's credential or state files, or process
   signals where a native request does the same job.

Taking an engine's SDK **package** as a dependency (e.g. `claude-agent-sdk`) is an architecture decision for Nathan,
not something this rule requires or permits on its own. Until then, "use the SDK feature" means "use the CLI/protocol
feature the SDK wraps".

### Native only counts if it works on Untether's surface

A feature is native **for us** only when all of these hold; otherwise treat it as missing or partial and say which
test failed:

- available in headless / stream-json mode (not TUI-, Remote-Control- or cloud-only);
- available without a language binding Untether doesn't use (TypeScript-only SDK features don't count), and not
  marked experimental;
- works with the user's own logged-in CLI (subscription auth), not API-key only;
- meets the feature's real requirement — e.g. survives process exit and restarts, works unattended. Session-scoped
  scheduling that dies with the process is not a native replacement for a durable loop.

### Procedure

1. **Check before designing**, against the *installed* version (never memory — the CLIs self-update): `--help`, the
   CLI and SDK references, the changelog, and `docs/reference/runners/<engine>/`. Check requests and message fields,
   not just flags — the 2026-10 Claude audit found the gaps there, not in argv.
2. **Native exists → use it.** No parallel Untether state for something the engine already tracks or enforces
   (budgets, context usage, model, permission mode, sessions, queueing/priority of follow-ups), no deriving a value
   the engine will report, no instructions prepended to the user prompt when a system-prompt option exists.
3. **Read the catch first.** Native often differs in semantics (an estimate that can overshoot, a value fixed at
   process start, an extra API call). If the difference breaks the feature, that is "partial": use the native part,
   fill only the remainder, and write the difference down.
4. **Native missing or partial → custom, recorded.** Add a row to the engine's register in
   `docs/reference/runners/<engine>/` (what was checked, engine version, date, which test above failed) and point a
   code comment at the issue. A workaround for an upstream bug is custom code too: record the upstream issue and the
   condition for retiring it.
5. **Detect, don't assume.** Gate a native feature on `system/init` capabilities or a cached `--help` probe, not a
   version comparison, and keep the custom path only as the fallback for CLIs that lack it.
6. **Existing custom code.** When a change touches it, re-check the register. Migrate in the same change only when it
   is small and in scope; otherwise file an issue and leave the code. Never swap a working custom path for a native
   one without a live dev-bot run — the docs are not proof (precedent for retiring: the ExitPlanMode cooldown, #570).

Untether's own cross-engine surface (Telegram UX, progress, triggers, daily budget, watchdogs) is not "custom engine
functionality", but it must be fed from native data wherever the engine exposes it.

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

Live sessions (#776) and empty-resume recovery (#631/#632, Claude only): idle live sessions close gracefully via
`_live_session_lifecycle`; a 0-turn/$0 resume quarantines the session (`session_quarantine.py`) and auto-resends once on a
fresh session. **Never retry the same poisoned session.** Clean idle closes (#791) and `stopped_clean` closes (#829 B2) are
not quarantined. Detail: `docs/reference/runners/claude/runner.md` → "Live sessions".

A live session's errored first result is delivered early (#900, `_deliver_error_early` in `handle_message`) only when no
post-return recovery would act on it. The early check and the post-return path share the same gate helpers
(`_empty_resend_due`, `_auto_continue_due`, `_stream_idle_retry_due`): change a recovery condition there, never in one
path only, or an error final gets delivered and then recovered (or held and never recovered).

## Final delivery (#928, #948, #991)

- A final counts as **sent once it is handed to the transport**: `delivery["sent"]` is set before `send_result_message`
  and reset only if that call raises. The outbox delivers a queued op even when its waiter is cancelled (the early
  path's 60 s `_EARLY_DELIVERY_TIMEOUT_S` bound), so never gate "sent" on the await completing — that resends the
  final at live-session close (duplicate message + a second `$0` `runner.completed`).
- What follows the run's early final (outbox files, live-notice gate, background status, "Stop at limit" close) lives
  in `_after_run_delivered`, passed as `on_delivered` and run in its own shielded, bounded scope (#991). Never append a
  step to `_on_run_completed`: a send that uses up the delivery bound cancels everything after it in that callback.
- Before any final / cancelled edit of a progress message, `await edits.stop_repaints()` — never set `_finalizing`
  by hand. A debounced repaint already past its check would otherwise land after (and over) the final.

## Resume auto-clear (#45, #952)

A failed resumed run clears the chat's saved session only through `_resume_failure_clears_session()`: a reported
`num_turns` decides (0 → clear); with none, only the turn-count engines in `_TURN_COUNT_ENGINES` (Claude, AMP) clear,
and every other engine clears only when the error matches `_RESUME_FAILURE_RE` (the CLI's own "session not found"
wording). A pre-spawn block (`usage[PRESPAWN_BLOCKED_KEY]`, #838 — RAM, concurrency, OpenCode 2.x) never clears.
Neither does a failed `/continue` (`resume_token.is_continue`, #1039): the token names no session, and the callback
clears by engine, so it would wipe a saved session the run never touched.
Never treat a missing `num_turns` as 0 again: Codex/OpenCode/Pi never report turns, so a bad model id or missing
API key would wipe a healthy session.

## Stall threshold (#953)

A child process earns the 15-min `subagent_timeout` threshold unconditionally only for engines in
`_CHILD_WORK_ENGINES` (Claude — its children are Agent/Bash work). Elsewhere a child is usually permanent (Codex's npm
shim, OpenCode's MCP servers), so it counts only while the process tree is using CPU (or on the TCP signal). A
stopped engine process (state `T`/`t`) never earns it and is reported as "Engine process is stopped".

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

0. Apply "Native-first" above: inventory the engine's native features (flags, config, hooks, protocol, SDK) in
   `docs/reference/runners/myengine/` first, and list any gap that needs custom code
1. Create `src/untether/runners/myengine.py` extending `JsonlSubprocessRunner`
2. Create `src/untether/schemas/myengine.py` with msgspec structs
3. Override: `command()`, `build_args()`, `translate()`, `new_state()`
4. Export `BACKEND = EngineBackend(id="myengine", build_runner=..., cli_cmd="myengine")`
5. Register in `pyproject.toml` entry points: `myengine = "untether.runners.myengine:BACKEND"`
6. Add reference docs in `docs/reference/runners/myengine/`
7. Add tests mirroring the Codex suite's patterns (`tests/test_codex_runner_helpers.py`, `tests/test_codex_schema.py`, `tests/test_codex_tool_result_summary.py`)
8. Bridge tables in `runner_bridge.py`: add the CLI's "session not found" wording to `_RESUME_FAILURE_RE` (or the
   engine to `_TURN_COUNT_ENGINES` if it reports `num_turns`); add it to `_CHILD_WORK_ENGINES` only if its child
   processes are real work, never a permanent wrapper
9. A pre-spawn refusal (e.g. an unsupported CLI version — OpenCode's 2.x guard, #970) yields one
   `completed_error(..., usage={PRESPAWN_BLOCKED_KEY: "<reason>"})` before anything spawns, so the saved session is kept

## Deprecated engines — sweep exemption

`gemini` and `amp` are **deprecated and no longer supported**. They still ship
and load, but no removal is scheduled (a future release may drop them; tracked
in [#722](https://github.com/littlebearapps/untether/issues/722)). Both
are non-functional on the maintainer's accounts today (Gemini: upstream EOL for
individual accounts 2026-06-18; AMP: remote `426` refusal of out-of-date
clients), and neither has observed Untether usage.

**The rule that matters for day-to-day work:** cross-engine sweeps mechanically
touch all seven runners — `stream_end_events` threading (#565),
`manage_subprocess` (#599), `_classify_jsonl_event` (#322), `build_args`. When a
sweep breaks `gemini` or `amp`:

> **`xfail`/`skip` the affected test — do NOT fix the runner.**

Mark it with a comment pointing at #722. This is the whole point of
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

Do not delete their tests while the engines still ship — they run against fake
CLIs, cost nothing, and deleting ~100 tests would flatter the 80% coverage gate
while reducing compatibility coverage.

Antigravity CLI ([#558](https://github.com/littlebearapps/untether/issues/558))
is a **separate, supported engine** (`antigravity`, from v0.36.1), not a `gemini`
rename — it must not reuse the `gemini` engine id, because its auth, flags, and
session semantics differ. Two rules specific to it:

- `agy` parses a dash-leading token after a value-taking flag as another flag
  (`--model --version` prints the version). Build every user-influenced argv
  value with `utils/antigravity_argv.agy_flag()` (allow-list + joined
  `--flag=value`); never append a separate value token.
- Text that came from `agy` (stderr, `result.error`, `AGY_ERROR`) is surfaced
  only through `utils/antigravity_redact.redact_agy_text()`.

Reference: `docs/reference/runners/antigravity/`.

## After changes

```bash
uv run pytest tests/test_*_runner.py tests/test_claude_control.py -x
```

If this change will be released, also run integration tests U1-U4, U6, U7 (all 5 supported engines) via `@untether_dev_bot`. See `docs/reference/integration-testing.md` — the "Changed area" table maps runner changes to required tests.
