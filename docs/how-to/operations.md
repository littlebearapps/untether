# Operations and monitoring

Untether runs as a long-lived process, typically in a terminal or managed by a process supervisor (systemd on Linux, etc.). This guide covers health checks, graceful restarts, diagnostics, and day-to-day operations — all controllable from [Telegram](https://telegram.org) without SSH.

## Health check

Send `/ping` in Telegram to verify the bot is running:

!!! untether "Untether"
    🏓 pong — up 3d 14h 22m 7s

The response includes the bot's uptime since last restart. Use this as a quick liveness check.

If triggers (crons or webhooks) target the current chat, `/ping` also shows a trigger summary, and while a Claude session in this chat still has background tasks running it says how many ([#777](https://github.com/littlebearapps/untether/issues/777)):

!!! untether "Untether"
    🏓 pong — up 3d 14h 22m 7s<br>
    ⏰ triggers: 1 cron (daily-review, 9:00 AM daily (Melbourne)), 1 webhook<br>
    ⏳ background: 2 tasks running

If [webhooks and cron](webhooks-and-cron.md) are enabled, the webhook server also exposes a health endpoint:

```
GET http://127.0.0.1:9876/health
```

Returns `{"status": "ok", "webhooks": N, "paused": false}` where N is the number of configured webhooks. While the master pause toggle ([#294](https://github.com/littlebearapps/untether/issues/294)) is engaged it returns `{"status": "paused", …, "paused": true}`, so external monitors can tell "paused but up" from healthy.

### Health snapshot

`/health` consolidates RAM, the Untether process, triggers, and today's API cost into a single message — handy as a one-shot diagnostic when a chat suddenly stops responding.

!!! untether "Untether"
    🏥 **Untether health**<br>
    • RAM: 13.8 GB used · 18.2 GB available (43%)<br>
    • Swap: 0 KB / 4.0 GB<br>
    • untether pid=1543657 · RSS 70 MB · 13 FDs · 1 children<br>
    • triggers: 2 crons, 1 webhook<br>
    • today's API cost: $1.42<br>
    • uptime: 3d 14h 22m

Each section degrades gracefully when its source is unavailable (non-Linux, no `trigger_manager`, no cost tracker). `/health` is project-aware — `children` reflects the current Untether process tree (Claude Code subprocesses, MCP servers, workerd grandchildren under #275-style cleanup). When triggers are disabled in config, the line reads `triggers: disabled`. The paused state of the trigger system is reported by the webhook server's `GET /health` endpoint above, not by this command.

For Claude subscription diagnostics, use `/usage debug` ([#410](https://github.com/littlebearapps/untether/issues/410)) — it appends a `🔧 debug` block to the standard `/usage` output showing last-fetch wall time and freshness, last-error class+message, OAuth token expiry, and the cumulative `claude_usage.schema_mismatch` counter. See [Subscription usage](subscription-usage.md#debug-page-usage-debug).

## RAM guard (#350)

Untether refuses to spawn a new engine subprocess when free RAM is below `[watchdog] prespawn_ram_block_mb` (default 500 MB), and warns at `prespawn_ram_warn_mb` (default 2000 MB). On block the run completes early with `🛑 Insufficient RAM` instead of spawning a doomed subprocess that would leak memory under OOM. Set either threshold to `0` to disable that tier; `0 / 0` disables the guard entirely. See [config: `[watchdog]`](../reference/config.md#watchdog).

The same check holds two concurrency limits ([#589](https://github.com/littlebearapps/untether/issues/589)): `prespawn_ram_per_run_reserve_mb` raises the block bar by that much for each engine run already in flight, and `max_concurrent_engine_runs` (default `0`, unlimited) refuses a spawn outright with `🛑 Too many engine runs in flight (N/M)`. `0 / 0 / 0` turns the whole guard off.

It applies to every engine, Claude Code included (before v0.35.5 Claude runs skipped it, [#838](https://github.com/littlebearapps/untether/issues/838)). Only new processes are checked: a follow-up written into a live Claude session isn't. A Claude session kept open after its reply for background work (or the idle grace) still holds its process and MCP children, so it counts as a run in flight; the block message names those idle sessions, and they close on their own. A block never clears the chat's saved session — the next message after the load drops resumes it (log `session.auto_clear_skipped reason=prespawn_blocked`).

## Graceful restart

Send `/restart` in Telegram to initiate a graceful shutdown. Untether replies `Draining active runs… will restart shortly.`, then:

1. Untether stops accepting new runs
2. Claude sessions that are only waiting between turns (for example holding for background tasks) are closed straight away, with the usual closing notice naming any background tasks they stop
3. Each chat (or forum topic) with a run still working gets `🔄 Restarting — waiting for your run to finish…` ([#665](https://github.com/littlebearapps/untether/issues/665))
4. Active runs are drained (allowed to finish)
5. The process exits cleanly
6. Run `untether` again in your terminal (or your process supervisor restarts it automatically)

If the only active run is the one that caused the restart — it's in the chat you sent `/restart` from, or its agent is the one waiting on `systemctl restart` / `stop` (or `launchctl`) — the drain waits 10 seconds instead of the full 120, since that run can't finish until the restart happens. Any other restart, such as an upgrade or a reboot, always gets the full drain ([#690](https://github.com/littlebearapps/untether/issues/690)).

!!! tip "Prefer /restart over killing the process"
    `/restart` lets in-progress runs complete before shutting down. Killing the process with `kill` or `systemctl restart` may interrupt active runs and lose work.

## SIGTERM behaviour

Sending SIGTERM to the Untether process triggers the same graceful drain as `/restart`:

1. New runs are rejected
2. Active runs are allowed to complete
3. After a 120-second drain timeout, remaining runs are cancelled and the process exits

This means `systemctl --user stop untether` (Linux) also drains gracefully, as systemd sends SIGTERM first. Pressing Ctrl+C in a terminal sends SIGINT, which triggers the same graceful drain.

### Message continuity across restarts

Untether persists the last Telegram `update_id` to `last_update_id.json` in the config directory. On startup, polling resumes from the saved offset — no messages are dropped or re-processed within Telegram's 24-hour retention window. Pending `/at` delays are cancelled during drain and not persisted (they are lost on restart).

!!! note "Drain timeout"
    The default drain timeout is 120 seconds. If active runs don't complete within this window, they are cancelled and each affected chat or topic gets `⚠️ Restart timed out — N run(s) interrupted.` with a hint that the session is saved and can be resumed.

## Orphan progress cleanup

When Untether restarts (after a crash, upgrade, or manual restart), any progress messages from the previous instance are still visible in Telegram — stuck showing "working" with stale elapsed time.

Untether automatically handles this: active progress messages are tracked in `active_progress.json` in the config directory. On startup, any orphan messages from a prior instance are edited to show:

!!! untether "Untether"
    ⚠️ interrupted by restart

This replaces the stale progress text and removes any inline keyboards (approval buttons), so there's no confusion about which messages are from the current session. Since v0.35.5 only runs that were genuinely in flight get this label; a run that had already been cancelled or had failed keeps its final message ([#810](https://github.com/littlebearapps/untether/issues/810)).

The cleanup happens before the startup message is sent, so by the time you see "Untether started", all orphan messages are already resolved.

<!-- TODO: capture screenshot: orphan-cleanup — progress message showing "interrupted by restart" -->

## Systemd service (Linux)

The recommended systemd unit file is provided at `contrib/untether.service`. Key settings:

| Setting | Value | Purpose |
|---------|-------|---------|
| `Type=notify` | — | Untether sends `READY=1` after startup completes; systemd knows the service is ready |
| `NotifyAccess=main` | — | Only the main process can send sd_notify signals |
| `RestartSec=2` | — | Wait 2 seconds before auto-restarting on failure |
| `OOMScoreAdjust=-100` | — | Makes Untether less likely to be OOM-killed than default processes |
| `OOMPolicy=continue` | — | Don't stop the service if a child process is OOM-killed |
| `KillMode=mixed` | — | Sends SIGTERM to main process, SIGKILL to remaining children after timeout |

Copy the unit file and reload:

```bash
cp contrib/untether.service ~/.config/systemd/user/untether.service
systemctl --user daemon-reload
systemctl --user enable --now untether
```

See the [dev instance reference](../reference/dev-instance.md) for full service file documentation.

## Auto-continue (Claude Code)

When Claude Code exits after receiving tool results without processing them (an upstream bug), Untether detects the premature exit and automatically resumes the session. You'll see a `🔁 Auto-resuming session after upstream Claude Code event` notice in the chat (with `(attempt N)` on later retries).

Auto-continue is enabled by default. It is suppressed for signal deaths (SIGTERM, SIGKILL) to prevent death spirals under memory pressure.

Configure via `[auto_continue]` in `untether.toml`:

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `true` | Enable automatic session resumption. |
| `max_retries` | `1` | Maximum consecutive retries per run (0–3). |

The other `[auto_continue]` keys (empty-resume recovery and its limits) are listed in the [config reference](../reference/config.md#auto_continue).

See [troubleshooting](troubleshooting.md#claude-code-exits-without-finishing-auto-continue) for details on when this triggers and how to tune it.

## Run diagnostics

Run the built-in preflight check to validate your configuration:

```sh
untether doctor
```

This validates:

- Telegram bot token is valid and the bot is reachable
- Chat ID is reachable by the bot
- Topics configuration (if enabled)
- File transfer settings (enabled, `allowed_user_ids`)
- Voice transcription has an API key

It doesn't check engine CLIs; the startup message lists engines that are `not installed`, `misconfigured` or `failed to load`. `untether doctor` exits with status 1 if any check reports an error.

It doesn't check engine CLIs or deny globs: the startup message lists engines that are missing, misconfigured or failed to load.

Run this after any config change, after upgrading, or when something isn't working.

## Debug mode

Start Untether with debug logging to troubleshoot issues:

```sh
untether --debug
```

This logs detailed information to `debug.log`, including:

- Engine JSONL events (every line from the subprocess)
- Telegram API requests and responses
- Rendered messages and inline keyboards
- Config loading and validation

!!! tip "Check debug.log first"
    When reporting issues, include the relevant section of `debug.log`. It contains everything needed to diagnose most problems.

## Config hot-reload

Enable config watching so Untether picks up changes without a restart:

=== "untether config"

    ```sh
    untether config set watch_config true
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    watch_config = true
    ```

When enabled, Untether watches the config file for changes and reloads most settings automatically. Some sections below (`[progress]`, `[watchdog]`, `[footer]`, `[cost_budget]`, `[security]`) are re-read on every run anyway, so edits to them reach the next run even with `watch_config = false`; the trigger and Telegram bridge items need the watcher.

**Hot-reloadable** (applied immediately):

- Trigger system: crons, webhooks, auth, timezones (switching `triggers.enabled` off clears them; switching it on, and the server `host`/`port`/`rate_limit`, need a restart)
- Telegram bridge: `voice_transcription`, `[files]` (the `outbox_*` settings apply from the next delivery, [#924](https://github.com/littlebearapps/untether/issues/924); turning outbox delivery on when it was off at startup still needs a restart), `allowed_user_ids`, `allow_any_user`, `show_resume_line`, timing
- `[security]` keys: `env_extra_allow`, `env_extra_prefix_allow` (re-read on next runner spawn)
- `[progress]` keys: `max_actions`, `verbosity`, `min_render_interval`, `heartbeat_interval`, `show_background_tasks`, `background_tasks_max_rows`, `consolidate_wake_turns`, `show_context_usage` ([#269](https://github.com/littlebearapps/untether/issues/269), [#481](https://github.com/littlebearapps/untether/issues/481), [#777](https://github.com/littlebearapps/untether/issues/777), [#819](https://github.com/littlebearapps/untether/issues/819)); `verbosity`, `max_actions` and `show_context_usage` also reach the next turn of an open Claude session ([#863](https://github.com/littlebearapps/untether/issues/863))
- `[watchdog]` keys: `tool_timeout`, `mcp_tool_timeout`, `claude_stream_idle_timeout_ms`, `post_result_idle_timeout`, `post_result_idle_enabled`, `bash_grace_seconds` (re-read per run); the live-session keys `post_result_bg_max_hold`, `bg_hold_rearm_on_progress`, `bg_hold_declared_waits` and `rearm_plan_mode` are read when a Claude session starts, so an open session keeps the old value until it closes
- `followup_mode` (the default for [steer follow-ups](steer-follow-ups.md))
- `[loop]` keys: `enabled` is read each time Claude tries to schedule, the caps when a loop is created, and `own_schedule` when a Claude session starts ([#925](https://github.com/littlebearapps/untether/issues/925))
- Trigger pause/resume: in-memory only, toggled via `/config → ⏰ Triggers` ([#294](https://github.com/littlebearapps/untether/issues/294)) — restart auto-resumes
- `[footer]` and `[cost_budget]` settings (re-read per call)
- Engine defaults, budget, cost/usage display flags

Untether re-reads `untether.toml` only when its contents (or the `UNTETHER__*` environment variables) change, and logs `config.loaded` with the reason each time it does ([#506](https://github.com/littlebearapps/untether/issues/506)). Set `UNTETHER_SETTINGS_CACHE=0` in the service environment to re-read it every time instead.

**Restart-only** (require `/restart` or `systemctl restart`):

- `bot_token`, `chat_id` (Telegram connectivity)
- `session_mode` and the whole `[transports.telegram.topics]` table (structural)
- `triggers.enabled` (off → on), `triggers.server.host` / `port` / `rate_limit` / `max_body_bytes`
- `message_overflow` (message splitting strategy)
- `[progress] group_chat_rps` (read when the Telegram client starts)
- the top-level `transport`
- `watch_config` itself (the watcher starts with the process)

## Process management

=== "Telegram (all platforms)"

    Send `/restart` in Telegram for a graceful restart with drain visibility.
    Use `/ping` to check the bot is running.

=== "Terminal (all platforms)"

    Stop with Ctrl+C (if running), then:

    ```sh
    untether
    ```

    View output directly in the terminal. Use `--debug` for verbose logging to `debug.log`.

=== "Linux (systemd)"

    ```bash
    systemctl --user restart untether
    journalctl --user -u untether -f       # live logs
    systemctl --user status untether       # check status
    journalctl --user -u untether -n 100   # recent logs
    ```

!!! warning "Restart vs /restart"
    `systemctl --user restart untether` sends SIGTERM, which triggers a graceful drain. However, `/restart` in Telegram gives you a confirmation message and visibility into the drain process. Prefer `/restart` when you have Telegram access — it works on all platforms.

## Related

- [Troubleshooting](troubleshooting.md) — common issues and debugging strategies
- [Configuration](../reference/config.md) — full config reference
- [Dev setup](dev-setup.md) — running from source for development
- [Security hardening](security.md) — securing your instance
