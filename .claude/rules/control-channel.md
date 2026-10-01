---
paths:
  - "src/untether/runners/claude.py"
  - "src/untether/runners/run_options.py"
  - "src/untether/telegram/commands/claude_control.py"
  - "src/untether/telegram/commands/ask_question.py"
  - "src/untether/live_followup.py"
---

# Control Channel Rules (Claude runner)

Invariants only. Mechanism detail (registry table, claim flow, async-hook hold, plan re-arm, live-session close
sequence): `.claude/skills/claude-stream-json/control-channel-internals.md` and `docs/reference/runners/claude/runner.md`.
Read those before changing any of these areas.

## PTY + registries
- Stdin is a PTY (`pty.openpty()`, `tty.setraw(master)`); **always close the master FD in `finally`** — leaks break later runs.
- Register session registries on the first `system.init`; clean **all** of them (incl. outline/approval state) in `run_impl`'s `finally`.
- Every stdin write goes through `_locked_send` (per-pipe `anyio.Lock`) — several tasks write concurrently.
- Control responses go through `write_control_response(...)`. Taps go through `respond_to_control_request()` (#685):
  **never write a response without a claim** (`claim_control_request()` before the dispatcher's first `await`).
- A CLI `control_cancel_request` (#684) retires the request everywhere and records `cancelled`; write nothing back.
  A cancel racing an in-flight tap defers via `_CANCELLED_DURING_WRITE`.

## Auto-approve
- Auto-approve the non-interactive request types in `_AUTO_APPROVE_TYPES`.
- `_TOOLS_REQUIRING_APPROVAL = {"ExitPlanMode", "AskUserQuestion"}` — **`ExitPlanMode` is never auto-approved**.
- Prompting modes (`default`/`manual`/`acceptEdits`) route every tool to Telegram (#749); `is_claude_prompting_mode()`
  in `runners/run_options.py` is the single classification point.

## Permission modes (#741)
- Only `plan-auto` is translated (→ CLI `plan` + ExitPlanMode rubber stamp). Every genuine CLI mode passes through verbatim.
- `auto` is the CLI's classifier mode: it must **never** arm `auto_approve_exit_plan_mode`.
- Never re-introduce an inline `"plan" if mode == "auto"` remap. Add new modes to `CLAUDE_CLI_PERMISSION_MODES`
  (drift test in `tests/test_claude_permission_modes.py`).
- Plan re-arm (#383): only when configured mode maps to CLI `plan`, session is live, plan was observed, it has left plan,
  nothing in flight. Never for prompting modes or `auto`/`dontAsk`/`bypassPermissions`. Approval flags are turn-scoped
  (`_open_followup_turn` clears `_PLAN_EXIT_APPROVED`; `_DISCUSS_APPROVED` survives one boundary via `_DISCUSS_CARRY`).
  A mid-turn steer fold is never a boundary. Defer while plan-exit-turn agents run — key on `origin_turn`, never start time.
- The idle-boundary re-arm is written **before** the turn-closing event is yielded (`_drain_plan_rearm_pre_yield`);
  never move it to the post-yield drains.

## Live sessions (#776)
- Never write a follow-up mid-turn outside steer mode — use `inject_when_idle`. `LiveSession.lock` serialises injection
  against `close_live_session`.
- `_SESSION_STDIN` means "a process owns this session"; ask `is_session_accepting()` before writing a follow-up.
- Async hooks (#812): `has_pending_async_hooks()` is a sibling predicate — never part of `has_live_background_work()`.
  Hook release is all-or-nothing; **never bind a process to a hook**; never rely on `sh -c` to identify hook processes.
- Timing knobs are slots-dataclass fields: set them on the instance in tests.

## Outline gate / post-outline approval
- After "Pause & Outline", the gate is text-based (`_OUTLINE_MIN_CHARS`): short → auto-deny, written → hold open with buttons.
- Synthetic buttons use the `da:` callback prefix (64-byte limit), handled in `claude_control.py` before approve/deny.
- "Let's discuss" holds the request open; the 5-min sweep is event-driven, not a timer.

## Parent-initiated control requests
- Request ids use the `ut_<feature>_<session_id>_<seq>` namespace (never collide with the CLI's `req_*`). Extend the
  internals doc when adding a subtype.

## After changes
```bash
uv run pytest tests/test_claude_control.py tests/test_ask_user_question.py tests/test_diff_preview.py -x
```
Before release: integration tests C1–C6, T8, S9 via `@untether_dev_bot` (`docs/reference/integration-testing.md`).
