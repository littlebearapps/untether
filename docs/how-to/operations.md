# Operations and monitoring

Untether runs as a long-lived process, typically managed by systemd. This guide covers health checks, graceful restarts, diagnostics, and day-to-day operations — all controllable from Telegram on iPhone, iPad, Android, Mac, Windows, Linux, or Telegram Web.

## Health check

Send `/ping` in Telegram to verify the bot is running:

!!! untether "Untether"
    pong — up 3d 14h 22m

The response includes the bot's uptime since last restart. Use this as a quick liveness check.

If [webhooks and cron](webhooks-and-cron.md) are enabled, the webhook server also exposes a health endpoint:

```
GET http://127.0.0.1:9876/health
```

Returns `{"status": "ok", "webhooks": N}` where N is the number of configured webhooks. Useful for external monitoring tools.

## Graceful restart

Send `/restart` in Telegram to initiate a graceful shutdown:

1. Untether stops accepting new runs
2. Active runs are drained (allowed to finish)
3. The process exits cleanly
4. Your process supervisor (systemd, etc.) restarts the service

!!! tip "Prefer /restart over killing the process"
    `/restart` lets in-progress runs complete before shutting down. Killing the process with `kill` or `systemctl restart` may interrupt active runs and lose work.

## SIGTERM behaviour

Sending SIGTERM to the Untether process triggers the same graceful drain as `/restart`:

1. New runs are rejected
2. Active runs are allowed to complete
3. After a 120-second drain timeout, remaining runs are cancelled and the process exits

This means `systemctl --user stop untether` also drains gracefully, as systemd sends SIGTERM first.

!!! note "Drain timeout"
    The default drain timeout is 120 seconds. If active runs don't complete within this window, they are cancelled and a timeout notification is sent to Telegram.

## Run diagnostics

Run the built-in preflight check to validate your configuration:

```sh
untether doctor
```

This validates:

- Telegram bot token is valid and the bot is reachable
- Chat ID is correct and the bot can send messages
- Topics configuration (if enabled)
- File transfer permissions and deny globs
- Voice transcription setup
- Engine availability (Claude, Codex, OpenCode, Pi)

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

When enabled, Untether watches the config file for changes and reloads most settings automatically. Transport settings (bot token, chat ID) are excluded — those require a full restart.

## Service management

For systemd-managed installations, common operations:

```bash
# Restart the service
systemctl --user restart untether

# Follow live logs
journalctl --user -u untether -f

# Check service status
systemctl --user status untether

# View recent logs (last 100 lines)
journalctl --user -u untether -n 100
```

!!! warning "Restart vs /restart"
    `systemctl --user restart untether` sends SIGTERM, which triggers a graceful drain. However, `/restart` in Telegram gives you a confirmation message and visibility into the drain process. Prefer `/restart` when you have Telegram access.

## Related

- [Troubleshooting](troubleshooting.md) — common issues and debugging strategies
- [Configuration](../reference/config.md) — full config reference
- [Dev setup](dev-setup.md) — running from source for development
- [Security hardening](security.md) — securing your instance
