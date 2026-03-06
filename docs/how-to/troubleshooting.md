# Troubleshooting

Common issues and fixes for Untether. If your agent isn't responding, messages aren't arriving, or something looks off — start here.

## Quick diagnostics

Before diving into specific issues, run these two commands:

```sh
untether --debug    # start with debug logging → writes debug.log
untether doctor     # preflight check: token, chat, topics, files, voice, engines
```

<!-- SCREENSHOT: untether doctor output showing check results -->

## Bot not responding

**Symptoms:** You send a message but the bot doesn't reply at all.

1. Check that Untether is running:
    - **Terminal**: Look at the terminal where you ran `untether` — is it still running?
    - **Linux (systemd)**: `systemctl --user status untether`
2. Verify your bot token: `untether doctor` will flag an invalid token
3. Check `allowed_user_ids` — if set, only listed users can interact. An empty list means everyone is allowed.
4. In a group chat, check trigger mode: if set to `mentions`, you must @mention the bot
5. Make sure you're messaging the correct bot (not a different one)

## Engine CLI not found

**Symptoms:** "codex: command not found" or similar error after sending a task.

The engine CLI isn't on your PATH. Install the engine you need:

```sh
# Codex
npm install -g @openai/codex

# Claude Code
npm install -g @anthropic-ai/claude-code

# OpenCode
npm install -g opencode-ai@latest

# Pi
npm install -g @mariozechner/pi-coding-agent
```

Verify with `which codex` (or `which claude`, etc.). If installed via `npm -g` but not found, check that npm's global bin directory is in your PATH.

Run `untether doctor` to see which engines are detected.

## Permission denied or auth errors

**Symptoms:** Engine starts but fails with authentication or permission errors.

- **Codex:** Run `codex` in a terminal and sign in with your ChatGPT account
- **Claude Code:** Run `claude login` to authenticate. On macOS, credentials are stored in Keychain; on Linux, in `~/.claude/.credentials.json`
- **OpenCode:** Run `opencode` and authenticate with your chosen provider
- **Pi:** Run `pi` and log in with your provider

## Progress stuck on "starting"

**Symptoms:** The progress message shows "starting" but never updates.

1. The engine might be doing a slow first-time setup (repo indexing, dependency install). Wait 30-60 seconds.
2. If it persists, `/cancel` (reply to the progress message) and try a more specific prompt
3. Check `debug.log` — the engine may have errored silently
4. Verify the engine works standalone: run `codex "hello"` (or equivalent) directly in a terminal

## Messages too long or truncated

**Symptoms:** The bot's response is cut off or split across multiple messages.

Telegram messages have a 4096-character limit. Untether handles this automatically:

- **Split mode** (default): Long responses are split across multiple messages (~3500 chars each)
- **Trim mode**: Single message, truncated to fit

To change:

=== "untether config"

    ```sh
    untether config set transports.telegram.message_overflow "trim"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram]
    message_overflow = "trim"    # or "split" (default)
    ```

## Voice transcription not working

**Symptoms:** Sending a voice note doesn't start a run, or you get a transcription error.

1. Check that voice transcription is enabled:

    ```toml
    [transports.telegram]
    voice_transcription = true
    ```

2. Make sure you have an OpenAI API key set (voice transcription uses the OpenAI transcription API by default)
3. Check the voice note size — default max is 10 MiB (`voice_max_bytes`)
4. If using a custom transcription server, verify `voice_transcription_base_url` is reachable

Run `untether doctor` to validate voice configuration.

## File transfer blocked

**Symptoms:** `/file put` or `/file get` fails, or dropped documents aren't saved.

1. Check that file transfer is enabled:

    ```toml
    [transports.telegram.files]
    enabled = true
    ```

2. Check `deny_globs` — files matching these patterns are blocked (default: `.git/**`, `.env`, `*.pem`, `.ssh/**`)
3. In group chats, file transfer requires admin or creator status (unless `files.allowed_user_ids` is set)
4. Check the `uploads_dir` path exists relative to the project root

## Topics not appearing

**Symptoms:** `/topic` doesn't work, or topics aren't binding to projects.

1. Topics require a **forum-enabled supergroup** (not a private chat or regular group)
2. The bot must be **admin with "Manage Topics" permission**
3. Topics must be enabled in config:

    ```toml
    [transports.telegram.topics]
    enabled = true
    scope = "auto"    # or "main", "projects", "all"
    ```

4. Run `untether doctor` — it checks topic permissions

## Webhook not receiving events

**Symptoms:** Webhooks are configured but never fire.

1. Check that triggers are enabled: `[triggers] enabled = true`
2. Verify the server is running: `curl http://127.0.0.1:9876/health` (adjust host/port)
3. Check auth — if using HMAC, the sending service must sign requests with the same secret
4. Check `event_filter` — if set, only matching event types are processed
5. Check firewall rules if the webhook server is behind NAT
6. Look at `debug.log` for incoming request logs

## Session not resuming

**Symptoms:** Sending a follow-up message starts a new session instead of continuing.

- **Chat mode** (`session_mode = "chat"`): Just send another message — it auto-resumes. Use `/new` to start fresh.
- **Stateless mode** (`session_mode = "stateless"`): You must **reply** to a message that contains a resume token. Plain messages start new sessions.
- If resume fails silently, the previous session may have been corrupted. Untether auto-clears broken resume tokens (0-turn sessions).

## Cost budget blocking runs

**Symptoms:** "Budget exceeded" message, or runs are cancelled mid-stream.

1. Check your budget settings:

    ```toml
    [cost_budget]
    enabled = true
    max_cost_per_run = 2.00      # USD per run
    max_cost_per_day = 20.00     # USD per day
    auto_cancel = true           # cancels runs exceeding per-run limit
    ```

