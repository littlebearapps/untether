# Troubleshooting

Common issues and fixes for Untether. If your agent isn't responding, messages aren't arriving, or something looks off — start here.

## Quick diagnostics

Before diving into specific issues, run these two commands:

```sh
untether --debug    # start with debug logging → writes debug.log
untether doctor     # preflight check: token, chat, topics, files, voice, engines
```

```
$ untether doctor
✓ bot token valid (@my_untether_bot)
✓ chat 123456789 reachable
✓ engine codex found at /usr/local/bin/codex
✓ engine claude found at /usr/local/bin/claude
✗ engine opencode not found
✓ voice transcription configured
✓ file transfer directory exists
```

<!-- TODO: capture screenshot -->
<!-- <img src="../assets/screenshots/doctor-output.jpg" alt="untether doctor output showing check results" width="360" loading="lazy" /> -->

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

# Gemini CLI
npm install -g @google/gemini-cli

# Amp
npm install -g @sourcegraph/amp
```

Verify with `which codex` (or `which claude`, etc.). If installed via `npm -g` but not found, check that npm's global bin directory is in your PATH.

Run `untether doctor` to see which engines are detected.

## Permission denied or auth errors

**Symptoms:** Engine starts but fails with authentication or permission errors.

- **Codex:** Run `codex` in a terminal and sign in with your ChatGPT account
- **Claude Code:** Run `claude login` to authenticate. On macOS, credentials are stored in Keychain; on Linux, in `~/.claude/.credentials.json`
- **OpenCode:** Run `opencode` and authenticate with your chosen provider
- **Pi:** Run `pi` and log in with your provider
- **Gemini CLI:** Run `gemini` and authenticate with your Google account
- **Amp:** Run `amp` and sign in with your Sourcegraph account

## Progress stuck on "starting"

**Symptoms:** The progress message shows "starting" but never updates.

1. The engine might be doing a slow first-time setup (repo indexing, dependency install). Wait 30-60 seconds.
2. If it persists, `/cancel` (reply to the progress message) and try a more specific prompt
3. Check `debug.log` — the engine may have errored silently
4. Verify the engine works standalone: run `codex "hello"` (or equivalent) directly in a terminal

## Engine hangs in headless mode

**Symptoms:** The engine starts but produces no output, eventually triggering stall warnings. Common with Codex and OpenCode when the engine needs user input (approval or question) but has no terminal to display it.

### Codex: approval hang

Codex may block waiting for terminal approval in headless mode if no `--ask-for-approval` flag is passed. **Fix:** upgrade to Untether v0.35.0+ which always passes `--ask-for-approval never` (or `untrusted` in safe permission mode). Older versions may not pass this flag, causing Codex to use its default terminal-based approval flow.

### OpenCode: unsupported event warning

If OpenCode emits a JSONL event type that Untether doesn't recognise (e.g. a `question` or `permission` event from a newer OpenCode version), Untether v0.35.0+ shows a visible warning in Telegram: "opencode emitted unsupported event: {type}". In older versions, these events were silently dropped, leaving the user with no feedback until the stall watchdog fired.

If you see this warning, check for an Untether update that adds support for the new event type. OpenCode's `run` command auto-denies questions via permission rules, so this should be rare — it most likely indicates an OpenCode protocol change.

## Stall warnings

**Symptoms:** Telegram shows "⏳ No progress for X min — session may be stuck" or "⏳ MCP tool running: server-name (X min)".

The stall watchdog monitors engine subprocesses for periods of inactivity (no JSONL events on stdout). Thresholds vary by context:

| Context | Threshold | Example |
|---------|-----------|---------|
| Normal (thinking/generation) | 5 min | Model is generating a response |
| Local tool running (Bash, Read, etc.) | 10 min | Long test suite or build |
| MCP tool running | 15 min | External API call (Cloudflare, GitHub, web search) |
| Pending user approval | 30 min | Waiting for Approve/Deny click |

**If the warning names an MCP tool** (e.g. "MCP tool running: cloudflare-observability"), the process is likely waiting on a slow external API. This is usually not a real stall — wait for it to complete or `/cancel` if it's taking too long.

**If the warning says "MCP tool may be hung"**, the MCP tool has been running with no new events for an extended period (3+ stall checks with a frozen event buffer). This usually means the MCP server is stuck in an internal retry loop. Use `/cancel` and retry with a more targeted prompt.

**If the warning says "CPU active, no new events"**, the process is using CPU but hasn't produced any new JSONL events for 3+ stall checks. This can happen when Claude Code is stuck in a long API call, extended thinking, or an internal retry loop. Use `/cancel` if the silence persists.

**If the warning says "Bash command still running (X min)"**, Claude Code is waiting for a long-running tool subprocess (benchmark, build, test suite). This warning fires once when the tool exceeds the threshold (10 min by default). While the child process is actively consuming CPU, repeat warnings are suppressed — you won't see the same message every 3 minutes. If the child process stops consuming CPU, warnings resume with "tool may be stuck".

**If the warning says "X tool may be stuck (N min, no CPU activity)"**, the tool subprocess has stopped consuming CPU, suggesting it may be genuinely stuck (e.g. a hung `curl`, a network timeout, a deadlock). Use `/cancel` and resume, asking Claude to skip the hung command.

**If the warning says "session may be stuck"**, the process may genuinely be stalled. Check:

1. Look at the diagnostics in the message — CPU active, TCP connections, RSS
2. If CPU is active and TCP connections exist, the process is likely still working
3. If CPU is idle and no TCP connections, the process may be truly stuck — use `/cancel`

**Tuning:** All thresholds are configurable via `[watchdog]` in `untether.toml`. Use `tool_timeout` to increase the initial threshold for local tools (default 10 min), and `mcp_tool_timeout` for MCP tools (default 15 min). See the [config reference](../reference/config.md#watchdog).

## Claude Code exits without finishing (auto-continue)

**Symptoms:** Claude Code exits after receiving tool results without processing them. You see "⚠️ Auto-continuing" in the chat, or the session ends prematurely with no final answer.

This is an upstream Claude Code bug ([#34142](https://github.com/anthropics/claude-code/issues/34142), [#30333](https://github.com/anthropics/claude-code/issues/30333)). Untether detects it automatically and resumes the session.

**How it works:** Normal sessions end with `last_event_type=result`. When Claude Code exits with `last_event_type=user` (tool results sent but never processed), Untether sends a "⚠️ Auto-continuing" notification and resumes the session.

**If auto-continue keeps firing:**

1. Check if the upstream bug is fixed in a newer Claude Code version: `npm i -g @anthropic-ai/claude-code@latest`
2. Disable auto-continue if it causes issues: set `enabled = false` in `[auto_continue]`
3. Increase max retries if a single retry isn't enough: set `max_retries = 2` (max 5)

**Auto-continue is suppressed for signal deaths** (rc=143/SIGTERM, rc=137/SIGKILL) to prevent death spirals under memory pressure. See the [config reference](../reference/config.md#auto_continue).

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

## Claude Code plugin interference

**Symptoms:** Agent completes successfully but the response is about "hooks", "context docs", or "false positive" instead of the content you actually asked for. The run shows `done` with a short answer that doesn't match your request.

This happens when Claude Code plugins with **Stop hooks** consume the final response. In a terminal, the user can scroll up to see earlier output. In Telegram, only the final message is visible — so if a Stop hook causes Claude to address hook concerns in its last turn, the actual content is replaced.

**Affected plugins:** Any Claude Code plugin that uses `"decision": "block"` in a Stop hook. The most common example is [PitchDocs](https://github.com/littlebearapps/lba-plugins) context-guard, which nudges Claude to update AI context docs when structural files change.

**Fix:**

1. **Update the plugin** — PitchDocs v1.20+ checks for `$UNTETHER_SESSION` and automatically skips blocking Stop hooks in Telegram sessions. Run `/pitchdocs:context-guard install` in your project to update the hooks.

2. **Verify `UNTETHER_SESSION` is set** — Untether v0.34.4+ sets `UNTETHER_SESSION=1` in the Claude runner subprocess environment. If you're on an older version, upgrade: `pipx upgrade untether`

3. **For custom plugins** — add this to your Stop hook script:

    ```bash
    [ -n "${UNTETHER_SESSION:-}" ] && echo '{}' && exit 0
    ```

This is not a security concern — `UNTETHER_SESSION` is a simple signal variable that tells plugins the session is running via Telegram. See the [interference audit](../audits/pitchdocs-context-guard-interference.md) for a detailed case study.

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

```
$ untether doctor
✓ bot token valid (@my_untether_bot)
✓ chat 123456789 reachable
✓ engine codex found at /usr/local/bin/codex
✓ engine claude found at /usr/local/bin/claude
✓ engine opencode found at /usr/local/bin/opencode
✓ voice transcription configured
✓ file transfer directory exists
all checks passed
```

<!-- TODO: capture screenshot -->
<!-- <img src="../assets/screenshots/doctor-all-passing.jpg" alt="untether doctor with all checks passing" width="360" loading="lazy" /> -->

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

### Key log events

| Event | Level | Meaning |
|-------|-------|---------|
| `handle.worker_failed` | ERROR | Engine run crashed |
| `handle.runner_failed` | ERROR | Runner subprocess failed |
| `config.read.toml_error` | ERROR | Config file couldn't be parsed |
| `footer_settings.load_failed` | WARNING | Footer config fell back to defaults |
| `watchdog_settings.load_failed` | WARNING | Watchdog config fell back to defaults |
| `auto_continue_settings.load_failed` | WARNING | Auto-continue config fell back to defaults |
| `preamble_settings.load_failed` | WARNING | Preamble config fell back to defaults |
| `outline_cleanup.delete_failed` | WARNING | Stale plan outline message couldn't be deleted |
| `handle.engine_resolved` | INFO | Engine and CWD successfully resolved for a run |
| `file_transfer.saved` | INFO | File uploaded and written to disk |
| `file_transfer.denied` | WARNING | File transfer blocked (permissions, deny glob) |
| `message.dropped` | DEBUG | Message from unrecognised chat silently dropped |
| `cost_budget.exceeded` | ERROR | Run or daily cost exceeded budget |

All logs include `session_id` once a session starts, enabling per-session filtering with `grep` or `jq`.

Telegram bot tokens, OpenAI API keys (`sk-...`), and GitHub tokens (`ghp_`, `ghs_`, `github_pat_`) are automatically redacted in all log output.

## Error hints

When an engine fails, Untether scans the error message and shows an actionable recovery hint above the raw error. The raw error is wrapped in a code block for visual separation. Hints are case-insensitive and pattern-matched — the first match wins. Your session is automatically saved in most cases, so you can resume after resolving the issue.

Untether recognises **67 error patterns** across 14 categories:

| Category | Examples | Engines |
|----------|----------|---------|
| Authentication | API key missing/invalid, token refresh, login required | All |
| Subscription & billing | Usage limits, quota exceeded, billing hard limit | Claude, Codex, OpenCode, Gemini |
| API overload & server | 500/502/503/504, overloaded | All |
| Rate limits | Rate limited, too many requests | All |
| Model errors | Model not found, invalid model | All |
| Context length | Context too long, max tokens exceeded | Claude, Codex, OpenCode |
| Content safety | Content filter, safety block, prompt blocked | Claude, Gemini |
| Invalid request | Malformed API request | Claude, Codex |
| Network & SSL | DNS, timeout, connection refused, certificate errors | All |
| CLI & filesystem | Command not found, disk full, permission denied | All |
| Signals | SIGTERM, SIGKILL, SIGABRT | All |
| Process & session | No result event, no session ID, execution errors | All |
| Engine-specific | AMP credits/login, Gemini result status | AMP, Gemini |
| Account & proxy | Account suspended, proxy auth, request timeout | All |

For the full list of patterns and hints, see the [Error Reference](../reference/errors.md).

## Related

- [Operations and monitoring](operations.md) — `/ping`, `/restart`, hot-reload
- [Configuration reference](../reference/config.md) — all config options
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
