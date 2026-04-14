# Integration Testing

Structured, repeatable integration test process run against `@untether_dev_bot` before every release. Tests exercise all 6 engines across the full feature surface.

## Infrastructure

| | Details |
|---|---|
| **Dev service** | `untether-dev.service` → `@untether_dev_bot` |
| **Test projects** | `test-projects/test-{claude,codex,opencode,pi,gemini,amp}/` |
| **Test chats** | 6 dedicated Telegram groups in the `ut-dev` folder, one per engine |
| **Engines** | Claude, Codex, OpenCode, Pi, Gemini, Amp |

## Automated Testing via Telegram MCP

All integration test tiers are fully automated by Claude Code using Telegram MCP tools and the Bash tool. The relevant MCP tools are:

- `send_message` — send test prompts and commands to engine chats
- `get_history` / `get_messages` — read back bot responses and verify expected behaviour
- `list_inline_buttons` — inspect inline keyboards (approval buttons, `/config` menus, `/browse`)
- `press_inline_button` — interact with inline keyboards (approve/deny, toggle settings)
- `reply_to_message` — reply to resume lines for session continuation tests (U4)

### Test chats

Tests are sent to 6 dedicated engine chats via `@untether_dev_bot` (bot ID `8678330610`).
For DM-only tests (commands, `/at`, `/cancel`), use the Nathan DM chat ID `8678330610`.

| Chat | Chat ID | Bot API chat_id |
|------|---------|-----------------|
| Claude Code | `5284581592` | `-5284581592` |
| Codex CLI | `4929463515` | `-4929463515` |
| OpenCode | `5200822877` | `-5200822877` |
| Pi | `5156256333` | `-5156256333` |
| Gemini CLI | `5207762142` | `-5207762142` |
| AMP CLI | `5230875989` | `-5230875989` |

> **Note:** The Telegram MCP (Telethon) accepts both positive and negative chat IDs.
> If a positive ID fails with `GEN-ERR-582` (PeerUser lookup), use the negative Bot API form.
> A local fix in `resolve_entity()` auto-retries with the negative form (applied 2026-04-14).

### Workflow

1. Claude Code sends a test prompt via `send_message` to the appropriate engine chat
2. Waits for the bot to process (sleep or poll via `get_history`)
3. Reads back the response via `get_history`/`get_messages` and verifies expected content
4. For interactive tests: uses `list_inline_buttons` and `press_inline_button` to interact with approval/config buttons
5. For resume tests: uses `reply_to_message` to reply to the resume line

### Additional MCP tools for media tests

- `send_voice` — send an OGG/Opus voice file as a voice message (for T1)
- `send_file` — send a file with optional caption (for T2, T3, T5)

### Log inspection and issue creation

After running integration tests, Claude Code MUST:

1. **Check dev bot logs** via Bash tool: `journalctl --user -u untether-dev --since "1 hour ago" | grep -E "WARNING|ERROR"`
2. **Check for zombies/FD leaks**: `ps aux | grep defunct`, FD count via `/proc/<pid>/fd`
3. **Track test results**: for each test, note pass/fail/error with reason. Distinguish between Untether bugs and upstream engine API errors (e.g. authentication failures, rate limits, engine-side crashes)
4. **Create GitHub issues** via GitHub MCP for any Untether bugs discovered during testing — engine API errors are not Untether bugs unless Untether handles them poorly (crashes, hangs, no error message)

### Tests with special tooling

These tests were previously considered "manual" but can be automated via MCP and Bash:

- **T1 (voice message)** — use `send_voice` with a pre-recorded OGG/Opus test file
- **T5 (media group)** — use `send_file` to send multiple files rapidly (may not trigger media group coalescing depending on Telegram API batching)
- **B4 (SIGTERM drain)** — use Bash tool: `kill -TERM $(pgrep -f '.venv/bin/untether')`
- **B5 (log inspection)** — use Bash tool: `journalctl --user -u untether-dev --since "1 hour ago"`

## Engine Feature Matrix

