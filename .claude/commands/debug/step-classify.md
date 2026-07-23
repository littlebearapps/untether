# Step 1 — Classify

This is the canonical Untether issue-class table. Pick exactly one **primary**
class per issue (the area where the root cause lives). If behaviour crosses
boundaries, note a **secondary** class.

For each row: the column "diagnostic hints" lists grep targets and the canonical
rule file that owns the area. Match aggressively — over-classifying is far less
costly than under-classifying.

## The 15 classes

| Class | Indicators in logs / chat | Diagnostic hints | Canonical rule |
|---|---|---|---|
| **runner-subprocess** | `subprocess.create.failed`, `subprocess.died_without_completion`, `runner_failed`, signal exits (rc=143/137), JSONL parse errors, EventFactory misuse | grep `engine=<X>` `event=session.summary`; check 3-event contract; verify `proc_returncode` and `last_event_type` | [`runner-development.md`](../../rules/runner-development.md) |
| **telegram-transport** | Outbox stuck, rate-limit 429, callback_data overflow, message-too-long, voice transcription failures, file upload denied, media-group dedup | grep `outbox`, `file_transfer`, `voice_transcription`, `rate_limit`, `429`; check `TelegramOutbox` invariants; verify callback_data ≤ 64 bytes | [`telegram-transport.md`](../../rules/telegram-transport.md) |
| **control-channel** | Approval buttons missing / stuck, PTY FD leak, ask-question crash, plan-mode cooldown abuse, ExitPlanMode loop, AskUserQuestion stuck-sequential | grep `control_request`, `control_response`, `_DISCUSS_COOLDOWN`, `_PENDING_ASK_REQUESTS`; check PTY `finally` cleanup; check session-registry cleanup | [`control-channel.md`](../../rules/control-channel.md) |
| **trigger-cron-webhook** | Cron fired wrong time / wrong tz, webhook 401/403/429/503, SSRF block, fetch-cron parse failure, hot-reload didn't apply, `run_once` re-fired | grep `trigger.fired`, `cron`, `webhook`, `ssrf`, `triggers paused`, `config.reload`; check timezone string in cron config; verify HMAC signature | (none — see `src/untether/triggers/`) |
| **session-resume-lock** | Wrong session resumed, stale resume token, session lock leak, registry not cleaned, `/continue` failures | grep `SessionLockMixin`, `lock_for`, `session_id`, `resume`; check `WeakValueDictionary` cleanup; verify lock acquired before subprocess spawn | [`runner-development.md`](../../rules/runner-development.md) |
| **stall-liveness-watchdog** | `subprocess.liveness_stall`, `subprocess.liveness_kill`, `stall_warning`, `peak_idle` excessive, repeat stall_warnings on cron sessions | grep `liveness`, `stall`, `proc_diag`, `cpu_active`; check `tool_timeout` vs `mcp_tool_timeout`; **see [`systemic-patterns.md`](./systemic-patterns.md)** for by-design cases (cron + plan-mode) | (none — see `src/untether/utils/proc_diag.py`) |
| **auto-continue** | Auto-continue loops (death spiral), missing auto-continue (Claude exit with `last_event_type=user`), retry exhaustion, signal-death suppression bypass | grep `auto_continue`, `last_event_type`, `proc_returncode`; check signal-death suppression (rc=143/137); verify `[auto_continue].max_retries` | [`runner-development.md`](../../rules/runner-development.md) |
| **config-hot-reload** | Restart-required key edited mid-run, hot-reload race, `update_from()` field copy missed, `TelegramBridgeConfig` corruption | grep `config.reload`, `restart_required`, `RESTART_REQUIRED_FIELDS`; check `update_from()` field coverage; verify slot preservation; **see `feedback_agent_self_restart_pattern` in MEMORY.md** | [`telegram-transport.md`](../../rules/telegram-transport.md) §TelegramBridgeConfig hot-reload |
| **fleet-rollout** | `fleet-rollout.sh` failure, attestation marker missing, version mismatch across hosts, partial-rollback state | check `~/.untether-dev/integration-test-pass-${VERSION}.json` exists; check `pipx list` on each host; verify integration-test gate not bypassed | [`release-discipline.md`](../../rules/release-discipline.md) §Fleet rollout |
| **outbox-delivery** | Files not delivered, deny-glob false-positive, size-limit hit, file-count cap, auto-cleanup didn't fire | grep `outbox`, `deny_glob`, `file_transfer`; check `.untether-outbox/` dir in project; verify `[transports.telegram.files]` config | [`telegram-transport.md`](../../rules/telegram-transport.md) §Outbox file delivery |
| **cost-budget** | Per-run budget exceeded silently, daily budget reset wrong, alert level skipped, auto-cancel didn't trigger | grep `cost_budget`, `budget.alert`, `auto_cancel`; check `cost_tracker.py`; verify daily reset timezone | (none — see `src/untether/cost_tracker.py`) |
| **at-scheduler-cancel-restart** | `/at` delay lost on restart, `/cancel` didn't drop pending delay, `/new` orphan tasks, `/restart` drain timeout | grep `at_scheduler`, `cancel.requested`, `shutdown.drain`, `_cancel_chat_tasks`; verify drain integration via `at_scheduler.active_count()` | (none — see `src/untether/telegram/at_scheduler.py`, `commands/topics.py`) |
| **help-faq-release-guard** | FAQ deleted/moved (hook block), guard-script edit blocked, force-push to master blocked, PR merge to master blocked, MCP write to master blocked | check `.claude/hooks.json` enforcement; verify `help-faq-protect.sh`, `release-guard.sh`, `release-guard-protect.sh`, `release-guard-mcp.sh`; check `docs/faq/faq.md` exists and has ≥7 question-shaped H2s | [`help-faq.md`](../../rules/help-faq.md), [`release-discipline.md`](../../rules/release-discipline.md) §Release guard |
| **auto-error-watcher** | Watcher daemon stopped, false-positive filings, duplicate issues, wrong host tag, signature dedup miss | check `systemctl --user status untether-issue-watcher` on each host; inspect `~/.local/state/untether-issue-watcher/seen.json`; verify `HOST` env var | (none — see MEMORY.md `untether-issue-watcher` section) |
| **ci-pipeline-release-guard** | CI job failure (format/ruff/ty/pytest/build/lockfile/install-test/pip-audit/bandit/codeql/docs), `validate_release.py` reject, `auto-tag-on-master` skip on stable bump, OIDC publish failure | check `.github/workflows/`; verify `pyproject.toml` version matches CHANGELOG.md heading; verify all changelog entries have `[#N]` links; check `uv lock --check` clean | [`release-discipline.md`](../../rules/release-discipline.md), [`context-quality.md`](../../rules/context-quality.md) |

