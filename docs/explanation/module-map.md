# Module map

This page is a high-level map of Untether’s internal modules: what they do and how they fit together.

## Entry points

| Module | Responsibility |
|--------|----------------|
| `cli/` | Typer CLI package (`run.py`, `doctor.py`, `config.py`, `init.py`, …); loads settings, selects engine/transport, runs the transport backend. |
| `telegram/backend.py` | Telegram transport backend: validates config, runs onboarding, builds and runs the Telegram bridge. |

## Orchestration and routing

| Module | Responsibility |
|--------|----------------|
| `runner_bridge.py` | Transport-agnostic orchestration: per-message handler, progress updates, final render, cancellation, resume coordination. |
| `router.py` | Auto-router: resolves resume tokens by polling runners; selects a runner for a message. |
| `scheduler.py` | Per-thread FIFO job queueing with serialisation. |
| `directives.py`, `context.py`, `worktrees.py` | Directive parsing (`/<engine>`, `/<project>`, `@branch`, `dir:` lines), run context types, and branch worktree creation. |
| `transport_runtime.py` | Facade used by transports and commands to resolve messages and runners without importing internal router/project types. |
| `cost_tracker.py` | Per-run and daily cost tracking with budget alerts (checked when a result arrives; the daily gate before a run lives in `budget_gate.py`). |
| `session_stats.py` | Per-engine run counts, actions and durations behind `/stats`. |
| `error_hints.py` | Maps engine error text to the actionable hints shown in Telegram. |
| `shutdown.py` | Graceful shutdown state and drain logic. |
| `live_followup.py` | Writes a queued follow-up into a still-running Claude session (live sessions) instead of resuming a new process. |
| `session_costs.py` | Per-session cost and token ledger, so a resumed session's running totals are recorded per run. |
| `session_quarantine.py` | Persisted markers for sessions that must not be resumed (empty-resume recovery). |
| `permission_audit.py` | Startup / reload audit of Claude permission modes and crons that would wait for an approval tap. |
| `telegram/at_scheduler.py` | One-shot delayed runs from `/at <duration>`; in-memory state, drained on shutdown. |
| `loop_scheduler.py` | Loop mode firing for Claude's `/loop` and `ScheduleWakeup`; persists `active_loops.json` so loops survive restart. Mirrors `at_scheduler` API. |

## Domain model and events

| Module | Responsibility |
|--------|----------------|
| `model.py` | Domain types: resume tokens, events, actions, run results. |
| `runner.py` | Runner protocol and event queue utilities. |
| `events.py` | Event factory helpers for building Untether events consistently. |

## Rendering and progress

| Module | Responsibility |
|--------|----------------|
| `progress.py` | Progress tracking: reduces untether events into progress snapshots. |
| `markdown.py` | Markdown formatting for progress/final messages; includes helpers like elapsed formatting. |
| `background_status.py` | Live background-task block and status message for Claude live sessions. |
| `presenter.py` | Presenter protocol: converts `ProgressState` into transport-specific messages. |
| `transport.py` | Transport protocol: send/edit/delete abstractions and message reference types. |

## Telegram implementation

| Module | Responsibility |
|--------|----------------|
| `telegram/loop.py` | Main update loop: polls updates, filters and classifies messages, dispatches built-in commands and runs, applies config hot-reload. |
| `telegram/bridge.py` | `TelegramPresenter`, `TelegramBridgeConfig` and `TelegramTransport`, plus the cancel/send helpers (`run_main_loop` here delegates to `loop.py`). |
| `telegram/parsing.py` | Parses Bot API updates into incoming messages and callbacks. |
| `telegram/client.py`, `telegram/client_api.py`, `telegram/outbox.py` | Telegram API wrapper, the pooled `httpx` Bot API client, and the paced outbox with retry semantics. |
| `telegram/chat_sessions.py`, `telegram/topic_state.py`, `telegram/chat_prefs.py` | Persisted chat sessions, forum-topic bindings and sessions, and per-chat preferences. |
| `telegram/render.py` | Telegram markdown rendering and trimming. |
| `telegram/onboarding.py` | Interactive setup and setup validation UX. |
| `telegram/commands/*` | In-chat command handlers (`/agent`, `/file`, `/topic`, `/ctx`, `/new`, …). |
| `telegram/outbox_delivery.py` | Agent-initiated file delivery: scan outbox, send files as Telegram documents, cleanup. |
| `telegram/progress_persistence.py` | Active progress message persistence for orphan cleanup on restart. |
| `telegram/steer.py`, `telegram/followup_mode.py` | `/steer` / `/queue` and the per-chat follow-up mode (Claude live sessions). |
| `telegram/files.py` | File-transfer path rules, including deny-glob matching shared by `/file`, outbox and `/browse`. |
| `telegram/voice.py` | Voice-note transcription, including the default vocabulary hint. |

