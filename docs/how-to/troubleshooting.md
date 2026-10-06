# Troubleshooting

Common issues and fixes for Untether. If your agent isn't responding, messages aren't arriving, or something looks off — start here.

## Quick diagnostics

Before diving into specific issues, run these two commands:

```sh
untether --debug    # start with debug logging → writes debug.log
untether doctor     # preflight check: token, chat, topics, file transfer, voice key
```

```
$ untether doctor
untether doctor
- telegram token: ok (@my_untether_bot)
- chat_id: ok (private (123456789))
- topics: ok (disabled)
- file transfer: ok (restricted to 1 user id(s))
- voice transcription: error (API key not set)
```

`untether doctor` exits with status 1 when any check reports `error`. It doesn't check engine CLIs: the startup message lists any that are `not installed`, `misconfigured` or `failed to load`.

<!-- TODO: capture screenshot -->
<!-- <img src="../assets/screenshots/doctor-output.jpg" alt="untether doctor output showing check results" width="360" loading="lazy" /> -->

## Bot fails to start: `allowed_user_ids is empty`

**Symptoms:** Untether exits at startup with `ConfigError: [transports.telegram] allowed_user_ids is empty …`.

This is the v0.35.3 ([#377](https://github.com/littlebearapps/untether/issues/377)) startup-block. Before v0.35.3 an empty allowlist was a silent insecure default — any Telegram user who knew the bot username could send commands. Fix by either:

- **Recommended**: populate the allowlist with your Telegram user ID(s):

    ```sh
    untether config set transports.telegram.allowed_user_ids "[<your_id>]"
    ```

    Get your ID with `untether chat-id`: it waits for you to send the bot a message, then prints `chat_id = …`. In a private chat with the bot, that number is your user ID.

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

# OpenCode (the 1.x CLI; 2.x isn't supported yet)
npm install -g opencode-ai@1

# Pi
npm install -g @mariozechner/pi-coding-agent

# Gemini CLI — DEPRECATED, see below
npm install -g @google/gemini-cli

# Amp — DEPRECATED, see below
npm install -g @sourcegraph/amp
```

Verify with `which codex` (or `which claude`, etc.). If installed via `npm -g` but not found, check that npm's global bin directory is in your PATH.

The startup message lists engines that are `not installed`, `misconfigured` or `failed to load`; `untether doctor` doesn't check engine CLIs.

## OpenCode says "isn't supported yet, so this run wasn't started"

**Symptoms:** An OpenCode run replies `🛑 OpenCode 2.x.y isn't supported yet, so this run wasn't started.` and nothing runs.

Untether drives the OpenCode **1.x** CLI (npm `opencode-ai`). OpenCode 2.x (npm `@opencode/cli`, whose binary is also called `opencode`) sends prompts to a shared background service outside Untether's environment allowlist, working directory, `/cancel` and stall watchdog, so Untether checks `opencode --version` before each run and refuses 2.x without starting anything ([#970](https://github.com/littlebearapps/untether/issues/970)). The chat's saved session is kept. Switch back to the 1.x CLI on that host:

```sh
npm uninstall -g @opencode/cli && npm install -g opencode-ai@1
```

The version is checked again whenever the `opencode` binary changes, so the next message runs normally — no restart needed. If the version can't be read, Untether logs `opencode.version.unknown` and runs anyway.

## Permission denied or auth errors

**Symptoms:** Engine starts but fails with authentication or permission errors.

- **Codex:** Run `codex` in a terminal and sign in with your ChatGPT account
- **Claude Code:** Run `claude login` to authenticate. On macOS, credentials are stored in Keychain; on Linux, in `~/.claude/.credentials.json`
- **OpenCode:** Run `opencode` and authenticate with your chosen provider
- **Pi:** Run `pi` and log in with your provider
- **Gemini CLI** (⚠️ deprecated): see below — individual Google accounts (free, Google AI Pro and Ultra) can no longer authenticate at all
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
`Auto-cancelled: session appears stuck (max_warnings)` — typically after about
half an hour.

Run standalone, `gemini` prints `IneligibleTierError: This client is no longer
supported for Gemini Code Assist for individuals` and exits 1. Spawned by
Untether, the subprocess instead **hangs** without exiting, so Untether sees no
events and no exit — hence the stall rather than an error message. Filed as
[#724](https://github.com/littlebearapps/untether/issues/724) and closed as
**won't fix**, because the engine is deprecated.

Gemini CLI reached **end-of-life for individual Google accounts (free, Google AI Pro and Ultra) on
18 June 2026** ([Google's announcement](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)). There is no fix — migrate to
[Antigravity CLI](https://antigravity.google) (Untether support is planned as a
separate engine in v0.36.1, [#558](https://github.com/littlebearapps/untether/issues/558)) or use a supported engine. Enterprise / Google Cloud licences
may still work, but Untether no longer verifies this.

### What to do

Both engines are deprecated and no longer supported: they are still included,
but get no fixes, are excluded from testing, and may be removed in a future
release — see
[deprecated engines](https://github.com/littlebearapps/untether#deprecated-engines).
Switch to `claude`, `codex`, `opencode`, or `pi` via `/config → Engine & model`.

## Progress stuck on "starting"

**Symptoms:** The progress message shows "starting" but never updates.

1. The engine might be doing a slow first-time setup (repo indexing, dependency install). Wait 30-60 seconds.
2. If it persists, `/cancel` (reply to the progress message) and try a more specific prompt
3. Check `debug.log` — the engine may have errored silently
4. Verify the engine works standalone: run `codex "hello"` (or equivalent) directly in a terminal

## Engine hangs in headless mode

**Symptoms:** The engine starts but produces no output, eventually triggering stall warnings. This used to happen when an engine needed user input (approval or question) but had no terminal to display it. Current Codex and OpenCode headless modes decide on their own instead of waiting (see below), so if one hangs, check the stall diagnostics and your MCP servers.

### Codex: approval hang

`codex exec` (the headless mode Untether runs) always runs with approval `never` and never waits for terminal approval, so an approval hang is not expected on any recent Codex CLI. Untether v0.36.0+ no longer passes `--ask-for-approval` at all; the Codex **safe** policy picks the read-only sandbox instead (`--sandbox read-only`). If Codex still stalls, check the stall diagnostics and your MCP servers.

### Codex: `invalid value 'untrusted'` / `is no longer supported; remove this setting`

- `error: invalid value 'untrusted' for '--ask-for-approval'` (rc=2, every run in a chat set to **Safe**): Untether before v0.36.0 passed `--ask-for-approval untrusted`, which codex-cli 0.149.0+ removed. **Fix:** upgrade Untether ([#830](https://github.com/littlebearapps/untether/issues/830)), or switch the chat to **Full auto** in `/config` → Approval policy until you can.
- `approval_policy = "untrusted" is no longer supported; remove this setting` (every Codex run, Full auto included): your own `~/.codex/config.toml` (or a `--profile` file) sets the retired value. **Fix:** remove the `approval_policy = "untrusted"` line. Untether's error hint names this case.
- A `sandbox_mode` and `default_permissions` conflict is possible if `[engines.codex] extra_args` sets `-c default_permissions=…` while the chat is in **Safe** (Safe sets the sandbox itself). Drop the `-c default_permissions` override, or use Full auto.

### Codex: a `⚠️` row in the progress message

A progress row such as `⚠️ Codex is ignoring …` is a non-fatal warning from Codex (for example a key in `~/.codex/config.toml` it doesn't recognise), not a failed step; the run carries on. Codex emits each config warning twice, and Untether shows it once ([#987](https://github.com/littlebearapps/untether/issues/987)). To make it go away, fix or remove the setting it names.

### OpenCode: unsupported event warning

If OpenCode emits a JSONL event type that Untether doesn't recognise (e.g. a `question` or `permission` event from a newer OpenCode version), Untether v0.35.0+ shows a visible warning in Telegram: "opencode emitted unsupported event: {type}". In older versions, these events were silently dropped, leaving the user with no feedback until the stall watchdog fired.

If you see this warning, check for an Untether update that adds support for the new event type. `opencode run` never waits for you: it denies the `question`, `plan_enter` and `plan_exit` tools and auto-rejects any permission your OpenCode rules set to `ask`, so this should be rare — it most likely indicates an OpenCode protocol change.

### OpenCode: a tool fails with a permission rejection

`opencode run` (the headless mode Untether runs) **auto-rejects** a tool call that your OpenCode permission rules resolve to `ask` — it does not approve it, and there are no Telegram buttons for OpenCode. The run shows the tool as failed with OpenCode's rejection text. **Fix:** change that rule to `allow` in your OpenCode config for the tools you want to run unattended. See [OpenCode runner → Permissions in `opencode run`](../reference/runners/opencode/runner.md#permissions-in-opencode-run).

## Stall warnings

**Symptoms:** Telegram shows "⏳ No progress for X min — session may be stuck" or "⏳ MCP tool running: server-name (X min)".

The stall watchdog monitors engine subprocesses for periods of inactivity (no JSONL events on stdout). Thresholds vary by context:

| Context | Threshold | Example |
|---------|-----------|---------|
| Normal (thinking/generation) | 5 min | Model is generating a response |
| Local tool running (Bash, Read, etc.) | 10 min | Long test suite or build |
| MCP tool running | 15 min | External API call (Cloudflare, GitHub, web search) |
| Child processes / subagents running | 15 min (`subagent_timeout`) | `⏳ Waiting for child processes (…)` or `⏳ Child processes idle (…)` |
| Engine process stopped (`kill -STOP`, state `T`) or blocked on I/O (state `D`) | 5 min (10 min while a tool runs) | `⏳ Engine process is stopped (state T, N min)` or `⏳ Engine process is blocked on I/O (state D, N min)` |
| Pending user approval / question | 10 min, then every 30 min | `⏳ Waiting for your approval to use Write (N min) — tap Approve or Deny above. The session is paused, not stuck.` |

The pending-approval reminder names what it's waiting for: a question reads "⏳ Waiting for your answer" (with the question underneath) and a plan reads "⏳ Waiting for you to approve the plan". The reminder is removed once you answer, and a later reminder replaces the earlier one.

For Codex, OpenCode and Pi, a child process only earns the 15 min threshold while the process tree is using CPU: Codex's npm wrapper and OpenCode's MCP servers are always there, so an idle engine falls back to 5 min ([#953](https://github.com/littlebearapps/untether/issues/953)).

**If the warning says "Engine process is stopped"**, the engine process itself has been suspended — for example by `kill -STOP` or a debugger — and won't produce anything until it is resumed. Resume it with `kill -CONT <pid>` or `/cancel` the run. A stopped engine never gets the longer child-process threshold. **"Engine process is blocked on I/O"** means it is waiting on the disk or a network filesystem.

**If the warning names an MCP tool** (e.g. "MCP tool running: cloudflare-observability"), the process is likely waiting on a slow external API. This is usually not a real stall — wait for it to complete or `/cancel` if it's taking too long.

**If the warning says "MCP tool may be hung"**, the MCP tool has been running with no new events for an extended period (3+ stall checks with a frozen event buffer). This usually means the MCP server is stuck in an internal retry loop. Use `/cancel` and retry with a more targeted prompt.

**If the warning says "CPU active, no new events"**, the process is using CPU but hasn't produced any new JSONL events for 3+ stall checks. This can happen when Claude Code is stuck in a long API call, extended thinking, or an internal retry loop. Use `/cancel` if the silence persists.

**If the warning says "Bash command still running (X min)"**, Claude Code is waiting for a long-running tool subprocess (benchmark, build, test suite). This warning fires once when the tool exceeds the threshold (10 min by default). While the child process is actively consuming CPU, repeat warnings are suppressed — you won't see the same message every 3 minutes. If the child process stops consuming CPU, warnings resume with "tool may be stuck".

**If the warning says "X tool may be stuck (N min, no CPU activity)"**, the tool subprocess has stopped consuming CPU, suggesting it may be genuinely stuck (e.g. a hung `curl`, a network timeout, a deadlock). Use `/cancel` and resume, asking Claude to skip the hung command.

**If the warning says "session may be stuck"**, the process may genuinely be stalled. Check:

1. Look at the diagnostics in the message — CPU active, TCP connections, RSS
2. If CPU is active and TCP connections exist, the process is likely still working
3. If CPU is idle and no TCP connections, the process may be truly stuck — use `/cancel`

**Tuning:** The tool, MCP and child-process thresholds are configurable via `[watchdog]` in `untether.toml` (the 5 min base and the approval reminders are fixed). Use `tool_timeout` to increase the initial threshold for local tools (default 10 min), `mcp_tool_timeout` for MCP tools (default 15 min), and `subagent_timeout` for child processes and subagents (default 15 min). See the [config reference](../reference/config.md#watchdog).

**Expected waits don't warn.** A Claude rate-limit rejection, an API retry back-off and a live session waiting between turns for background work are all silent by design, so no stall warning fires for them (see the next section, and *Messages arrive after the run finished* below). A live session's idle hold is reported separately as `peak_live_idle_seconds` in the `session.summary` log line, not as `peak_idle_seconds` ([#787](https://github.com/littlebearapps/untether/issues/787)).

## Rate-limit and API-retry notes (Claude)

**Symptoms:** The progress message shows one of these lines:

| Note | Meaning |
|---|---|
| `⚠️ 5h limit 85% used — resets 17:30` | Heads-up only. Your subscription window is nearly used up; the run keeps going. Shown at most once per window ([#790](https://github.com/littlebearapps/untether/issues/790)). |
| `⏳ Rate limited until 17:30 (~30 min)` | Claude Code reported the limit as reached and extra usage isn't covering it. Untether treats the wait as expected until the reset time, so no stall warning fires. |
| `⛔ Fable limit reached — may not clear on a timer; switch with /model or manage usage credits on claude.ai` | A cap that needs action rather than a wait: a model allowance (e.g. Fable), used-up usage credits (`Usage credits used up`), a monthly spend limit (`Monthly spend limit reached`) or a team budget (`Team budget reached`). There is no countdown; switch model with `/model`, or manage usage credits on claude.ai ([#701](https://github.com/littlebearapps/untether/issues/701), [#922](https://github.com/littlebearapps/untether/issues/922)). |
| `🔁 API error 529 (overloaded) — retrying in 8s (attempt 2/10)` | Anthropic's API returned a retryable error and Claude Code is backing off before retrying. The line updates in place for each attempt ([#792](https://github.com/littlebearapps/untether/issues/792)). |

Before v0.36.0 Untether misread Claude Code's routine usage snapshots and showed a brief `Rate limited` note every few minutes on healthy sessions. That no longer happens: routine snapshots are recorded but not shown.

Once a limit is hit, background tasks that finish afterwards don't each push the same limit error: the repeats fold into the first error message as a counter (`+2 more background wake-ups hit the same limit`, [#890](https://github.com/littlebearapps/untether/issues/890)). A failed later turn of a live session shows `live turn N` with that turn's own cost, and the whole session's spend labelled `session cost:` ([#889](https://github.com/littlebearapps/untether/issues/889)).

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

**Symptoms:** Claude Code exits after receiving tool results without processing them. You see `🔁 Auto-resuming session after upstream Claude Code event` in the chat (with ` (attempt N)` on later retries), or the session ends prematurely with no final answer.

This is an upstream Claude Code bug ([#34142](https://github.com/anthropics/claude-code/issues/34142), [#30333](https://github.com/anthropics/claude-code/issues/30333)). Untether detects it automatically and resumes the session.

**How it works:** Normal sessions end with `last_event_type=result`. When Claude Code exits with `last_event_type=user` (tool results sent but never processed), Untether posts the `🔁 Auto-resuming session …` notice and resumes the session.

**If auto-continue keeps firing:**

1. Check if the upstream bug is fixed in a newer Claude Code version: `npm i -g @anthropic-ai/claude-code@latest`
2. Disable auto-continue if it causes issues: set `enabled = false` in `[auto_continue]`
3. Increase max retries if a single retry isn't enough: set `max_retries = 2` (max 3)

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

!!! note "v0.36.0 and later"
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

    Since v0.36.0 ([#679](https://github.com/littlebearapps/untether/issues/679)) the voice note reply names the blocked host and the exact entry to add (for example ``voice transcription endpoint `localhost` is blocked by the SSRF guard …``), and the log shows `voice.base_url.ssrf_blocked` with `host`, `blocked_addresses` and `suggested_allowlist`. The same problem is logged at startup and after a hot-reload as `voice.base_url.not_permitted` (WARNING); `voice.base_url.permitted` confirms a fix. A reply saying the endpoint "could not be resolved" means a DNS failure, not the SSRF guard: check the hostname in `voice_transcription_base_url`.

Run `untether doctor` to check the voice API key. It doesn't check the transcription endpoint yet; use the startup log line above for that.

## File transfer blocked

**Symptoms:** `/file put` or `/file get` fails, or dropped documents aren't saved.

1. Check that file transfer is enabled:

    ```toml
    [transports.telegram.files]
    enabled = true
    ```

2. Check `deny_globs` — files matching these patterns are blocked. The defaults cover `.git/**`, `.env` and `.env.*` files, `.envrc`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.ssh/**`, `.netrc`, `.npmrc` and `.pypirc` at any depth, including the project root (see [Security → File transfer deny globs](security.md#file-transfer-deny-globs)). The reply names the rule that matched, for example ``path denied by rule: `**/.env.*` ``. Since v0.36.0 a project-root `.env.example` matches too ([#831](https://github.com/littlebearapps/untether/issues/831)).
3. A symlink is checked at the path it points to as well, so `cfg.txt → .env` is refused, and the reply adds `(resolves to …)` ([#390](https://github.com/littlebearapps/untether/issues/390))
4. In group chats, file transfer requires admin or creator status (unless `files.allowed_user_ids` is set)
5. Check the `uploads_dir` path exists relative to the project root

## `/browse` says "No project directory for this chat"

Since v0.36.0 `/browse` only works in a chat bound to a project (`chat_id` under `[projects.<alias>]`), or when `default_project` is set in `untether.toml`. It used to fall back to the process working directory, which under systemd is your home directory ([#389](https://github.com/littlebearapps/untether/issues/389)). Bind the chat, or set `default_project`. Hidden paths and paths matching `deny_globs` are refused with a short reply such as `Hidden paths can't be browsed.` — see [Browse files](browse-files.md#limits-and-filtering).

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
8. **A cron fired but no run started?** Each trigger first posts a `⏰ Scheduled: cron:<id>` (or `⚡ Trigger: webhook:<id>`) message. If Telegram can't be reached, Untether retries that send after 5 s and 30 s (`triggers.dispatch.send_retry`) and only then gives up (`triggers.dispatch.send_failed`) ([#758](https://github.com/littlebearapps/untether/issues/758)).
9. **Ran on the wrong engine?** A cron or webhook with a `project` and no `engine` runs on that project's default engine since v0.36.0; before, it used the global default ([#862](https://github.com/littlebearapps/untether/issues/862)). Set `engine` on the trigger to pin it.

## Config change didn't take effect

**Symptoms:** You edited `untether.toml` but the change doesn't seem to apply.

1. **Check `watch_config`:** Hot-reload requires `watch_config = true` in the top-level config. Without it, changes only apply on restart.
2. **Hot-reloadable settings** apply immediately: `voice_transcription`, `[files]`, `allowed_user_ids`, `show_resume_line`, `followup_mode`, trigger crons/webhooks/auth/timezones, plus `[progress]`, `[footer]` and most `[watchdog]` keys on the next run — see the full list in [Operations → Config hot-reload](operations.md#config-hot-reload). Some `[watchdog]` keys (`post_result_bg_max_hold`, `bg_hold_rearm_on_progress`, `bg_hold_declared_waits`, `rearm_plan_mode`) are read when a Claude session starts, so an already-open session keeps the old value until it closes.
3. **Restart-only settings** require `/restart` or `systemctl restart`: `bot_token`, `chat_id`, `session_mode`, the `[transports.telegram.topics]` table, `message_overflow`, `triggers.enabled` (off to on), `triggers.server.host`/`port`/`rate_limit`/`max_body_bytes`. Editing one of the Telegram keys or `triggers.enabled` in a running bot triggers a Telegram restart-required warning to every project chat plus any `allowed_user_ids` admin DM ([#318](https://github.com/littlebearapps/untether/issues/318)) so you won't silently keep running on the stale value.
4. Check the log for `config.reload.applied` (success), `config.reload.transport_config_changed restart_required=True` (restart needed), or `config.reload.restart_notify.sent` (Telegram warning broadcast).

## /at delay not firing

**Symptoms:** You scheduled `/at 30m Check the build` but the prompt never runs.

- Pending `/at` delays are held in memory — they are **lost on restart**. If Untether restarted after you scheduled, the delay was cancelled.
- `/cancel` (not as a reply, with no run in progress) cancels pending delays and loops together and says how many: `❌ cancelled 1 pending /at run and 2 active loops.` A Claude session that is only idling after its answer is closed on the way. If it replies `nothing running in this chat.` (or `nothing running in this chat — closed the idle session.`), there were none to cancel.
- Minimum duration: 60 seconds. Maximum: 24 hours. Values outside this range are rejected.
- Per-chat cap: 20 pending delays. The 21st is rejected with an error message.

## Session not resuming

**Symptoms:** Sending a follow-up message starts a new session instead of continuing.

- **Chat mode** (`session_mode = "chat"`): Just send another message — it auto-resumes. Use `/new` to start fresh.
- **Stateless mode** (`session_mode = "stateless"`): You must **reply** to a message that contains a resume token. Plain messages start new sessions.
- If a resumed run fails and its error card ends with `ℹ️ The saved session couldn't be resumed, so it was cleared — your next message starts a new session.`, the saved session couldn't be found (Codex `no rollout found`, OpenCode `Session not found`, Pi `No session found`) or, for Claude, the run ended with no turns. Just send your message again. For Codex, OpenCode and Pi, a resumed run that fails for an unrelated reason (a bad `/model`, a missing API key) keeps the saved session, so fix the cause and carry on in the same conversation ([#952](https://github.com/littlebearapps/untether/issues/952)).
- If resume fails silently, the previous session may be **poisoned** by an upstream turn-state bug (a resume that returns 0 turns / an empty answer). Untether detects this, quarantines that session so it is never resumed again, and automatically re-sends your message on a **fresh** session — you'll see a short notice that it did so ([#631](https://github.com/littlebearapps/untether/issues/631), [#632](https://github.com/littlebearapps/untether/issues/632)). A session force-killed after delivering its result is quarantined proactively, so your *next* message diverts fresh before any empty result appears. Since v0.36.0 sessions normally close gracefully, so this fresh-session divert is rare, and an idle session that is merely slow to exit is no longer quarantined ([#791](https://github.com/littlebearapps/untether/issues/791)).

## Follow-up message says it's "queued"

**Symptoms:** You send a follow-up (or voice note) and it shows a `queued` notice instead of running immediately.

A follow-up waits for the current Claude turn to finish; it isn't mixed into a turn that's still running. Since v0.36.0, if the session is still open (for example, background tasks are running after the reply), the message goes into **that same session** as soon as the turn ends, usually within seconds, even while the background tasks keep going ([#776](https://github.com/littlebearapps/untether/issues/776), [#647](https://github.com/littlebearapps/untether/issues/647)). The notice reads `⏳ Queued — sent as soon as Claude's current turn ends (background tasks keep running).` ([#781](https://github.com/littlebearapps/untether/issues/781)). With `[watchdog] live_sessions = false` the follow-up really does wait for the background work, and the notice says `⏳ Queued behind the previous run's N background task(s) — starts when they finish …` instead. If you see `⚠️ The session ended before this message ran — please send it again`, the session closed before your message started; resend it. If a queue is genuinely stuck, `/cancel` and resend.

## Messages arrive after the run finished (🔔 / 📡 / ⏰)

**Symptoms:** After Claude's answer, more messages appear on their own: `🔔 Background task finished — …`, `📡 Monitor — …`, `⏰ Scheduled wake-up`, or `🪝 Hook feedback`.

This is expected since v0.36.0. When Claude starts a background task, a subagent, a `Monitor` or a `ScheduleWakeup` and ends its turn, Untether keeps the session open. Claude then carries on by itself when the work finishes, and each of those turns is delivered as its own message. Before v0.36.0 these turns ran with nothing shown in Telegram. Monitor updates arrive silently (no notification); the others notify, once per finished task ([#785](https://github.com/littlebearapps/untether/issues/785)). Approval buttons work inside these turns as normal ([#776](https://github.com/littlebearapps/untether/issues/776)). `🪝 Hook feedback` is a Claude Code hook that ran in the background (for example a commit review) reporting back; the session is held open while such a hook runs, up to `[watchdog] async_hook_max_hold` ([#812](https://github.com/littlebearapps/untether/issues/812)). Short replies to finished background tasks may be folded into the background status message instead of arriving separately — see [Verbose progress → Background tasks](verbose-progress.md#background-tasks-claude).

The session stays open while background work is live, until it has shown no activity for 30 minutes (`[watchdog] post_result_bg_max_hold`) or has been open for 4 hours in total (`live_session_max_s`). Activity means a turn, a background agent's progress (every tool call it makes, including one long-running command), or new output from a background command. A working agent is therefore not stopped at 30 minutes, but a silent `sleep` or a stuck agent is — unless Claude gave the background command a longer `timeout` or a scheduled wake-up is pending: the session then stays open until that time (at most 2 hours / 1 hour; `[watchdog] bg_hold_declared_waits`, [#872](https://github.com/littlebearapps/untether/issues/872)). With no background work the session closes about a minute after the reply. When it closes over running tasks you get a notice naming them, such as `⏳ Closing session — 1 background task still running with no progress for 30 min: … Stopping it.` Once the session has stopped, a quiet follow-up tells you what happens next. `↩️ Reply to continue in the same session.` means a reply resumes the same conversation. The `⚠️ … your next message starts a fresh session` warning means the session didn't stop cleanly, so Claude won't remember that run, and partial work may be left in the working tree. `/cancel`, changing settings (`/planmode`, model, or the project/branch with `/ctx set` / `/ctx clear`, [#996](https://github.com/littlebearapps/untether/issues/996)) and Untether restarts close the session the same way, with a notice. If the session's process is killed from outside Untether while it waits (`kill`, the out-of-memory killer, a crash), you get one notice replying to the message that started the work, such as `⚠️ The Claude session ended unexpectedly (terminated by SIGTERM / exit 143) — 2 background tasks were stopped: …`, followed by the same "reply to continue" or "fresh session" line, and the background status message ends `⏹️ … stopped (session ended unexpectedly)` ([#1001](https://github.com/littlebearapps/untether/issues/1001)). To go back to the old turn-based hold, set `[watchdog] bg_hold_rearm_on_progress = false` ([#829](https://github.com/littlebearapps/untether/issues/829)).

To turn this off and get the pre-v0.36.0 behaviour back (stop at the first answer), set `live_sessions = false` under `[watchdog]`.

## "Asked for auto mode — Claude Code is running default"

**Symptoms:** The progress message shows `⚠️ Asked for auto mode — Claude Code is running default (auto mode isn't available for this model); approvals will be requested`, and Approve / Deny buttons appear in a chat set to **Auto**.

Claude Code's auto mode needs a model that supports it. On one that doesn't (for example Haiku via `/model`), Claude Code silently starts in its prompting `default` mode instead. Since v0.36.0 Untether notices, shows this line, logs `claude.permission_mode.mismatch`, and sends each permission request to Telegram rather than approving it unseen ([#751](https://github.com/littlebearapps/untether/issues/751)). Switch to a model that supports auto mode (Opus 4.6+, Sonnet 4.6+, Fable 5), or pick a different mode with `/planmode`.

## A cron or webhook run stopped with a plan, or shows `🔒 unattended … denied`

**Symptoms:** A scheduled or webhook-started Claude run ends with a plan or a short report instead of doing the work, and its final carries a line like `🔒 unattended (cron:<id>) · denied Write ×2 — nobody to approve`.

Since v0.36.0 an unattended run never waits on an approval nobody can tap ([#835](https://github.com/littlebearapps/untether/issues/835)). Anything it would have asked about (a tool its mode asks about, a plan approval, a question) is denied straight away and logged as `permission.unattended_deny`. A cron with no `permission_mode` takes the chat's `/planmode` and then `[engines.claude] permission_mode`, so a cron that lands on `plan` stops at the plan. Set an explicit `permission_mode` (`plan-auto`, `auto` or `bypassPermissions`) on every Claude cron that should act on its own; a webhook follows its chat's `/planmode`. The startup message lists the crons that will hit this (`unattended approvals (auto-denied): …`). To carry on by hand, reply to the run's message: the reply runs in an attended session with normal buttons. See [Webhooks and cron](webhooks-and-cron.md).

## "🛡️ safeguards stopped a response"

**Symptoms:** A run shows a `🛡️ … safeguards stopped a response · retried once` row (or `switched to <model>` / `not retried`), and the final carries a matching footer line.

Anthropic's safeguards stopped one of Claude's responses. Claude Code may retry once or switch to a fallback model; Untether reports which happened ([#814](https://github.com/littlebearapps/untether/issues/814)). The first time a session hits one, the footer links to guidance. If nothing was retried and no answer came back, the final explains that instead of showing an empty error. Rephrasing the request usually helps.

## Tapping an old approval button

**Symptoms:** A button tap shows a short popup instead of doing anything.

| Popup | Meaning |
|---|---|
| `Already answered` | Someone already answered this request. A silent `ℹ️ Already answered — <what the first tap did>` line follows; nothing more is sent to Claude Code ([#685](https://github.com/littlebearapps/untether/issues/685)). |
| `No longer needed` | Claude Code withdrew the request (for example the turn ended) ([#684](https://github.com/littlebearapps/untether/issues/684)). |
| `This request has expired` | The request timed out, or Untether no longer knows about it (for example after a restart). |

A tap that lands while a request is being withdrawn is answered with `⏹️ Claude Code withdrew this request — nothing to answer.`

## An engine "failed to load" after editing `extra_args`

**Symptoms:** The startup message lists an engine under `failed to load:`, chats using it reply that the engine is unavailable, or Untether won't start at all.

Since v0.36.0 `[engines.claude]` / `[engines.codex]` `extra_args` may not carry flags that bypass approvals or the sandbox (`--dangerously-skip-permissions`, `--allowedTools`, `--yolo`, `--sandbox danger-full-access`, …) or flags Untether manages itself ([#209](https://github.com/littlebearapps/untether/issues/209)). The error names the flag. Remove it, or use the dedicated key (`allowed_tools`, `permission_mode`). See [Security → Engine CLI flags](security.md#engine-cli-flags-extra_args).

## "🗑️ Dropped N messages sent just before /new"

Messages that arrive within about a second of each other are merged into one prompt. If a `/cancel`, `/new` or `/continue` lands in that window, the waiting messages are dropped rather than run in the wrong session, and Untether says so ([#807](https://github.com/littlebearapps/untether/issues/807)). Send them again if you still need them. See [Chat sessions → Sending several messages quickly](chat-sessions.md#sending-several-messages-quickly).

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

## Cost budget alerts

**Symptoms:** A `🛑 … exceeded … budget` line (or a `🛑 budget` / `⚠️ 73%` suffix on the `💰` cost line) on a final message.

Budgets are checked when a result arrives, so on their own they alert rather than stop runs. With `auto_cancel = true` (**Stop at limit**) new runs are refused once the daily budget is reached (`🛑 Daily budget reached …`, with a **Run anyway** button) and a live session ends after the reply that passes the per-run budget (`🛑 Stopped: …`); see [Stop at limit](cost-budgets.md#stop-at-limit). Nothing budget-related cuts a reply mid-stream: a run stopped mid-stream was cancelled by something else (`/cancel`, the stall watchdog, a restart).

1. Check your budget settings:

    ```toml
    [cost_budget]
    enabled = true
    max_cost_per_run = 2.00      # USD per run
    max_cost_per_day = 20.00     # USD per day
    warn_at_pct = 70             # early warning threshold
    ```

2. Daily budgets reset at midnight in the server's local time zone. The day's total survives restarts (`daily_cost.json` next to `untether.toml`)
3. To temporarily bypass: set `enabled = false` or increase the limits
4. Check today's spend with `/health` (`today's API cost`); `/usage` shows Claude subscription usage, not spend

## Group chat: bot ignoring messages

**Symptoms:** Bot works in private chat but ignores messages in a group.

1. Check **listen mode**: the default is `all`, but a chat or topic may have been set to `mentions`. Send `/listen` to check, or `/listen all` to respond to everything. (`/trigger` still works as a deprecated alias from v0.35.3 onward.)
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

Run `untether doctor` for a preflight check of your Telegram setup:

```sh
untether doctor
```

It checks:

- Telegram bot token (connects and fetches the bot's name)
- Chat ID (the bot can fetch the chat)
- Topics configuration (supergroup, topics enabled, bot is an admin)
- File transfer (enabled, and whether it's restricted to `allowed_user_ids`)
- Voice transcription (an API key is set; the endpoint isn't contacted)

```
$ untether doctor
untether doctor
- telegram token: ok (@my_untether_bot)
- chat_id: ok (supergroup (-1001234567890))
- topics: ok (scope=main)
- file transfer: ok (restricted to 1 user id(s))
- voice transcription: ok (OPENAI_API_KEY set)
```

There's no summary line: any `error` row makes the command exit with status 1. Engine CLIs aren't checked; the startup message lists engines that are `not installed`, `misconfigured` or `failed to load`.

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
| `file_transfer.denied` | WARNING | File transfer refused for this user (not allowlisted, not a group admin) |
| `file_transfer.path_denied` | INFO | Path refused by a deny glob or hidden-path rule (WARNING when a symlink was involved) |
| `browse.path_denied` | INFO | `/browse` refused a hidden or deny-globbed path |
| `claude.permission_mode.mismatch` | WARNING | Claude Code started in a different permission mode from the one requested |
| `trigger.unattended_approval_risk` | WARNING | A cron or webhook will run in a mode that asks for approval — those requests will be denied (`outcome=denied`) |
| `permission.unattended_deny` | WARNING | An unattended (cron / webhook) Claude run denied a request nobody could approve — set the trigger's `permission_mode` ([#835](https://github.com/littlebearapps/untether/issues/835)) |
| `cost.run_outlier` | WARNING | A single run cost more than `[cost_budget] warn_run_above_usd` (default $20) |
| `message.dropped` | DEBUG | Message from unrecognised chat silently dropped |
| `cost_budget.exceeded` | ERROR | Run or daily cost exceeded budget |
| `cost_budget.run_blocked` | WARNING | Stop at limit refused a new run (`scope=per_day`; `trigger` names a skipped cron/webhook) |
| `cost_budget.run_anyway` | WARNING | Someone tapped **Run anyway** past the daily budget |
| `cost_budget.run_stopped` | WARNING | Stop at limit ended a live session after the reply that passed the budget (`scope=per_run` or `per_day`) |
| `callback.not_originator` / `ask_user_question.not_originator` | WARNING | With `approval_originator_only = true`, someone other than the run's originator tried to answer an approval or question ([#388](https://github.com/littlebearapps/untether/issues/388)) |
| `opencode.version.unsupported` | ERROR | The installed OpenCode CLI is 2.x; the run was refused before starting ([#970](https://github.com/littlebearapps/untether/issues/970)) |
| `session.auto_clear_skipped` | INFO | A resumed Codex/OpenCode/Pi run failed for a reason other than the resume, so the saved session was kept (`reason=not_resume_failure`, [#952](https://github.com/littlebearapps/untether/issues/952)) |
| `final.early_delivery_timeout` | WARNING | A live session's first answer took over 60 s to hand to Telegram ([#928](https://github.com/littlebearapps/untether/issues/928)) |

All logs include `session_id` once a session starts, enabling per-session filtering with `grep` or `jq`.

Telegram failures (`telegram.http_error`, `telegram.api_error`, `telegram.network_error`, `telegram.benign_rejection`, `outbox.op.failed`, `transport.send.failed` / `edit.failed` / `delete.failed`) carry a `kind` field naming the message they were for — `progress`, `final`, `bg_status`, `approval_surface`, `approval_notify`, `approval_reminder`, `outline` or `ephemeral` (`null` when untagged) ([#823](https://github.com/littlebearapps/untether/issues/823)).

Telegram bot tokens, OpenAI API keys (`sk-...`), GitHub tokens (`ghp_`, `ghs_`, `github_pat_`), `Authorization:`/`Bearer` credentials, JWTs, and `api_key=`/`token=`/`secret=`/`password=` values are automatically redacted in all log output. Token *counts* such as `total_tokens=52000` are left alone.

## Error hints

When an engine fails, Untether scans the error message and shows an actionable recovery hint above the raw error. The raw error is wrapped in a code block for visual separation. Hints are case-insensitive and pattern-matched — the first match wins. Your session is automatically saved in most cases, so you can resume after resolving the issue.

Untether recognises **77 error patterns**, grouped like this:

| Category | Examples | Engines |
|----------|----------|---------|
| Unsupported client | Engine refuses this client version, Gemini individual-tier end-of-life | AMP, Gemini |
| CLI flag and config drift | A flag the installed CLI rejects, a retired Codex config value | Codex |
| Authentication | API key missing/invalid, token refresh, login required | All |
| Subscription & billing | Usage limits, quota exceeded, billing hard limit | Claude, Codex, OpenCode, Gemini |
| API overload & server | 500/502/503/504, overloaded | All |
| Rate limits | Rate limited, too many requests | All |
| Model errors | Model not found, invalid model | All |
| Context length | Context too long, max tokens exceeded | Claude, Codex, OpenCode |
| Content safety | Content filter, safety block, prompt blocked | Claude, Gemini |
| Reasoning level | `reasoning.effort` not supported by the model or with web search | Codex |
| Invalid request | Malformed API request | Claude, Codex |
| Network & SSL | DNS, timeout, connection refused, certificate errors | All |
| CLI & filesystem | Command not found, disk full, permission denied | All |
| Signals | SIGTERM, SIGKILL, SIGABRT | All |
| Process & session | No result event, no session ID, execution errors | All |
| Engine-specific | AMP credits/login, Gemini result status | AMP, Gemini |
| Account & proxy | Account suspended, proxy auth, request timeout | All |

For the full list of patterns and hints, see the [Error Reference](../reference/errors.md).

## Loop didn't fire / loop fired too many times

Loop mode (`/config → 🔁 Loop mode`) decides whether Claude can schedule repeated runs. With it on, Untether runs Claude Code's `/loop` schedules itself, with the `[loop]` caps; with it off, Claude can't schedule recurring or timed tasks. ([#289](https://github.com/littlebearapps/untether/issues/289), [#925](https://github.com/littlebearapps/untether/issues/925))

| Symptom | Likely cause | Fix |
|---|---|---|
| Claude says scheduling is off, or points you to Loop mode or `/at` | Loop mode is off (the default), so Untether declined Claude's `CronCreate` | `/config → 🔁 Loop mode → 🔁 On`, then ask again; for a one-off delay use `/at <delay> <prompt>` |
| Loop stopped after 20 iterations | Hit the iteration cap (`[loop] max_iterations`, 20 by default) | Restart the loop with a fresh `/loop`, or raise `max_iterations` in `untether.toml` |
| `⏰ Self-paced wake-up limit reached` and the session closed | A chain of self-paced wake-ups (`ScheduleWakeup`, a dynamic `/loop`) hit `[loop] max_iterations` in a row, in either Loop mode | Send a message to carry on (it resets the count), or raise `max_iterations` |
| A loop fired inside an open session, ignored the caps, or came back after a resume | Untether older than v0.36.0, or `[loop] own_schedule = false`: Claude Code's own scheduler ran the task | Upgrade and leave `own_schedule` at its default (`true`); `/new` starts a session that can't bring the old task back |
| Loop stopped after 4 hours, or a week after it was created | Hit the wall-clock cap (`max_total_duration_hours`, 4 h) or the expiry (`expiry_days`, 7) | Restart the loop with a fresh `/loop` |
| Loop fire refused with `🛑 Daily budget reached` | **Stop at limit** is on and the daily budget is spent | Raise `[cost_budget] max_cost_per_day` in `untether.toml`, or wait for the daily reset |
| Loop fires happened but each was a "fresh user turn" rather than autonomous | This is by design — Untether re-issues the original prompt at each fire (see [Schedule tasks → Loop mode](schedule-tasks.md#loop-mode)) | N/A — expected behaviour |
| Loop kept firing after `/cancel` | Before v0.36.0 a Claude session idling after its answer, or a pending `/at` run, could stop `/cancel` from reaching the loops ([#902](https://github.com/littlebearapps/untether/issues/902)), and before v0.36.0 a cancelled loop could come back with your next message ([#926](https://github.com/littlebearapps/untether/issues/926)). A `/cancel` sent while a run is in progress only cancels that run | Upgrade, then send `/cancel` again once nothing is running: it drops `/at` runs and loops together |
| Loop didn't survive a restart | `active_loops.json` is missing or corrupt | Check `journalctl --user -u untether -f` for `loop.restore.read_failed` warnings; the file lives next to your `untether.toml` |

## Related

- [Operations and monitoring](operations.md) — `/ping`, `/restart`, hot-reload
- [Configuration reference](../reference/config.md) — all config options
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
