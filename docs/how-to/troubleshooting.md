# Troubleshooting

Common issues and fixes for Untether, the Telegram bridge for coding agents. Untether works via [Telegram](https://telegram.org), available on iPhone, iPad, Android, Mac, Windows, Linux, and [Telegram Web](https://web.telegram.org).

## Quick diagnostics

Before diving into specific issues, run these two commands:

```sh
untether --debug    # start with debug logging → writes debug.log
untether doctor     # preflight check: token, chat, topics, files, voice, engines
```

<!-- SCREENSHOT: untether doctor output showing check results -->

## Bot not responding

**Symptoms:** You send a message but the bot doesn't reply at all.

1. Check that Untether is running in your terminal (or via systemd)
2. Verify your bot token: `untether doctor` will flag an invalid token
3. Check `allowed_user_ids` — if set, only listed users can interact. An empty list means everyone is allowed.
4. In a group chat, check trigger mode: if set to `mentions`, you must @mention the bot
5. Make sure you're messaging the correct bot (not a different one)

If using systemd:

```sh
systemctl --user status untether
journalctl --user -u untether -f    # live logs
```

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

| Platform | Claude credentials | Path |
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

## Checking service logs

If running Untether as a systemd service:

```sh
# Live logs
journalctl --user -u untether -f

# Last 100 lines
journalctl --user -u untether -n 100

# Logs since last boot
journalctl --user -u untether -b
```

Look for `handle.worker_failed`, `handle.runner_failed`, or `config.read.toml_error` entries.

## Related

- [Operations and monitoring](operations.md) — `/ping`, `/restart`, hot-reload
- [Configuration reference](../reference/config.md) — all config options
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