## Plugins

| Module | Responsibility |
|--------|----------------|
| `plugins.py` | Entrypoint discovery and lazy loading (capture load errors, filter by enabled list). |
| `engines.py` | Engine backend discovery and loading via entrypoints. |
| `transports.py` | Transport backend discovery and loading via entrypoints. |
| `commands.py` | Command backend discovery and loading via entrypoints; command execution helpers. |
| `ids.py` | Shared ID regex and collision checks for plugin ids and Telegram command names. |
| `api.py` | Public plugin API boundary (`untether.api` re-exports). |

## Runners and schemas

| Module | Responsibility |
|--------|----------------|
| `runners/*` | Engine runner implementations (Claude Code, Codex, OpenCode, Pi, and the deprecated Gemini CLI and Amp). |
| `runners/run_options.py` | Per-run options (model, reasoning, permission mode) and the Claude permission-mode tables. |
| `runners/extra_args_guard.py` | Rejects approval- and sandbox-bypass flags in `extra_args`. |
| `schemas/*` | msgspec schemas / decoders for engine JSONL streams. |

## Triggers

| Module | Responsibility |
|--------|----------------|
| `triggers/manager.py` | Mutable holder for crons + webhooks; hot-reload on TOML change; master `pause()` / `resume()` / `is_paused` toggle. |
| `triggers/server.py` | Webhook HTTP server (aiohttp); returns `503 triggers paused` while master pause is active; `/health` reflects paused state. |
| `triggers/dispatcher.py` | Routes webhook/cron fires to `run_job()` or non-agent action handlers. |
| `triggers/cron.py` | Cron expression parser, timezone-aware scheduler loop. |
| `triggers/history.py` | Persistent JSON history of cron/webhook fire times for `/stats` triggered/manual breakdown. |
| `triggers/describe.py` | Human-friendly cron rendering for `/ping`, `/config → ⏰ Triggers`. |
| `triggers/actions.py`, `triggers/fetch.py` | Non-agent webhook actions (`file_write`, `http_forward`, `notify_only`) and cron `fetch` steps. |
| `triggers/auth.py`, `triggers/ssrf.py` | Webhook authentication and the SSRF guard for outbound trigger requests. |

## Configuration and persistence

| Module | Responsibility |
|--------|----------------|
| `settings.py` | Loads `untether.toml` (TOML + env), validates with pydantic-settings. |
| `config.py` | Raw TOML read/write (merge/update without clobbering extra sections) and project config types. |
| `config_watch.py` | Watches `untether.toml` and triggers hot-reload (when `watch_config = true`). |
| `config_reload_notification.py` | Formats the Telegram notice after a reload ("No restart needed" / "Restart required"). |
| `lockfile.py` | Single-instance lock (`untether.lock`) so two processes can't poll the same bot. |
| `runtime_loader.py` | Builds the runtime (engines, router, projects) from settings at startup and on reload. |
| `config_migrations.py` | One-time edits to on-disk config (e.g. legacy Telegram key migration). |

## Utilities

| Module | Responsibility |
|--------|----------------|
| `utils/env_policy.py` | Engine subprocess environment allowlist (`[security] env_extra_allow` / `env_extra_prefix_allow`). |
| `utils/paths.py` | Path/command relativisation helpers. |
| `utils/streams.py` | Async stream helpers (`iter_bytes_lines`, stderr draining). |
| `utils/subprocess.py` | Subprocess management helpers (terminate/kill best-effort). |
| `utils/proc_diag.py` | Process diagnostics for stall analysis (CPU, RSS, TCP, FDs, children). |