| Capability | Claude | Codex | OpenCode | Pi | Gemini | Amp |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Interactive approval | Yes | - | - | - | Flag only | - |
| Plan mode | Yes | - | - | - | - | - |
| Ask questions | Yes | - | - | - | - | - |
| Resume/continue | Yes | Yes | Yes | Yes | Yes | Yes |
| Model override | Yes | Yes | Yes | Yes | Yes | Yes |
| Reasoning levels | Yes | Yes | - | - | - | - |
| API cost tracking | Yes | - | Yes | - | Yes | Yes |
| Subscription usage | Yes | - | - | - | - | - |
| Diff preview | Yes | - | - | - | - | - |

---

## Test Tiers

### Tier 1: Universal Tests (all 6 engines)

Run in every engine's dedicated chat. Validates the core event pipeline.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| U1 | **Basic prompt** | `create a file called hello.txt with "hello world"` | Progress messages appear, final answer renders, footer shows model name, resume line present | #62 (missing model), #65 (footer repeat), stream threading (#98) |
| U2 | **Multi-tool prompt** | `list the files in this directory, then read the README if one exists` | Multiple action phases show in progress, tool names visible in verbose mode | Event counting, action tracking |
| U3 | **Long response** | `write a detailed explanation of how TCP/IP works, at least 2000 words` | Message splits correctly across multiple Telegram messages, no truncation, footer only on last chunk | #65 (footer repeat), #59 (entity overflow), message splitting |
| U4 | **Resume session** | After U1 completes, reply to the resume line: `now rename hello.txt to greetings.txt` | Resume token works, session continues, new progress + final answer | Resume token parsing per engine |
| U5 | **Model override** | Via `/config` → Model → set a different model, then send a prompt | Footer shows overridden model name | #77 (AMP model flag), build_args correctness |
| U6 | **Cancel mid-run** | Send a long prompt, then `/cancel` before it finishes | Run stops, completion message appears, no orphan process | Graceful cancellation, process cleanup |
| U7 | **Error handling** | Send a prompt that will fail (e.g. `read /nonexistent/file/path`) | Error renders in Telegram, no crash, session ends cleanly | Stderr sanitisation (#85), error formatting |
| U8 | **/usage** | `/usage` after a completed run | Shows cost or subscription info (engine-dependent) | #89 (429 handling), cost tracking |
| U9 | **/export** | `/export` after a completed run | Markdown export downloads, contains prompt and response | #63 (missing usage in export) |
| U10 | **/browse** | `/browse` | File browser appears with inline keyboard, can navigate directories | Browse command, path traversal safety |

### Tier 2: Claude-Specific Tests (interactive features)

Run in the Claude test chat only. Requires plan mode ON for most tests.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| C1 | **Tool approval** | Send a prompt requiring Bash (e.g. `run ls -la`), with plan mode ON | Approve/Deny/Discuss buttons appear, clicking Approve proceeds, tool executes | #104 (buttons not appearing), #103 (progress stuck) |
| C2 | **Tool denial** | Same as C1, click Deny | Denial message reaches Claude, Claude acknowledges and continues | #66 (deny retry loop) |
| C3 | **Plan mode outline** | Send a complex prompt, click "Pause & Outline Plan" | Claude writes outline, then Approve/Deny/Let's discuss buttons appear automatically | Cooldown mechanics (#87), post-outline approval |
| C4 | **Ask question** | Send a prompt that triggers AskUserQuestion (e.g. `should I use TypeScript or JavaScript for this?`) | Question appears with option buttons, user reply routes back to Claude | AskUserQuestion flow |
| C5 | **Diff preview** | With plan mode ON, send a prompt that edits a file | Diff preview shows in approval message (old/new lines) | Diff preview rendering |
| C6 | **Rapid approve/deny** | Approve a tool, then quickly deny the next one | No spinner hang, no stale buttons, clean state transitions | Early callback answering, button cleanup |
| C7 | **Subscription usage** | `/usage` with subscription footer enabled | Shows 5h/weekly format | Subscription footer rendering |

### Tier 3: Telegram Transport Tests

Tests specific to how Untether uses Telegram — message formatting, media, input types. Run in any engine chat unless noted.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| T1 | **Voice message** | Record and send a voice note as prompt | Transcription appears, prompt runs, response renders | Voice transcription pipeline, codec handling |
| T2 | **File upload** | Send a file with caption `/file put src/test.txt` | File appears in project directory, confirmation message | File transfer, path safety, size limits |
| T3 | **File download** | `/file get README.md` | File downloads to Telegram chat | File serving, MIME types |
| T4 | **Forward coalescing** | Forward 3 messages rapidly from another chat | Messages combined into single prompt, one run starts (not three) | `forward_coalesce_s` debounce, metadata annotation |
| T5 | **Media group** | Send 3+ images/files at once (shift-click to batch) | Bundled as single upload batch, not 3 separate runs. **Note:** MCP `send_file` sends individual documents, not Telegram albums — true media group coalescing requires the Telegram client's batch-send. MCP tests verify file handling and no-crash behaviour. | `media_group_debounce_s`, auto-put mode |
| T6 | **Emoji in response** | `respond with 5 different emoji flags and bold the country names` | Entities render correctly, no offset corruption | UTF-16 entity offsets (emoji = 2 code units, not 1 Python codepoint) |
| T7 | **Code block splitting** | `write a 200-line Python script` | Code blocks split cleanly across messages, syntax highlighting preserved | Entity boundary splitting, pre/code nesting rules |
| T8 | **Stale button click** | Wait for a session to complete + clean up, then click an old Approve button | Toast "Expired" or similar, no crash, no spinner hang | Stale callback_data, cleaned-up session registry |
| T9 | **Directive routing** | `/codex list the files here` (in Claude chat) | Codex runs instead of Claude, correct project context | Directive parsing, engine override |
| T10 | **Branch directive** | `/claude @develop create hello.txt` | Run uses `develop` branch, not default | Branch directive, context resolution |

### Tier 4: Configuration and Overrides

Tests for per-chat and per-topic settings that affect run behaviour. Use forum topics if available.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| O1 | **Engine override** | `/agent set gemini`, then send a plain prompt (no directive) | Gemini runs, footer shows Gemini model | Per-chat engine default, override hierarchy |
| O2 | **Reasoning level** | `/config` → Reasoning → enable, then send a prompt | Reasoning model used, footer reflects it | Reasoning flag in build_args |
| O3 | **Trigger mode** | `/trigger mentions` in group, send plain text, then `@bot do something` | Plain text ignored, @mention triggers run | Trigger mode filtering |
| O4 | **Ask mode toggle** | `/config` → Ask → off, send prompt that would trigger AskUserQuestion | Question auto-denied instead of shown | Ask mode auto-deny path |
| O5 | **Context set** | `/ctx set test-claude main`, send prompt | Run uses test-claude project on main branch | Context resolution, project switching |
| O6 | **Context clear** | `/ctx clear`, send prompt | Falls back to chat/project default | Context fallback chain |
| O7 | **Chat session mode** | Set `session_mode = "chat"` in config, restart dev bot, send prompt 1, then prompt 2 (no reply) | Prompt 2 continues same session without needing resume reply | Stateful session mode |
| O8 | **Override persistence** | Set `/agent set pi`, restart dev bot, send prompt | Pi still runs — override survived restart | State file persistence |
| O9 | **Override clear** | `/agent clear`, send prompt | Falls back to project/global default engine | Override cleanup |

### Tier 5: Cost, Budget, and Operational

Tests for cost tracking, budget enforcement, and operational commands.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| B1 | **Budget auto-cancel** | Set `max_cost_per_run = 0.01` in config, restart, send expensive prompt | Run auto-cancels with budget warning message | Cost tracker, auto-cancel flag |
| B2 | **Daily budget warning** | Set `max_cost_per_day = 0.05`, run several cheap prompts | Warning appears when approaching threshold | Daily accumulation, warn_at_pct |
| B3 | **/stats** | Run several prompts across engines, then `/stats` | Per-engine run counts, action counts, durations render | Stats aggregation |
| B4 | **SIGTERM drain** | Start a run, then `kill -TERM $(pidof untether)` from shell | Active run drains, completion message sent, bot exits cleanly | Signal handling, graceful shutdown |
| B5 | **Log inspection** | After running several tests, check structured logs | No unhandled exceptions, no FD leak warnings, no zombie processes | Operational health |

### Tier 6: Stress and Edge Cases

Harder to trigger but catches the most production bugs.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| S1 | **Stall detection** | Send a prompt likely to take >5 minutes, or `kill -STOP` the engine process. For MCP tool threshold: send a prompt that triggers a slow MCP tool (e.g. Cloudflare observability query) | Stall warning appears in Telegram after threshold; MCP tool stalls show "MCP tool running: {server}" instead of "session may be stuck"; `/proc` diagnostics available | #95 (stall not detected), #97 (no diagnostics), #99 (stall loops), #105 (stall during tools), #154 (MCP tool threshold) |
| S2 | **Concurrent sessions** | Send prompts in two different engine chats simultaneously | Both run independently, no cross-contamination, both complete | Session isolation |
| S3 | **Bot restart mid-run** | Start a run, then `/restart` | Active run drains gracefully, bot restarts, can start new runs | Graceful restart, drain logic |
| S4 | **Verbose mode** | `/verbose` on, then send a prompt | Progress shows tool details (file paths, commands, patterns) | Verbose rendering |
| S5 | **Config persistence** | Toggle settings via `/config`, restart dev bot, verify settings stick | Settings survive restart | State file persistence |
| S6 | **Empty/whitespace prompt** | Send just spaces or an empty forward | Bot handles gracefully, no crash | Input validation |
| S7 | **Rapid-fire prompts** | Send 5 messages in quick succession to same chat | Only one run starts (or queues), no double-spawn, no crash | Race condition, session locking |
| S8 | **Very long prompt** | Paste 4000+ characters as a single message | Prompt reaches engine intact, no truncation | Telegram message limits, prompt forwarding |
| S9 | **Concurrent button clicks** | Two rapid clicks on the same Approve button | Only one approval processed, second gets toast, no double-execute | Callback deduplication |

### Tier 7: Command Smoke Tests (quick, any engine)

Run quickly to verify all commands respond.

| # | Command | Expected | Time |
|---|---------|----------|------|
| Q1 | `/ping` | Pong + uptime | 1s |
| Q2 | `/config` | Settings menu with buttons | 1s |
| Q3 | `/usage` | Usage info or "no session" | 1s |
| Q4 | `/export` | Export or "no session" | 1s |
| Q5 | `/browse` | File browser | 1s |
| Q6 | `/verbose` | Toggle confirmation | 1s |
| Q7 | `/cancel` | "Nothing running" or cancels | 1s |
| Q8 | `/planmode` (Claude chat) | Mode toggle | 1s |
| Q9 | `/stats` | Session statistics or empty | 1s |
| Q10 | `/ctx` | Current context or "none set" | 1s |
| Q11 | `/agent` | Current engine override or default | 1s |
| Q12 | `/trigger` | Current trigger mode | 1s |
| Q13 | `/file` | Usage help or file browser | 1s |
| Q14 | `/at 60s smoke test` | "⏳ Scheduled" confirmation; run fires after ~60s | 70s |
| Q15 | `/at 5m test` then `/cancel` | Scheduling confirmation; cancel drops pending; no run after 5m | 10s (skip 5m wait) |
| Q16 | `/ping` in chat with cron | Pong + `⏰ triggers: ... cron (...)` line appears | 1s |

---

## rc4 scenarios (v0.35.1rc4)

Run these in addition to the standard tiers for rc4.

| # | Scenario | Expected |
|---|----------|----------|
| R1 | **Hot-reload cron add** | Edit `~/.untether-dev/untether.toml` to add a `* * * * *` cron; no restart; wait 60s | New cron fires at next minute; `triggers.manager.updated` log line present |
| R2 | **Hot-reload webhook add** | Add a new `[[triggers.webhooks]]` entry; curl the new path | Returns 202; run dispatched to the configured chat |
| R3 | **Hot-reload webhook secret change** | Change `secret` on existing webhook; curl with old secret | 401; new secret returns 202 |
| R4 | **`run_once` cron** | Add `run_once = true` cron with `* * * * *` | Fires once, skips next minute, `triggers.cron.run_once_completed` log line |
| R5 | **Trigger source in footer** | Trigger a cron run | Final message footer shows `⏰ cron:<id>` next to model |
| R6 | **Bridge voice hot-reload** | Toggle `voice_transcription = false` in TOML; send a voice note | Not transcribed; `config.reload.transport_config_hot_reloaded` log line with `keys=['voice_transcription']` |
| R7 | **Bridge allowed_user_ids hot-reload** | Add a new user id to `allowed_user_ids`; have that user send a message | Message routed on the next message (no restart) |
| R8 | **update_id persistence** | `systemctl --user restart untether-dev` mid-conversation | Startup log `startup.offset.resumed`; no duplicate processing of pre-restart messages |
| R9 | **sd_notify READY=1** | `systemctl --user status untether-dev` after start | "Active: active (running)" only appears after READY=1 |
| R10 | **sd_notify STOPPING=1 during drain** | `systemctl --user restart untether-dev` while a run is active | journalctl shows `sdnotify.stopping` before `shutdown.draining` |

---

## Upgrade Path Testing

Run before **minor and major** releases to verify backward compatibility.

### Config compatibility

```bash
# Save current staging config
cp ~/.untether/untether.toml /tmp/staging-config-backup.toml

# Test current code parses old config without error
UNTETHER_CONFIG=/tmp/prod-config-backup.toml uv run python -c "from untether.settings import load; load()"

# Verify new config keys have defaults (old configs missing them still work)
diff ~/.untether/untether.toml ~/.untether-dev/untether.toml
```

### Rollback safety

```bash
# Before releasing: verify the previous version still installs and starts
pip install untether==$CURRENT_PROD_VERSION --dry-run

# After release: if issues found, rollback path is:
# pipx install untether==$OLD_VERSION && systemctl --user restart untether
```

### State file compatibility

If any state files exist (chat preferences, topic state), verify they survive upgrade:

```bash
# Check state files before upgrade
ls -la ~/.untether-dev/state/

# After restart with new code, verify no parse errors in logs
journalctl --user -u untether-dev --since "1 minute ago" | grep -iE "error|parse|corrupt"
```

---

## Execution Process

Integration tests are run by Claude Code via Telegram MCP tools (see "Automated Testing via Telegram MCP" above). Claude Code sends prompts and commands to the `ut-dev:` engine chats, reads back responses, interacts with inline buttons, and verifies expected behaviour. Voice messages (T1) use `send_voice`, file tests use `send_file`, SIGTERM (B4) and log inspection (B5) use the Bash tool. All tiers are fully automatable by Claude Code.

### Before every version bump

```
1. Code changes complete, unit tests pass
   uv run pytest && uv run ruff check src/ && uv run ruff format --check src/ tests/

2. Restart dev bot
   systemctl --user restart untether-dev

3. Tail logs in a separate terminal
   journalctl --user -u untether-dev -f

4. Run Tier 7 (command smoke) — 2 minutes
   Claude Code sends each command to an engine chat via MCP, verifies responses

5. Run Tier 1 (universal) — 30 minutes
   Claude Code runs U1-U10 in ALL 6 engine chats via MCP
   Focus on: progress rendering, final message, model footer, resume

6. Run Tier 2 (Claude-specific) — 15 minutes
   Claude Code runs C1-C7 in Claude test chat with plan mode ON
   Uses list_inline_buttons/press_inline_button for approval tests

7. Run Tier 3 (Telegram transport) — 15 minutes
   Run T1-T10 based on what changed. Always run T6 (emoji) and T8 (stale buttons)
   T1 (voice) uses send_voice, T5 (media group) uses send_file

8. Run Tier 4 (overrides) — 10 minutes
   Run O1-O9 if config/override code changed. Always run O1 and O8

9. Run Tier 5 (cost/operational) — 5 minutes
   Run B1-B3 if cost tracking changed. B4 (SIGTERM) and B5 (logs) require shell access

10. Run Tier 6 (stress) — 15 minutes
    Pick 2-3 stress tests based on what changed:
    - Bug fix release → S1 (stall), S2 (concurrent), S7 (rapid-fire)
    - New feature → S4 (verbose), S5 (config persistence)
    - Major change → all of S1-S9

11. Run upgrade path tests (minor/major only) — 5 minutes
    Config compatibility, state file compatibility

12. Check logs for warnings/errors (via Bash tool)
    journalctl --user -u untether-dev --since "1 hour ago" | grep -E "WARNING|ERROR"
    Check FD count and zombie processes
    Create GitHub issues for any Untether bugs found

13. Report results: list each test as pass/fail/error with reason
    Distinguish Untether bugs from upstream engine API errors

14. If all pass: commit, tag, release
```

### Per release type

| Release type | Required tiers | Focus areas | Time |
|-------------|---------------|-------------|------|
| **Patch** (bug fix) | Tier 7 + Tier 1 (affected engine + Claude) + relevant Tier 6 | The specific bug area + regression check | ~30 min |
| **Minor** (new feature) | Tier 7 + Tier 1 (all) + Tier 2 + Tier 3 (relevant) + Tier 4 (relevant) + Tier 6 + upgrade path | New feature + all engine regression + config compat | ~75 min |
| **Major** (breaking) | All tiers, all engines, full upgrade path | Everything — no shortcuts | ~120 min |

### What to focus on per change type

| Changed area | Must-run tests |
|---|---|
| Runner code (`runners/*.py`) | U1-U4 (all engines), U6, U7 |
| Telegram transport (`telegram/*.py`) | T1-T10, S7, S8 |
| Control channel (`claude_control.py`) | C1-C6, T8, S9 |
| Config/settings (`settings.py`) | O1-O9, S5, upgrade path |
| Cost tracking (`cost_tracker.py`) | B1-B3, U8 |
| Progress/formatting (`markdown.py`) | U3, T6, T7, S4, S8 |
| Commands (`commands/*.py`) | Tier 7 (all), specific command test |
| File transfer (`file_transfer.py`) | T2, T3, T5 |
| Voice (`voice.py`) | T1 |
| Topics (`topics.py`, `topic_state.py`) | O1, O5, O6, O8 |
| Directives (`directives.py`) | T9, T10 |
| Shutdown (`shutdown.py`) | S3, B4 |

---

## Quick Reference

### Common test prompts

```
# U1 — basic prompt (all engines)
create a file called hello.txt with "hello world"

# U2 — multi-tool (all engines)
list the files in this directory, then read the README if one exists

# U3 — long response (all engines)
write a detailed explanation of how TCP/IP works, at least 2000 words

# U4 — resume (reply to resume line after U1)
now rename hello.txt to greetings.txt

# U7 — error handling (all engines)
read /nonexistent/file/path

# C1 — tool approval (Claude, plan mode ON)
run ls -la

# C4 — ask question (Claude)
should I use TypeScript or JavaScript for this?

# T6 — emoji entities
respond with 5 different emoji flags and bold the country names

# T9 — directive routing (send in Claude chat)
/codex list the files here

# S8 — long prompt
[paste 4000+ characters of text]
```

### Log inspection

```bash
# Tail dev bot logs
journalctl --user -u untether-dev -f

# Recent warnings/errors
journalctl --user -u untether-dev --since "1 hour ago" | grep -E "WARNING|ERROR"

# Specific event types
journalctl --user -u untether-dev --since "1 hour ago" | grep -E "stall|cancel|error"

# Full structured logs (JSON)
journalctl --user -u untether-dev --since "1 hour ago" -o cat

# FD count for bot process (detect leaks)
ls /proc/$(pidof untether)/fd 2>/dev/null | wc -l

# Zombie process check
ps aux | grep -E "defunct|Z " | grep -v grep
```

### Dev bot lifecycle

```bash
# Restart dev bot (picks up local source changes)
systemctl --user restart untether-dev

# Check status
systemctl --user status untether-dev

# NEVER restart staging for testing
# systemctl --user restart untether  ← WRONG
```

---

## Known Limitations and Gotchas

### Unexpected engine behaviour

During integration testing, Claude Code must watch for and note any **unexpected engine behaviour**, especially:

- **Phantom responses**: Engine produces substantive output from empty/garbage input (e.g. empty voice transcription triggers an unrelated long response). This may indicate session state leaking, hallucinated context, or the engine inventing a task.
- **Wrong engine running**: Directive routing sends to the wrong engine, or engine override doesn't take effect.
- **Session cross-contamination**: Response references files/context from a different engine's test project.
- **Disproportionate cost**: Simple test prompt generates unexpectedly high token/cost usage.

When detected, note the engine, chat ID, message IDs, and exact behaviour. Create a GitHub issue if the root cause is in Untether (e.g. wrong context forwarded, preamble confusion). If the root cause is upstream engine behaviour, note it in the test results as an engine quirk rather than an Untether bug.

### Timing and determinism

- **Stall tests (S1)** are timing-dependent — thresholds vary by `[watchdog]` config and by context (5 min normal, 10 min local tool, 15 min MCP tool, 30 min approval). Check `~/.untether-dev/untether.toml` for current values.
- **Ask question (C4)** is hard to trigger deterministically — Claude decides when to ask. Try ambiguous prompts.
- **Forward coalescing (T4)** depends on `forward_coalesce_s` debounce window — send forwards quickly enough to be within the window.
- **Budget auto-cancel (B1)** depends on how fast the engine reports costs — some engines report at the end, not incrementally.

### Engine-specific

- **OpenCode: no auto-compaction** — OpenCode sessions accumulate unbounded context across turns (no compaction events). After 4-5 prompts, response times degrade significantly (72k → 77k+ input tokens). Use `/new` to start a fresh session before isolated tests (e.g. error handling) to avoid slowdowns from prior context.
- **Resume (U4)** requires replying to the specific resume line in the final message. Resume token format varies by engine.
- **Model override (U5)** availability depends on which models each engine supports. Use `/config` → Model to see available options.
- **Long response (U3)** behaviour varies by engine — some produce shorter responses. The key check is message splitting, not word count.
- **Concurrent sessions (S2)** may hit rate limits on some engine APIs. Space the prompts a few seconds apart.
- **Reasoning levels (O2)** only available for Claude and Codex.

### Config and state

- **Subscription usage (C7)** requires `[footer]` configured in `~/.untether-dev/untether.toml`.
- **Export (U9)** requires a completed session in the current chat. Run a prompt first if `/export` returns "no session".
- **Chat session mode (O7)** requires config change and restart — cannot toggle at runtime.
- **Override persistence (O8)** depends on state file location — verify `~/.untether-dev/state/` exists.

### Telegram platform

- **Stale button clicks (T8)** — Telegram delivers callback queries for buttons on messages of any age. Bot must handle gracefully.
- **UTF-16 entity offsets (T6)** — Telegram uses UTF-16 code units for entity offsets. A single emoji flag sequence occupies 2 code units but 1 Python codepoint. Test with emoji-heavy text.
- **4096-char limit** applies after entity parsing, not before. Splitting must account for entity boundaries.
- **Voice messages (T1)** require Opus/OGG format, max 10MB by default. Transcription depends on configured API endpoint being accessible.
- **429 rate limits** block ALL Telegram sends for the full `retry_after` duration, not just the rate-limited chat. Monitor logs for 429s during high-volume testing.
