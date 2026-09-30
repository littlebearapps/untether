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
| RC12-10 | **Voice vocabulary ([#789](https://github.com/littlebearapps/untether/issues/789))** | `send_voice` a clip saying *"open CLAUDE dot MD and AGENTS dot MD and summarise them"* with no `voice_transcription_prompt` set in the dev config. | Transcript contains `CLAUDE.md` and `AGENTS.md` (not "Claw.md"); both render as inline code in the echoed transcript. Effect is model-dependent, so a near-miss is a soft fail: note it and don't block the release. |

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
| Telegram transport (`telegram/*.py`) | T1-T10, S7, S8 |
| Control channel (`claude_control.py`) | C1-C6, T8, S9 |
| Config/settings (`settings.py`) | O1-O9, S5, upgrade path |
| Cost tracking (`cost_tracker.py`) | B1-B3, U8 |
| Progress/formatting (`markdown.py`, `telegram/render.py`) | U3, T6, T7, S4, S8, RC12-8, RC12-9 |
| Commands (`commands/*.py`) | Tier 7 (all), specific command test |
| File transfer (`file_transfer.py`) | T2, T3, T5 |
| Voice (`voice.py`) | T1, RC12-10 |
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

## rc15 scenarios (0.35.5rc15)

### #383 — plan approvals are turn-scoped

Tier 2 (Claude interactive). Claude `ut-dev` chat `5284581592` (Bot API `-5284581592`). Pre-reqs: dev tree on `untether-dev` (restart it from a terminal, never from inside a dev-bot run), `/planmode on` in the chat, `/config` → Diff preview → on for R15-4c. Scratch dir `/tmp/r15-383/` (create it first; `rm -rf` it after). Log command for every step: `journalctl --user -u untether-dev --since "15 min ago" | grep -E "claude\.(permission_mode|plan_approval|live_session\.(injected|stdin_closed|plan_rearm))|claude\.turn\.started"`. Also run, unchanged: C1, C3 (the post-outline title carries the caption; the feedback edit reads `✅ Plan approved — Claude will carry it out now`), C5, C6, B-LIVE-3, RC13-5 (a) and (b), and Tier 7 `/ping`.

| ID | Steps | Expected Telegram | Expected logs |
|---|---|---|---|
| **R15-4c** Diff-preview bypass ends with the reply (default allowlist) | Diff preview on. Send `Plan first: edit /tmp/r15-383/a.txt to say one-edited.` (create the file first). Approve the plan. Within 45 s send `Append a second line to /tmp/r15-383/a.txt.` | Reply 1: no diff-preview prompt after the plan approval (intra-turn #283 kept). Reply 2: plans again and, after approval, behaves exactly as reply 1 | `plan_approval.cleared reason=turn_boundary` at reply 2's `turn.started`. Under the default allowlist `Edit` never reaches stage 6, so the log line is the evidence here; R15-4c′ shows the gate itself |
| **R15-4c′** Diff-preview gate visibly returns (custom allowlist) | Set `[engines.claude] allowed_tools = ["Read"]` and `[watchdog] rearm_plan_mode = false` in `~/.untether-dev/untether.toml`, then restart `untether-dev` from a terminal (engine config). Diff preview on, `/planmode on`. Send `Plan first: edit /tmp/r15-383/a.txt to say two-edited.`, approve. Within 45 s send `Append "three" to /tmp/r15-383/a.txt.` Restore both keys and restart afterwards | Reply 1: after Approve Plan, the Edit runs with **no** diff prompt (intra-turn bypass). Reply 2 (no re-plan, because the re-arm is off): the `Edit` shows a **diff-preview approval** (`- …` / `+ …` lines with Approve/Deny). On rc14 it would have run silently | `control_request.diff_preview_gate tool_name=Edit` in reply 2; `plan_approval.cleared reason=turn_boundary` |
| **R15-4g** Post-outline approval carries one turn | Send a complex prompt; on the ExitPlanMode message tap **📋 Pause & Outline Plan**. If Claude writes the outline and **ends its reply** without new buttons (or shows the `da:` Approve Plan buttons), tap **✅ Approve Plan**, then send `go ahead`. Then, in a fresh plan, repeat but send two messages (`wait` then `go ahead`) after tapping | Run 1: Claude proceeds on `go ahead` **without** a second approval. Run 2: the second message's ExitPlanMode shows normal buttons again (the carry lasted one turn) | Run 1: `plan_approval.carried` at the `go ahead` turn, then `control_request.discuss_approved`. Run 2: `carried` at `wait`, `cleared` at `go ahead` |