2. Daily budgets reset at midnight UTC
3. To temporarily bypass: set `enabled = false` or increase the limits
4. Check current spend with `/usage`

## Group chat: bot ignoring messages

**Symptoms:** Bot works in private chat but ignores messages in a group.

1. Check **trigger mode**: groups default to `mentions` in many setups. Send `/trigger` to check, or `/trigger all` to respond to everything.
2. Check **bot privacy mode** in BotFather: send `/setprivacy` to @BotFather and select your bot. Set to "Disable" so the bot can see all messages (not just commands and @mentions).
3. Check `allowed_user_ids` — if set, group members not in the list are ignored.
4. If using topics, make sure the bot has "Manage Topics" permission.

## macOS and Linux credential differences

| Platform | Claude Code credentials | Path |
|----------|-------------------|------|
| Linux | Plain-text JSON file | `~/.claude/.credentials.json` |
| macOS | macOS Keychain | Entry: `Claude Code-credentials` |

Untether checks both locations automatically. If you've recently changed platforms or reinstalled, run `claude login` to refresh credentials.

## Using debug mode

Start Untether with `--debug` for full diagnostic logging:

```sh
untether --debug
```

This writes to `debug.log` in the current directory. The log includes:

- Engine JSONL events (every line the subprocess emits)
- Telegram API requests and responses
- Rendered message content
- Error tracebacks

Include `debug.log` when reporting issues on [GitHub](https://github.com/littlebearapps/untether/issues).

## Using untether doctor

Run `untether doctor` for a comprehensive preflight check:

```sh
untether doctor
```

It validates:

- Telegram bot token (connects and verifies)
- Chat ID (reachable)
- Topics configuration (permissions, forum group status)
- File transfer settings (deny globs, permissions)
- Voice transcription configuration (API reachability)
- Engine CLI availability (on PATH)

<!-- SCREENSHOT: untether doctor output with all checks passing -->

## Checking logs

=== "Terminal (all platforms)"

    Untether logs to the terminal by default. For detailed logs:

    ```sh
    untether --debug    # writes debug.log in current directory
    ```

=== "Linux (systemd)"

    ```sh
    journalctl --user -u untether -f       # live logs
    journalctl --user -u untether -n 100   # last 100 lines
    journalctl --user -u untether -b       # since last boot
    ```

Look for `handle.worker_failed`, `handle.runner_failed`, or `config.read.toml_error` entries.

## Error hints

When an engine fails, Untether scans the error message and shows an actionable recovery hint below the error. These hints cover the most common failure modes across all engines and providers.

### Authentication errors

| Error | Hint |
|-------|------|
| Access token could not be refreshed | Run `codex login --device-auth` to re-authenticate |
| Log out and sign in again | Run `codex login` to re-authenticate |
| `anthropic_api_key` | Check that ANTHROPIC_API_KEY is set in your environment |
| `openai_api_key` | Check that OPENAI_API_KEY is set in your environment |
| `google_api_key` | Check that your Google API key is set in your environment |

### Subscription and billing limits

| Error | Hint |
|-------|------|
| Out of extra usage / hit your limit | Subscription usage limit reached — wait for the reset window, then resume |
| `insufficient_quota` / exceeded your current quota | OpenAI billing quota exceeded — add credits at platform.openai.com |
| `billing_hard_limit_reached` | OpenAI billing hard limit — increase your spend limit at platform.openai.com |
| `resource_exhausted` | Google API quota exhausted — check quota at console.cloud.google.com |

### API overload and server errors

| Error | Hint |
|-------|------|
| `overloaded_error` (529) | Anthropic API overloaded — temporary, session saved, try again in a few minutes |
| Server is overloaded | API server overloaded — temporary, try again in a few minutes |
| `internal_server_error` (500) | Internal server error — usually temporary, try again shortly |
| Bad gateway (502) | Bad gateway error — usually temporary, try again shortly |
| Service unavailable (503) | API temporarily unavailable — try again in a few minutes |
| Gateway timeout (504) | Gateway timed out — usually temporary, try again shortly |

### Rate limits

| Error | Hint |
|-------|------|
| Rate limit / too many requests | Rate limited — the engine will retry automatically |

### Network errors

| Error | Hint |
|-------|------|
| Connection refused | Check that the target service is running |
| Connect timeout | Connection timed out — check your network, then try again |
| Read timeout | Connection timed out — usually transient, try again |
| Name or service not known | DNS resolution failed — check your network connection |
| Network is unreachable | Network unreachable — check your internet connection |

### Process signals

| Error | Hint |
|-------|------|
| SIGTERM | Untether was restarted — session saved, resume by sending a new message |
| SIGKILL | Process forcefully terminated (timeout or OOM) — session saved, try resuming |
| SIGABRT | Process aborted unexpectedly — try starting a fresh session with `/new` |

### Session and process errors

| Error | Hint |
|-------|------|
| Session not found | Try a fresh session without --session flag |
| Error during execution | Session failed to load (possibly corrupted) — send `/new` to start fresh |
| Finished without a result event | Engine exited before producing a final answer (crash or timeout) — session saved, try resuming |
| Finished but no session_id | Engine crashed during startup — check that the engine CLI is installed and working |

All hints are case-insensitive and pattern-matched against the full error output. The first matching hint wins. Your session is automatically saved in most cases, so you can resume after resolving the issue.

## Related

- [Operations and monitoring](operations.md) — `/ping`, `/restart`, hot-reload
- [Configuration reference](../reference/config.md) — all config options
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