## How to choose between adjacent classes

- **runner-subprocess vs control-channel** — if the error happens in JSONL
  event translation or subprocess lifecycle, it's runner-subprocess. If it
  happens in approval/permission/ask-question handling, it's control-channel.
- **control-channel vs telegram-transport** — if the issue is the *content* of
  the callback or the registry that tracks it, it's control-channel. If it's
  the *delivery* of the message (outbox, rate limit, edit), it's
  telegram-transport.
- **stall-liveness-watchdog vs auto-continue** — both surface when a session
  goes silent. Stall fires *during* the session (silence threshold breached).
  Auto-continue fires *after* the session exits with `last_event_type=user`.
- **config-hot-reload vs runner-subprocess** — if the symptom is a runner
  picking up stale config (e.g. wrong `env_extra_allow` list), it's
  config-hot-reload. If the runner can't start at all because env is missing,
  it's runner-subprocess.
- **trigger-cron-webhook vs at-scheduler-cancel-restart** — crons are scheduled
  via `[triggers]` TOML; `/at` delays are user-issued one-shots. Different
  code paths.

## Memory-aware exceptions

Before classifying any "error" as a bug, check whether the
[`systemic-patterns.md`](./systemic-patterns.md) by-design list covers it.
For example: long `peak_idle` on a cron-fired session in default plan mode is
by-design (waiting for user approval) — do not escalate.

## Output

The classification appears under `### Classification` in the Debug Report
template ([`output-template.md`](./output-template.md)). Quote the class name
verbatim from the table above.
