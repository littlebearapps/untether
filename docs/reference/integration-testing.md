# Integration Testing

Structured, repeatable integration test process run against `@untether_dev_bot` before every release. Tests exercise all 4 supported engines across the full feature surface.

> **Deprecated engines are out of the matrix.** `gemini` and `amp` are deprecated
> and targeted for removal in 0.36.0. Both are currently non-functional on the
> dev host — Gemini rejects individual accounts (upstream EOL 2026-06-18) and AMP
> returns `426` for out-of-date clients — so they **cannot** pass U1 and are no
> longer required at any release tier. Their chats and test projects stay in
> place for opt-in spot checks only. See
> [`runner-development.md`](../../.claude/rules/runner-development.md) →
> "Deprecated engines — sweep exemption".

## Infrastructure

| | Details |
|---|---|
| **Dev service** | `untether-dev.service` → `@untether_dev_bot` |
| **Test projects** | `test-projects/test-{claude,codex,opencode,pi}/` (plus deprecated `test-{gemini,amp}/`) |
| **Test chats** | 6 dedicated Telegram groups in the `ut-dev` folder, one per engine (2 deprecated) |
| **Engines** | Claude, Codex, OpenCode, Pi (⚠️ Gemini, Amp — deprecated, opt-in only) |

## Automated Testing via Telegram MCP

All integration test tiers are fully automated by Claude Code using Telegram MCP tools and the Bash tool. The relevant MCP tools are:

- `send_message` — send test prompts and commands to engine chats
- `get_history` / `get_messages` — read back bot responses and verify expected behaviour
- `list_inline_buttons` — inspect inline keyboards (approval buttons, `/config` menus, `/browse`)
- `press_inline_button` — interact with inline keyboards (approve/deny, toggle settings)
- `reply_to_message` — reply to resume lines for session continuation tests (U4)

### Test chats

Tests are sent to 6 dedicated engine chats via `@untether_dev_bot` (bot ID `8678330610`).
For DM-only tests (commands, `/at`, `/cancel`), use Nathan's personal DM chat ID with the bot — **not** the bot ID itself. The bot ID identifies the bot account; private chats are addressed by the user's chat ID. Resolve via the Telegram MCP `resolve_username` or by inspecting incoming `update.message.from.id` in the dev logs.

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

| Capability | Claude | Codex | OpenCode | Pi | Gemini ⚠️ | Amp ⚠️ |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Support status** | Yes | Yes | Yes | Yes | Deprecated | Deprecated |
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

### Tier 1: Universal Tests (all 4 supported engines)

