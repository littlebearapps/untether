# Untether

Telegram bridge for Claude Code, Codex, OpenCode, Pi, Gemini CLI, Amp, and other agent CLIs. Control your coding agents from anywhere — walking the dog, watching footy, at a friend's place.

**Repo**: [littlebearapps/untether](https://github.com/littlebearapps/untether)
**Based on**: [banteg/takopi](https://github.com/banteg/takopi) (upstream)

Untether adds interactive permission control, plan mode support, and several UX improvements on top of upstream takopi. All interactive features are Claude Code-specific; Codex, OpenCode, and other engines use standard non-interactive mode.

## Features (vs upstream takopi)

- **Interactive permission control** — bidirectional Telegram buttons for tool approval, plan mode, and clarifying questions
- **Pause & Outline Plan** — third button on plan approval; after Claude writes the outline, Approve/Deny buttons appear automatically (hold-open keeps session alive while user reads)
- **Agent context preamble** — configurable prompt preamble tells agents they're on Telegram and requests structured end-of-task summaries; `[preamble]` config section
- **`/planmode`** — toggle permission mode per chat (on/off/auto)
- **Ask mode** — interactive AskUserQuestion with option buttons, sequential multi-question flows, and `/config` toggle; Claude-only
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
- **Model/mode footer** — final messages show model name + permission mode (e.g. `🏷 sonnet · plan`) from `StartedEvent.meta`; all engines populate model info
- **`/verbose`** — toggle verbose progress mode per chat; shows tool details (file paths, commands, patterns) in progress messages
- **`/config`** — inline settings menu with navigable sub-pages; toggle plan mode, ask mode, verbose, engine, trigger via buttons
- **`[progress]` config** — global verbosity and max_actions settings in `untether.toml`
- **Pi context compaction** — `AutoCompactionStart`/`AutoCompactionEnd` events rendered as progress actions
- **Stall diagnostics & liveness watchdog** — `/proc` process diagnostics (CPU, RSS, TCP, FDs), progressive stall warnings with Telegram notifications, liveness watchdog for alive-but-silent subprocesses, stall auto-cancel (dead process, no-PID zombie, absolute cap) with CPU-active suppression, `session.summary` structured log; `[watchdog]` config section
- **File upload deduplication** — auto-appends `_1`, `_2`, … when target file exists, instead of requiring `--force`; media groups without captions auto-save to `incoming/`
- **Resume line formatting** — visual separation with blank line and ↩️ prefix in final message footer
- **`/continue`** — cross-environment resume; pick up the most recent CLI session from Telegram using each engine's native continue flag (`--continue`, `resume --last`, `--resume latest`); supported for Claude, Codex, OpenCode, Pi, Gemini (not AMP)

See `.claude/skills/claude-stream-json/` and `.claude/rules/control-channel.md` for implementation details.

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi/gemini/amp)
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
| `runners/gemini.py` | Gemini CLI runner |
| `runners/amp.py` | AMP CLI runner (Sourcegraph) |
| `runner_bridge.py` | Connects runners to Telegram presenter, injects agent preamble |
| `cost_tracker.py` | Per-run/daily cost tracking and budget alerts |
| `commands/claude_control.py` | Approve/Deny/Discuss callback handler |
| `commands/dispatch.py` | Callback dispatch and command routing |
| `markdown.py` | Progress/final message formatting, meta_line footer |
| `commands/planmode.py` | `/planmode` toggle command |
| `commands/usage.py` | `/usage` command |
| `commands/export.py` | `/export` command |
| `commands/browse.py` | `/browse` file browser |
| `commands/restart.py` | `/restart` graceful restart command |
| `commands/verbose.py` | `/verbose` toggle command |
| `commands/config.py` | `/config` inline settings menu |
| `commands/ask_question.py` | AskUserQuestion option button handler |
| `utils/proc_diag.py` | `/proc` process diagnostics for stall analysis (CPU, RSS, TCP, FDs, children) |
| `shutdown.py` | Graceful shutdown state and drain logic |
| `telegram/bridge.py` | Telegram message rendering |
| `telegram/loop.py` | Telegram update loop, signal handlers, drain-then-exit |
| `telegram/files.py` | File upload helpers, deduplication, deny globs, atomic writes |
| `commands.py` | Command result types |
| `scripts/validate_release.py` | Release validation (changelog format, issue links, version match) |
| `scripts/healthcheck.sh` | Post-deploy health check (systemd, version, logs, Bot API) |
| `cliff.toml` | git-cliff config for changelog drafting |

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
| Gemini runner spec | `docs/reference/runners/gemini/runner.md` | CLI invocation, stream-json, model selection |
| Gemini stream-json | `docs/reference/runners/gemini/stream-json-cheatsheet.md` | JSONL event shapes (`init`, `message`, `tool_use`, `tool_result`, `result`, `error`) |
| Gemini event mapping | `docs/reference/runners/gemini/untether-events.md` | Gemini JSONL → Untether event translation rules |
| AMP runner spec | `docs/reference/runners/amp/runner.md` | CLI invocation, stream-json, mode/model selection |
| AMP stream-json | `docs/reference/runners/amp/stream-json-cheatsheet.md` | JSONL event shapes (`system`, `assistant`, `user`, `result`) |
| AMP event mapping | `docs/reference/runners/amp/untether-events.md` | AMP JSONL → Untether event translation rules |
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
| release-guard | Bash: `git push`, `git tag`, `gh pr merge`, `gh release` | Blocks pushes to master/main, tag creation, PR merging, releases; allows feature branch pushes |
| release-guard-protect | Edit/Write to guard scripts or `hooks.json` | Prevents modification of release guard infrastructure |
| release-guard-mcp | GitHub MCP write tools | Blocks `merge_pull_request` and writes to master/main; allows feature branches |
| dev-workflow-guard | `systemctl` with `untether` | Blocks staging restarts during dev; guides to `untether-dev`; allows `staging.sh`/`pipx upgrade` path |
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
| `testing-conventions.md` | `tests/**` | pytest+anyio, stub patterns, 80% coverage threshold |
| `release-discipline.md` | `CHANGELOG.md`, `pyproject.toml` | GitHub issue linking, changelog format, semantic versioning |
| `dev-workflow.md` | `src/untether/**` | Dev vs staging separation, never restart staging for testing, always use untether-dev |

## Tests

1578 unit tests, 80% coverage threshold. Integration testing against `@untether_dev_bot` is **mandatory before every release** — see `docs/reference/integration-testing.md` for the full playbook with per-release-type tier requirements (patch/minor/major). All integration test tiers are fully automated by Claude Code via Telegram MCP tools and Bash.

Key test files:

- `test_claude_control.py` — 82 tests: control requests, response routing, registry lifecycle, auto-approve/auto-deny, tool auto-approve, custom deny messages, discuss action, early toast, progressive cooldown, auto permission mode
- `test_callback_dispatch.py` — 25 tests: callback parsing, dispatch toast/ephemeral behaviour, early answering
- `test_exec_bridge.py` — 91 tests: ephemeral notification cleanup, approval push notifications, progressive stall warnings, stall diagnostics, stall auto-cancel with CPU-active suppression, approval-aware stall threshold, session summary, PID/stream threading
- `test_ask_user_question.py` — 25 tests: AskUserQuestion control request handling, question extraction, pending request registry, answer routing, option button rendering, multi-question flows, structured answer responses, ask mode toggle auto-deny
- `test_diff_preview.py` — 14 tests: Edit diff display, Write content preview, Bash command display, line/char truncation
- `test_cost_tracker.py` — 12 tests: cost accumulation, per-run/daily budget thresholds, warning levels, daily reset, auto-cancel flag
- `test_export_command.py` — 15 tests: session event recording, markdown/JSON export formatting, usage integration, session trimming
- `test_browse_command.py` — 39 tests: path registry, directory listing, file preview, inline keyboard buttons, project-aware root resolution, security (path traversal)
- `test_meta_line.py` — 43 tests: model name shortening, meta line formatting, ProgressTracker meta storage/snapshot, footer ordering (context/meta/resume)
- `test_runner_utils.py` — 34 tests: error formatting helpers, drain_stderr capture, enriched error messages, stderr sanitisation
- `test_shutdown.py` — 4 tests: shutdown state transitions, idempotency, reset
- `test_preamble.py` — 5 tests: default preamble injection, disabled preamble, custom text override, empty text disables, settings defaults
- `test_restart_command.py` — 3 tests: command triggers shutdown, idempotent response, command id
- `test_cooldown_bypass.py` — 19 tests: outline bypass, rapid retry auto-deny, no-text auto-deny, cooldown escalation, hold-open outline flow
- `test_verbose_progress.py` — 21 tests: format_verbose_detail() for each tool type, MarkdownFormatter verbose mode, compact regression
- `test_verbose_command.py` — 7 tests: /verbose toggle on/off/clear, backend id
- `test_config_command.py` — 195 tests: home page, plan mode/ask mode/verbose/engine/trigger/model/reasoning sub-pages, toggle actions, callback vs command routing, button layout, engine-aware visibility, default resolution
- `test_pi_compaction.py` — 6 tests: compaction start/end, aborted, no tokens, sequence
- `test_proc_diag.py` — 24 tests: format_diag, is_cpu_active, collect_proc_diag (Linux /proc reads), ProcessDiag defaults
- `test_exec_runner.py` — 28 tests: event tracking (event_count, recent_events ring buffer, PID in StartedEvent meta), JsonlStreamState defaults
- `test_build_args.py` — 33 tests: CLI argument construction for all 6 engines, model/reasoning/permission flags
- `test_telegram_files.py` — 17 tests: file helpers, deduplication, deny globs, default upload paths
- `test_telegram_file_transfer_helpers.py` — 48 tests: `/file put` and `/file get` command handling, media groups, force overwrite
- `test_loop_coverage.py` — 29 tests: update loop edge cases, message routing, callback dispatch, shutdown integration

## Development

Two instances run on lba-1 — staging (PyPI/TestPyPI) and dev (local editable source). See `docs/reference/dev-instance.md` for full quickref including the staging workflow. See `docs/reference/integration-testing.md` for the structured integration test playbook run against `@untether_dev_bot` before every release. All integration test tiers are fully automated by Claude Code via Telegram MCP tools (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`, `reply_to_message`, `send_voice`, `send_file`) and Bash (`journalctl`, `kill -TERM`, FD/zombie checks).

| | Staging (`@hetz_lba1_bot`) | Dev (`@untether_dev_bot`) |
|---|---|---|
| **Service** | `untether.service` | `untether-dev.service` |
| **Binary** | `~/.local/bin/untether` (pipx) | `.venv/bin/untether` (editable) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |
| **Source** | PyPI release or TestPyPI rc | Local `/home/nathan/untether/src/` |

### 3-phase release workflow (MANDATORY)

1. **Dev** — fix code, run unit tests, test via `@untether_dev_bot` (6 engine chats), run integration tests
2. **Staging** — bump to `X.Y.ZrcN`, push master → CI publishes to TestPyPI, install on `@hetz_lba1_bot` via `scripts/staging.sh`, Nathan dogfoods for 1+ week
3. **Release** — bump to `X.Y.Z`, write changelog, tag `vX.Y.Z`, push — `release.yml` publishes to PyPI (requires Nathan's approval in GitHub Actions UI)

**NEVER skip staging for minor/major releases. NEVER go directly from dev to PyPI tagging.**

**Claude Code's role in each phase:**
- **Dev**: edit code, run tests, push feature branches, create PRs, run integration tests via Telegram MCP
- **Staging/Release**: prepare version bumps, changelog entries, and commit locally — Nathan pushes to master, creates tags, and approves PyPI deploys

Claude Code MUST NOT push to master, merge PRs, create version tags, or trigger releases. These are enforced by hooks and GitHub rulesets (see "Release guard" below).

### Dev/staging separation (CRITICAL)

- **NEVER restart `untether.service` (staging)** to test local code changes. Staging runs a PyPI/TestPyPI wheel — local edits have no effect on it. Restarting staging during development is always wrong.
- **ALWAYS use `untether-dev.service`** for testing. It runs the local editable source.
- **ALWAYS test via `@untether_dev_bot`** before merging/releasing. Staging (`@hetz_lba1_bot`) runs released wheels only.
- Staging is restarted after `scripts/staging.sh install` (TestPyPI rc) or `pipx upgrade untether` (PyPI release).

See `.claude/rules/dev-workflow.md` for full rules.

### Release guard (CRITICAL)

Multi-layer protection prevents accidental merges to master and PyPI publishes. Claude Code cannot circumvent these protections.

**GitHub server-side (unbyppassable):**
- **Branch ruleset** "Protect master — no direct push" — all changes to master require a PR, no admin bypass
- **`pypi` environment** — required reviewer (Nathan), admin bypass OFF; PyPI publish pauses until Nathan approves in GitHub Actions UI
- **`testpypi` environment** — no reviewer required (staging deploys automatically)
- **CODEOWNERS** — `* @littlebearapps/core`

**Local hooks (defense-in-depth):**
- `release-guard.sh` — blocks `git push` to master/main, `git tag v*`, `gh release create`, `gh pr merge`; feature branch pushes allowed
- `release-guard-protect.sh` — blocks Edit/Write to guard scripts and `.claude/hooks.json`
- `release-guard-mcp.sh` — blocks GitHub MCP `merge_pull_request` and writes to master/main; feature branches allowed

**Claude Code MUST:**
- Push to feature branches: `git push -u origin feature/<name>`
- Create PRs for Nathan to review: `gh pr create --title "..." --body "..."`
- Let Nathan merge PRs, create tags, and approve PyPI deploys manually

**Self-guarding:** the hook scripts, `.claude/hooks.json`, and GitHub rulesets cannot be modified by Claude Code. Only Nathan can change these by editing files manually outside Claude Code.

```bash
# Dev cycle: edit source → restart dev → test via @untether_dev_bot
systemctl --user restart untether-dev
journalctl --user -u untether-dev -f

# Staging: install rc from TestPyPI for dogfooding
scripts/staging.sh install X.Y.ZrcN
systemctl --user restart untether

# Promote to stable (only after PyPI release)
scripts/staging.sh reset && systemctl --user restart untether

# Tests / lint
uv run pytest
uv run ruff check src/
```

## CI Pipeline

GitHub Actions CI runs on push to master and on PRs:

| Job | What it checks |
|-----|---------------|
| format | `ruff format --check --diff` |
| ruff | `ruff check` with GitHub annotations |
| ty | Type checking (Astral's ty) |
| pytest | Tests on Python 3.12, 3.13, 3.14 with 80% coverage threshold |
| build | `uv build` + `twine check` + `check-wheel-contents` validation |
| lockfile | `uv lock --check` ensures lockfile is in sync |
| install-test | Clean wheel install + smoke-test imports (catches undeclared deps) |
| testpypi-publish | Publishes to TestPyPI on master push (OIDC, `skip-existing: true`) |
| release-validation | PR-only: validates changelog format, issue links, date when version changes |
| pip-audit | Dependency vulnerability scanning (PyPA advisory DB) |
| bandit | Python SAST (security static analysis) |
| codeql | CodeQL code scanning (Python + Actions), blocks PRs on new alerts |
| docs | Zensical docs build |
| prerelease-deps | Weekly (Monday): tests with `--upgrade --prerelease=allow` (informational) |

All third-party actions are pinned to commit SHAs (supply chain protection). Top-level `permissions: {}` restricts to least-privilege.

Dependabot auto-merge (`dependabot-auto-merge.yml`) auto-squash-merges dependency updates after CI passes. GitHub Actions deps (CI-only, never shipped) are auto-merged for all version bumps including major. Python deps (shipped in wheel) are auto-merged for patch/minor only; major bumps get flagged for manual review.

Release pipeline (`release.yml`) uses PyPI trusted publishing with OIDC. The `pypi` GitHub Environment requires Nathan's approval (admin bypass OFF) before publishing. The `testpypi` environment deploys automatically (no reviewer). `scripts/validate_release.py` enforces changelog/version consistency. `CODEOWNERS` (`* @littlebearapps/core`) requires team review on all PRs.

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

## Documentation screenshots

44 screenshots in `docs/assets/screenshots/` with a tracking checklist in `CAPTURES.md`. README uses a composite hero collage (`hero-collage.jpg`) built with ImageMagick for mobile responsiveness. Doc files use HTML `<img>` tags with `width="360"` and `loading="lazy"` (works in both GitHub and MkDocs). 11 screenshots are still missing and commented out with `<!-- TODO: capture screenshot -->` markers.

## Conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points in `pyproject.toml`
