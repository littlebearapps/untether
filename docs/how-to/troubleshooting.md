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

## Bot fails to start: `allowed_user_ids is empty`

**Symptoms:** Untether exits at startup with `ConfigError: [transports.telegram] allowed_user_ids is empty …`.

This is the v0.35.3 ([#377](https://github.com/littlebearapps/untether/issues/377)) startup-block. Before v0.35.3 an empty allowlist was a silent insecure default — any Telegram user who knew the bot username could send commands. Fix by either:

- **Recommended**: populate the allowlist with your Telegram user ID(s):

    ```sh
    untether config set transports.telegram.allowed_user_ids "[<your_id>]"
    ```

    Get your ID with `untether chat-id` (sends a message in your chat and prints the IDs).

- **Dev/demo escape hatch**: opt in to an open bot. Logged at INFO every boot so the deviation stays visible:

    ```toml title="~/.untether/untether.toml"
    [transports.telegram]
    allow_any_user = true
    ```

See [security.md](security.md#restrict-access) for the full discussion.

## Bot not responding

**Symptoms:** You send a message but the bot doesn't reply at all.

1. Check that Untether is running:
    - **Terminal**: Look at the terminal where you ran `untether` — is it still running?
    - **Linux (systemd)**: `systemctl --user status untether`
2. Verify your bot token: `untether doctor` will flag an invalid token
3. Check `allowed_user_ids` — only listed users can interact. As of v0.35.3, an empty list is rejected at startup unless `allow_any_user = true` is set ([#377](https://github.com/littlebearapps/untether/issues/377)).
4. In a group chat, check listen mode (`/listen`): if set to `mentions`, you must @mention the bot
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

# Gemini CLI — DEPRECATED, see below
npm install -g @google/gemini-cli

# Amp — DEPRECATED, see below
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
- **Gemini CLI** (⚠️ deprecated): see below — individual and free Google accounts can no longer authenticate at all
- **Amp** (⚠️ deprecated): see below — `amp login` still works, but the client version is refused remotely

## Why does my Gemini run stall, or my Amp run fail immediately?

Both engines are **deprecated** and currently non-functional on ordinary accounts.
They fail in two different ways.

Confirm by running the CLI directly — both exit non-zero:

```bash
gemini --output-format stream-json --prompt="say OK"   # exits 1
amp -x "say OK"                                        # exits 1
```

### Amp — fails fast with a version refusal

**Symptom:** the run ends in a few seconds as `error · amp`, with a `426` in the
message.

```
426 {"error":{"message":"This version of Amp is no longer supported.
     Run `amp update` to continue. ..."}}
```

Amp remotely refuses clients it considers out of date. `amp update` may restore
it, but Amp re-refuses on its own schedule, so this recurs. Untether does not
track that cadence.

Note `amp threads list` (and therefore `/threads`) is a **local** command that
does not hit the version gate — `/threads` can list threads normally while
`amp -x` is refused.

### Gemini — stalls instead of failing

**Symptom:** the progress message sits at `starting · gemini` and never advances.
Eventually the stall watchdog fires and you get
`Auto-cancelled: session appears stuck (max_warnings)` — typically after ~10
minutes.

Run standalone, `gemini` prints `IneligibleTierError: This client is no longer
supported for Gemini Code Assist for individuals` and exits 1. Spawned by
Untether, the subprocess instead **hangs** without exiting, so Untether sees no
events and no exit — hence the stall rather than an error message. Tracked as
[#724](https://github.com/littlebearapps/untether/issues/724) and **not being
fixed**, because the engine is deprecated.

Gemini CLI reached **end-of-life for individual and free Google accounts on
18 June 2026**. There is no fix — migrate to
[Antigravity CLI](https://antigravity.google) (Untether support is planned as a
separate engine) or use a supported engine. Enterprise / Google Cloud licences
may still work, but Untether no longer verifies this.

### What to do

Both engines are targeted for removal in 0.36.0 — see
[deprecated engines](https://github.com/littlebearapps/untether#deprecated-engines).
Switch to `claude`, `codex`, `opencode`, or `pi` via `/config → Engine & model`.

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

## Engine output line cap

Individual engine stdout lines are capped at 10 MB. If an engine emits a single JSONL line exceeding this limit (e.g. a very large base64 image in a tool result), the line is truncated and a warning is logged. This prevents unbounded memory growth from malformed engine output.

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

**Expected waits don't warn.** A Claude rate-limit rejection, an API retry back-off and a live session waiting between turns for background work are all silent by design, so no stall warning fires for them (see the next section, and *Messages arrive after the run finished* below). A live session's idle hold is reported separately as `peak_live_idle_seconds` in the `session.summary` log line, not as `peak_idle_seconds` ([#787](https://github.com/littlebearapps/untether/issues/787)).

## Rate-limit and API-retry notes (Claude)

**Symptoms:** The progress message shows one of these lines:

| Note | Meaning |
|---|---|
| `⚠️ 5h limit 85% used — resets 17:30` | Heads-up only. Your subscription window is nearly used up; the run keeps going. Shown at most once per window ([#790](https://github.com/littlebearapps/untether/issues/790)). |
| `⏳ Rate limited until 17:30 (~30 min)` | Claude Code reported the limit as reached and extra usage isn't covering it. Untether treats the wait as expected until the reset time, so no stall warning fires. |
| `🔁 API error 529 (overloaded) — retrying in 8s (attempt 2/10)` | Anthropic's API returned a retryable error and Claude Code is backing off before retrying. The line updates in place for each attempt ([#792](https://github.com/littlebearapps/untether/issues/792)). |

Before v0.35.5 Untether misread Claude Code's routine usage snapshots and showed a brief `Rate limited` note every few minutes on healthy sessions. That no longer happens: routine snapshots are recorded but not shown.

## Claude Code hangs after an MCP tool_result

**Symptoms:** Claude Code goes silent immediately after an MCP tool returns — the `tool_result` arrives in the JSONL stream but the assistant never responds. Ring buffer fills with `user`/`tool_result` events and stays there. Often hits Cloudflare's remote MCP servers via `mcp-remote`.

Root cause is upstream — [claude-code#39700](https://github.com/anthropics/claude-code/issues/39700) combined with undici's idle-body timeout in `mcp-remote` ([geelen/mcp-remote#226](https://github.com/geelen/mcp-remote/issues/226)) — but Untether ships an opt-in detector plus a tiered workaround ([#322](https://github.com/littlebearapps/untether/issues/322)).

Enable in `~/.untether/untether.toml`:

```toml
[watchdog]
detect_stuck_after_tool_result = true
```

On detection (default 5 min after `tool_result` arrives with no assistant follow-up), Untether logs `progress_edits.stuck_after_tool_result`, SIGTERMs any `mcp-remote` / `@modelcontextprotocol` adapter children to force the SSE reader to error out, and finally cancels the run if the engine stays silent for another 60 seconds. Tune via `stuck_after_tool_result_timeout` and `stuck_after_tool_result_recovery_delay`. See the [config reference](../reference/config.md#watchdog).

## Claude Code exits without finishing (auto-continue)

**Symptoms:** Claude Code exits after receiving tool results without processing them. You see "⚠️ Auto-continuing" in the chat, or the session ends prematurely with no final answer.

This is an upstream Claude Code bug ([#34142](https://github.com/anthropics/claude-code/issues/34142), [#30333](https://github.com/anthropics/claude-code/issues/30333)). Untether detects it automatically and resumes the session.

**How it works:** Normal sessions end with `last_event_type=result`. When Claude Code exits with `last_event_type=user` (tool results sent but never processed), Untether sends a "⚠️ Auto-continuing" notification and resumes the session.

**If auto-continue keeps firing:**

1. Check if the upstream bug is fixed in a newer Claude Code version: `npm i -g @anthropic-ai/claude-code@latest`
2. Disable auto-continue if it causes issues: set `enabled = false` in `[auto_continue]`
3. Increase max retries if a single retry isn't enough: set `max_retries = 2` (max 5)

**Auto-continue is suppressed for signal deaths** (rc=143/SIGTERM, rc=137/SIGKILL) to prevent death spirals under memory pressure. See the [config reference](../reference/config.md#auto_continue).

## "Stream idle timeout - partial response received" (Claude)

**Symptoms:** Claude Code fails with `API Error: Stream idle timeout - partial response received` mid-run, with a Type-A or Type-B classification appended to the failure message.

The error message is classified inline ([#438](https://github.com/littlebearapps/untether/issues/438)) so you don't have to guess which mitigation applies:

* **Type-A (mid-generation stall)** — `num_turns ≥ 1 && duration_api_ms > 0`. Anthropic SSE went silent partway through a generation. Common on long opus 4.7 1M plan-mode runs. **Mitigation:** raise `[watchdog] claude_stream_idle_timeout_ms` to ride out longer silences.
  ```toml
  [watchdog]
  claude_stream_idle_timeout_ms = 600000   # 10 min (default 300000 / 5 min; max 1800000 / 30 min)
  ```
  Shell-set `CLAUDE_STREAM_IDLE_TIMEOUT_MS` still wins.
* **Type-B (cold-start zero-byte stall)** — `num_turns ≤ 1 && duration_api_ms == 0`. The connection opened and went silent before Anthropic produced any tokens. This is an upstream API outage, **not** a watchdog miscalibration — raising the timeout will not help. Wait it out, retry, or check the [Anthropic status page](https://status.anthropic.com).

**Auto-retry (opt-in, since v0.35.4):** a Type-A stall can now auto-resume the session instead of surfacing a terminal error ([#572](https://github.com/littlebearapps/untether/issues/572)). It's off by default:

```toml
[watchdog]
stream_idle_auto_retry = true    # resume Type-A stalls automatically (🔁 notice)
stream_idle_max_retries = 1      # attempt cap, 1–3
```

Type-B (cold-start zero-byte) is **never** retried — retrying just hammers a down API. Cost-budget caps and signal-death suppression still apply to a retried run, so it can't spiral under memory pressure or blow a daily budget.

## Claude session looks alive 30+ min after the final message

**Symptoms:** Claude has clearly finished the turn (you can see the final answer in Telegram), but the session metadata indicates it's still running. The bidirectional Claude CLI is sitting idle holding stdin open.

!!! note "v0.35.5 and later"
    With live sessions on (the default), a session that stays open after its answer is usually doing background work, and it closes itself about a minute after that work ends. See [Messages arrive after the run finished](#messages-arrive-after-the-run-finished). The post-result watchdog settings below apply when `[watchdog] live_sessions = false`.

The post-result idle watchdog ([#333](https://github.com/littlebearapps/untether/issues/333)) closes the gap: every successful `result` event arms `[watchdog] post_result_idle_timeout` (default 600s / 10 min, range 30s–1h). Once the deadline passes the runner closes stdin and the CLI exits cleanly (rc=0). The footer also shows a `✓ turn complete` marker on every successful turn so you have an immediate visual confirmation that the turn has ended even if the process is still alive briefly.

**To disable the timer entirely** (Claude CLI handles its own exit):

```toml
[watchdog]
post_result_idle_enabled = false
```

**To shorten the timeout** for impatient deployments:

```toml
[watchdog]
post_result_idle_timeout = 60   # 1 minute
```

If a button-click `control_response` is mid-flight when the deadline arrives, the timer re-arms instead of closing — preventing orphaned approvals. Look for `claude.post_result_idle.armed`, `claude.post_result_idle.deferred` and `claude.post_result_idle.closing_stdin` in the logs to confirm the watchdog's behaviour. The every-30-s `claude.post_result_idle.tick` heartbeat is INFO only while the timer is armed or an approval is pending; otherwise it is DEBUG (start with `untether --debug` to see it).

When the watchdog actually closes stdin, Untether also sends one (and only one) Telegram closing message: `✓ turn complete · session closed after Nm idle`. While the watchdog is running, stall warnings are suppressed (`progress_edits.stall_post_result_suppressed`) so you don't get noise during the legitimate idle window — genuinely-frozen post-result sessions still warn via the frozen-ring escalation.

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
5. **Since v0.35.4**, a `voice_transcription_base_url` that resolves to a private/loopback address (e.g. a self-hosted Whisper on `localhost`, `10.x`, or `192.168.x`) is **rejected by the SSRF guard** ([#381](https://github.com/littlebearapps/untether/issues/381)). Opt the endpoint back in with its CIDR range:

    ```toml
    [transports.telegram]
    voice_transcription_url_allowlist = ["127.0.0.0/8"]   # or your private range
    ```

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
3. **Port already in use?** As of [#320](https://github.com/littlebearapps/untether/issues/320), a port conflict degrades gracefully — the rest of the bot (polling, commands, crons) stays up, but webhook delivery is disabled. Look for `triggers.server.bind_failed` in the log (`journalctl --user -u untether \| grep bind_failed`); the entry includes the occupied port and a `fix` suggestion. Free the port or set `[triggers.server] port = <N>` in `untether.toml`.
4. Check auth — if using HMAC, the sending service must sign requests with the same secret
5. Check `event_filter` — if set, only matching event types are processed
6. Check firewall rules if the webhook server is behind NAT
7. Look at `debug.log` for incoming request logs

## Config change didn't take effect

**Symptoms:** You edited `untether.toml` but the change doesn't seem to apply.

1. **Check `watch_config`:** Hot-reload requires `watch_config = true` in the top-level config. Without it, changes only apply on restart.
2. **Hot-reloadable settings** apply immediately: `voice_transcription`, `[files]`, `allowed_user_ids`, `show_resume_line`, trigger crons/webhooks/auth/timezones.
3. **Restart-only settings** require `/restart` or `systemctl restart`: `bot_token`, `chat_id`, `session_mode`, `topics.enabled`, `message_overflow`, `triggers.server.host`/`port`. Editing one of these in a running bot triggers a Telegram 🔄 warning to every project chat plus any `allowed_user_ids` admin DM ([#318](https://github.com/littlebearapps/untether/issues/318)) so you won't silently keep running on the stale value.
4. Check the log for `config.reload.applied` (success), `config.reload.transport_config_changed restart_required=True` (restart needed), or `config.reload.restart_notify.sent` (Telegram warning broadcast).

## /at delay not firing

**Symptoms:** You scheduled `/at 30m Check the build` but the prompt never runs.

- Pending `/at` delays are held in memory — they are **lost on restart**. If Untether restarted after you scheduled, the delay was cancelled.
- Use `/cancel` to see how many pending delays exist. If it says "nothing running", there are no pending delays.
- Minimum duration: 60 seconds. Maximum: 24 hours. Values outside this range are rejected.
- Per-chat cap: 20 pending delays. The 21st is rejected with an error message.

## Session not resuming

**Symptoms:** Sending a follow-up message starts a new session instead of continuing.

- **Chat mode** (`session_mode = "chat"`): Just send another message — it auto-resumes. Use `/new` to start fresh.
- **Stateless mode** (`session_mode = "stateless"`): You must **reply** to a message that contains a resume token. Plain messages start new sessions.
- If resume fails silently, the previous session may be **poisoned** by an upstream turn-state bug (a resume that returns 0 turns / an empty answer). Untether detects this, quarantines that session so it is never resumed again, and automatically re-sends your message on a **fresh** session — you'll see a short notice that it did so ([#631](https://github.com/littlebearapps/untether/issues/631), [#632](https://github.com/littlebearapps/untether/issues/632)). A session force-killed after delivering its result is quarantined proactively, so your *next* message diverts fresh before any empty result appears. Since v0.35.5 sessions normally close gracefully, so this fresh-session divert is rare, and an idle session that is merely slow to exit is no longer quarantined ([#791](https://github.com/littlebearapps/untether/issues/791)).

## Follow-up message says it's "queued"

**Symptoms:** You send a follow-up (or voice note) and it shows a `queued` notice instead of running immediately.

A follow-up waits for the current Claude turn to finish; it isn't mixed into a turn that's still running. Since v0.35.5, if the session is still open (for example, background tasks are running after the reply), the message goes into **that same session** as soon as the turn ends, usually within seconds, even while the background tasks keep going ([#776](https://github.com/littlebearapps/untether/issues/776), [#647](https://github.com/littlebearapps/untether/issues/647)). The notice reads `⏳ Queued — sent as soon as Claude's current turn ends (background tasks keep running).` ([#781](https://github.com/littlebearapps/untether/issues/781)). With `[watchdog] live_sessions = false` the follow-up really does wait for the background work, and the notice says `⏳ Queued behind the previous run's N background task(s) — starts when they finish …` instead. If you see `⚠️ The session ended before this message ran — please send it again`, the session closed before your message started; resend it. If a queue is genuinely stuck, `/cancel` and resend.

## Messages arrive after the run finished (🔔 / 📡 / ⏰)

**Symptoms:** After Claude's answer, more messages appear on their own: `🔔 Background task finished — …`, `📡 Monitor — …`, or `⏰ Scheduled wake-up`.

This is expected since v0.35.5. When Claude starts a background task, a subagent, a `Monitor` or a `ScheduleWakeup` and ends its turn, Untether keeps the session open. Claude then carries on by itself when the work finishes, and each of those turns is delivered as its own message. Before v0.35.5 these turns ran with nothing shown in Telegram. Monitor updates arrive silently (no notification); the others notify, once per finished task ([#785](https://github.com/littlebearapps/untether/issues/785)). Approval buttons work inside these turns as normal ([#776](https://github.com/littlebearapps/untether/issues/776)).

The session stays open while background work is live, until it has shown no activity for 30 minutes (`[watchdog] post_result_bg_max_hold`) or has been open for 4 hours in total (`live_session_max_s`). Activity means a turn, a background agent's progress (every tool call it makes, including one long-running command), or new output from a background command. A working agent is therefore not stopped at 30 minutes, but a silent `sleep` or a stuck agent is. With no background work the session closes about a minute after the reply. When it closes over running tasks you get a notice naming them, such as `⏳ Closing session — 1 background task still running with no progress for 30 min: … Stopping it.` Once the session has stopped, a quiet follow-up tells you what happens next. `↩️ Reply to continue in the same session.` means a reply resumes the same conversation. The `⚠️ … your next message starts a fresh session` warning means the session didn't stop cleanly, so Claude won't remember that run, and partial work may be left in the working tree. `/cancel`, changing settings (`/planmode`, model) and Untether restarts close the session the same way, with a notice. To go back to the old turn-based hold, set `[watchdog] bg_hold_rearm_on_progress = false` ([#829](https://github.com/littlebearapps/untether/issues/829)).

To turn this off and get the pre-v0.35.5 behaviour back (stop at the first answer), set `live_sessions = false` under `[watchdog]`.

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

1. Check **listen mode**: groups default to `mentions` in many setups. Send `/listen` to check, or `/listen all` to respond to everything. (`/trigger` still works as a deprecated alias from v0.35.3 onward.)
2. Check **bot privacy mode** in BotFather: send `/setprivacy` to @BotFather and select your bot. Set to "Disable" so the bot can see all messages (not just commands and @mentions).
3. Check `allowed_user_ids` — group members not in the list are ignored. (As of v0.35.3 the list is required at startup unless `allow_any_user = true` is set — see [security.md](security.md#restrict-access).)
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

Telegram bot tokens, OpenAI API keys (`sk-...`), GitHub tokens (`ghp_`, `ghs_`, `github_pat_`), `Authorization:`/`Bearer` credentials, JWTs, and `api_key=`/`token=`/`secret=`/`password=` values are automatically redacted in all log output. Token *counts* such as `total_tokens=52000` are left alone.

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

## Loop didn't fire / loop fired too many times

Loop mode (`/config → 🔁 Loop mode`) gates Untether's observation of Claude Code's `/loop` and `ScheduleWakeup` tools. ([#289](https://github.com/littlebearapps/untether/issues/289))

| Symptom | Likely cause | Fix |
|---|---|---|
| `/loop` registered during the turn but no fires happened afterwards | Loop mode toggle is OFF (the default) | `/config → 🔁 Loop mode → 🔁 On` |
| Loop stopped after N iterations | Hit `[loop] max_iterations` cap | Raise `max_iterations` in `untether.toml`, or restart the loop with a fresh `/loop` |
| Loop ended with `daily_budget_exceeded` | Hit `[cost_budget] max_cost_per_day` | Raise the cap in `/config → 💰 Cost & usage`, or wait for the daily reset |
| Loop fires happened but each was a "fresh user turn" rather than autonomous | This is by design — Untether re-issues the original prompt at each fire (see [Schedule tasks → Loop mode](schedule-tasks.md#loop-mode)) | N/A — expected behaviour |
| Loop kept firing after `/cancel` | Stale `active_loops.json` | Restart `untether` (or the dev/staging unit) — the do-not-resume sentinel is loaded at startup and blocks future fires for cancelled sessions |
| Loop didn't survive a restart | `active_loops.json` is missing or corrupt | Check `journalctl --user -u untether-dev -f` for `loop.restore.read_failed` warnings; the file lives next to your `untether.toml` |

## Related

- [Operations and monitoring](operations.md) — `/ping`, `/restart`, hot-reload
- [Configuration reference](../reference/config.md) — all config options
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
