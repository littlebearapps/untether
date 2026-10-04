# Step 4 — Cross-reference systemic patterns

This is the canonical catalogue of known Untether systemic patterns. Walk this
list **before** scoring any signal or escalating any "error" to a bug.

For each pattern:
- **Sig** = grep signature in journalctl / chat
- **Class** = primary Step-1 class
- **Canonical issue** = the open or closed GH issue (if any) tracking the pattern
- **Posture** = `bug` (fix needed), `by-design` (do NOT escalate), `regression-watch` (was fixed, watch for relapse), `rate-limited` (real but expected at low frequency), `mitigation-exists` (Untether already mitigates this engine quirk)

## The pattern catalogue

### 1. Auto-continue death spiral on signal exits
- **Sig**: rapid `auto_continue` followed by `proc_returncode=143` or `137`, repeating
- **Class**: auto-continue
- **Canonical issue**: mitigation via `_is_signal_death()` in `runner_bridge.py`; configurable `[auto_continue].max_retries`
- **Posture**: regression-watch — signal-death suppression should kick in. If you see >1 retry on signal death, that's a bug.

### 2. PTY master_fd leak across runs
- **Sig**: subsequent Claude runs fail to spawn; `subprocess.create.failed` with FD-exhaustion-like errors; `lsof` shows growing FD count
- **Class**: control-channel
- **Canonical issue**: per `control-channel.md` §PTY lifecycle — `finally` must close `master_fd`
- **Posture**: regression-watch — every Claude runner exit path must close `master_fd`.

### 3. callback_data > 64 bytes silently dropped
- **Sig**: button click does nothing; no `callback_query` in journalctl; outbound message had a button with long `callback_data`
- **Class**: telegram-transport
- **Posture**: bug if surfaced — Telegram enforces 64 bytes silently. Always check `callback_data` length at construction.

### 4. Hot-reload race during active run
- **Sig**: `config.reload` event during an active session; subsequent events use stale config
- **Class**: config-hot-reload
- **Canonical**: `telegram-transport.md` §TelegramBridgeConfig hot-reload; `update_from()` must copy all fields atomically
- **Posture**: regression-watch — new fields added to `TelegramBridgeConfig` must be added to `update_from()`.

