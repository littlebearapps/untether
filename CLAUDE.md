# Untether

Telegram bridge for Claude Code, Codex, OpenCode, Pi and other agent CLIs — control coding agents from your phone.
**Repo**: [littlebearapps/untether](https://github.com/littlebearapps/untether) · **Upstream**: [banteg/takopi](https://github.com/banteg/takopi).
Untether adds interactive permission control (Telegram approval buttons, plan mode, AskUserQuestion), live sessions,
cost/usage tracking and many UX fixes. Interactive features are **Claude Code-only**; other engines run non-interactively.

<!-- Context budget: keep this file under ~200 lines / ~15k chars. Feature detail goes in
     docs/reference/feature-catalog.md, test-file detail in docs/reference/test-catalog.md.
     Area-specific rules go in .claude/rules/*.md WITH `paths:` frontmatter (not `applies_to:`). -->

## Where the detail lives (read on demand)

| Need | Read |
|---|---|
| What a feature does / which file owns it | `docs/reference/feature-catalog.md` |
| What each test file covers (+ counts) | `docs/reference/test-catalog.md` |
| Engine protocol, event mapping | `docs/reference/runners/<engine>/{runner,stream-json-cheatsheet,untether-events}.md` (Codex: `exec-json-cheatsheet.md` + `untether-events.md`, no `runner.md`) |
| Claude permission modes, live sessions, control channel | `docs/reference/runners/claude/runner.md` |
| Telegram transport | `docs/reference/transports/telegram.md` |
| Dev/staging instances | `docs/reference/dev-instance.md` |
| Integration-test playbook (tiers, chat IDs) | `docs/reference/integration-testing.md` |
| Workflow commands / loops | `docs/LOOPS.md` |

Area rules in `.claude/rules/` load automatically when you read matching files; skills in `.claude/skills/` load on demand.

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi/gemini/amp)
                                       |
                                  ProgressTracker
```

- **Runners** (`src/untether/runners/`) — engine subprocess managers; `claude.py` holds all interactive features
- **RunnerBridge** (`src/untether/runner_bridge.py`) — runner ↔ presenter, preamble, auto-continue, empty-resume recovery
- **TelegramPresenter** (`src/untether/telegram/bridge.py`) — progress, inline keyboards, answers
- **Commands** (`src/untether/telegram/commands/`) — command/callback handlers
- **Schemas** (`src/untether/schemas/`) — msgspec structs for JSONL; **Triggers** (`src/untether/triggers/`) — cron/webhooks
- Config: `untether.toml` (with `watch_config = true` most sections hot-reload — off by default; `bot_token`, `chat_id`, `session_mode`, `topics`, `message_overflow` need a restart, as does turning `[triggers] enabled` on)

## Deprecated engines (Gemini CLI, AMP)

Both still ship and load but are strictly deprecated and no longer supported — no removal is scheduled, though a future
release may drop them (Gemini: upstream EOL for individual accounts, hangs under Untether until the watchdog cancels;
AMP: remote `426` refusal). **When a cross-engine sweep breaks either runner, `xfail`/`skip` the test — do NOT fix the
runner.** Security and doc-accuracy fixes still apply. Both are excluded from every integration-test tier. Antigravity
CLI (#558, ships in v0.36.1) is a new engine and must not reuse the `gemini` id.
Engine parity roadmap: v0.36.1 Antigravity (#558), v0.36.2 Codex app-server (#960–#968), v0.36.3 OpenCode ACP (#969–#974).
OpenCode support means the 1.x CLI (npm `opencode-ai`); 2.x (`@opencode/cli`) is refused before spawning (#970).

## Commands

```bash
uv run pytest                                  # all tests (80% coverage gate)
uv run pytest tests/test_claude_control.py -x  # one file
uv run ruff format src/ tests/ && uv run ruff check src/ tests/   # CI checks formatting too
uv lock --check                                # lockfile in sync
python3 scripts/validate_release.py            # changelog/version validation
systemctl --user restart untether-dev          # pick up local source changes (dev bot)
journalctl --user -u untether-dev -f
```

CI (`.github/workflows/`): format, ruff, ty (informational), pytest on 3.12–3.14, build, lockfile, install-test,
pip-audit, bandit, CodeQL, docs; TestPyPI publish on `dev` push; `auto-tag-on-master.yml` + `release.yml` publish stable
versions to PyPI. Third-party actions are pinned to SHAs.

## Dev vs staging (CRITICAL)

| | Staging (`@hetz_lba1_bot`) | Dev (`@untether_dev_bot`) |
|---|---|---|
| Service | `untether.service` | `untether-dev.service` |
| Binary | `~/.local/bin/untether` (pipx wheel) | `.venv/bin/untether` (editable) |
| Config | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |

- **NEVER restart `untether.service` (staging) to test local code** — it runs a PyPI/TestPyPI wheel. Restart it only after
  `scripts/staging.sh install X.Y.ZrcN` or `pipx upgrade untether`.
- **ALWAYS test via `untether-dev` / `@untether_dev_bot`.** Never test against staging.
- Never `systemctl restart` Untether from inside an active Untether session — config hot-reloads, and the 120 s drain drops
  the final message.

## Release guard (CRITICAL)

- Branches: `feature/*` / `fix/*` → PR → `dev` (→ TestPyPI) → PR → `master` (→ PyPI). Master always matches latest PyPI.
- Everyday work goes to `dev` → TestPyPI only. Allowed: push feature branches, `gh pr create --base dev`,
  `gh pr merge <n> --squash` when base = `dev`.
- Merging the `dev`→`master` PR is the release (auto-tag → `release.yml` → PyPI → fleet rollout). Claude may do it
  **only** after Nathan explicitly approves that version in the conversation, via `/pr-main X.Y.Z --merge`
  (`gh pr merge <n> --squash --admin`). The guard then requires head = `dev` + green CI and **asks Nathan to confirm**
  ([#917](https://github.com/littlebearapps/untether/issues/917)). Never infer approval; never retry a declined prompt.
- Claude Code **MUST NOT** push to `master`/`main`, create `v*` tags or run `gh release create` — the pipeline does.
- GitHub rulesets + CODEOWNERS (`* @littlebearapps/core`) block direct pushes to `master`, but Nathan's admin token
  bypasses review and CI with `--admin`, so the local guard ([#915](https://github.com/littlebearapps/untether/issues/915),
  registered in `.claude/settings.json`) is what gates a Claude release merge. It denies master/main pushes, tags,
  releases and non-`dev`-head or red-CI `master` merges, and asks before a release merge, any `gh workflow run` / `gh run rerun`, or a non-dev Untether restart.
  It's a tripwire, not a boundary — obey the rules regardless, and never work around a block. **Never edit
  `.claude/settings.json` or the guard scripts** (`.claude/hooks/release-guard*.sh`, `help-faq-protect.sh`); personal
  settings go in `.claude/settings.local.json`.
- `docs/faq/faq.md` backs the marketing-site FAQPage schema: **never delete or move it**; editing is encouraged.

## Release workflow (summary — full rules in `.claude/rules/release-discipline.md`)

1. **Dev** — fix, unit tests, test via `@untether_dev_bot`, integration tests per `docs/reference/integration-testing.md`.
2. **rc** — bump `X.Y.ZrcN`, merge to `dev` (TestPyPI), attest with `scripts/run-integration-tests.sh X.Y.ZrcN --manual`,
   then `scripts/fleet-rollout.sh X.Y.ZrcN` (5 hosts: lba-1 staging, nsd, channelo, sl, mac). The marker is the gate.
3. **Stable** — `/pr-main X.Y.Z` (bump, CHANGELOG, PR `dev`→`master`); on Nathan's explicit go, `/pr-main X.Y.Z --merge`
   (or Nathan merges), then `scripts/fleet-rollout.sh X.Y.Z` once PyPI has it.

Pre-1.0, a line with any `### breaking` entry ships as a **minor**: the 0.35.5rc1–rc20 line ships as **v0.36.0**
(rcs continue as `0.36.0rcN`, milestone `v0.36.0`; [#947](https://github.com/littlebearapps/untether/issues/947)).

**NEVER skip integration testing or the attestation gate.** Every bug fix / significant change needs a GitHub issue
(labels `bug`/`enhancement`/`documentation`, `severity:*`, `priority: *`), linked from CHANGELOG as
`[#N](https://github.com/littlebearapps/untether/issues/N)`. Auto-filed issues carry `auto:error-report` (issue-watcher
daemon) or `auto:monitor-audit` (`/monitor`).

**Milestone titles and descriptions are public:** littlebearapps.com shows open milestones as the Untether roadmap. Keep
each description to one or two user-facing sentences (Australian English). No file paths, renaming history, status logs,
issue chains or open security gaps — working notes go in the tracking issue or a gitignored `docs/plans/` file
([#1018](https://github.com/littlebearapps/untether/issues/1018)).

## Conventions

- Python 3.12+, anyio (async), msgspec (JSONL), structlog (logging), ruff, pytest + coverage
- Runner backends registered via entry points in `pyproject.toml`
- Australian English in user-facing text; conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`)
- Every new feature bullet → `docs/reference/feature-catalog.md`; every new/changed test file → `docs/reference/test-catalog.md`