Run in every supported engine's dedicated chat. Validates the core event pipeline.
The deprecated `gemini` and `amp` chats are excluded — they cannot pass U1 and are
not required at any tier.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| U1 | **Basic prompt** | `create a file called hello.txt with "hello world"` | Progress messages appear, final answer renders, footer shows model name, resume line present | #62 (missing model), #65 (footer repeat), stream threading (#98) |
| U2 | **Multi-tool prompt** | `list the files in this directory, then read the README if one exists` | Multiple action phases show in progress, tool names visible in verbose mode | Event counting, action tracking |
| U3 | **Long response** | `write a detailed explanation of how TCP/IP works, at least 2000 words` | Message splits correctly across multiple Telegram messages, no truncation, footer only on last chunk | #65 (footer repeat), #59 (entity overflow), message splitting |
| U4 | **Resume session** | After U1 completes, reply to the resume line: `now rename hello.txt to greetings.txt` | Resume token works, session continues, new progress + final answer | Resume token parsing per engine |
| U5 | **Model override** | Via `/config` → Model → set a different model, then send a prompt | Footer shows overridden model name | #77 (AMP model flag), build_args correctness |
| U6 | **Cancel mid-run** | Send a long prompt, then `/cancel` before it finishes | Run stops, completion message appears, no orphan process | Graceful cancellation, process cleanup |
| U7 | **Error handling** | Send a prompt that will fail (e.g. `read /nonexistent/file/path`) | Error renders in Telegram, no crash, session ends cleanly | Stderr sanitisation (#85), error formatting |
| U8 | **/usage** | `/usage` after a completed run | Claude: subscription info; Codex/OpenCode: last-session token totals (`📊 <engine> · last session in this chat`, #417) | #89 (429 handling), cost tracking |
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
| T11 | **Markdown table** ([#797](https://github.com/littlebearapps/untether/issues/797)) | `Reply with a 3-row, 3-column markdown table comparing tea, coffee and water (columns: drink, caffeine, notes), with inline code in one cell, then one sentence after it` | Each row on its own line; header row bold; no `|---|` separator line; inline code still renders as code; the sentence after the table is on its own line | commonmark has no table rule — rows used to collapse into one run-on line of pipes |

### Tier 4: Configuration and Overrides

Tests for per-chat and per-topic settings that affect run behaviour. Use forum topics if available.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| O1 | **Engine override** | `/agent set opencode`, then send a plain prompt (no directive) | OpenCode runs, footer shows OpenCode model | Per-chat engine default, override hierarchy |
| O2 | **Reasoning level** | `/config` → Reasoning → enable, then send a prompt | Reasoning model used, footer reflects it | Reasoning flag in build_args |
| O3 | **Listen mode** | `/listen mentions` in group, send plain text, then `@bot do something` | Plain text ignored, @mention triggers run | Listen mode filtering (renamed from `/trigger` in v0.35.3 [#297](https://github.com/littlebearapps/untether/issues/297); deprecated alias still works) |
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
| S7 | **Rapid-fire prompts** | Send 5 short numbered messages (`rapid 1` … `rapid 5`) in quick succession to the same chat | No text is lost: every message is either merged into a run's prompt (all merged texts reach the agent, in order) or answered/queued on its own. No double-spawn, no crash. Ask the agent to echo the numbers it received, and check `forward.prompt.merged` (`merged_count`) / `forward.prompt.flushed` in the logs | Race condition, session locking, forward-coalesce merge ([#794](https://github.com/littlebearapps/untether/issues/794)) |
| S8 | **Very long prompt** | Paste 4000+ characters as a single message | Prompt reaches engine intact, no truncation | Telegram message limits, prompt forwarding |
| S9 | **Concurrent button clicks** | Two rapid clicks on the same Approve button | Only one approval processed, second gets toast, no double-execute | Callback deduplication |

> **S7 and [#794](https://github.com/littlebearapps/untether/issues/794) (fixed in 0.35.5rc13):** prompts sent inside the `forward_coalesce_s` window (default 1 s) are now **merged** into one run instead of replacing each other. Messages sent further apart than the window run (or queue) separately, so a 5-message burst may produce one run or a few — either is fine. The pass bar is **no lost text**: for each of the 5 messages, confirm its number reached the agent (in a merged prompt, its own run, or a visible queue note). A message whose text appears in no run is a **FAIL**. `journalctl --user -u untether-dev -o cat | grep -E "forward.prompt.(merged|flushed)"` shows each merge (`merged_count`, `merged_message_ids`) and each early flush (`reason`).

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
| Q12 | `/listen` | Current listen mode | 1s |
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

## rc7 scenarios (v0.35.4rc7 — no-op empty-resume recovery)

Bespoke scenario for the dangling-tool_use → empty-resume regression fixed in
[#634](https://github.com/littlebearapps/untether/issues/634) (see
`docs/plans/2026-07-16-noop-resume-remediation/`). Run in the Claude chat
(`5284581592`) — the failure mode is specific to Claude's background-subagent
lifecycle and the control channel (`.claude/rules/control-channel.md`).

### B-RESUME: background-subagent lingering resume recovery

| Step | Action | What to verify |
|---|---|---|
| 1 | Send a prompt that spawns a background subagent AND keeps the model busy, e.g. `Launch a background agent to summarise the README, then wait for it.` Don't send anything else — let the run sit so the process lingers past the result (post-result limbo, the dangling-tool_use shape). | Progress messages appear; a result phase completes but the process doesn't fully wind down immediately |
| 2 | Send a follow-up in the same chat: `what did it find?` — this resumes the session from step 1 | A follow-up run starts using the resume token from step 1 |
| 3 | **Assert** on the step-2 response | A REAL answer (`num_turns > 0`, non-empty result); the run footer's session id is EITHER a **different** id than step 1 (fresh recovery) OR the **same** id together with real work shown (healthy resume); NO "engine returned an empty result" notice appears anywhere in the response |
| 4 | **Negative control** — in a fresh exchange with no background agent involved, send a plain two-message conversation, e.g. `what's 2+2?` then reply to the resume line with `and 3+3?` | Both messages resume the SAME session id — a healthy resume must not trigger spurious quarantine or fresh-session diversion |
| 5 | **Log verification**: `journalctl --user -u untether-dev --since "10 minutes ago" \| grep -E "runner.empty_result\|session.auto_resend_fresh\|session.quarantined\|session.resume_diverted_fresh"` | If steps 1-3 hit the dangling/empty-resume path, every `runner.empty_result` line is followed by `session.auto_resend_fresh` (or the session already carries `session.quarantined` + `session.resume_diverted_fresh` from a prior hit) — never a bare `runner.empty_result` with no recovery event after it |

> **0.35.5rc11 (#776):** with live sessions a follow-up to a lingering session is normally *injected into it* (same session id, no resume), so steps 1–3 now expect the SAME session id and a real answer, with `claude.live_session.injected` in the log and no `runner.empty_result`. The quarantine/fresh path is only expected with `[watchdog] live_sessions = false`. The B-LIVE scenarios below cover the new behaviour directly.

### B-LIVE: live-session scenarios (0.35.5rc11+, #776)

Run in the Claude chat (`5284581592`). Prompts that background work should say *"end your turn immediately"* so the result lands before the work finishes. Log checks: `journalctl --user -u untether-dev -o cat | grep -E "claude.task|claude.turn|live_turn|live_session|resume_guard|cost.turn_delta"`.

| # | Scenario | Prompt shape | Pass criteria |
|---|---|---|---|
| B-LIVE-1 | Background Bash wake | start `sleep 40 && echo X` with `run_in_background`, end turn; reply "GOT: <output>" when notified | turn-1 final at result time; a **new** message `🔔 Background task finished — <desc>` with the output ~40 s later; `claude.turn.started reason=task_finished`; `cost.turn_delta` for both |
| B-LIVE-2 | Background Agent wake + follow-up while it runs | launch one background subagent (`sleep 45`, report), end turn; while it works send a quick question | the question is answered **immediately** under its own message (`claude.live_session.injected`); the agent's result arrives later with the 🔔 header (not "Claude continued") |
| B-LIVE-3 | Follow-up into a live session | start a 50 s background task, end turn; send a follow-up | exactly **one** `subprocess.spawn` for the exchange; follow-up answered within seconds; wake delivered afterwards; no `session.resume_diverted_fresh` / `auto_resend_fresh` |
| B-LIVE-4 | Restart with a live task | start a 600 s background task, end turn; `systemctl --user restart untether-dev` | restart completes in seconds (not the 120 s drain); `shutdown.live_sessions_closed count=1`; notice `⏳ Untether is restarting — stopping 1 background task: …`; rc=0, nothing quarantined |
| B-LIVE-5 | Monitor ticks | Monitor a 3-tick loop (10 s apart), end turn; reply "TICK n" per tick | one silent `📡 Monitor — <desc>` message per tick; the stream end arrives as a 🔔 wake |
| B-LIVE-6 | `/cancel` idle session, then resume | start a 600 s background task, end turn; `/cancel`; then ask a question | `⏹ Stopped 1 background task: …`; `claude.live_session.stdin_closed reason=cancel`; the question resumes the same session and gets a real answer; `claude.resume_guard.absorbed`; no `runner.empty_result` |
| B-LIVE-7 | Approval inside a wake turn (plan mode) | start a 20 s background task, end turn; "when it finishes, create /tmp/x via ExitPlanMode" | the ExitPlanMode keyboard renders on the **wake turn's** progress message; Approve → file written; wake final delivered |

**Required tiers:** rc7 → Tier 7 (command smoke) + Tier 1 (Claude only) + B-RESUME. rc8 → add Tier 1 (all 6 engines, confirm no cross-engine regression from the quarantine store) + Tier 2 (interactive/plan).

Automate via Telegram MCP (`send_message`, `get_history`) + Bash (`journalctl --user -u untether-dev`) exactly as the other tiers. See `scripts/audit-noop-resume.sh` for the post-deploy fleet-wide correlation check (Layer 4 of the remediation plan) that runs the same five-event correlation across all hosts after rollout.

---

## rc12 scenarios (0.35.5rc12)

Run these in addition to the standard tiers and B-LIVE for rc12. Unless noted, use the Claude chat (`5284581592`). Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| RC12-1 | **Per-run stream binding ([#510](https://github.com/littlebearapps/untether/issues/510))** | Start a long Claude run in the Claude chat (e.g. `run sleep 90 in the foreground, then say DONE`). While it runs, send a short Claude prompt in a second chat (the Codex chat with a `/claude` directive, see T9). | Two `session.summary` lines with **different** `session_id`s; the short run's `event_count` / `duration_seconds` are its own (small), and the long run's summary, written after the short one finished, shows its own `event_count` and `last_event_type=result` — never the short run's values. The long run shows no stall warning or wake-up countdown borrowed from the other chat. |
| RC12-2 | **No false rate-limit notes ([#790](https://github.com/littlebearapps/untether/issues/790))** | Run U1-U4 and B-LIVE-1 in the Claude chat; also `uv run pytest tests/test_claude_cli_schema_drift.py` against the installed CLI. | No `⏳ Rate limited` note on healthy runs; no `claude.rate_limit_event` line with `retry_after_source=bare` or `default` unless a real `rejected` snapshot arrived. A `⚠️ 5h limit N% used — resets HH:MM` note appears at most once per window, and only if utilisation is ≥ 70%. Drift test passes (or skips when the CLI is absent). |
| RC12-3 | **API-retry note ([#792](https://github.com/littlebearapps/untether/issues/792))** | Opportunistic: only if a `claude.api_retry` line appears during the session (Anthropic 429/529/5xx). | The progress message shows one `🔁 API error <status> (<category>) — retrying in Ns (attempt n/m)` line that updates in place; no stall WARN during the back-off (`threshold_reason=api_retry_waiting`); `claude.api_retry` is INFO, WARN only on the final attempt. If no retry occurs, mark *not exercised*, not fail. |
| RC12-4 | **Live-idle hold is not a stall ([#787](https://github.com/littlebearapps/untether/issues/787))** | Start `sleep 300` with `run_in_background`, end the turn; wait for the 🔔 wake. | No `progress_edits.stall_detected` and no stall message during the hold; one `progress_edits.stall_live_idle_suppressed` INFO; the run's `session.summary` has `stall_warnings=0`, `peak_live_idle_seconds` close to the hold, and a small `peak_idle_seconds`. |
| RC12-5 | **Close-grace overrun, no quarantine ([#791](https://github.com/littlebearapps/untether/issues/791))** | Passive: after any idle close, check `close_grace_expired`. Forced repro (optional): after a plain reply with no background work, find the Claude PID and `kill -STOP <pid>` so it can't honour stdin EOF; wait ~80 s (60 s idle + 15 s grace + 5 s); then send a follow-up. | Any `claude.live_session.close_grace_expired` WARN carries a proc snapshot (state, wchan, CPU, children) and `idle_clean=true` for an idle close; it is followed by SIGINT, then `claude.live_session.forced_teardown ... quarantined=false`. No `session.quarantined reason=forced_teardown_after_result` for that session; the follow-up resumes the **same** session id with no `session.resume_diverted_fresh`. A close over a live task (not idle) is still quarantined. |
| RC12-6 | **Wake-turn attribution, one push per finish ([#785](https://github.com/littlebearapps/untether/issues/785))** | Launch one background subagent that runs 2-3 tool calls and reports, end the turn. | Exactly **one** notifying `🔔 Background task finished — <task description>` message per finish (not `🔔 Claude continued`, and not a subagent's inner task name); any second turn for the same task arrives silently. Logs may show `claude.turn.retro_attributed`, `claude.turn.task_end_paired`, `live_turn.retro_attributed` or `claude.turn.notification_ignored`. |
| RC12-7 | **Queued note under a live session ([#781](https://github.com/littlebearapps/untether/issues/781))** | Start a 60 s background task, end the turn; immediately send a follow-up. | The follow-up shows `⏳ Queued — sent as soon as Claude's current turn ends (background tasks keep running).` (no `/cancel to drop it`) and is answered within seconds in the same session. With `live_sessions = false` the old `⏳ Queued behind the previous run's N background task(s) …` wording returns. |
| RC12-8 | **`<br>` rendering ([#786](https://github.com/littlebearapps/untether/issues/786))** | `Reply with exactly: first line<br>second line, then a two-row markdown table with a <br> inside one cell, then the literal text <br> inside backticks` | The first `<br>` renders as a line break; the table cell shows a space, not `<br>`; the backticked `<br>` stays literal code; no other HTML tag is interpreted. |
| RC12-9 | **Filenames not auto-linked ([#788](https://github.com/littlebearapps/untether/issues/788))** | `Mention CLAUDE.md, scripts/healthcheck.sh:12, src/untether/runner.py and https://example.com/notes.md in plain text, no code formatting` | The three filenames render as inline code, not links (no `claude.md` domain link); the `https://` URL stays a clickable link. |
| RC12-10 | **Voice vocabulary ([#789](https://github.com/littlebearapps/untether/issues/789))** — *superseded by R15-11 (its "no prompt set" precondition wasn't true on the dev bot)* | `send_voice` a clip saying *"open CLAUDE dot MD and AGENTS dot MD and summarise them"* with no `voice_transcription_prompt` set in the dev config. | Transcript contains `CLAUDE.md` and `AGENTS.md` (not "Claw.md"); both render as inline code in the echoed transcript. Effect is model-dependent, so a near-miss is a soft fail: note it and don't block the release. |

**Required for rc12:** Tier 7 + Tier 1 (all 4 supported engines, because #510 changed the base `run_impl` spawn order) + B-LIVE-1…7 + RC12-1…9, RC12-10 if a voice clip is available.

---

## rc13 scenarios (0.35.5rc13)

Claude chat (`5284581592`) unless noted. Background prompts should use `python3 -c "import time; time.sleep(N)"` rather than a bare `sleep N`: the CLI blocks long foreground `sleep` calls inside subagents. Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| RC13-1 | **Background status ([#777](https://github.com/littlebearapps/untether/issues/777))** | `/planmode auto`; launch a background Agent (40 s Python sleep) plus two background Bash jobs (25 s, 75 s), reply "launched", end the turn; send `/ping` while they run | The progress message shows `⏳ background (N)` rows; after the answer, one **silent** status message replies to the prompt and is edited in place (`🤖 … · tok · tools`, `🐚 … · elapsed`); `/ping` shows `⏳ background: 3 tasks running`; the message finalises to `✅ all 3 background tasks done`. Logs: `background_status.opened` / `.finalised` |
| RC13-2 | **Wake-ack consolidation ([#785](https://github.com/littlebearapps/untether/issues/785))** | Same as RC13-1, asking for one short sentence per finish and a summary at the end | Short acks appear as `↳` lines in the status message (`live_turn.fold_decision decision=fold`); only the final summary arrives as a new **pushed** 🔔 message; no `🔔 Claude continued` no-op push after it |
| RC13-3 | **Wake reply anchor ([#795](https://github.com/littlebearapps/untether/issues/795))** | Launch background work from a follow-up that was injected into a live session | The 🔔 message and the status message reply to the follow-up that launched the task, not to the run's first prompt |
| RC13-4 | **Resumed / orphaned agent ([#801](https://github.com/littlebearapps/untether/issues/801))** | Background Agent prints `first`; on notify, SendMessage it back to run a 60 s Python sleep with `run_in_background=true`; reply with the output | `claude.task.revived`; the orphaned bash (`owned_by_subagent=True`) is listed in the status message and ends `status=completed` (never `killed`); `stdin_closed reason=idle_no_tasks` only after it; the output reaches the chat |
| RC13-5 | **Steer ([#775](https://github.com/littlebearapps/untether/issues/775))** | (a) run a 40 s foreground Python sleep, then `/steer also tell me the hostname`; (b) `/config` → ↪️ Follow-up → Steer, run `echo hi` + a 400-word story, send a plain message while the story streams; (c) with steer on, `/cancel` a run then send a question; (d) Codex chat: `/steer hi`; then `/queue` to reset | (a) `↪️ Steered into the current run.`, a `↪️ steer received` progress row, one final answering both (`claude.live_session.injected_absorbed`); (b) toast `Follow-up: steer`, the plain message runs as its own turn replying to it; (c) `steer_window_closed reason=cancel` then `↪️ No live Claude run to steer — queued instead.` and a normal resumed answer; (d) `↪️ Steer isn't supported on codex — queued instead.`; an idle live session gets no steer ack |
| RC13-6 | **Plan label ([#793](https://github.com/littlebearapps/untether/issues/793))** | `/planmode on`: plan A, tap ❌ Deny; in the same session plan B, tap ✅ Approve | The deny final has no `📋 Plan (approved):`; the approve final shows **plan B**; any `claude.plan.stale_input` line shows the file won |
| RC13-7 | **Turn complete + footer ([#798](https://github.com/littlebearapps/untether/issues/798), [#770](https://github.com/littlebearapps/untether/issues/770))** | Reply to a short prompt, then send a follow-up within 60 s; run U3 | The injected follow-up final ends `· ✓ turn complete`; on U3 only the last chunk has `💰`/`⚡`/`🏷`/`↩️` |
| RC13-8 | **Rapid prompts + tables ([#794](https://github.com/littlebearapps/untether/issues/794), [#797](https://github.com/littlebearapps/untether/issues/797))** | Send `rapid 1`, `rapid 2`, `rapid 3 — reply with the numbers` back to back; then `ALPHA` + `/codex … BETA` back to back; ask for a 3-row markdown table | One run answering 1, 2, 3 (`forward.prompt.merged merged_count=3`); ALPHA and BETA answered separately (`forward.prompt.flushed reason=directive`); every table row on its own line |
| RC13-9 | **Logs ([#799](https://github.com/littlebearapps/untether/issues/799), [#800](https://github.com/littlebearapps/untether/issues/800))** | After the above, count `claude.post_result_idle.tick` lines; grep for `Bearer [^[<]` and `eyJ[A-Za-z0-9_-]{10}` | Ticks appear only while an approval or question is pending; no credential shapes in the journal |

**Required for rc13:** Tier 7 + Tier 1 (all 4 supported engines) + Tier 2 (C1, C2, plan approve/deny) + B-LIVE + RC13-1…9.

---

## rc14 scenarios (0.35.5rc14)

Claude chat (`5284581592`) unless noted. As for rc13, use `python3 -c "import time; time.sleep(N)"` rather than a bare `sleep N` for anything a subagent runs. Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| RC14-1 | **Cancel a follow-up turn ([#806](https://github.com/littlebearapps/untether/issues/806))** | Reply to a short prompt, then within 60 s send a follow-up that runs a 60 s foreground Python sleep; `/cancel` while it runs | The follow-up's final reads `cancelled · claude · Ns` (not `error · the session ended before this turn finished`); `live_turn.cancelled turn=2 reason=cancel`; the turn's cost still appears in `cost.turn_delta` / `runner.completed` |
| RC14-2 | **Command barrier ([#807](https://github.com/littlebearapps/untether/issues/807))** | Send each pair back to back (inside `forward_coalesce_s`): `A` + `/new`; `A` + `/cancel`; `A` + `/continue`; `A` + `/ping` | The first three each get a `🗑️ Dropped 1 message sent just before /<cmd> — send it again if you still need it.` reply on `A` and `forward.prompt.dropped reason=<cmd>`; with `/ping`, `A` runs (`forward.prompt.flushed reason=command`) and `/ping` answers too |
| RC14-3 | **Cancelled message survives restart ([#810](https://github.com/littlebearapps/untether/issues/810))** | Start a 60 s foreground run, `/cancel` it, then `systemctl --user restart untether-dev` | The message still reads `cancelled` after the restart (no `⚠️ interrupted by restart`); `progress_persistence.released reason=cancelled` (DEBUG) and no orphan relabel for that message id at startup |
| RC14-4 | **Spent one-shot cron ([#809](https://github.com/littlebearapps/untether/issues/809))** | Add a `run_once = true` cron to the dev config that has already fired (listed in `run_once_fired.json`), restart dev | The startup message's triggers line counts only scheduled crons and appends `, 1 spent one-shot` |
| RC14-5 | **`peak_live_idle_seconds` ([#811](https://github.com/littlebearapps/untether/issues/811))** | Reply to a short prompt, then send a follow-up running a 60 s foreground Python sleep; let the session idle-close | `session.summary followup_turns=1 peak_live_idle_seconds` ≈ 60 (the idle gap before the close), not the follow-up's run time added on top |
| RC14-6 | **Async-hook hold + rewake ([#812](https://github.com/littlebearapps/untether/issues/812))** | In the dev project's `.claude/settings.json` add a `Stop` command hook with `"asyncRewake": true` that sleeps, prints findings to stderr and `exit 2`. Two variants: `sleep 90` and a sleep longer than the bound. Send a short prompt and wait | 90 s variant: `claude.hook.pending_hold` after the reply, then `claude.turn.hook_rewake` and a new **pushed** `🪝 Hook feedback — Stop` message with the findings; no `close_grace_expired`. Bound variant: `claude.hook.hold_expired`, then at the close `claude.live_session.async_hook_killed` and the notice `⏳ Closing session — a background hook (Stop) was still running; its feedback wasn't delivered.` A plain `async: true` hook that exits quickly must not hold: `claude.hook.hold_released reason=no_hook_process` within a few seconds |
| RC14-7 | **Read-only acks fold ([#813](https://github.com/littlebearapps/untether/issues/813))** | The RC13-2 shape with 3 background agents, asking for one short sentence per finish (Claude will usually `Read` each output file) | No `live_turn.fold_decision decision=tools` on acks whose only tools are `Read`/`Glob`/`Grep`; each `↳` line sits under the task it describes; only the final summary is pushed |
| RC14-8 | **Safeguard stop ([#814](https://github.com/littlebearapps/untether/issues/814))** | **Opportunistic only — never provoke a refusal.** Grep the session's logs for `claude.safeguard_stop` | If one occurred: a `🛡️ … safeguards stopped a response · <outcome>` progress row, a `🛡️ safeguards stopped N response(s)` footer, one `💡` hint link per session, and the run is not marked as an error. If none occurred, record **not exercised**, not fail |

Practical notes for RC14-6:

- **Make the hook one-shot with a marker file.** A `Stop` hook that exits 2 fires again at the end of every turn, including the rewake turn it caused, so it loops. Have the script exit 0 at once if a marker exists and create the marker before its sleep, e.g. `[ -e /tmp/rc14-hook-fired ] && exit 0; touch /tmp/rc14-hook-fired; sleep 90; echo "rc14 findings" >&2; exit 2`. Delete the marker between runs.
- **Shorten the bound for the second variant.** Set `[watchdog] async_hook_max_hold = 60` in `~/.untether-dev/untether.toml` (hot-reloaded for new runs) and use `sleep 120`, so the bound variant takes about two minutes instead of eleven. Put the default (630) back afterwards.
- Hook events need `--include-hook-events`: check `claude.hook_events.probe supported=true` once after the dev restart. `[watchdog] hold_for_async_hooks = false` is the kill switch (no flag, no hold).

**Required for rc14:** minor-release tiers because #812 touches the engine-agnostic `runner.py`: Tier 7 + Tier 1 (all 4 supported engines) + Tier 2 (C1–C6) + B-LIVE-1…7 + `uv run pytest tests/test_claude_cli_schema_drift.py` against the installed CLI + RC14-1…8.

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
   Claude Code runs U1-U10 in the 4 supported engine chats via MCP
   (skip the deprecated gemini/amp chats — they cannot pass U1)
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

14. If all pass: write the attestation marker, then fleet-roll the rc/stable
    scripts/run-integration-tests.sh X.Y.ZrcN --manual --tiers "tier7,tier1-claude,..." --notes "..."
    scripts/fleet-rollout.sh X.Y.ZrcN          # parallel across lba-1/nsd/channelo/sl/mac
    See .claude/rules/release-discipline.md → Fleet rollout for the full gate + escape hatches.
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
| Per-run stream binding (`runner.py` `RunStreamHandle` / `publish_run_stream`, `runner_bridge.py` stall monitor) | RC12-1, S1, S2, U1-U4 (all engines), B-LIVE-1 |
| Claude stream schema / rate-limit / API-retry handling (`schemas/claude.py`, `runners/claude.py`) | `uv run pytest tests/test_claude_cli_schema_drift.py`, RC12-2, RC12-3, S1 |
| Runner bridge / auto-continue / no-op resume recovery (`runner_bridge.py`, `runners/claude.py`) | B-RESUME, U1-U4 (Claude), U6, U7 |
| Live sessions / follow-up injection / scheduler (`runners/claude.py`, `runner_bridge.py`, `live_followup.py`, `scheduler.py`) | B-LIVE-1…7, RC12-4…7, C1-C6, S7, U1-U4 (Claude) |
| Telegram transport (`telegram/*.py`) | T1-T10, S7, S8, R15-9-1 (benign edit/delete 400s at startup) |
| Control channel (`claude_control.py`) | C1-C6, T8, S9, R15-5 |
| Config/settings (`settings.py`) | O1-O9, S5, upgrade path, R15-13a…d (settings parse cache) |
| Cost tracking (`cost_tracker.py`) | B1-B3, U8 |
| Progress/formatting (`markdown.py`, `telegram/render.py`) | U3, T6, T7, S4, S8, RC12-8, RC12-9 |
| Commands (`commands/*.py`) | Tier 7 (all), specific command test |
| `/browse` + shared path checks (`commands/browse.py`, `telegram/files.py`) | Q5, U10, T2, T3, R15-2 |
| File transfer (`file_transfer.py`) | T2, T3, T5, R15-3 |
| Voice (`voice.py`) | T1, RC12-10 |
| File transfer (`file_transfer.py`) | T2, T3, T5 |
| Voice (`voice.py`) | T1, R15-11 (vocabulary), R15-10 (endpoint) |
| Topics (`topics.py`, `topic_state.py`) | O1, O5, O6, O8 |
| Directives (`directives.py`) | T9, T10 |
| Shutdown (`shutdown.py`) | S3, B4 |
| Error hints / reasoning levels (`error_hints.py`, `engine_overrides.py`, `commands/config.py`, `commands/executor.py`) | Tier 7 Q2, Tier 4 O2 (Codex), U7, R15-14a…e |
| `extra_args` guard (`runners/extra_args_guard.py`, Claude/Codex `build_runner`, `runtime_loader.py`) | R15-1-1…4, U1 (Claude, Codex), C1, drift tests (`tests/test_claude_cli_schema_drift.py`, `tests/test_codex_cli_schema_drift.py`) |

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

---

## rc15 scenarios (0.35.5rc15)

Dev bot only (`@untether_dev_bot`). Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`. Each subsection below comes from one rc15 plan and keeps that plan's scenario IDs.

### #390 — Symlinked upload/download targets

Claude chat (`5284581592`, project `claude-test` → `test-projects/test-claude`). The dev config has `[transports.telegram.files] enabled = true`, `auto_put_mode = "prompt"`, `uploads_dir = "incoming"` and the default deny globs. Setup, after the dev restart:

```bash
cd ~/untether/test-projects/test-claude
mkdir -p r15/.git/hooks r15/real/.ssh r15/benign
printf 'R15_SENTINEL=1\n' > r15/.env
ln -s .env        r15/cfg.txt        # file symlink → deny target
ln -s .git/hooks  r15/hooks-link     # dir symlink → .git
ln -s real/.ssh   r15/keys           # dir symlink → **/.ssh/**
ln -s benign      r15/link           # benign
printf 'r15 ok\n' > /tmp/r15-ok.txt
```

| # | Action | Expected reply (prefix) | Expected log / check |
|---|---|---|---|
| R15-3a | `send_file /tmp/r15-ok.txt`, caption `/file put r15/cfg.txt --force` | `path denied by rule: .env (resolves to r15/.env)` | `file_transfer.path_denied direction=put rule=.env via_symlink=True requested=r15/cfg.txt resolved=r15/.env`; `cat r15/.env` still prints `R15_SENTINEL=1` |
| R15-3b | caption `/file put r15/hooks-link/pre-commit` | `path denied by rule: .git/** (resolves to r15/.git/hooks/pre-commit)` | `path_denied … rule=.git/**`; `test ! -e r15/.git/hooks/pre-commit` |
| R15-3c | caption `/file put r15/keys/` (dir form, one document) | `path denied by rule: **/.ssh/** (resolves to r15/real/.ssh/r15-ok.txt)` — the dir-level check passes, the per-file check denies | `path_denied … rule=**/.ssh/**` |
| R15-3d | caption `/file put r15/link/ok.txt` | ``saved `r15/benign/ok.txt` in `claude-test` (…)`` | `file_transfer.saved path=r15/benign/ok.txt` |
| R15-3e | Repeat R15-3d (no `--force`) | ``saved `r15/benign/ok_1.txt` …`` (dedup through a symlink, no crash) | `file.deduplicate` + `file_transfer.saved path=r15/benign/ok_1.txt`; no `Traceback` / `handle.worker_failed`; `systemctl --user show untether-dev -p NRestarts` unchanged |
| R15-3f | `/file get r15/cfg.txt` | `path denied by rule: .env (resolves to r15/.env)`; no document | `path_denied direction=get` |
| R15-3g | `/file get r15/hooks-link` | `path denied by rule: .git/** (resolves to r15/.git/hooks)`; no zip | `path_denied direction=get rule=.git/**` |
| R15-3h | `/file get r15/link/ok.txt` | Document `ok.txt` delivered, content `r15 ok` | `file_transfer.sent filename=ok.txt` |

Regression: T2, T3 (use `CLAUDE.md` in test-claude) and T5 as written, plus Q13 `/file` → usage. Global negative grep: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "Traceback|ValueError|RuntimeError|handle.worker_failed"` must be empty. Not live-testable (unit-only, say so in the attestation notes): the symlinked `run_root` crash (F5) and the symlink loop (F6). Cleanup: `rm -rf ~/untether/test-projects/test-claude/r15 /tmp/r15-ok.txt` and any `incoming/r15-ok*.txt`.

### #389 — `/browse` explicit root, deny globs and per-chat ids

Setup in `test-projects/test-claude` (remove afterwards): `.env` with `RC15_FAKE=do-not-leak`, `key.pem` with `-----BEGIN FAKE-----`, `mkdir -p .untether && echo 'bot_token = "FAKE"' > .untether/untether.toml`, `ln -s /home/nathan rc15-home`, `ln -s .env rc15-notes.txt`. Then `systemctl --user restart untether-dev`.

| # | Where | Steps | Pass criteria |
|---|---|---|---|
| R15-2a | Owner DM with the dev bot (unbound; dev cwd = repo root) | `/browse`, then `/browse .envrc` | Both reply `No project directory for this chat. Bind the chat …`; nothing from the untether repo is listed; `browse.no_project_root` in logs |
| R15-2b | Claude chat | `/browse .env`, `/browse key.pem`, `/browse .untether/untether.toml`, `/browse rc15-notes.txt`, `/browse src/../.env` | `Path denied by rule: …` / `Hidden paths can't be browsed.`; neither `do-not-leak` nor `FAKE` appears; `browse.path_denied` × 5 |
| R15-2c | Claude chat | `/browse`; inspect buttons with `list_inline_buttons` | No button for `.env`, `key.pem`, `.untether/`, `rc15-home/`, `rc15-notes.txt`; `.github/` and ordinary dirs present; tapping `.github/` lists it |
| R15-2d | Claude chat | `/browse rc15-home` | `Path outside project.`; `browse.path_escape_attempted via=arg` |
| R15-2e | Claude chat → Codex chat | In the Claude chat send `/browse` and read a file button's `callback_data` (`browse:f:<pid>`) with `list_inline_buttons`; in the Codex chat send `/browse f:<pid>` as text | Codex chat replies `Path expired. Use /browse to start over.`; the same `/browse f:<pid>` in the Claude chat previews the file |
| R15-2f | Claude chat | `/file get key.pem` (T3 variant, #831) | `path denied by rule: **/*.pem` (rc14 delivered it) |

Regression: U10 navigation (dir → subdir → `..` → file preview → Back) in the Claude and Codex chats; Q5 in every supported engine chat. Logs: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "browse\.(no_project_root|path_denied|path_escape_attempted|project_root\.error)|command\.failed|callback\.failed"` — no `command.failed` / `callback.failed`. Clean up the fixtures and confirm `git -C /home/nathan/untether status --short test-projects/` shows nothing new.

### #506 — settings parse cache

Log check: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "config\.loaded|config\.read\.|_settings\.load_failed|config\.reload\."`

| # | Scenario | Steps | Pass criteria |
|---|---|---|---|
| R15-13a | **Config edit applies mid-live-session ([#506](https://github.com/littlebearapps/untether/issues/506))** | Claude chat. (1) Send `reply with the single word ALPHA`. The final shows the `💰` cost line. (2) Within 60 s, while the session is live-idle (`claude.live_session.*` shows no idle close yet), edit `~/.untether-dev/untether.toml`: set `[footer] show_api_cost = false` (add the table if it's absent). Use `sed -i`, which is an atomic rename. (3) Reply to the ALPHA final: `reply with the single word BETA`. (4) Revert the edit. | The BETA turn (injected into the live session, `followup_turns=1` in `session.summary`) renders **without** the `💰` line. Exactly one `config.loaded reason=content_changed` between the two turns. No restart. If the dev chat has a per-chat footer override, clear it first via `/config`. |
| R15-13b | **No re-parse without edits** | Note the restart time, restart `untether-dev`, then run Q2 (`/config`, open two sub-pages), U2 (multi-tool prompt) and U4 (resume) in the Claude chat with no config edits. | `journalctl --user -u untether-dev -o cat --since "<restart time>" \| grep 'config.loaded' \| grep -c 'reason=first_load'` is **1**, and there are 0 `reason=content_changed` or `reason=env_changed` lines. Every startup read (`cli/run.py:30`, `telegram/backend.py:42`), the `/config` renders (`telegram/commands/config.py:293,351,…`) and every run read share one cache entry for the dev config path, so only the earliest read parses. The strict `load_settings()` path logs only at DEBUG (`reason=uncached`) and isn't counted. The count must not grow with tool calls, turns or page renders. Unit test #2 covers the same property deterministically. |
| R15-13c | **Kill switch** | Never open, `cat` or `systemctl cat` the unit (it holds secrets). Create a drop-in with a heredoc: `mkdir -p ~/.config/systemd/user/untether-dev.service.d && cat > ~/.config/systemd/user/untether-dev.service.d/r15-13c-settings-cache.conf <<'EOF'` / `[Service]` / `Environment=UNTETHER_SETTINGS_CACHE=0` / `EOF`, then `systemctl --user daemon-reload && systemctl --user restart untether-dev`, and run U1. Afterwards: `rm ~/.config/systemd/user/untether-dev.service.d/r15-13c-settings-cache.conf && systemctl --user daemon-reload && systemctl --user restart untether-dev`. | U1 passes. There are no INFO `config.loaded reason=…` lines since the restart (the cached path is off), and the behaviour matches rc14. After removing the drop-in, the next message logs one `reason=first_load`. |
| R15-13d | **Invalid edit is not masked** | Introduce a TOML syntax error in the dev config (for example a stray `[[`), send `ping` in the Claude chat, then fix it. | The run still completes on defaults. The logs show `config.read.toml_error` plus the helpers' `*_settings.load_failed` warnings (today's behaviour), **not** a silent stale config. After the fix, the next message logs `config.loaded reason=content_changed`. |

### #746 — benign Telegram edit/delete 400s

| # | Test | What to send / do | What to verify |
|---|---|---|---|
| R15-9-1 | **Benign orphan 400s are INFO ([#746](https://github.com/littlebearapps/untether/issues/746))** | **Run from a terminal Claude Code session on lba-1 (Telegram MCP + Bash), never from a dev-bot chat**: step 3 stops `untether-dev`, which would kill a session driven through it. Claude chat `5284581592` (Bot API `-5284581592`). (1) Send `/ping`; note **your** message id `U` and the bot's reply id `B1` (`get_history`). (2) Send `/ping` again; note the reply id `B2`, then delete `B2` with the Telegram MCP `delete_message`. (3) `systemctl --user stop untether-dev`. (4) Seed the orphans with the **guarded script below**: it backs the existing `~/.untether-dev/active_progress.json` up to `.bak`, **aborts unless that file is empty or `{}`**, and writes `{"r15:a":{"chat_id":-5284581592,"message_id":B1},"r15:b":{"chat_id":-5284581592,"message_id":B2},"r15:c":{"chat_id":-5284581592,"message_id":U}}`. (5) `systemctl --user start untether-dev`; wait for the startup message. (6) **Always** restore: `mv ~/.untether-dev/active_progress.json.bak ~/.untether-dev/active_progress.json` (even when an earlier step failed). | Telegram: `B1` now reads `⚠️ interrupted by restart`; `B2` stays deleted; `U` is unchanged. Logs: `startup.orphan_cleanup count=3`; `telegram.benign_rejection method=editMessageText … reason_class=target_gone message_id=B2`; `telegram.benign_rejection … reason_class=not_editable message_id=U` (a bot can't edit a user's message); `startup.orphan_cleanup.done count=3 edited=1 failed=2 skipped=0`; **no** `telegram.http_error` and **no** `[error` line between `startup.orphan_cleanup` and `startup.sent`. `active_progress.json` is gone or empty afterwards. |

**Step 4/6 script** (Bash in the terminal session; fill in `B1`, `B2`, `U`):
```bash
set -euo pipefail
F=~/.untether-dev/active_progress.json
systemctl --user is-active --quiet untether-dev && { echo "ABORT: stop untether-dev first"; exit 1; }
restore() { if [ -e "$F.bak" ]; then mv -f "$F.bak" "$F"; else rm -f "$F"; fi; }
if [ -e "$F" ]; then
  cp -p "$F" "$F.bak"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])) if open(sys.argv[1]).read().strip() else {}; sys.exit(0 if d == {} else 3)' "$F" \
    || { echo "ABORT: $F holds real orphans; not touching it"; rm -f "$F.bak"; exit 1; }
fi
trap restore EXIT          # guarantees step 6 even if step 5 or the checks fail
python3 - "$F" B1 B2 U <<'EOF'
import json, sys
f, b1, b2, u = sys.argv[1], *map(int, sys.argv[2:])
c = -5284581592
json.dump({"r15:a": {"chat_id": c, "message_id": b1},
           "r15:b": {"chat_id": c, "message_id": b2},
           "r15:c": {"chat_id": c, "message_id": u}}, open(f, "w"))
EOF
systemctl --user start untether-dev
sleep 20                   # startup: backlog drain → orphan cleanup → startup message
journalctl --user -u untether-dev --since "-2 min" -o cat \
  | grep -E "startup\.orphan_cleanup|telegram\.benign_rejection|telegram\.http_error|startup\.sent"
```
The `trap` restores the original (empty) file after cleanup has cleared the seeded one. If the
pre-check aborts, the `.bak` is removed and the real file is untouched: pick another time, or
first let a normal dev restart clean those orphans.

**Log checks** (run straight after step 5):
```bash
journalctl --user -u untether-dev --since "-3 min" -o cat \
  | grep -E "startup\.orphan_cleanup|telegram\.benign_rejection|telegram\.http_error|startup\.sent"
# the /monitor-equivalent grep A must return nothing for this window:
journalctl --user -u untether-dev --since "-3 min" -o cat | grep -iE "\[(warning|warn|error)"
```
Expected: the four INFO lines above, and an **empty** second grep. The second grep also catches
an unexpected `transport.edit.failed`, which isn't on this path.

**Negative control (optional, no code change).** A genuine 400 still logs ERROR. It isn't
practical to provoke live without a malformed request, so the `test_746_*_stays_error` unit tests in
`tests/test_telegram_client_api.py` cover it. Mark it
"covered by unit tests" in the attestation notes.

**Practical notes.**
- Use only the dev bot. Never edit `~/.untether/` (staging).
- If `delete_message` can't delete the bot's message (the user isn't an admin of the basic
  group), use `message_id: 1` for `B2` instead. That message never existed, and it produces the
  same "message to edit not found" (Track D §1: "not found" also covers ids you can't see).
- Record the three ids in the attestation `--notes`.
`@untether_dev_bot` only. Back up `~/.untether-dev/untether.toml` before any config-editing row and restore it afterwards. Drive every row from an lba-1 terminal Claude Code session (Telegram MCP + Bash), never from inside a dev-bot chat. Run the zero-token drift suites on lba-1 as part of the rc (CI has neither CLI; a skip on lba-1 counts as a fail): `uv run pytest tests/test_claude_cli_schema_drift.py tests/test_codex_cli_schema_drift.py -v -rs`.

### #830 — Codex safe mode = read-only sandbox

Codex chat `4929463515` (Bot API `-4929463515`), project `codex-test` → `/home/nathan/untether/test-projects/test-codex`. Logs: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "runner.start|codex.process.failed|codex.argv.rejected|codex.permission_mode.unknown|config.approval_policy.set"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-CS0 | **Repro before the fix (zero-token) ([#830](https://github.com/littlebearapps/untether/issues/830))** | Unfixed tree (0.35.5rc14). `/config` → Approval policy → **Safe**, then send `say hi`. The direct-CLI form needs no bot: run the argv `build_args` produces for `safe` against the installed `codex` with stdin `/dev/null` and a temp `CODEX_HOME` | Final shows `codex exec failed (rc=2).` with `invalid value 'untrusted' for '--ask-for-approval`. Log `runner.start … '--ask-for-approval', 'untrusted', 'exec'` + `codex.process.failed rc=2`. No tokens spent (clap exits before auth) |
| R15-CS1 | **Safe: reads work** | Fixed tree, restart dev, still Safe. Send `print the first line of README.md if there is one, otherwise list the files here` | Run completes (✓, not an error). Footer `🏷 <model> · safe`. `runner.start` args contain `'exec', '--json', '--skip-git-repo-check', '--color=never', '--sandbox', 'read-only', '-'` and **no** `--ask-for-approval` |
| R15-CS2 | **Safe: writes blocked by the sandbox, not by the model** | Send exactly: ``run this shell command exactly and show me its full output, do not change it: `touch safe-probe.txt; echo rc=$?` `` | Completes in normal time, no stall warning. **Required:** a failed `command_execution` row for that command with output containing `Read-only file system` (EROFS) **or** `Permission denied`, and a non-zero `rc=`. `test -e …/test-codex/safe-probe.txt` → false. **A model refusal (no command row) is a FAIL / re-run.** Record the item status and exact stderr |
| R15-CS3 | **Safe on resume** | Reply to CS2's resume line with exactly: ``run exactly `touch safe-probe-resume.txt; echo rc=$?` and show the output`` | `runner.start` args contain `'--sandbox', 'read-only', 'resume', '<thread_id>', '-'`. Same failed-row criterion as CS2, file absent, `git -C …/test-codex status --porcelain` unchanged. A refusal is a FAIL / re-run |
| R15-CS4 | **Full auto control + unchanged** | `/config` → Approval policy → **Full auto**, send the **same** CS2 prompt, then run **U1** | The command row succeeds with `rc=0` and `safe-probe.txt` **exists** (CS2's failure came from the sandbox). U1's file is created. Args contain neither `--sandbox` nor `--ask-for-approval`. Home hint reads `Codex's own sandbox`; the page copy includes the cache and `/tmp` warning. Clean up `hello.txt` / `safe-probe*.txt` |
| R15-CS5 | **Safe outranks a root-level bypass in `extra_args`** | `cp ~/.untether-dev/untether.toml /tmp/untether-dev.toml.cs5-bak`; run every variant inside `trap 'cp /tmp/untether-dev.toml.cs5-bak ~/.untether-dev/untether.toml && systemctl --user restart untether-dev' EXIT`. Variant 1: `[engines.codex] extra_args = ["-c", "notify=[]", "--dangerously-bypass-approvals-and-sandbox"]`. Variant 2: `extra_args = ["-c", "notify=[]", "-c", "sandbox_mode=\"danger-full-access\""]`. Restart dev, `/config` → **Safe**, send the CS2 prompt | With #209 landed, both variants are **rejected at load** (`codex.config.invalid`, `config.reload.failed` / refusal to start): record that as the pass for both. For a precedence check that #209 does not reject, use variant 3 `extra_args = ["-c", "notify=[]", "-s", "workspace-write"]`: `runner.start` shows the root `-s workspace-write` **before** `exec` and `'--sandbox', 'read-only'` after it; the touch row fails with EROFS / `Permission denied`, the file is absent. Control: variant 3 under **Full auto** → `rc=0` (delete the file). Afterwards `diff /tmp/untether-dev.toml.cs5-bak ~/.untether-dev/untether.toml` is empty and `/ping` answers |

### #209 — `extra_args` bypass flags refused

`~/.untether-dev/untether.toml` has `default_engine = "codex"` and `watch_config = true`; Claude is non-default. Back up first: `cp ~/.untether-dev/untether.toml /tmp/r15-1-dev.toml.bak`. Log check: `journalctl --user -u untether-dev -o cat --since "10 minutes ago" | grep -E "config.invalid|config.reload|setup.summary|setup.warning|runner.start"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-1-1 | **Claude bypass flag via hot-reload is refused ([#209](https://github.com/littlebearapps/untether/issues/209))** | (1) In the Claude chat send `/planmode on`. (2) Add `extra_args = ["--dangerously-skip-permissions"]` to `[engines.claude]`; save (hot-reload). (3) Send `run ls -la in the project root` (C1 shape). (4) Restore the backup | After (2): `claude.config.invalid` with `flags=['--dangerously-skip-permissions']`, `setup.summary` whose `found` lacks `claude`, `setup.warning engine=claude issue=…dangerously-skip-permissions…`, `config.reload.applied`. (3): the bot replies that the engine is unavailable, naming the flag, and **no run starts** (no `runner.start` for claude). After (4): the next reload lists `claude` in `found` again and C1 shows Approve/Deny buttons |
| R15-1-2 | **Codex bypass flag on the default engine keeps the old runtime** | (1) Append `[engines.codex]` with `extra_args = ["-c", "notify=[]", "--yolo"]`; save. (2) In the Codex chat send U1. (3) Restore the backup | (1): `codex.config.invalid flags=['--yolo']` then `config.reload.failed error=…'--yolo'…`; **no** `config.reload.applied` for that change. (2) runs normally; its `runner.start` argv has no `--yolo`. (3): `config.reload.applied` |
| R15-1-3 | **Spellings are caught end-to-end** | Repeat R15-1-2 (1) with each of `"-s=danger-full-access"`, `'-csandbox_mode="danger-full-access"'`, `'-cdefault_permissions=":danger-full-access"'`, `"-C/tmp"`, `"--"`; restore after each | Each gives `codex.config.invalid` naming `--sandbox`, `--config`, `--config`, `--cd`, `--` respectively and `config.reload.failed`; the `sandbox_mode` value never appears in the log line |
| R15-1-4 | **Legitimate passthrough still works** | Set `[engines.claude] extra_args = ["--strict-mcp-config"]` and `[engines.codex] extra_args = ["-c", "notify=[]", "-s", "workspace-write"]`; save; run U1 in both chats. Then `/config` → Codex Approval policy → Safe and send `print the first line of README.md`; set it back. Restore | `config.reload.applied`, `bad_config=[]`; both U1 pass; `runner.start` argv contains the passed flags. Safe variant: the run completes (no `cannot be used multiple times`), argv has `-s workspace-write` before `exec` and `--sandbox read-only` after it |
| R15-1-5 *(optional, startup path)* | **Default engine with a blocked flag refuses to start** | From the terminal: set R15-1-2's `--yolo` config, `systemctl --user restart untether-dev`, wait 10 s, `systemctl --user status untether-dev`; then restore and restart | Service not active / restarting; journal shows ``Invalid `codex.extra_args` in …; flag '--yolo' …``. After restore: startup message arrives, `/ping` answers |
| R15-1-cleanup | **Always** | `cp /tmp/r15-1-dev.toml.bak ~/.untether-dev/untether.toml`; if R15-1-5 ran, restart dev from the terminal | `diff /tmp/r15-1-dev.toml.bak ~/.untether-dev/untether.toml` empty; `/ping` in the Claude chat answers; the startup message has no `misconfigured:` / `failed to load:` note |

### #679 — voice SSRF guidance

Chat: Claude `5284581592` (any engine works: transcription happens before the runner). Clip: the
T1 pre-recorded OGG via `send_voice`.

**Safety.** Never restart `untether-dev` while `voice_transcription_base_url` is an IP literal
without an allowlist: config load fails and the dev bot won't start. Don't run a fake Whisper
server that logs headers — with an allowlist in place, the API key goes to whatever listens on
`:8000`. Always restore the backup at the end.

**Setup:** `cp ~/.untether-dev/untether.toml /tmp/untether-dev.toml.r15-10.bak`.

**Key-leak preflight (mandatory before R15-10c and again right before R15-10d):**
`ss -ltn | grep -E '[:.]8000\b' && { echo "ABORT: something listens on :8000"; exit 1; }`. Abort
R15-10c–d if anything is listening. For steps c–d also set
`voice_transcription_api_key = "r15-dummy"` (the backup restores the real key in R15-10g).

**Log grep:** `journalctl --user -u untether-dev -o cat --since "15 minutes ago" | grep -E "voice\.base_url\.|config\.reload\.(failed|transport_config_hot_reloaded)|openai\.transcribe\.error"`

| Step | Action | Expected Telegram | Expected log |
|---|---|---|---|
| R15-10a | **Localhost warns on reload ([#679](https://github.com/littlebearapps/untether/issues/679))** — edit the dev TOML: `voice_transcription_base_url = "http://localhost:8000/v1"`, no allowlist key | The "Hot-reloaded" notice (#548) | `config.reload.transport_config_hot_reloaded keys=['voice_transcription_base_url']`, then `voice.base_url.not_permitted phase=reload host=localhost … allowlist_key=voice_transcription_url_allowlist suggested_allowlist=['127.0.0.0/8']` [warning] |
| R15-10b | `send_voice` the T1 clip | A reply naming `` `localhost` `` with a toml block `voice_transcription_url_allowlist = ["127.0.0.0/8"]`; no URL path or port; **no run starts** | `voice.base_url.ssrf_blocked host=localhost port=8000 reason=blocked_address blocked_addresses=… suggested_allowlist=['127.0.0.0/8']` [error] |
| R15-10c | Run the preflight; add `voice_transcription_url_allowlist = ["127.0.0.0/8"]` and the dummy key | Hot-reload notice | `voice.base_url.permitted phase=reload host=localhost` [info] |
| R15-10d | Re-run the preflight, then `send_voice` again (nothing listens on `:8000`) | After the SDK retries (about 10–30 s): `couldn't reach the transcription service — transient network issue…`, which proves the guard passed | `openai.transcribe.error … error_type=APIConnectionError endpoint=http://localhost:8000/v1`; no `ssrf_blocked` |
| R15-10e | Remove the allowlist line and the dummy key (keep `localhost`), then `systemctl --user restart untether-dev` | The startup message arrives normally; `/ping` answers | `voice.base_url.not_permitted phase=startup host=localhost`. The bot is **not** blocked from starting. |
| R15-10f | Set `voice_transcription_base_url = "http://127.0.0.1:8000/v1"` (hot-reload only; **do not restart**) | No hot-reload notice (reload rejected); `/ping` still works | `config.reload.failed error=… voice_transcription_base_url is not permitted: Blocked: 127.0.0.1 …; to allow it, add "127.0.0.0/8" to [transports.telegram] voice_transcription_url_allowlist` |
| R15-10g | Restore: `cp /tmp/untether-dev.toml.r15-10.bak ~/.untether-dev/untether.toml`, then `send_voice` | A normal `🎙 <transcript>` echo and a normal run (T1 regression) | `voice.base_url.permitted phase=reload host=api.groq.com`; no `not_permitted` |

Then run Tier 7 (`/ping`, `/config`) to confirm nothing else regressed.

### #789 — default voice prompt vocabulary

Needs recorded clips (Nathan records them once as Telegram voice notes to Saved Messages): C2a–h
(bare "Claude" in varied positions), C3 (*"Yes, continue."*) and C5a–c ("cloud" controls). Clips live
only in `~/.untether-dev/test-clips/789/` and are deleted when the run ends. Never send client-chat
audio (C1) to the bot or Groq; score it offline with local Whisper, counts only. The plan's
pre-registered A/B criteria (offline, Groq + local Whisper, V0/V1/V2/no prompt) decide
ship / tie / no-improvement before this live row runs.

| # | Scenario | Steps | Pass criteria |
|---|---|---|---|
| R15-11 | **Voice vocabulary: bare "Claude" ([#789](https://github.com/littlebearapps/untether/issues/789))** | Back up the dev TOML (`cp -p ~/.untether-dev/untether.toml ~/.untether-dev/untether.toml.bak-789-$(date +%Y%m%dT%H%M%S)`) and set a restore trap. Comment out `voice_transcription_prompt` with `sed -i 's/^voice_transcription_prompt/# &/'` (never echo the value), and prove via `load_settings()` + `resolve_transcription_prompt()` that it resolves to the shipped default (print booleans only). Restart `untether-dev`. With `/planmode on`, `send_voice` C2a–h, C3 and C5a–c in the Claude chat, sending `/cancel` after each `🎙` echo. Then set a throwaway override (one hot-reload line, `keys=['voice_transcription_prompt']`, send C2c) and `""` (one hot-reload line, send C3). Restore the TOML from the backup even if the run aborts, re-run the loader check (expect `resolved_to_default=False`) and restart. | Every bare "Claude" is echoed as `Claude`, and `CLAUDE.md`/`AGENTS.md` appear as inline code. "cloud" is never turned into `Claude`. The short clip has no inserted terms. No `voice.transcribe.error`/`timeout`/`unexpected` or `config.read.toml_error`. `journalctl --user -u untether-dev --since "60 minutes ago" \| grep -c "AGENTS.md, Codex, OpenCode"` prints `0` (the prompt is never logged). Restored TOML `sha256sum` matches the backup. |

### #829 — background hold counts quiet time; honest close notices

Setup: `[watchdog] post_result_bg_max_hold = 60` in `~/.untether-dev/untether.toml`. The key is read **per spawn**, so start **every row with `/new`**; restore `1800` afterwards and send `/new` again. Chat: `ut-dev: Claude Code` (`-5284581592`). Log greps: `journalctl --user -u untether-dev`. Runs in the Claude-interactive block (after R15-6, before R15-19). Background: [`docs/findings/2026-09-30-claude-bg-agent-activity-and-eof.md`](../findings/2026-09-30-claude-bg-agent-activity-and-eof.md) — P0 selected **B2** (agents ignore EOF; SIGINT → rc 0, resumable), kept **A.2** (owned-foreground task marks its agent active) and the **output-file** Bash fallback.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R15-21a | `/new`; background Agent that reads and summarises 30 files one at a time (≈3–5 min); reply "STARTED" | panel advances past 60 s; **no** closing notice at 60 s; `🔔 Background task finished` wake turn at the end | ≥1 `claude.live_session.hold_rearmed source=task_progress`; **no** `stdin_closed reason=max_hold` before `claude.task.ended` |
| R15-21b | `/new`; background Bash `python3 -c "import time; time.sleep(600)"` (silent, idle); reply "STARTED" | at ≈60 s: `⏳ Closing session — 1 background task still running with no progress for 1 min: … Stopping it.` then `↩️ Reply to continue in the same session.` (silent) | `stdin_closed reason=max_hold` at 60–90 s; `lifecycle_exited reason=exited_after_close` (or `reader_done`/`cancelled` right after `stdin_closed`); `claude.live_session.closed quarantined=False`; **no** `session.quarantined` — the negative guard for the Bash fallback |
| R15-21c | right after 21b: "what did you just start?" | answer remembers the sleep task | `handle.incoming resume=<same sid>`; `claude.resume_guard.absorbed` (F11 stopped-task replay); no `session.resume_diverted_fresh` |
| R15-21d *(opportunistic)* | `/new`; background Agent told to run a foreground `python3 -c "import time; time.sleep(150)"` between files | no close during the sleep (A.2 kept) | `hold_rearmed source=agent_tool`; if it later closes: `exited_after_sigint stopped_clean=True`, no `session.quarantined` (B2) |
| R15-21e | `bg_hold_rearm_on_progress = false`; `/new`; repeat 21a | closing notice at ≈60 s despite progress: `… still running at the background hold limit: … Stopping it.`; then the `closed` line | `stdin_closed reason=max_hold`; no `hold_rearmed`; the agent ignores EOF, so `close_grace_expired` then `exited_after_sigint stopped_clean=True` |
| R15-21f | `/new`; background Bash printing `tick N` every 2 s for 3 min | no close while ticking; wake turn at the end | `hold_rearmed source=bash_output` |
### #416 — Codex reasoning `minimal` retired

Codex chat `4929463515` (Bot API `-4929463515`). **Precondition (read-only):** the chat must not be in `safe` mode — `jq '.chats["-4929463515"].engine_overrides.codex' ~/.untether-dev/telegram_chat_prefs_state.json` → `null`, and `grep -A8 '^\[engines.codex\]' ~/.untether-dev/untether.toml` shows no `permission_mode = "safe"`. If it ever does, record the value, switch to full auto via `/config`, run the rows, then restore it. Logs: `journalctl --user -u untether-dev -o cat --since "15 minutes ago" | grep -E "run.reasoning.unsupported_level_ignored|config.reasoning|model_reasoning_effort=minimal|session.auto_cleared|handle.(runner|worker)_failed"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-14a | **No `Minimal` button ([#416](https://github.com/littlebearapps/untether/issues/416))** | `/config` → Reasoning | Buttons `Low · Medium · High` / `Xhigh`, then `Clear override` / `← Back`. No `Minimal`, no "fastest responses" line |
| R15-14b | **`/reasoning` lists four levels** | `/reasoning` | `available levels: low, medium, high, xhigh` |
| R15-14c | **`/reasoning set minimal` refused** | `/reasoning set minimal` | `unknown reasoning level minimal` + the available levels; `/config` home unchanged |
| R15-14d | **Stale saved `minimal` is ignored with a note** | `/config` → Reasoning → **Low** (creates the override), then `jq '(.chats["-4929463515"].engine_overrides.codex.reasoning) = "minimal"' ~/.untether-dev/telegram_chat_prefs_state.json > /tmp/p && mv /tmp/p ~/.untether-dev/telegram_chat_prefs_state.json`; send `In one sentence: what is 2+2?` | Progress shows ``reasoning level `minimal` isn't supported for `codex` any more; using the engine default. …``; the run **succeeds**; footer has no `· minimal`; `/config` home shows `Reasoning: default · minimal not supported`. Exactly one `run.reasoning.unsupported_level_ignored engine=codex level=minimal`; `grep -c 'model_reasoning_effort=minimal'` → 0; no `session.auto_cleared engine=codex` |
| R15-14e | **Picking a level clears it** | `/config` → Reasoning → **Low**, send the same prompt | No note; footer `· low`; `config.reasoning.set engine=codex level=low`; no further `unsupported_level_ignored` |
| R15-14f *(optional)* | **Stale `Minimal` button from a pre-upgrade message** | Needs rc14 on the dev bot first (`git switch --detach 11cbee3`, restart dev, open `/config` → Reasoning so a `Minimal` button exists), then back to the rc15 branch, restart dev, tap that old **Minimal** | No "Reasoning: minimal" toast; the Reasoning page re-renders with current buttons; prefs hold no `minimal`; no `config.reasoning.unsupported_level` log. Unit test `test_reasoning_set_min_rejected_for_codex` covers the same callback |
| R15-14g *(optional, paid ≈ 1c)* | **U4 capture of the 400 wording** | Done during implementation (2026-09-30, codex-cli 0.157.1, gpt-5.5): `tests/fixtures/codex_turn_failed_minimal_reasoning.jsonl` | Specific hint for the web_search 400; broad hint for the unsupported-value 400 (`minimal` is rejected even with `-c web_search="disabled"`) |
| R15-14-cleanup | **Always** | `/config` → Reasoning → **Clear override** | `jq '.chats["-4929463515"].engine_overrides.codex'` back to its pre-run value |

Offline evidence (AC5, lba-1 only — CI has no `codex`, so a skip there is not evidence): `uv run pytest tests/test_codex_cli_schema_drift.py -v -rs -k "catalogue or listed_models or minimal"` shows `PASSED` for `test_bundled_catalogue_has_no_minimal`, `test_listed_models_support_untether_codex_levels` and `test_client_passes_minimal_unvalidated`.

### #383 — plan approvals are turn-scoped

Tier 2 (Claude interactive). Claude `ut-dev` chat `5284581592` (Bot API `-5284581592`). Pre-reqs: dev tree on `untether-dev` (restart it from a terminal, never from inside a dev-bot run), `/planmode on` in the chat, `/config` → Diff preview → on for R15-4c. Scratch dir `/tmp/r15-383/` (create it first; `rm -rf` it after). Log command for every step: `journalctl --user -u untether-dev --since "15 min ago" | grep -E "claude\.(permission_mode|plan_approval|live_session\.(injected|stdin_closed|plan_rearm))|claude\.turn\.started"`. Also run, unchanged: C1, C3 (the post-outline title carries the caption; the feedback edit reads `✅ Plan approved — Claude will carry it out now`), C5, C6, B-LIVE-3, RC13-5 (a) and (b), and Tier 7 `/ping`.

| ID | Steps | Expected Telegram | Expected logs |
|---|---|---|---|
| **R15-4a** Follow-up is planned again | 1. Send `Plan it first: create /tmp/r15-383/a.txt containing one.` 2. When the ExitPlanMode message appears, read its buttons with `list_inline_buttons`. 3. Tap **✅ Approve Plan**. 4. **Within 45 s** of the final (before the 60 s idle close), send `Now create /tmp/r15-383/b.txt containing two.` | Step 2: first button `✅ Approve Plan`; the title's caption line is `Approving lets Claude carry out this plan without further prompts. Plan mode resumes when this reply ends, or after the background agents it starts have finished.` Step 3: `a.txt` created, final delivered, feedback `✅ Plan approved — Claude will carry it out now · plan mode resumes when it's done`. Step 4: a **new ExitPlanMode approval** appears (Claude plans again); `test ! -e /tmp/r15-383/b.txt` holds until you tap Approve | `permission_mode.changed … to=default source=status` after step 3; then, **before** the final's delivery lines, exactly one `permission_mode.rearm_sent reason=idle` and `rearm_ack mode=plan` (never `reason=followup` in this step: the idle re-arm is written before the result is yielded); `live_session.injected` (proves the live path, not a resume); `plan_approval.cleared`; `turn.started reason=followup` |
| **R15-4b** Wake turn is planned (slow job) | Send `Plan: start "sleep 25 && echo done" as a background Bash task and end your reply; when it finishes, create /tmp/r15-383/c.txt.` Tap Approve Plan. Wait ~30 s | The wake message (`🔔 Background task finished …`) carries a **new** ExitPlanMode approval instead of writing `c.txt`; the file is absent until approved | `rearm_sent reason=idle` after the first final; `rearm_ack`; `turn.started reason=task_finished`; no `rearm_deferred` (bash doesn't defer) |
| **R15-4b′** Wake turn is planned (job finishes as the reply ends) | As R15-4b but with `sleep 2` and "end your reply immediately after starting it". Repeat 3 times | In each run, a new ExitPlanMode approval in the wake message, or (probe P-6 showed a residual window when the notification is queued at result time) a wake turn whose first step runs unplanned and which then asks for approval; record whether `c.txt` appears unapproved | Per run: the `rearm_sent reason=idle` timestamp precedes that run's `turn.started reason=task_finished`; record the gap. `c.txt` written without an approval is the known residual (first model call before the re-arm is read, and plan-mode writes reach Untether's autonomous stage-6 handler on CLI 2.1.285) — note it with the three timestamps rather than filing it as a #383 regression |
| **R15-4c** Diff-preview bypass ends with the reply (default allowlist) | Diff preview on. Send `Plan first: edit /tmp/r15-383/a.txt to say one-edited.` (create the file first). Approve the plan. Within 45 s send `Append a second line to /tmp/r15-383/a.txt.` | Reply 1: no diff-preview prompt after the plan approval (intra-turn #283 kept). Reply 2: plans again and, after approval, behaves exactly as reply 1 | `plan_approval.cleared reason=turn_boundary` at reply 2's `turn.started`. Under the default allowlist `Edit` never reaches stage 6, so the log line is the evidence here; R15-4c′ shows the gate itself |
| **R15-4c′** Diff-preview gate visibly returns (custom allowlist) | Set `[engines.claude] allowed_tools = ["Read"]` and `[watchdog] rearm_plan_mode = false` in `~/.untether-dev/untether.toml`, then restart `untether-dev` from a terminal (engine config). Diff preview on, `/planmode on`. Send `Plan first: edit /tmp/r15-383/a.txt to say two-edited.`, approve. Within 45 s send `Append "three" to /tmp/r15-383/a.txt.` Restore both keys and restart afterwards | Reply 1: after Approve Plan, the Edit runs with **no** diff prompt (intra-turn bypass). Reply 2 (no re-plan, because the re-arm is off): the `Edit` shows a **diff-preview approval** (`- …` / `+ …` lines with Approve/Deny). On rc14 it would have run silently | `control_request.diff_preview_gate tool_name=Edit` in reply 2; `plan_approval.cleared reason=turn_boundary` |
| **R15-4g** Post-outline approval carries one turn | Send a complex prompt; on the ExitPlanMode message tap **📋 Pause & Outline Plan**. If Claude writes the outline and **ends its reply** without new buttons (or shows the `da:` Approve Plan buttons), tap **✅ Approve Plan**, then send `go ahead`. Then, in a fresh plan, repeat but send two messages (`wait` then `go ahead`) after tapping | Run 1: Claude proceeds on `go ahead` **without** a second approval. Run 2: the second message's ExitPlanMode shows normal buttons again (the carry lasted one turn) | Run 1: `plan_approval.carried` at the `go ahead` turn, then `control_request.discuss_approved`. Run 2: `carried` at `wait`, `cleared` at `go ahead` |
| **R15-4d** Approved background agent isn't disrupted (C4) | Send `Plan: launch a background agent that waits 40 seconds and then writes /tmp/r15-383/agent.txt; end your reply while it works.` Approve. Within 20 s send `Quick question: what time is it?` | The question is answered promptly and its reply starts with `⚠️ Not re-planned: the approved plan's background agents are still running. Plan mode resumes when they finish.`; `agent.txt` **is** written ~40 s later (the agent was not switched back into planning); the agent's wake message (`🔔 Background task finished …`) runs planned — if it wants to act it raises a new ExitPlanMode approval — and a follow-up after it is planned again with no ⚠️ line | `rearm_deferred reason=live_agents agents=1 plan_exit_turn=1` at the first final (and again at the question's turn close); when the agent ends: `rearm_deferral_ended reason=agents_done`, `rearm_sent reason=agents_done` and `rearm_ack mode=plan` **before** `turn.started reason=task_finished`; never a `rearm_sent` while the agent runs |
| **R15-4e** Non-plan chats untouched | `/planmode off` (acceptEdits). Send `create /tmp/r15-383/e.txt`, then within 45 s `delete it`. Then `/planmode auto` and repeat once. `/planmode on` at the end | Normal behaviour for each mode; no plan buttons | **zero** `claude.permission_mode.rearm_*` lines for these runs; `live_session.injected` present |
| **R15-4f** Kill switch | Set `[watchdog] rearm_plan_mode = false` (read per spawn, no restart needed). Rerun R15-4a steps 1-4 | Step 2 caption has **no** "Plan mode resumes…" clause; step 4: Claude writes `b.txt` **without** a new plan (rc14 behaviour) | no `rearm_sent`; `plan_approval.cleared` still present. Restore `true` afterwards |
| **R15-4h** plan-auto (Decision 6 default) | `/planmode plan-auto`. Send `Plan: start a Monitor on "for i in 1 2 3; do sleep 10; echo tick $i; done" and after each tick append the tick to /tmp/r15-383/ticks.txt.` Then after the ticks, send `summarise ticks.txt` | No approval buttons at any point; ticks are appended without a plan per tick; the follow-up plans (a plan appears in the progress/final) and is auto-approved | No `rearm_sent reason=idle` for the tick turns; one `rearm_sent reason=followup` before the follow-up; one `control_request.auto_approve_exit_plan_mode` for the follow-up |

After the run, `journalctl --user -u untether-dev --since "1 hour ago" -p warning | grep -E "permission_mode|plan_rearm"` must be empty.
### #419 — Codex usage fields, per-thread token deltas, web-search titles

Engine chat: **Codex** `4929463515` (Bot API `-4929463515`). The footer needs #417 as well, so run R15-15a–c and R15-16a–e back to back in one Codex session. Log check: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "usage.token_delta|usage.token_delta_failed|jsonl.msgspec.invalid|runner.completed"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-15a | **Codex web-search titles ([#419](https://github.com/littlebearapps/untether/issues/419))** | Codex chat: `Use web search to find the latest release version of the openai/codex CLI and answer in one line.` (default `web_search` mode is `cached`, which still sends the tool) | While running, the progress shows a `web search` row (never a blank `searched: `); the same row completes as `searched: <query>` (or `opened: <url>` for a page open). Final answer renders. `grep -c jsonl.msgspec.invalid` = 0 since the restart. If Codex chose not to search, re-prompt once with `You must call the web search tool.`; if still no search, record **not exercised** |
| R15-15b | **Codex resumed run counts only its own tokens ([#419](https://github.com/littlebearapps/untether/issues/419))** | Codex chat: U1 prompt (`create a file called hello.txt with "hello world"`), then reply to its resume line: `now rename hello.txt to greetings.txt` | Logs: `usage.token_delta engine=codex source=new_session` for run 1, then `source=ledger` for run 2 with `input_delta` = run-2 `cumulative_input` − run-1 `cumulative_input` (compute from the two lines), `runs=2`. `runner.completed` for run 2 carries `input_tokens` equal to that delta. No `usage.token_delta_failed` |
| R15-15c | **Ledger survives restart** | After R15-15b: `systemctl --user restart untether-dev`, then reply to run 2's resume line: `say done` | `usage.token_delta … source=ledger runs=3` (not `baseline_unknown`) |

### #417 — `/usage` for non-Claude engines + Codex token footer

Chats: **Codex** `4929463515`, **OpenCode** `5200822877`, **Claude** `5284581592` (Bot API ids with a leading `-`). Run straight after R15-15a–c (same Codex session). Dev config has `[footer] show_api_cost = true`; if a chat has a per-chat footer override, clear it via `/config` first. Also scan `grep -E "usage.token_delta_failed|export_event.record_failed|usage.fetch_failed"` → 0 lines from the Codex/OpenCode chats.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-16a | **Codex token footer, per run ([#417](https://github.com/littlebearapps/untether/issues/417))** | Read the two finals from R15-15b | Run 1's final has a token-only footer `🔢<in>/<out>` (e.g. `🔢14.2k/310`; no `💰`, D9). Run 2's input figure matches `input_delta` of run 2's `usage.token_delta` line (compact-formatted) and is **smaller** than that line's `cumulative_input`. No token footer at all would have appeared on rc14. If R15-15c ran, run 3's footer has no `· thread total` suffix (`source=ledger`) |
| R15-16b | **Codex `/usage` = last session tokens** | In the Codex chat send `/usage`, then `/export` | `/usage` reply starts `📊 codex · last session in this chat`, shows `Session total:` / `Last run:` / `N runs` and the `/export` pointer; it never says `Usage tracking is not available`. `/export`'s header `N in / M out tokens` equals the last `usage.token_delta` line's `cumulative_input` / `cumulative_output` — the same thread total `/usage` summarises (the issue's "agree" criterion) |
| R15-16c | **No history after restart** | `systemctl --user restart untether-dev`, then `/usage` in the Codex chat before any prompt | `…not available for the codex engine, and this chat has no completed codex run since Untether last started…` |
| R15-16d | **OpenCode session totals** | OpenCode chat: U1 prompt, then reply to it with `now rename hello.txt to greetings.txt`, then `/usage`, then `/export` | `/usage` shows `2 runs`, a `Session total:` equal to the sum of the two runs' footers (within rounding), and `Last run cost: $…` if the model is priced. Logs: two `usage.token_delta engine=opencode source=per_run`. `/export` header shows cost, or tokens when the model is free |
| R15-16e | **Claude `/usage` unchanged** | Claude chat: `/usage` and `/usage debug` | Same subscription view as rc14 (C7); no `📊 claude · last session` text |

### #685 — control-button double tap

Tier 2 (Claude interactive) + T8 (stale button) + S9 (concurrent clicks). Claude `ut-dev` chat `5284581592` (Bot API `-5284581592`), `/planmode on`. Logs: `journalctl --user -u untether-dev -o cat --since "15 minutes ago" | grep -E "claude_control\.|control_response\.|callback\.answered"`.

| ID | Telegram action | Expected in Telegram | Expected in logs |
|---|---|---|---|
| **R15-5a** Approve twice | Send `run ls -la /tmp with Bash`; when the Approve/Deny keyboard appears, `press_inline_button` **Approve twice back to back** (two calls, no wait) | First press toast `Approved`; second press toast `Already answered` (or `Approved`, if both were in flight); a silent `ℹ️ Already answered — approved` line; the tool runs once | Exactly **one** `control_response.sent approved=True request_id=<X>` and **one** `claude_control.sent approved=True request_id=<X>`; one `claude_control.already_handled request_id=<X> first_action=approve`; **no** `callback.failed` |
| **R15-5b** Outline, then Approve | A prompt that triggers ExitPlanMode (C3 shape); press **📋 Pause & Outline Plan**, then at once **✅ Approve Plan** on the same keyboard | Second press toast `Already answered`; silent line `ℹ️ Already answered — outline requested`; Claude writes the outline (not an approval) | One `control_response.sent approved=False`; `claude_control.already_handled first_action=discuss`; **no** `claude_control.sent approved=True` for `<X>` |
| **R15-5c** Stale tap | After the run finishes, `/new`, then replay the old callback. If `press_inline_button` can't reach a stripped keyboard, use the Telethon venv fallback (`GetBotCallbackAnswerRequest(peer, msg_id, data=b"claude_control:approve:<X>")`) | Toast `Already answered` (the id is still in the LRU) | `claude_control.already_handled`; no WARNING |
| **R15-5d** Unknown id | Replay a made-up id: `data=b"claude_control:approve:00000000-0000-4000-8000-000000000000"` | Toast `This request has expired`; message `⚠️ Control request not found or session ended` | WARNING `claude_control.not_found reason=unknown` |
| **R15-5e** `da:` Deny → Approve (opportunistic) | If the escalation path is reachable (Pause & Outline, then Claude calls ExitPlanMode without an outline): press **❌ Deny** then **✅ Approve Plan** on the `da:` keyboard | Second press `Already answered — denied`; the next ExitPlanMode shows buttons again (not auto-approved) | No `control_request.discuss_approved` after the deny; `claude_control.already_handled first_action=deny`. Record "not exercised" if Claude always writes the outline first |

Pass = R15-5a–d match, plus C1/C2/C3/C6/T8/S9 unchanged. Post-rollout invariant (nsd, 24 h): every `claude_control.sent` has a `control_response.sent` for the same `request_id` within 1 s before it — unpaired count must be 0.
### #819 — Claude context-window use (`% ctx`)

Chats: **Claude** `5284581592`, **Codex** `4929463515` (Bot API ids with a leading `-`). Claude-only (the Codex half is [#832](https://github.com/littlebearapps/untether/issues/832)). This lane ships #819 C1–C2 only; R15-19c (manual `/compact` rows) and R15-19e (auto-compaction rows) join once C3/C4 land. Log sweep: `journalctl --user -u untether-dev -o cat --since "60 minutes ago" | grep -E "claude\.context\.(window_learned|over_window|window_miss)|runner\.completed|jsonl\.msgspec\.invalid"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-19a | **Header `% ctx` ([#819](https://github.com/littlebearapps/untether/issues/819))** | Claude chat, U2 prompt | Progress header gains `· NN% ctx` once the first answer frame arrives (or at the final on the first run after a restart for an unseen model); final `done · claude · … · step N · NN% ctx`; the `🏷` footer line is byte-identical to a pre-rc15 final (no ctx segment). Log once: `claude.context.window_learned model=<id> context_window=<n>`; `runner.completed … context_pct=NN` |
| R15-19b | **Turn header** | Reply to R15-19a's answer within 60 s with a follow-up | Follow-up final header carries `% ctx` ≥ R15-19a's; `claude.live_session.injected` |
| R15-19d | **`/compact` outside the live window (unchanged behaviour)** | Wait > 60 s for idle close (`claude.live_session.stdin_closed reason=idle…`), then send `/compact` | rc15 does **not** change this path (bare-`/compact` handling is an rc16 follow-up): the run is resumed with the preamble-prefixed text, Claude answers in prose, no 🗜️ row, no `runner.empty_result`. Records the baseline |
| R15-19f | **Toggle** | Set `[progress] show_context_usage = false` in `~/.untether-dev/untether.toml` (hot reload), run U1 | No `% ctx` in progress or final. Revert, run U1 again: the segment is back without a restart |
| R15-19g | **Codex regression** | Codex chat U1 + U4 | Header has no `% ctx`; no new WARN/ERROR |