### 5. Restart-required config key edited silently
- **Sig**: `restart_required=true` log line emitted but service was not restarted; user-visible behaviour is stale
- **Class**: config-hot-reload
- **Canonical**: `RESTART_REQUIRED_FIELDS` on `TelegramTransportSettings` (`settings.py`); `_notify_restart_required` (`telegram/loop.py`) broadcasts to project chats + admin DMs (#318 follow-up)
- **Posture**: regression-watch — broadcast must reach the user.

### 6. Agent self-restart during active run (`feedback_agent_self_restart_pattern`)
- **Sig**: agent runs `systemctl --user restart untether` inside an active session; 120s graceful drain timeout; outbox message dropped silently
- **Class**: config-hot-reload (root cause: agent confusion about hot-reload)
- **Canonical**: #547 (closed v0.35.3) + MEMORY.md `feedback_agent_self_restart_pattern`
- **Posture**: by-design behavior on the *kernel/systemd* side — fix is to educate the agent (preamble + this debug-rule), not patch the daemon. If observed: flag, never blame the daemon.

### 7. Cron + plan-mode stalls (`feedback_cron_plan_mode_stalls`)
- **Sig**: long `peak_idle` (>10 min) + repeat `stall_warning` on a session that was triggered by a cron (look for `trigger=cron:<id>` in StartedEvent meta)
- **Class**: stall-liveness-watchdog
- **Canonical**: warning-UX fixed by #526 (approval-pending stalls, closed v0.35.3; the #527 unified-predicate rewrite was closed not-planned); MEMORY.md `feedback_cron_plan_mode_stalls`
- **Posture**: **by-design** — cron-fired sessions in default plan mode are correctly waiting for user approval. **Do not escalate as a bug.** UX rendering of these warnings is the only legitimate fix surface.

### 8. CLI-style Telegram summary brevity drift
- **Sig**: final Telegram message > 5000 chars; plan body re-pasted in the final summary
- **Class**: control-channel (Claude agent prompt)
- **Canonical**: feedback memory `feedback_telegram_summary_brevity`; rc11 overshot at 42k chars; rc13 #515 walked it back
- **Posture**: regression-watch — finals should be 500–1500 chars / 3–7 bullets.

### 9. Stale callback buttons (ephemeral cleanup miss)
- **Sig**: old buttons still respond after a run completed; `_EPHEMERAL_MSGS` registry (`runner_bridge.py`) not drained in `finally`
- **Class**: telegram-transport
- **Canonical**: `register_ephemeral_message` + `ProgressEdits.delete_ephemeral()`
- **Posture**: regression-watch — every run handler must drain ephemerals in `finally`.

### 10. Session registry cleanup miss (PTY/outline/ask)
- **Sig**: `_SESSION_STDIN`, `_REQUEST_TO_SESSION`, `_OUTLINE_PENDING`, `_DISCUSS_APPROVED`, `_PLAN_EXIT_APPROVED`, `_PENDING_ASK_REQUESTS`, `_ASK_QUESTION_FLOWS` retain entries for terminated sessions (`_DISCUSS_COOLDOWN` was retired in #570)
- **Class**: session-resume-lock
- **Canonical**: `control-channel.md` §Session registries — clean up in `finally` of `run_impl`
- **Posture**: regression-watch.

### 11. MCP catalog staleness on Claude runs (#365)
- **Sig**: `event=catalog_staleness.detected` followed by Claude using a stale MCP catalog (missing servers, wrong status)
- **Class**: runner-subprocess (claude only)
- **Canonical**: #365 — opt-in `notify_catalog_refresh` (default off); detection-only by default
- **Posture**: mitigation-exists — if user reports MCP issues, suggest enabling the proactive refresh.

### 12. Stall watchdog threshold mismatch (MCP vs local tool)
- **Sig**: stall fired at 10-min mark while an MCP tool was active; expected 15-min threshold
- **Class**: stall-liveness-watchdog
- **Canonical**: `[watchdog]` config — `tool_timeout` (10m default) vs `mcp_tool_timeout` (15m default); detection via tool_name + MCP server name in stall context
- **Posture**: regression-watch — MCP-aware threshold detection must use the right context field.

### 13. Outbox deny-glob false positive
- **Sig**: `file_transfer.denied` for a legitimate file the user expected to receive
- **Class**: outbox-delivery
- **Canonical**: `[transports.telegram.files]` config — `deny_globs` list (shared by `/file` transfers and outbox delivery)
- **Posture**: bug surface for user-config tuning; check the deny pattern matched, then consider config narrowing.

### 14. Auto-error-watcher noisy signature
- **Sig**: same `auto:error-report` issue refiled multiple times, or a new signature creates duplicate issues across hosts
- **Class**: auto-error-watcher
- **Canonical**: `~/.local/state/untether-issue-watcher/seen.json` per-host dedup; cross-host dedup is by signature
- **Posture**: bug — signature should match across hosts. If two hosts file separate issues for the same signature, the dedup logic missed.

### 15. Trigger pause/resume gating not honoured (#294)
- **Sig**: cron fired while `TriggerManager.is_paused()` should have been true; or webhook returned 200 instead of 503 while paused
- **Class**: trigger-cron-webhook
- **Canonical**: #294 — master pause toggle; in-memory only, restart auto-resumes
- **Posture**: regression-watch.

### 16. ExitPlanMode re-issued immediately after a denial
- **Sig**: `ExitPlanMode` rapid-fire retries after a Telegram deny, with no intervening assistant text turn
- **Class**: control-channel
- **Canonical**: `control-channel.md` §Outline gate. The gate is now purely **text-based** — a re-issue is auto-denied until ≥200 chars of visible outline exist (`_OUTLINE_PENDING`). The 30/60/90/120s progressive cooldown (`_DISCUSS_COOLDOWN`) that used to back this was **retired in #570**, after the upstream loop was verified fixed on CLI 2.1.215 (2026-07-20).
- **Posture**: regression-watch — on the *upstream* loop, not on the retired cooldown. Repro: deny an ExitPlanMode control_request via the Telegram buttons and watch for an immediate re-issue. If it returns, reopen #570's lineage (#126) rather than reinstating a timer.

### 17. `_clear_background_handle` racing watchdog read (#374, #333, #507 redux)
- **Sig**: background-handle scalar wiped before watchdog reads it; "dead wakeup" symptom
- **Class**: stall-liveness-watchdog
- **Canonical**: MEMORY.md `project_channelo_rc15_dead_wakeup_507_redux`; fixed by #374 (handle cleared on terminal signal) and #573 (lifecycle v2), both closed in v0.35.5
- **Posture**: regression-watch — was a known defect in the v0.35.3 line.

### 18. Integration-test attestation gate bypass
- **Sig**: `fleet-rollout.sh` proceeded without `~/.untether-dev/integration-test-pass-${VERSION}.json` existing; `--skip-test-gate` used silently
- **Class**: fleet-rollout
- **Canonical**: `release-discipline.md` §Pre-rollout integration test attestation
- **Posture**: bug — gate exists explicitly to prevent this.

### 19. Help-FAQ silently regressed
- **Sig**: `docs/faq/faq.md` has fewer than 7 question-shaped H2s, or contains TODO/placeholder, or breaks the marketing-site `docs-sync.config.ts` mapping
- **Class**: help-faq-release-guard
- **Canonical**: #477, #483 in `.claude/rules/help-faq.md`
- **Posture**: bug — FAQ MUST stay current; `help-faq-protect.sh` blocks deletes but does not enforce content shape.

### 20. CI ty diagnostics pile-up
- **Sig**: ty job in CI has hundreds of pre-existing diagnostics; `continue-on-error: true`
- **Class**: ci-pipeline-release-guard
- **Canonical**: MEMORY.md pattern note — non-blocking since rc9
- **Posture**: by-design — informational. Don't escalate. Tackling ty is a planned enhancement, not a bug.

## How to use this list

1. In sweep mode (Step S-3 in `../debug.md`): for each error signature in the
   aggregated log buffer, walk the table top to bottom. Mark each finding with
   the matched pattern (if any) and its posture. Findings with posture
   `by-design` are dropped from the triage report; `rate-limited` are noted but
   not ranked highly; `regression-watch`, `mitigation-exists`, `bug` are all
   ranked.

2. In targeted mode (Step 4 in `../debug.md`): pull the issue body, walk the
   table, and pick the matching pattern. If a match exists with a canonical
   issue, comment on the canonical issue rather than creating a new one. If
   the pattern is `by-design`, say so clearly and stop — no fix.

## Maintenance

- New systemic pattern observed during debugging → add a row here.
- Pattern resolved by a release → keep the row, change posture to
  `regression-watch`, add the fix commit SHA.
- Pattern proves to be a one-off (not systemic) → remove it.

Cross-reference this file against:
- `MEMORY.md` (project memories with `project_*`, `feedback_*` prefixes)
- `CHANGELOG.md` (recent fixes — candidates for `regression-watch` posture)
- `.claude/rules/` (canonical rule files often encode the prevention)
