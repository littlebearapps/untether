# Untether

Telegram bridge for Claude Code, Codex, OpenCode, and other agent CLIs. Control your coding agents from anywhere — walking the dog, watching footy, at a friend's place.

**Repo**: [littlebearapps/untether](https://github.com/littlebearapps/untether)
**Based on**: [banteg/takopi](https://github.com/banteg/takopi) (upstream)

Untether adds interactive permission control, plan mode support, and several UX improvements on top of upstream takopi. All interactive features are Claude Code-specific; Codex, OpenCode, and other engines use standard non-interactive mode.

## Features (vs upstream takopi)

### Interactive permission control

Bidirectional control channel for Claude Code's `--permission-mode plan --permission-prompt-tool stdio` protocol:

- **ExitPlanMode approval** — Claude enters plan mode, Telegram shows **Approve / Deny / Pause & Outline Plan** buttons
- **Tool permission requests** — any tool requiring approval shows inline keyboard buttons
- **Tool auto-approve** — routine tools (Grep, Glob, Read, Bash, etc.) are auto-approved silently; only `ExitPlanMode` and `AskUserQuestion` prompt the user
- **AskUserQuestion support** — Claude's clarifying questions are shown in Telegram; user replies with text, which is routed back as the answer via `_PENDING_ASK_REQUESTS` registry
- **Diff preview in approvals** — Edit/Write/Bash tool approval messages include a compact diff preview (`_format_diff_preview`)
- **Concurrent sessions** — multiple chats can run Claude Code simultaneously via `_SESSION_STDIN` and `_REQUEST_TO_SESSION` registries

### "Pause & Outline Plan" button

When Claude requests ExitPlanMode, a third button lets the user ask Claude to write a step-by-step plan outline in the chat before proceeding:

- Sends a detailed deny message instructing Claude to list every file, change, and phase as visible text
- **Progressive cooldown** — auto-denies rapid ExitPlanMode retries: 30s → 60s → 90s → 120s (capped), escalating with clear BLOCKED messages
- Cooldown count is preserved across expiry so repeated discuss clicks keep escalating
- Deny count resets on explicit Approve or Deny

### `/planmode` command

Toggle Claude Code's `--permission-mode` per chat:

- `/planmode` — toggle on/off (treats `auto` as "on" for toggle)
- `/planmode on` — full plan mode with manual ExitPlanMode approval
- `/planmode auto` — plan mode with auto-approved ExitPlanMode (no buttons shown)
- `/planmode off` — acceptEdits mode, no plan phase
- `/planmode show` — show current state
- `/planmode clear` — remove override, use engine config default

`auto` mode stores `permission_mode = "auto"` in overrides but passes `--permission-mode plan` to the CLI. The `auto_approve_exit_plan_mode` flag on `ClaudeStreamState` causes ExitPlanMode requests to be silently auto-approved. Also works from `untether.toml`: `permission_mode = "auto"` in `[claude]`.

Persisted via `ChatPrefsStore` as an `EngineOverrides.permission_mode` field.

### Early callback answering

Telegram inline buttons show a spinner until `answerCallbackQuery` is called. Upstream defers this to the `finally` block (~150-300ms delay). Backends can set `answer_early = True` and provide `early_answer_toast()` to clear the spinner immediately with a toast ("Approved", "Denied", "Outlining plan...").

### Approval push notifications

`edit_message_text` doesn't trigger phone push notifications. Untether detects when approval buttons appear and sends a separate `notify=True` message ("Action required — approval needed"). The `_approval_notified` flag resets when buttons disappear, so subsequent approvals in the same run also notify.

### Ephemeral message cleanup

Approval-related messages auto-delete to keep the chat clean:

- "Action required" notification — deleted when the user clicks a button
- "Approved/Denied" feedback — deleted when the run finishes

Tracked via `_approval_notify_ref` (in `ProgressEdits`) and `_EPHEMERAL_MSGS` (in `runner_bridge.py`).

### Bold formatting in command responses

`/planmode`, `/agent`, `/model`, `/reasoning`, and `/trigger` commands return responses with key state values bolded. `CommandResult` supports a `parse_mode` field for HTML formatting through the command dispatch path.

### `/usage` command

Shows Claude Code API usage and cost for the current session.

### `/export` command

Exports the last session transcript as markdown or JSON:

- `/export` — markdown format with event timeline, usage stats, and summary
- `/export json` — structured JSON with all events and metadata

Session history is recorded automatically during runs (up to 20 sessions). Tracked in `_SESSION_HISTORY` within `commands/export.py`.

### `/browse` command

Navigate project files via inline keyboard buttons:

- `/browse` — list project root directory
- `/browse src` — browse a subdirectory by path
- Button navigation: tap directories to descend, `..` to go up, files to preview

**Project-aware**: resolves the project root from the chat's configured project route via `TransportRuntime.default_context_for_chat()`. Falls back to `get_run_base_dir()` or CWD. Path registry maps short numeric IDs to paths (avoids 64-byte callback_data limit).

### Cost tracking and budget

Per-run and daily cost tracking with configurable budgets:

```toml
[cost_budget]
enabled = true
max_cost_per_run = 2.00
max_cost_per_day = 10.00
warn_at_pct = 70
auto_cancel = false
```

- Cost displayed in progress footer: `💰 $0.37 · 9 turns · 1m 47s API · 11 in / 1.2k out`
- Budget alerts at warning threshold (70% by default) and exceeded
- Optional `auto_cancel` to stop runs that exceed the per-run budget
- Daily cost accumulates across runs, resets at midnight

Implemented in `cost_tracker.py` with budget checking in `runner_bridge.py`.

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi)
                                       |
                                  ProgressTracker
```

- **Runners** (`src/untether/runners/`) — engine-specific subprocess managers
- **RunnerBridge** (`src/untether/runner_bridge.py`) — connects runners to Telegram presenter, manages `ProgressEdits`
- **TelegramPresenter** (`src/untether/telegram/bridge.py`) — renders progress, inline keyboards, and answers
- **Commands** (`src/untether/telegram/commands/`) — command/callback handlers

### Concurrency design

`ClaudeRunner` is a singleton per engine, shared across all chats:

1. `self._proc_stdin` is unreliable (overwritten by last subprocess) — all stdin refs captured locally at spawn time
2. `_SESSION_STDIN` maps `session_id -> stdin pipe` (registered in `_iter_jsonl_events`)
3. `_REQUEST_TO_SESSION` maps `request_id -> session_id` for callback routing
4. Stdout pipe breaks immediately after `CompletedEvent` (MCP servers inherit the FD)
5. `ControlInitializeRequest` and auto-approved requests drained after every JSONL line

## Key files

| File | Purpose |
|------|---------|
| `runners/claude.py` | Control channel, stdin/session registries, `write_control_response` (with `deny_message`), Outline Plan button, progressive discuss cooldown, `auto` permission mode, AskUserQuestion handling, diff preview |
| `runner_bridge.py` | Approval push notifications, ephemeral message tracking/cleanup, cost budget checking, session export recording |
| `cost_tracker.py` | Per-run/daily cost accumulation, budget alerts (`CostBudget`, `CostAlert`) |
| `commands/claude_control.py` | Approve/Deny/Discuss handler, early answer toast, cooldown wiring |
| `commands/dispatch.py` | Callback dispatch, `callback_query_id` passthrough, ephemeral registration, early answering, `parse_mode` support |
| `commands/planmode.py` | `/planmode` toggle command |
| `commands/usage.py` | `/usage` command |
| `commands/export.py` | `/export` command, session history recording |
| `commands/browse.py` | `/browse` file browser with inline keyboard navigation |
| `commands/model.py` | Bold formatting for model override responses |
| `commands/reasoning.py` | Bold formatting for reasoning override responses |
| `commands/trigger.py` | Bold formatting for trigger mode responses |
| `commands/agent.py` | Bold formatting for engine selection responses |
| `telegram/bridge.py` | Inline keyboard rendering for control requests |
| `telegram/loop.py` | `callback_query_id` passthrough, AskUserQuestion text interception |
| `commands.py` | `parse_mode` field on `CommandResult` |

## Reference docs

Detailed protocol specs and event cheatsheets for each integration:

| Doc | Path | Covers |
|-----|------|--------|
| Claude runner spec | `docs/reference/runners/claude/runner.md` | CLI invocation, stream-json protocol, control channel, permission modes |
| Claude stream-json | `docs/reference/runners/claude/stream-json-cheatsheet.md` | JSONL event shapes (`system`, `assistant`, `user`, `result`) with examples |
| Claude event mapping | `docs/reference/runners/claude/untether-events.md` | Claude JSONL → Untether event translation rules |
| Codex exec-json | `docs/reference/runners/codex/exec-json-cheatsheet.md` | Thread/item/turn JSONL event shapes with examples |
| Codex event mapping | `docs/reference/runners/codex/untether-events.md` | Codex JSONL → Untether event translation rules |
| OpenCode runner spec | `docs/reference/runners/opencode/runner.md` | CLI invocation, step-based event model, session IDs |
| OpenCode stream-json | `docs/reference/runners/opencode/stream-json-cheatsheet.md` | JSONL event shapes (`StepStart`, `ToolUse`, `Text`, `StepFinish`) |
| OpenCode event mapping | `docs/reference/runners/opencode/untether-events.md` | OpenCode JSONL → Untether event translation rules |
| Pi runner spec | `docs/reference/runners/pi/runner.md` | CLI invocation, file-based sessions, provider/model selection |
| Pi stream-json | `docs/reference/runners/pi/stream-json-cheatsheet.md` | JSONL event shapes (`SessionHeader`, `AgentStart`, `ToolExecution`) |
| Pi event mapping | `docs/reference/runners/pi/untether-events.md` | Pi JSONL → Untether event translation rules |
| Telegram transport | `docs/reference/transports/telegram.md` | Bot API client, outbox/rate-limiting, voice transcription, forum topics |

## Skills (project-scoped)

Domain-specific Claude Code skills for working on Untether:

| Skill | Path | Use when |
|-------|------|----------|
| Telegram Bot API | `.claude/skills/telegram-bot-api/` | Working on Telegram transport, inline keyboards, outbox, rate limiting, voice, topics |
| JSONL Subprocess Runner | `.claude/skills/jsonl-subprocess-runner/` | Working on runner base class, event translation, session locking, adding engines |
| Claude stream-json | `.claude/skills/claude-stream-json/` | Working on Claude runner, control channel, permission modes, auto-approve, cooldown |
| Codex/OpenCode/Pi | `.claude/skills/codex-opencode-pi/` | Working on non-Claude runners, comparing engine protocols |
| Untether Architecture | `.claude/skills/untether-architecture/` | Understanding overall data flow, config system, progress tracking, project system |

## Hooks (project-scoped)

Project hooks in `.claude/hooks.json` fire automatically:

| Hook | Trigger | What it does |
|------|---------|-------------|
| pre-deploy-validation | `systemctl restart untether` | Reminds to run pytest + ruff first |
| runner-edit-context | Edit/Write to `runners/*.py` | 3-event contract, PTY lifecycle, test/doc reminders |
| schema-edit-context | Edit/Write to `schemas/*.py` | msgspec impact on parsing, fixture updates |
| telegram-edit-context | Edit/Write to `telegram/*.py` | Outbox model, callback_data limits, early answering |

## Rules (project-scoped)

Rules in `.claude/rules/` auto-load when editing matching files:

| Rule | Applies to | Key constraints |
|------|-----------|----------------|
| `runner-development.md` | `runners/**`, `runner.py` | EventFactory usage, session locking, entry point registration |
| `telegram-transport.md` | `telegram/**` | Outbox-only writes, 64-byte callback data, ephemeral cleanup |
| `control-channel.md` | `runners/claude.py`, `claude_control.py` | PTY lifecycle, session registries, cooldown mechanics |
| `testing-conventions.md` | `tests/**` | pytest+anyio, stub patterns, 81% coverage threshold |

## Tests

- `test_claude_control.py` — 56 tests: control requests, response routing, registry lifecycle, auto-approve/auto-deny, tool auto-approve, custom deny messages, discuss action, early toast, progressive cooldown, auto permission mode
- `test_callback_dispatch.py` — 28 tests: callback parsing, dispatch toast/ephemeral behaviour, early answering
- `test_exec_bridge.py` — 24 tests: ephemeral notification cleanup, approval push notifications
- `test_ask_user_question.py` — 10 tests: AskUserQuestion control request handling, question extraction (direct and nested `questions` array format), pending request registry, answer routing
- `test_diff_preview.py` — 10 tests: Edit diff display, Write content preview, Bash command display, line/char truncation
- `test_cost_tracker.py` — 56 tests: cost accumulation, per-run/daily budget thresholds, warning levels, daily reset, auto-cancel flag
- `test_export_command.py` — 28 tests: session event recording, markdown/JSON export formatting, usage integration, session trimming
- `test_browse_command.py` — 36 tests: path registry, directory listing, file preview, inline keyboard buttons, project-aware root resolution, security (path traversal)

## Development

```bash
# Install (editable)
pipx install -e /home/nathan/untether

# Run as systemd service
systemctl --user restart untether
journalctl --user -u untether -f

# Config
~/.untether/untether.toml

# Tests
cd /home/nathan/untether && uv run pytest

# Lint
cd /home/nathan/untether && uv run ruff check src/
```

## Conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points in `pyproject.toml`
