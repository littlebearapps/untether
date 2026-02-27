# Untether

Telegram bridge for Claude Code, Codex, OpenCode, and other agent CLIs. Control your coding agents from anywhere — walking the dog, watching footy, at a friend's place.

**Repo**: [littlebearapps/untether](https://github.com/littlebearapps/untether)
**Based on**: [banteg/takopi](https://github.com/banteg/takopi) (upstream)

Untether adds interactive permission control, plan mode support, and several UX improvements on top of upstream takopi. All interactive features are Claude Code-specific; Codex, OpenCode, and other engines use standard non-interactive mode.

## Features (vs upstream takopi)

- **Interactive permission control** — bidirectional Telegram buttons for tool approval, plan mode, and clarifying questions
- **Pause & Outline Plan** — third button on plan approval to request a written plan before proceeding, with progressive cooldown
- **`/planmode`** — toggle permission mode per chat (on/off/auto)
- **Early callback answering** — clears button spinners immediately instead of waiting for processing
- **Approval push notifications** — separate notify message when approval buttons appear
- **Ephemeral message cleanup** — approval-related messages auto-delete when run finishes
- **Bold formatting** — command responses use HTML bold for key values
- **`/usage`** — shows API usage and cost for the current session
- **`/export`** — exports session transcript as markdown or JSON
- **`/browse`** — navigate project files via inline keyboard buttons
- **Cost tracking and budget** — per-run and daily cost limits with configurable alerts
- **Subscription usage footer** — configurable `[footer]` to show 5h/weekly subscription usage instead of/alongside API costs
- **Graceful restart** — `/restart` command drains active runs before restarting; SIGTERM also triggers graceful drain
- **Compact startup message** — version number, conditional diagnostics (only shows mode/topics/triggers/engines when they carry signal), project count instead of full list
- **Model/mode footer** — final messages show model name + permission mode (e.g. `🏷 sonnet · plan`) from `StartedEvent.meta`; all 4 engines populate model info

See `.claude/skills/claude-stream-json/` and `.claude/rules/control-channel.md` for implementation details.

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

## Key files

| File | Purpose |
|------|---------|
| `runners/claude.py` | Claude Code runner, interactive features |
| `runner_bridge.py` | Connects runners to Telegram presenter |
| `cost_tracker.py` | Per-run/daily cost tracking and budget alerts |
| `commands/claude_control.py` | Approve/Deny/Discuss callback handler |
| `commands/dispatch.py` | Callback dispatch and command routing |
| `markdown.py` | Progress/final message formatting, meta_line footer |
| `commands/planmode.py` | `/planmode` toggle command |
| `commands/usage.py` | `/usage` command |
| `commands/export.py` | `/export` command |
| `commands/browse.py` | `/browse` file browser |
| `commands/restart.py` | `/restart` graceful restart command |
| `shutdown.py` | Graceful shutdown state and drain logic |
| `telegram/bridge.py` | Telegram message rendering |
| `telegram/loop.py` | Telegram update loop, signal handlers, drain-then-exit |
| `commands.py` | Command result types |

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
| Release Coordination | `.claude/skills/release-coordination/` | Preparing releases, version bumps, changelog drafting, issue audits, rollback procedures |

## Hooks (project-scoped)

Project hooks in `.claude/hooks.json` fire automatically:

| Hook | Trigger | What it does |
|------|---------|-------------|
| pre-deploy-validation | `systemctl restart untether` | Reminds to run pytest + ruff first |
| runner-edit-context | Edit/Write to `runners/*.py` | 3-event contract, PTY lifecycle, test/doc reminders |
| schema-edit-context | Edit/Write to `schemas/*.py` | msgspec impact on parsing, fixture updates |
| telegram-edit-context | Edit/Write to `telegram/*.py` | Outbox model, callback_data limits, early answering |
| version-bump-checklist | Edit/Write to `pyproject.toml` (version change) | GitHub issues, CHANGELOG entry, `uv lock`, release checklist |

## Rules (project-scoped)

Rules in `.claude/rules/` auto-load when editing matching files:

| Rule | Applies to | Key constraints |
|------|-----------|----------------|
| `runner-development.md` | `runners/**`, `runner.py` | EventFactory usage, session locking, entry point registration |
| `telegram-transport.md` | `telegram/**` | Outbox-only writes, 64-byte callback data, ephemeral cleanup |
| `control-channel.md` | `runners/claude.py`, `claude_control.py` | PTY lifecycle, session registries, cooldown mechanics |
| `testing-conventions.md` | `tests/**` | pytest+anyio, stub patterns, 81% coverage threshold |
| `release-discipline.md` | `CHANGELOG.md`, `pyproject.toml` | GitHub issue linking, changelog format, semantic versioning |

## Tests

888 tests, 80% coverage threshold. Key test files:

- `test_claude_control.py` — 56 tests: control requests, response routing, registry lifecycle, auto-approve/auto-deny, tool auto-approve, custom deny messages, discuss action, early toast, progressive cooldown, auto permission mode
- `test_callback_dispatch.py` — 28 tests: callback parsing, dispatch toast/ephemeral behaviour, early answering
- `test_exec_bridge.py` — 24 tests: ephemeral notification cleanup, approval push notifications
- `test_ask_user_question.py` — 10 tests: AskUserQuestion control request handling, question extraction (direct and nested `questions` array format), pending request registry, answer routing
- `test_diff_preview.py` — 10 tests: Edit diff display, Write content preview, Bash command display, line/char truncation
- `test_cost_tracker.py` — 56 tests: cost accumulation, per-run/daily budget thresholds, warning levels, daily reset, auto-cancel flag
- `test_export_command.py` — 28 tests: session event recording, markdown/JSON export formatting, usage integration, session trimming
- `test_browse_command.py` — 36 tests: path registry, directory listing, file preview, inline keyboard buttons, project-aware root resolution, security (path traversal)
- `test_meta_line.py` — 26 tests: model name shortening, meta line formatting, ProgressTracker meta storage/snapshot, footer ordering (context/meta/resume)
- `test_runner_utils.py` — error formatting helpers, drain_stderr capture, enriched error messages
- `test_shutdown.py` — 4 tests: shutdown state transitions, idempotency, reset
- `test_restart_command.py` — 3 tests: command triggers shutdown, idempotent response, command id

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

## CI Pipeline

GitHub Actions CI runs on push to master and on PRs:

| Job | What it checks |
|-----|---------------|
| format | `ruff format --check --diff` |
| ruff | `ruff check` with GitHub annotations |
| ty | Type checking (Astral's ty) |
| pytest | Tests on Python 3.12, 3.13, 3.14 with 80% coverage threshold |
| build | `uv build` wheel + sdist validation |
| lockfile | `uv lock --check` ensures lockfile is in sync |
| pip-audit | Dependency vulnerability scanning (PyPA advisory DB) |
| bandit | Python SAST (security static analysis) |
| docs | Zensical docs build |

All third-party actions are pinned to commit SHAs (supply chain protection). Top-level `permissions: {}` restricts to least-privilege.

Release pipeline (`release.yml`) uses PyPI trusted publishing with OIDC.

## Issue tracking & releases

### GitHub issues

Every bug fix and significant change MUST have a GitHub issue:
- **Bugs found during debugging**: create an issue before or alongside the fix
- **Issue body**: description, impact, affected files, fix reference
- **Labels**: `bug`, `enhancement`, `documentation` as appropriate
- **Closing**: reference the fixing PR or commit in a close comment

### Changelog

`CHANGELOG.md` must be updated with every version bump:
- **Format**: `## vX.Y.Z (YYYY-MM-DD)` with `### fixes`, `### changes`, `### breaking`, `### docs`, `### tests` subsections
- **Issue links**: every fix/change entry must reference its GitHub issue: `[#N](https://github.com/littlebearapps/untether/issues/N)`
- **Scope**: one changelog section per release, no retroactive edits to prior sections

### Version bumps (semantic versioning)

- **Patch** (0.23.x → 0.23.y): bug fixes, schema additions for new upstream events, dependency updates
- **Minor** (0.x.0 → 0.y.0): new features, new commands, new engine support, config additions
- **Major** (x.0.0 → y.0.0): breaking changes to config format, runner protocol, or public API

### Release checklist

Before tagging a release:
1. All related GitHub issues exist and are referenced in CHANGELOG.md
2. CHANGELOG.md has an entry for the new version with correct date
3. `pyproject.toml` version matches the changelog heading
4. Tests pass: `uv run pytest`
5. Lint clean: `uv run ruff check src/`
6. Lockfile synced: `uv lock --check`

## Conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points in `pyproject.toml`
