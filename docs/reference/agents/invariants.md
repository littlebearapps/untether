# Invariants

These are the “don’t break this” rules that keep Untether reliable.

## Runner contract

The runner contract is enforced by `tests/test_runner_contract.py`:

- Exactly one `StartedEvent`
- Exactly one `CompletedEvent`
- `CompletedEvent` is last
- `CompletedEvent.resume == StartedEvent.resume`

The one exception: a Claude live session may follow `CompletedEvent` with
`TurnEvent(started) → ActionEvent* → TurnEvent(completed)` segments, one per later turn
([#776](https://github.com/littlebearapps/untether/issues/776)). It still emits exactly one
`StartedEvent` and one `CompletedEvent`.

See also the [Plugin API](../plugin-api.md) runner contract section.

## Per-thread serialisation

At most one active run may operate on the same thread/session at a time.
This is enforced both by scheduling and by per-resume-token runner locks.

- New runs and `/continue` runs lock the session id named by their first `StartedEvent`;
  a `/continue` token is never used as a lock key ([#817](https://github.com/littlebearapps/untether/issues/817)).
- Follow-ups for a live Claude session are injected into the running process rather than
  started as a second run; the CLI serialises the turns.
- A message for a chat whose first run hasn't reported its session yet waits for that run
  instead of starting a second conversation; `/cancel` and `/new` drop such waiting messages
  ([#1157](https://github.com/littlebearapps/untether/issues/1157), `telegram/first_start.py`).
- Follow-ups in one chat, topic or session scope keep their arrival order: each takes a ticket in
  `telegram/arrival_order.py` before its first await, and a default steer never overtakes an earlier
  follow-up that is still queued (`ThreadScheduler.has_unstarted`). `/new`, and `/cancel` while the first
  run is starting, drop prompts still waiting their turn ([#1164](https://github.com/littlebearapps/untether/issues/1164)).

## Per-run state

Runner instances are shared across chats. Never read a run's stream state or PID from the
runner instance (`current_stream` / `last_pid` are diagnostics only): the bridge reads the
per-run `RunStreamHandle` the runner publishes through a ContextVar
([#510](https://github.com/littlebearapps/untether/issues/510)). Wrapping generators close
`run_impl` with `contextlib.aclosing` so cancellation unwinds in the right task
([#854](https://github.com/littlebearapps/untether/issues/854)).

Normative details live in the [Specification](../specification.md) (§5.2).

## Antigravity permission boundary

- `decide_antigravity_mode()` (`runners/antigravity.py`) is the only place that grants agy's
  `--dangerously-skip-permissions`: Full access, or Ask me once Untether's agy gate is ready for the
  project. Ask me without a ready gate is refused before agy starts, never downgraded silently.
- The gate's `PreToolUse` hook fails closed (no socket, bad reply or timeout → `deny`).
- Every user-influenced agy argv value is joined to its flag (`--flag=value`) through
  `utils/antigravity_argv.py`, and every piece of agy text shown or logged passes
  `utils/antigravity_redact.py` ([#558](https://github.com/littlebearapps/untether/issues/558)).

## Resume lines

Resume lines embedded in chat are the engine’s canonical resume command (e.g. `claude --resume <id>`).

- The runner is authoritative for formatting and extraction.
- Transports/rendering must preserve the resume line reliably (even when trimming/splitting).

Normative details live in the [Specification](../specification.md) (§3).

## Local contribution hygiene

- Run `just check` before code commits (ruff format check, ruff, ty, pytest; see `Justfile`).

