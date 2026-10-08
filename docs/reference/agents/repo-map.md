# Repo map

Quick pointers for navigating the Untether codebase.

## Where things start

- CLI entry point: `src/untether/cli/` (`__init__.py` builds the Typer app; `run.py`, `init.py`, `doctor.py`, `plugins.py`, `config.py`)
- Telegram backend entry point: `src/untether/telegram/backend.py`
- Telegram update loop: `src/untether/telegram/loop.py`
- Telegram presenter, transport and bridge config: `src/untether/telegram/bridge.py`
- Transport-agnostic handler: `src/untether/runner_bridge.py`
- Runtime assembly (backends, router, projects): `src/untether/runtime_loader.py`, `src/untether/transport_runtime.py`

## Core concepts

- Domain types (resume tokens, events incl. `TurnEvent`, actions): `src/untether/model.py`
- Event factory: `src/untether/events.py`
- Runner protocol, `BaseRunner` locking, `JsonlSubprocessRunner`, per-run `RunStreamHandle`: `src/untether/runner.py`
- Directives and `dir:` context lines: `src/untether/directives.py`, `src/untether/context.py`, `src/untether/worktrees.py`
- Router selection and resume polling: `src/untether/router.py`
- Per-thread scheduling: `src/untether/scheduler.py`
- Progress reduction and rendering: `src/untether/progress.py`, `src/untether/markdown.py`

## Engines and streaming

- Runner implementations: `src/untether/runners/*` (`claude.py` holds all interactive features; `gemini.py` and `amp.py` are deprecated and no longer supported, but still load)
- Per-run engine options (model, reasoning, permission mode) via ContextVar: `src/untether/runners/run_options.py`
- `extra_args` deny-list tokeniser: `src/untether/runners/extra_args_guard.py`
- JSONL decoding schemas: `src/untether/schemas/*`

## Live sessions and follow-ups (Claude)

- Follow-up injection into a live session: `src/untether/live_followup.py`
- Background-task status message: `src/untether/background_status.py`
- Steer / follow-up mode: `src/untether/telegram/steer.py`, `src/untether/telegram/followup_mode.py`, `src/untether/telegram/commands/followup.py`
- Empty-resume quarantine markers: `src/untether/session_quarantine.py`

## Sessions, costs and permissions

- Chat-mode and topic session stores: `src/untether/telegram/chat_sessions.py`, `src/untether/telegram/topic_state.py`
- Per-session cost and token ledger: `src/untether/session_costs.py`; budgets: `src/untether/cost_tracker.py`
- Permission-mode audit and shared mode text: `src/untether/permission_audit.py`, `src/untether/telegram/commands/_permission_mode_text.py`
- Triggers (cron/webhooks): `src/untether/triggers/*`

## Plugins

- Public API boundary (`untether.api`): `src/untether/api.py`
- Entrypoint discovery + lazy loading: `src/untether/plugins.py`
- Engine/transport/command backend loading: `src/untether/engines.py`, `src/untether/transports.py`, `src/untether/commands.py`

## Configuration

- Settings model + TOML/env loading: `src/untether/settings.py`
- Config migrations: `src/untether/config_migrations.py`
- Hot reload: `src/untether/config_watch.py`
- Single-instance lock: `src/untether/lockfile.py`

## Docs and contracts

- Normative behaviour: [Specification](../specification.md)
- Runner invariants: `tests/test_runner_contract.py`

