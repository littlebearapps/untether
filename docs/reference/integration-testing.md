# Integration Testing

Structured, repeatable integration test process run against `@untether_dev_bot` before every release. Tests exercise all 5 supported engines across the full feature surface.

> **Deprecated engines are out of the matrix.** `gemini` and `amp` are deprecated
> and no longer supported (they may be removed in a future release). Both are currently non-functional on the
> dev host — Gemini rejects individual accounts (upstream EOL 2026-06-18) and AMP
> returns `426` for out-of-date clients — so they **cannot** pass U1 and are no
> longer required at any release tier. Their chats and test projects stay in
> place for opt-in spot checks only. See
> [`runner-development.md`](https://github.com/littlebearapps/untether/blob/master/.claude/rules/runner-development.md) →
> "Deprecated engines — sweep exemption".

## Infrastructure

| | Details |
|---|---|
| **Dev service** | `untether-dev.service` → `@untether_dev_bot` |
| **Test projects** | `test-projects/test-{claude,codex,opencode,pi,antigravity}/` (plus deprecated `test-{gemini,amp}/`) |
| **Test chats** | 7 dedicated Telegram groups in the `ut-dev` folder, one per engine (2 deprecated) |
| **Engines** | Claude, Codex, OpenCode, Pi, Antigravity (⚠️ Gemini, Amp — deprecated, opt-in only) |

Antigravity (`agy`, [#558](https://github.com/littlebearapps/untether/issues/558)) joins the matrix with 0.36.1rc1. Of the five fleet hosts only lba-1 has `agy` installed, and its test project and test chat are set up as part of the 0.36.1rc1 run (see "0.36.1rc1 scenarios / results" at the end of this file).

## Automated Testing via Telegram MCP

All integration test tiers are fully automated by Claude Code using Telegram MCP tools and the Bash tool. The relevant MCP tools are:

- `send_message` — send test prompts and commands to engine chats
- `get_history` / `get_messages` — read back bot responses and verify expected behaviour
- `list_inline_buttons` — inspect inline keyboards (approval buttons, `/config` menus, `/browse`)
- `press_inline_button` — interact with inline keyboards (approve/deny, toggle settings)
- `reply_to_message` — reply to resume lines for session continuation tests (U4)

### Test chats

Tests are sent to the 5 supported engine chats (plus 2 opt-in chats for the deprecated, unsupported Gemini and AMP engines) via `@untether_dev_bot` (bot ID `8678330610`).
For DM-only tests (commands, `/at`, `/cancel`), use Nathan's personal DM chat ID with the bot — **not** the bot ID itself. The bot ID identifies the bot account; private chats are addressed by the user's chat ID. Resolve via the Telegram MCP `resolve_username` or by inspecting incoming `update.message.from.id` in the dev logs.

| Chat | Chat ID | Bot API chat_id |
|------|---------|-----------------|
| Claude Code | `5284581592` | `-5284581592` |
| Codex CLI | `4929463515` | `-4929463515` |
| OpenCode | `5200822877` | `-5200822877` |
| Pi | `5156256333` | `-5156256333` |
| Antigravity CLI | `5540298446` | `-5540298446` |
| Gemini CLI | `5207762142` | `-5207762142` |
| AMP CLI | `5230875989` | `-5230875989` |

> **Note:** The Telegram MCP (Telethon) accepts both positive and negative chat IDs.
> If a positive ID fails with `GEN-ERR-582` (PeerUser lookup), use the negative Bot API form.
> The negative-form retry in `resolve_entity()` started as a local patch (2026-04-14) and is now upstream:
> lba-1 runs telegram-mcp v3.2.8 with no local patch.

### Workflow

1. Claude Code sends a test prompt via `send_message` to the appropriate engine chat
2. Waits for the bot to process (sleep or poll via `get_history`)
3. Reads back the response via `get_history`/`get_messages` and verifies expected content
4. For interactive tests: uses `list_inline_buttons` and `press_inline_button` to interact with approval/config buttons
5. For resume tests: uses `reply_to_message` to reply to the resume line

### Additional MCP tools for media tests

- `send_voice` — send an OGG/Opus voice file as a voice message (for T1)
- `send_file` — send a file with optional caption (for T2, T3, T5)

`send_file` / `send_voice` only accept paths under the MCP's workspace root, so stage upload files under a gitignored path in the repo (e.g. `test-projects/.r15-src/`) and delete them afterwards.

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
- **B4 (SIGTERM drain)** — use Bash tool: `kill -TERM $(systemctl --user show -p MainPID --value untether-dev)` (never `pgrep -f '.venv/bin/untether'` or `pidof untether`: the demo, dev-hf and dev-ws instances run the same binary, and `pidof` also matches staging)
- **B5 (log inspection)** — use Bash tool: `journalctl --user -u untether-dev --since "1 hour ago"`

## Engine Feature Matrix

| Capability | Claude | Codex | OpenCode | Pi | Antigravity | Gemini ⚠️ | Amp ⚠️ |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **Support status** | Yes | Yes | Yes | Yes | Yes | Deprecated | Deprecated |
| Interactive approval | Yes | - | - | - | - (pre-run mode; gate in rc3) | Flag only | - |
| Plan mode | Yes | - | - | - | rc3 (gate) | - | - |
| Ask questions | Yes | - | - | - | - | - | - |
| Resume/continue | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Model override | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Reasoning levels | Yes | Yes | - | - | Yes (per model) | - | - |
| API cost tracking | Yes | - | Yes | - | - (tokens only) | Yes | Yes |
| Subscription usage | Yes | - | - | - | Quota groups (`/usage`) | - | - |
| Diff preview | Yes | - | - | - | - | - | - |

Antigravity in 0.36.1rc1 has two permission modes, picked before the run: **Workspace** (the default: file edits, no
shell) and **Full access** (explicit). **Ask me** and **Plan first** are shown as `· soon` and are refused before
anything spawns until the approval gate ships in rc3. A blocked tool shows as a `⚠️ Blocked: …` progress row.

---

## Test Tiers

### Tier 1: Universal Tests (all 5 supported engines)

Run in every supported engine's dedicated chat. Validates the core event pipeline.
The deprecated `gemini` and `amp` chats are excluded — they cannot pass U1 and are
not required at any tier.

In the Antigravity chat the default **Workspace** mode is enough for every row: U4's rename and U7's read of a
missing file need no shell. U6 needs a prompt that runs long without a shell command (for example a 1500-word essay),
because Workspace blocks shell loops.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| U1 | **Basic prompt** | `create a file called hello.txt with "hello world"` | Progress messages appear, final answer renders, footer shows model name, resume line present | #62 (missing model), #65 (footer repeat), stream threading (#98) |
| U2 | **Multi-tool prompt** | `list the files in this directory, then read the README if one exists` | Multiple action phases show in progress, tool names visible in verbose mode | Event counting, action tracking |
| U3 | **Long response** | `write a detailed explanation of how TCP/IP works, at least 2000 words` | Message splits correctly across multiple Telegram messages, no truncation, footer only on last chunk | #65 (footer repeat), #59 (entity overflow), message splitting |
| U4 | **Resume session** | After U1 completes, reply to the resume line: `now rename hello.txt to greetings.txt` | Resume token works, session continues, new progress + final answer | Resume token parsing per engine |
| U5 | **Model override** | `/model set <name>` (the `/config` → Engine & model page points there; it has no picker), then send a prompt; `/model clear` afterwards | Footer shows overridden model name | #77 (AMP model flag), build_args correctness |
| U6 | **Cancel mid-run** | Send a long prompt (not a bare `sleep N` — Claude Code may block a standalone foreground sleep (`Blocked: standalone sleep 90` seen on rc18); use e.g. a `for i in $(seq 1 90); do sleep 1; done` loop), then `/cancel` before it finishes | Run stops, completion message appears, no orphan process | Graceful cancellation, process cleanup |
| U7 | **Error handling** | Send a prompt that will fail (e.g. `read /nonexistent/file/path`) | Error renders in Telegram, no crash, session ends cleanly | Stderr sanitisation (#85), error formatting |
| U8 | **/usage** | `/usage` after a completed run | Claude: subscription info; Codex/OpenCode/Pi: last-session token totals (`📊 <engine> · last session in this chat`, #417); Antigravity: per-group quota + last-session tokens | #89 (429 handling), cost tracking |
| U9 | **/export** | `/export` after a completed run | A reply with an attached `untether-export-<engine>-<sid>-<stamp>.md` document captioned `📄 Session export — <engine> · N events · Markdown` / `Session: <id>` (`/export json` attaches a `.json`). The file has the session header, `**Usage:** … · last run` (`· thread total` for Codex), every action and the full, untruncated answers; it doesn't include the user prompts. Log: `command.attachment_sent command=export` | #63 (missing usage in export), #418 (file attachment) |
| U10 | **/browse** | `/browse` | File browser appears with inline keyboard, can navigate directories | Browse command, path traversal safety |

### Tier 2: Claude-Specific Tests (interactive features)

Run in the Claude test chat only. C1, C2, C5 and C6 need `/planmode off` (Accept edits); C3 needs `/planmode on`. Since #749, plan mode never raises a Bash or Edit button, so C1 cannot pass with plan mode on ([#747](https://github.com/littlebearapps/untether/issues/747)).

> **Dev-config precondition (rc15 run).** `~/.untether-dev/untether.toml` has `[engines.claude] allowed_tools = ["Bash", "Read"]`, so **Bash never raises an approval button** on the dev bot (`claude.allowed_tools.prompting_mode_override`, #749), and `~/.claude/settings.json` allows `WebFetch(domain:example.com)`. Approval recipes therefore use **Write to a path outside the project** (acceptEdits auto-accepts writes inside it) or **WebFetch to a domain that isn't allowlisted** (e.g. `https://www.iana.org`). This applies to C1, C2, C6, R15-5a, R15-6a, R15-7d and R15-8k.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| C1 | **Tool approval** | `/planmode off`, then `use the Write tool to create /tmp/c1-probe containing x` (outside the project) | Approve/Deny/Discuss buttons appear, clicking Approve proceeds, tool executes | #104 (buttons not appearing), #103 (progress stuck) |
| C2 | **Tool denial** | `use WebFetch to fetch https://www.iana.org and summarise it`, click Deny | Denial message reaches Claude, Claude acknowledges and continues | #66 (deny retry loop) |
| C3 | **Plan mode outline** | Send a complex prompt, click "Pause & Outline Plan" | Claude writes outline, then Approve/Deny/Let's discuss buttons appear automatically | Outline flow (#87; the cooldown was retired in [#570](https://github.com/littlebearapps/untether/issues/570)), post-outline approval |
| C4 | **Ask question** | Send a prompt that triggers AskUserQuestion (e.g. `should I use TypeScript or JavaScript for this?`) | Question appears with option buttons, user reply routes back to Claude | AskUserQuestion flow |
| C5 | **Diff preview** | `/planmode off`, `/config` → Diff preview → on, then `create /tmp/c5.txt containing hello` (outside the project; R15-8n confirmed this recipe in the rc15 run) | The Write approval shows a fenced diff block: `📝 /tmp/c5.txt` then `+ hello`; for an Edit, removed lines start `- ` and added lines `+ ` (before the rc15 fix added lines rendered as `- ` list items) | Diff preview rendering |
| C6 | **Rapid approve/deny** | Two WebFetches to non-allowlisted domains: approve the first, quickly deny the second | No spinner hang, no stale buttons, clean state transitions | Early callback answering, button cleanup |
| C7 | **Subscription usage** | `/usage` with subscription footer enabled | Shows 5h/weekly format | Subscription footer rendering |

### Tier 3: Telegram Transport Tests

Tests specific to how Untether uses Telegram — message formatting, media, input types. Run in any engine chat unless noted.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| T1 | **Voice message** | Record and send a voice note as prompt | Transcription appears, prompt runs, response renders | Voice transcription pipeline, codec handling |
| T2 | **File upload** | Send a file with caption `/file put src/test.txt` | File appears in project directory, confirmation message | File transfer, path safety, size limits |
| T3 | **File download** | `/file get CLAUDE.md` (every test project has one; not all have a README) | File downloads to Telegram chat | File serving, MIME types |
| T4 | **Forward coalescing** | Send a prompt and forward 3 messages within `forward_coalesce_s` (1 s) — forwards on their own, with no prompt, are ignored by design (`forward.message.ignored`, DEBUG) | Messages combined into single prompt, one run starts (not three) | `forward_coalesce_s` debounce, metadata annotation |
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
| O2 | **Reasoning level** | `/config` → Reasoning → pick a level (e.g. Low), then send a prompt (Claude and Codex; Antigravity's page is **Effort**, with the levels its current model accepts) | Reasoning model used, footer reflects it | Reasoning flag in build_args |
| O3 | **Listen mode** | `/listen mentions` in group, send plain text, then `@bot do something` | Plain text ignored, @mention triggers run | Listen mode filtering (renamed from `/trigger` in v0.35.3 [#297](https://github.com/littlebearapps/untether/issues/297); deprecated alias still works) |
| O4 | **Ask mode toggle** | `/config` → ❓ Ask mode → off, send prompt that would trigger AskUserQuestion | Question auto-denied instead of shown | Ask mode auto-deny path |
| O5 | **Context set** | `/ctx set test-claude main`, send prompt | Run uses test-claude project on main branch | Context resolution, project switching |
| O6 | **Context clear** | `/ctx clear`, send prompt | Falls back to chat/project default | Context fallback chain |
| O7 | **Chat session mode** | Set `session_mode = "chat"` in config (the dev config already has it), restart dev bot, send prompt 1, then prompt 2 (no reply) | Prompt 2 continues same session without needing resume reply | Stateful session mode |
| O8 | **Override persistence** | Set `/agent set pi`, restart dev bot, send prompt | Pi still runs — override survived restart | State file persistence |
| O9 | **Override clear** | `/agent clear`, send prompt | Falls back to project/global default engine | Override cleanup |

### Tier 5: Cost, Budget, and Operational

Tests for cost tracking, budget enforcement, and operational commands.

| # | Test | What to send | What to verify | Catches |
|---|------|-------------|----------------|---------|
| B1 | **Budget Stop at limit** | Set `[cost_budget] enabled = true`, `max_cost_per_day = 0.01`, `auto_cancel = true` (hot-reloads), send two prompts; tap **Run anyway**. Then `max_cost_per_run = 0.01` and a prompt that starts a background task | 2nd prompt refused with `🛑 Daily budget reached …` + **Run anyway**, which runs it once; per-run: first reply carries `🛑 Stopped: run cost …` and the session closes with no wake turn | Daily gate, Run anyway, turn-boundary stop (#896) |
| B2 | **Daily budget warning** | Set `[cost_budget] enabled = true`, `max_cost_per_day = 0.05`, run several cheap prompts | Warning appears when approaching threshold | Daily accumulation, warn_at_pct |
| B3 | **/stats** | Run several prompts across engines, then `/stats` | Per-engine run counts, action counts, durations render | Stats aggregation |
| B4 | **SIGTERM drain** | Start a run, then `kill -TERM $(systemctl --user show -p MainPID --value untether-dev)` from shell | Active run drains, completion message sent, bot exits cleanly | Signal handling, graceful shutdown |
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
| S6 | **Empty/whitespace prompt** | Send an empty forward (Telegram clients and Telethon refuse to send a whitespace-only text message, so that variant can't be exercised) | Bot handles gracefully, no crash | Input validation |
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
| Q4 | `/export` | Export file (📎 `.md` + caption) or "no session" | 1s |
| Q5 | `/browse` | File browser | 1s |
| Q6 | `/verbose` | Toggle confirmation | 1s |
| Q7 | `/cancel` | "Nothing running" or cancels | 1s |
| Q8 | `/planmode show` (Claude chat) | Current permission mode, unchanged (bare `/planmode` toggles the mode, so a smoke test must not send it) | 1s |
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

> Release-specific sections: rc4, rc7, B-LIVE and rc12–rc14 follow here; **rc15 and rc17 are at the end of this
> file**, after "Known Limitations and Gotchas" (rc16 shipped the rc15 run's fixes and added no section of its own).

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
| B-LIVE-5 | Monitor ticks | Monitor a 3-tick loop (10 s apart), end turn; reply "TICK n" per tick | since rc13 ([#785](https://github.com/littlebearapps/untether/issues/785)) each tick is folded into the background status message as a `↳ TICK n` line (`background_status.folded`, `live_turn.fold_decision decision=fold`), not a separate message; the stream end arrives as a 🔔 wake |
| B-LIVE-6 | `/cancel` idle session, then resume | start a 600 s background task, end turn; `/cancel`; then ask a question | `⏹ Stopped 1 background task: …`; `claude.live_session.stdin_closed reason=cancel`; the question resumes the same session and gets a real answer; `claude.resume_guard.absorbed`; no `runner.empty_result` |
| B-LIVE-7 | Approval inside a wake turn (plan mode) | start a 20 s background task, end turn; "when it finishes, create /tmp/x via ExitPlanMode" | the ExitPlanMode keyboard renders on the **wake turn's** progress message; Approve → file written; wake final delivered |

**Required tiers:** rc7 → Tier 7 (command smoke) + Tier 1 (Claude only) + B-RESUME. rc8 → add Tier 1 (all 4 supported engines, confirm no cross-engine regression from the quarantine store) + Tier 2 (interactive/plan).

Automate via Telegram MCP (`send_message`, `get_history`) + Bash (`journalctl --user -u untether-dev`) exactly as the other tiers. See `scripts/audit-noop-resume.sh` for the post-deploy fleet-wide correlation check (Layer 4 of the remediation plan) that runs the same five-event correlation across all hosts after rollout.

---

## rc12 scenarios (0.35.5rc12)

Run these in addition to the standard tiers and B-LIVE for rc12. Unless noted, use the Claude chat (`5284581592`). Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| RC12-1 | **Per-run stream binding ([#510](https://github.com/littlebearapps/untether/issues/510))** | Start a long Claude run in the Claude chat (e.g. `run sleep 90 in the foreground, then say DONE`). While it runs, send a short Claude prompt in a second chat (the Codex chat with a `/claude` directive, see T9). | Two `session.summary` lines with **different** `session_id`s; the short run's `event_count` / `duration_seconds` are its own (small), and the long run's summary, written after the short one finished, shows its own `event_count` and `last_event_type=result` — never the short run's values. The long run shows no stall warning or wake-up countdown borrowed from the other chat. |
| RC12-2 | **No false rate-limit notes ([#790](https://github.com/littlebearapps/untether/issues/790))** | Run U1-U4 and B-LIVE-1 in the Claude chat; also `uv run pytest tests/test_claude_cli_schema_drift.py` against the installed CLI. | No `⏳ Rate limited` note on healthy runs; no `claude.rate_limit_event` line with `retry_after_source=bare` or `default` unless a real `rejected` snapshot arrived. A `⚠️ 5h limit N% used — resets HH:MM` note appears at most once per window, and only if utilisation is ≥ 70%. Drift test passes (or skips when the CLI is absent). |
| RC12-3 | **API-retry note ([#792](https://github.com/littlebearapps/untether/issues/792))** | Opportunistic: only if a `claude.api_retry` line appears during the session (Anthropic 429/529/5xx). | The progress message shows one `🔁 API error <status> (<category>) — retrying in Ns (attempt n/m)` line that updates in place; no stall WARN during the back-off (`threshold_reason=api_retry_waiting`); `claude.api_retry` is INFO, WARN only on the final attempt. If no retry occurs, mark *not exercised*, not fail. |
| RC12-4 | **Live-idle hold is not a stall ([#787](https://github.com/littlebearapps/untether/issues/787))** | Set `[watchdog] subagent_timeout = 120` (the Claude CLI always has child processes — its MCP servers — so the threshold is `active_children` = `subagent_timeout`, 900 s by default, not 300 s), start a 200 s background sleep (`python3 -c "import time; time.sleep(200)"`, `run_in_background`), end the turn; wait for the 🔔 wake; restore the key. Without the override, the hold must exceed 900 s | No `progress_edits.stall_detected` and no stall message during the hold; one `progress_edits.stall_live_idle_suppressed` INFO; the run's `session.summary` has `stall_warnings=0`, `peak_live_idle_seconds` close to the hold, and a small `peak_idle_seconds`. |
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
| RC13-5 | **Steer ([#775](https://github.com/littlebearapps/untether/issues/775))** | (a) run a 40 s foreground Python sleep, then `/steer also tell me the hostname`; (b) `/config` → ↪️ Follow-up → Steer, run `echo hi` + a 400-word story, send a plain message while the story streams; (c) with steer on, `/cancel` a run then send a question; (d) Codex chat: `/steer hi`; then `/queue` to reset | (a) `↪️ Steered into the current run.`, a `↪️ steer received` progress row, one final answering both (`claude.live_session.injected_absorbed`); (b) toast `Follow-up: steer`; a plain message sent while the story streams is steered into the run and answered in the same final (it only runs as its own turn when the session is idle); (c) `steer_window_closed reason=cancel`, `steer.fallback reason=no_live` and a normal resumed answer (a plain message gets no notice; only an explicit `/steer` replies `↪️ No live Claude run to steer — queued instead.`); (d) `↪️ Steer isn't supported on codex — queued instead.`; an idle live session gets no steer ack |
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
| RC14-3 | **Cancelled message survives restart ([#810](https://github.com/littlebearapps/untether/issues/810))** | Start a 60 s foreground run, `/cancel` it, then `systemctl --user restart untether-dev` | The message still reads `cancelled` after the restart (no `⚠️ interrupted by restart`); no orphan relabel for that message id at startup (`progress_persistence.released reason=cancelled` is DEBUG, so it isn't in the journal) |
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
UNTETHER_CONFIG_PATH=/tmp/staging-config-backup.toml uv run python -c "from untether.settings import load_settings; load_settings()"

# Verify new config keys have defaults (old configs missing them still work)
diff ~/.untether/untether.toml ~/.untether-dev/untether.toml
```

### Rollback safety

```bash
# Before releasing: verify the previous version still installs and starts
pip install untether==$CURRENT_PROD_VERSION --dry-run

# After release: if issues found, rollback path is:
# 0. clean each host's untether.toml + chat prefs first (see below)
# pipx install untether==$OLD_VERSION && systemctl --user restart untether   # lba-1 staging only
# scripts/fleet-rollback.sh $OLD_VERSION [--only HOST]                         # all 5 hosts
```

**Config cleanup before a rollback.** Older versions reject config keys they don't know (every section is
`extra="forbid"`), and `fleet-rollback.sh` reinstalls the package without touching config. Before rolling back across a
release that added keys, remove them from each host's `untether.toml`, or the config won't load (an unknown key on a
cron instead switches off every cron and webhook via `triggers.init_failed`). For 0.36.0 → 0.35.4 that means about 20
keys plus reverting `plan-auto` → `auto` in the toml, crons and chat prefs; the full list is in
[Rolling back to 0.35.4](../how-to/update.md#rolling-back-to-0354)
([#956](https://github.com/littlebearapps/untether/issues/956)). Check with
`journalctl --user -u untether --since "2 minutes ago" | grep -E "extra_forbidden|Extra inputs|triggers.init_failed|permission-mode"`
after the restart.

### State file compatibility

If any state files exist (chat preferences, topic state), verify they survive upgrade:

```bash
# Check state files before upgrade
ls -la ~/.untether-dev/*.json   # state files live directly in the config dir

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
   Claude Code runs U1-U10 in the 5 supported engine chats via MCP
   (skip the deprecated gemini/amp chats — they cannot pass U1)
   Focus on: progress rendering, final message, model footer, resume

6. Run Tier 2 (Claude-specific) — 15 minutes
   Claude Code runs C1-C7 in Claude test chat (per-test /planmode preconditions above)
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
| **Minor** (new feature) | Tier 7 + Tier 1 (all 5 supported engines) + Tier 2 + Tier 3 (relevant) + Tier 4 (relevant) + Tier 6 + upgrade path | New feature + all engine regression + config compat | ~75 min |
| **Major** (breaking) | All tiers, all supported engines, full upgrade path | Everything — no shortcuts | ~120 min |

### What to focus on per change type

| Changed area | Must-run tests |
|---|---|
| Runner code (`runners/*.py`) | U1-U4 (all supported engines), U6, U7 |
| Antigravity engine (`runners/antigravity.py`, `schemas/antigravity.py`, `utils/antigravity_quota.py`) | Tier 1 (Antigravity) + AG-* |
| Base runner / liveness watchdog / stall monitor (`runner.py` `JsonlSubprocessRunner`, `_subprocess_watchdog`; `runner_bridge.py` expected-wait branches) | Tier 1 U1 + U6 on every supported engine |
| Preamble (`runner_bridge.py` `_apply_preamble`) | U1 + U2 + U6 on Codex, OpenCode and Pi; C1, C2, C4 on Claude (`preamble.applied preamble_len=3338` unchanged for Claude) |
| Per-run stream binding (`runner.py` `RunStreamHandle` / `publish_run_stream`, `runner_bridge.py` stall monitor) | RC12-1, S1, S2, U1-U4 (all supported engines), B-LIVE-1 |
| Claude stream schema / rate-limit / API-retry handling (`schemas/claude.py`, `runners/claude.py`) | `uv run pytest tests/test_claude_cli_schema_drift.py`, RC12-2, RC12-3, S1, R20-922a…c (usage-credits cap latch) |
| Runner bridge / auto-continue / no-op resume recovery (`runner_bridge.py`, `runners/claude.py`) | B-RESUME, U1-U4 (Claude), U6, U7, R20-919a…d + R20-920a…e (approval reminders) |
| Live sessions / follow-up injection / scheduler (`runners/claude.py`, `runner_bridge.py`, `live_followup.py`, `scheduler.py`) | B-LIVE-1…7, RC12-4…7, C1-C6, S7, U1-U4 (Claude), R15-6, R20-921a/b (cancel vs queued follow-up), R20-923 + R20-928a…c (hook rewake, no-query results), R20-929a…f (idle approval surface) |
| Claude hooks / `--include-hook-events` (`runners/claude.py` hook frames, SDK hook callbacks) | RC14-6, R17-828, R20-923, R20-925a/d/h (hook callbacks on every control-channel spawn) |
| Loop scheduling (`loop_scheduler.py`, `telegram/loop.py`, `[loop]` settings, Claude CronCreate/CronDelete/ScheduleWakeup handling) | R20-925a…m, R20-926a…c, R17-01d, U6, Q16 |
| Telegram transport (`telegram/*.py`) | T1-T10, S7, S8, R15-9-1 (benign edit/delete 400s at startup) |
| Outbox delivery (`telegram/outbox_delivery.py`, `[transports.telegram.files] outbox_*`) | R20-924a…g, T3, U1 (Claude, Codex), U4, B-RESUME |
| Control channel (`claude_control.py`, `orphan_approvals.py`) | C1-C6, T8, S9, R15-5, R15-6, R17-13a/b, R17-16, R20-929a…f |
| Config/settings (`settings.py`) | O1-O9, S5, upgrade path, R15-13a…d (settings parse cache), R17-18a…d (content-keyed watcher), R20-925g/m (`[loop] own_schedule`), R20-924e (`outbox_stale_policy`) |
| Service detection / restart hints (`service_manager.py`, `telegram/loop.py` reload notices, preamble) | R20-927a…d |
| Cost tracking (`cost_tracker.py`) | B1-B3, U8 |
| Progress/formatting (`markdown.py`, `telegram/render.py`) | U3, T6, T7, S4, S8, RC12-8, RC12-9, R17-06a…d, R17-07a…e, R17-08a/b |
| Commands (`commands/*.py`) | Tier 7 (all), specific command test |
| `/browse` + shared path checks (`commands/browse.py`, `telegram/files.py`) | Q5, U10, T2, T3, R15-2 |
| File transfer (`file_transfer.py`) | T2, T3, T5, R15-3 |
| Voice (`voice.py`) | T1, R15-11 (vocabulary; supersedes RC12-10), R15-10 (endpoint), R17-19a…c (URL userinfo masked) |
| Topics (`topics.py`, `topic_state.py`) | O1, O5, O6, O8, R17-14 (dev-ws forum) |
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

# C1 — tool approval (Claude, /planmode off first; Bash is pre-approved on dev)
use the Write tool to create /tmp/c1-probe containing x

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
ls /proc/$(systemctl --user show -p MainPID --value untether-dev)/fd 2>/dev/null | wc -l

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

- **Stall tests (S1)** are timing-dependent — thresholds vary by `[watchdog]` config and by context (defaults: 5 min normal, 10 min local tool `tool_timeout`, 15 min MCP tool `mcp_tool_timeout`, 15 min child processes/subagents `subagent_timeout`, approval pending 10 min for the first reminder then 30 min). Check `~/.untether-dev/untether.toml` for current values.
- **Ask question (C4)** is hard to trigger deterministically — Claude decides when to ask. Try ambiguous prompts.
- **Forward coalescing (T4)** depends on `forward_coalesce_s` debounce window — send forwards quickly enough to be within the window.
- **Budget Stop at limit (B1)** acts only when a reply finishes (Claude reports cost per result); it never cuts a reply mid-turn.

### Engine-specific

- **OpenCode: compaction not surfaced** — OpenCode auto-compacts, but `opencode run --format json` doesn't report it, so context grows across turns with no `🗜️` row until OpenCode compacts. After 4-5 prompts, response times degrade significantly (72k → 77k+ input tokens). Use `/new` to start a fresh session before isolated tests (e.g. error handling) to avoid slowdowns from prior context.
- **Resume (U4)** requires replying to the specific resume line in the final message. Resume token format varies by engine.
- **Model override (U5)** availability depends on which models each engine supports. `/model` shows the current model; `/config` → Engine & model has no picker (an interactive picker is [#512](https://github.com/littlebearapps/untether/issues/512), planned for v0.36.2), so pass a model id to `/model set`.
- **Long response (U3)** behaviour varies by engine — some produce shorter responses. The key check is message splitting, not word count.
- **Concurrent sessions (S2)** may hit rate limits on some engine APIs. Space the prompts a few seconds apart.
- **Reasoning levels (O2)** only available for Claude, Codex and Antigravity (per model: `/config` → Effort).

### Config and state

- **Subscription usage (C7)** requires `[footer]` configured in `~/.untether-dev/untether.toml`.
- **Export (U9)** requires a completed session in the current chat. Run a prompt first if `/export` returns "no session".
- **Chat session mode (O7)** requires config change and restart — cannot toggle at runtime.
- **Override persistence (O8)** depends on the state file — per-chat overrides live in `~/.untether-dev/telegram_chat_prefs_state.json` (state files sit directly in the config directory; there is no `state/` subdirectory).

### Telegram platform

- **Stale button clicks (T8)** — Telegram delivers callback queries for buttons on messages of any age. Bot must handle gracefully.
- **UTF-16 entity offsets (T6)** — Telegram uses UTF-16 code units for entity offsets. A single emoji flag sequence occupies 2 code units but 1 Python codepoint. Test with emoji-heavy text.
- **4096-char limit** applies after entity parsing, not before. Splitting must account for entity boundaries.
- **Voice messages (T1)** require Opus/OGG format, max 10MB by default. Transcription depends on configured API endpoint being accessible.
- **429 rate limits** block ALL Telegram sends for the full `retry_after` duration, not just the rate-limited chat. Monitor logs for 429s during high-volume testing.

---

## rc15 scenarios (0.35.5rc15)

Dev bot only (`@untether_dev_bot`). Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`. Each subsection below comes from one rc15 plan and keeps that plan's scenario IDs.

Back up `~/.untether-dev/untether.toml` before any config-editing row and restore it afterwards. Drive every row from an lba-1 terminal Claude Code session (Telegram MCP + Bash), never from inside a dev-bot chat. Run the zero-token drift suites on lba-1 as part of the rc (CI has neither CLI; a skip on lba-1 counts as a fail): `uv run pytest tests/test_claude_cli_schema_drift.py tests/test_codex_cli_schema_drift.py -v -rs`.

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
| R15-13d | **Invalid edit is not masked** | Introduce a TOML syntax error in the dev config (for example a stray `[[`), send `ping` in the Claude chat, then fix it. | The run still completes on defaults. The logs show `config.read.toml_error` plus the helpers' `*_settings.load_failed` warnings (today's behaviour), **not** a silent stale config. After the fix, the next message logs `config.loaded reason=content_changed` — but only if the fixed file's bytes differ from the last good parse: restoring the identical original bytes reuses the cached entry and logs nothing (errors aren't cached), which is correct. Fix with a different edit to see the line. |

### #746 — benign Telegram edit/delete 400s

| # | Test | What to send / do | What to verify |
|---|---|---|---|
| R15-9-1 | **Benign orphan 400s are INFO ([#746](https://github.com/littlebearapps/untether/issues/746))** | **Run from a terminal Claude Code session on lba-1 (Telegram MCP + Bash), never from a dev-bot chat**: step 3 stops `untether-dev`, which would kill a session driven through it. Claude chat `5284581592` (Bot API `-5284581592`). (1) Send `/ping`; note **your** message id `U` and the bot's reply id `B1`. **Use Bot API ids:** in a basic group the Telethon/`get_history` ids differ from the Bot API's (on the dev Claude chat, Bot API id = Telethon id − 115001), so read them from the journal (`user_msg_id=` / `message_id=` on the `/ping` lines) — Telethon ids make all three edits fail as `target_gone`. (2) Send `/ping` again; note the reply id `B2`, then delete `B2` with the Telegram MCP `delete_message`. (3) `systemctl --user stop untether-dev`. (4) Seed the orphans with the **guarded script below**: it backs the existing `~/.untether-dev/active_progress.json` up to `.bak`, **aborts unless that file is empty or `{}`**, and writes `{"r15:a":{"chat_id":-5284581592,"message_id":B1},"r15:b":{"chat_id":-5284581592,"message_id":B2},"r15:c":{"chat_id":-5284581592,"message_id":U}}`. (5) `systemctl --user start untether-dev`; wait for the startup message. (6) **Always** restore: `mv ~/.untether-dev/active_progress.json.bak ~/.untether-dev/active_progress.json` (even when an earlier step failed). | Telegram: `B1` now reads `⚠️ interrupted by restart`; `B2` stays deleted; `U` is unchanged. Logs: `startup.orphan_cleanup count=3`; `telegram.benign_rejection method=editMessageText … reason_class=target_gone message_id=B2`; `telegram.benign_rejection … reason_class=not_editable message_id=U` (a bot can't edit a user's message); `startup.orphan_cleanup.done count=3 edited=1 failed=2 skipped=0`; **no** `telegram.http_error` and **no** `[error` line between `startup.orphan_cleanup` and `startup.sent`. `active_progress.json` is gone or empty afterwards. |

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

### #830 — Codex safe mode = read-only sandbox

Codex chat `4929463515` (Bot API `-4929463515`), project `codex-test` → `/home/nathan/untether/test-projects/test-codex`. Logs: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "runner.start|codex.process.failed|codex.argv.rejected|codex.permission_mode.unknown|config.approval_policy.set"`.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-CS0 | **Repro before the fix (zero-token) ([#830](https://github.com/littlebearapps/untether/issues/830))** | Unfixed tree (0.35.5rc14). `/config` → Approval policy → **Safe**, then send `say hi`. The direct-CLI form needs no bot: run the argv `build_args` produces for `safe` against the installed `codex` with stdin `/dev/null` and a temp `CODEX_HOME` | Final shows `codex exec failed (rc=2).` with `invalid value 'untrusted' for '--ask-for-approval`. Log `runner.start … '--ask-for-approval', 'untrusted', 'exec'` + `codex.process.failed rc=2`. No tokens spent (clap exits before auth) |
| R15-CS1 | **Safe: reads work** | Fixed tree, restart dev, still Safe. Send `print the first line of README.md if there is one, otherwise list the files here` | Run completes (✓, not an error). Footer `🏷 <model> · safe`. `runner.start` args contain `'exec', '--json', '--skip-git-repo-check', '--color=never', '--sandbox', 'read-only', '-'` and **no** `--ask-for-approval` |
| R15-CS2 | **Safe: writes blocked by the sandbox, not by the model** | Send exactly: ``run this shell command exactly and show me its full output, do not change it: `touch safe-probe.txt; echo rc=$?` `` | Completes in normal time, no stall warning. **Required:** the `command_execution` output contains `Read-only file system` (EROFS) **or** `Permission denied`, and `rc=1`. The row itself completes with exit 0, because the trailing `echo` succeeds — that's expected. `test -e …/test-codex/safe-probe.txt` → false. **A model refusal (no command row) is a FAIL / re-run.** Record the item status and exact stderr |
| R15-CS3 | **Safe on resume** | Reply to CS2's resume line with exactly: ``run exactly `touch safe-probe-resume.txt; echo rc=$?` and show the output`` | `runner.start` args contain `'--sandbox', 'read-only', 'resume', '<thread_id>', '-'`. Same output criterion as CS2 (`Read-only file system` + `rc=1`), file absent, `git -C …/test-codex status --porcelain` unchanged. A refusal is a FAIL / re-run |
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
| R15-21b | `/new`; background Bash `python3 -c "import time; time.sleep(600)"` (silent, idle); reply "STARTED" | at ≈60 s: `⏳ Closing session — 1 background task still running with no progress for 1 min: … Stopping it.` then `↩️ Reply to continue in the same session.` (silent) | `stdin_closed reason=max_hold` at 60–90 s; `lifecycle_exited reason=exited_after_close close_reason=max_hold` ([#820](https://github.com/littlebearapps/untether/issues/820)); `claude.live_session.closed quarantined=False`; **no** `session.quarantined` — the negative guard for the Bash fallback |
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
| R15-14d | **Stale saved `minimal` is ignored with a note** | `/config` → Reasoning → **Low** (creates the override), then `jq '(.chats["-4929463515"].engine_overrides.codex.reasoning) = "minimal"' ~/.untether-dev/telegram_chat_prefs_state.json > /tmp/p && mv /tmp/p ~/.untether-dev/telegram_chat_prefs_state.json`; send `In one sentence: what is 2+2?` | Progress shows ``reasoning level `minimal` isn't supported for `codex` any more; using the engine default. …``; the run **succeeds**; footer has no `· minimal`; `/config` home shows `Reasoning: default · minimal not supported`. Exactly one `run.reasoning.unsupported_level_ignored engine=codex reasoning_level=minimal`; `grep -c 'model_reasoning_effort=minimal'` → 0; no `session.auto_cleared engine=codex` |
| R15-14e | **Picking a level clears it** | `/config` → Reasoning → **Low**, send the same prompt | No note; footer `· low`; `config.reasoning.set engine=codex reasoning_level=low`; no further `unsupported_level_ignored` |
| R15-14f *(optional)* | **Stale `Minimal` button from a pre-upgrade message** | Needs rc14 on the dev bot first (`git switch --detach 11cbee3`, restart dev, open `/config` → Reasoning so a `Minimal` button exists), then back to the rc15 branch, restart dev, tap that old **Minimal** | No "Reasoning: minimal" toast; the Reasoning page re-renders with current buttons; prefs hold no `minimal`; no `config.reasoning.unsupported_level` log. Unit test `test_reasoning_set_min_rejected_for_codex` covers the same callback |
| R15-14g *(optional, paid ≈ 1c)* | **U4 capture of the 400 wording** | Done during implementation (2026-09-30, codex-cli 0.157.1, gpt-5.5): `tests/fixtures/codex_turn_failed_minimal_reasoning.jsonl` | Specific hint for the web_search 400; broad hint for the unsupported-value 400 (`minimal` is rejected even with `-c web_search="disabled"`) |
| R15-14-cleanup | **Always** | `/config` → Reasoning → **Clear override** | `jq '.chats["-4929463515"].engine_overrides.codex'` back to its pre-run value |

Offline evidence (AC5, lba-1 only — CI has no `codex`, so a skip there is not evidence): `uv run pytest tests/test_codex_cli_schema_drift.py -v -rs -k "catalogue or listed_models or minimal"` shows `PASSED` for `test_bundled_catalogue_has_no_minimal`, `test_listed_models_support_untether_codex_levels` and `test_client_passes_minimal_unvalidated`.

### #383 — plan approvals are turn-scoped

Tier 2 (Claude interactive). Claude `ut-dev` chat `5284581592` (Bot API `-5284581592`). Pre-reqs: dev tree on `untether-dev` (restart it from a terminal, never from inside a dev-bot run), `/planmode on` in the chat, `/config` → Diff preview → on for R15-4c. Scratch dir `/tmp/r15-383/` (create it first; `rm -rf` it after). Log command for every step: `journalctl --user -u untether-dev --since "15 min ago" | grep -E "claude\.(permission_mode|plan_approval|live_session\.(injected|stdin_closed|plan_rearm))|claude\.turn\.started"`. Also run, unchanged: C1, C3 (the post-outline title carries the caption; the feedback edit reads `✅ Plan approved — Claude will carry it out now`), C5, C6, B-LIVE-3, RC13-5 (a) and (b), and Tier 7 `/ping`.

| ID | Steps | Expected Telegram | Expected logs |
|---|---|---|---|
| **R15-4a** Follow-up is planned again | 1. Send `Plan it first: create /tmp/r15-383/a.txt containing one.` 2. When the ExitPlanMode message appears, read its buttons with `list_inline_buttons`. 3. Tap **✅ Approve Plan**. 4. **Within 45 s** of the final (before the 60 s idle close), send `Now create /tmp/r15-383/b.txt containing two.` | Step 2: first button `✅ Approve Plan`; the title's caption line is `Approving lets Claude carry out this plan without further prompts. Plan mode resumes when this reply ends, or after the background agents it starts have finished.` Step 3: `a.txt` created, final delivered, feedback `✅ Plan approved — Claude will carry it out now · plan mode resumes when it's done`. Step 4: a **new ExitPlanMode approval** appears (Claude plans again); `test ! -e /tmp/r15-383/b.txt` holds until you tap Approve | `permission_mode.changed … to=default source=status` after step 3; then, **before** the final's delivery lines, exactly one `permission_mode.rearm_sent reason=idle` and `rearm_ack mode=plan` (never `reason=followup` in this step: the idle re-arm is written before the result is yielded); `live_session.injected` (proves the live path, not a resume); `plan_approval.cleared` (`reason=plan_rearmed` when the ack lands before the next turn opens — the usual case, 7–12 s after `rearm_sent` — otherwise `reason=turn_boundary`); `turn.started reason=followup`. The `✅ Plan approved — …` feedback edit only appears in the outline flow (C3) |
| **R15-4b** Wake turn is planned (slow job) | Send `Plan: start "sleep 25 && echo done" as a background Bash task and end your reply; when it finishes, create /tmp/r15-383/c.txt.` Tap Approve Plan. Wait ~30 s | The wake message (`🔔 Background task finished …`) carries a **new** ExitPlanMode approval instead of writing `c.txt`; the file is absent until approved | `rearm_sent reason=idle` after the first final; `rearm_ack`; `turn.started reason=task_finished`; no `rearm_deferred` (bash doesn't defer) |
| **R15-4b′** Wake turn is planned (job finishes as the reply ends) | As R15-4b but with `sleep 2` and "end your reply immediately after starting it". Repeat 3 times | In each run, a new ExitPlanMode approval in the wake message, or (probe P-6 showed a residual window when the notification is queued at result time) a wake turn whose first step runs unplanned and which then asks for approval; record whether `c.txt` appears unapproved | Per run: the `rearm_sent reason=idle` timestamp precedes that run's `turn.started reason=task_finished`; record the gap. `c.txt` written without an approval is the known residual (first model call before the re-arm is read, and plan-mode writes reach Untether's autonomous stage-6 handler on CLI 2.1.285) — note it with the three timestamps rather than filing it as a #383 regression |
| **R15-4c** Diff-preview bypass ends with the reply (default allowlist) | Diff preview on. Send `Plan first: edit /tmp/r15-383/a.txt to say one-edited.` (create the file first). Approve the plan. Within 45 s send `Append a second line to /tmp/r15-383/a.txt.` | Reply 1: no diff-preview prompt after the plan approval (intra-turn #283 kept). Reply 2: plans again and, after approval, behaves exactly as reply 1 | `plan_approval.cleared` before reply 2's `turn.started` (`reason=plan_rearmed` when the re-arm ack lands first, otherwise `reason=turn_boundary`). Under the default allowlist `Edit` never reaches stage 6, so the log line is the evidence here; R15-4c′ shows the gate itself |
| **R15-4c′** Diff-preview gate visibly returns (custom allowlist) | Set `[engines.claude] allowed_tools = ["Read"]` and `[watchdog] rearm_plan_mode = false` in `~/.untether-dev/untether.toml`, then restart `untether-dev` from a terminal (engine config). Diff preview on, `/planmode on`. Send `Plan first: edit /tmp/r15-383/a.txt to say two-edited.`, approve. Within 45 s send `Append "three" to /tmp/r15-383/a.txt.` Restore both keys and restart afterwards | Reply 1: after Approve Plan, the Edit runs with **no** diff prompt (intra-turn bypass). Reply 2 (no re-plan, because the re-arm is off): the `Edit` shows a **diff-preview approval** (`- …` / `+ …` lines with Approve/Deny). On rc14 it would have run silently | The visible diff-preview prompt is the evidence (`control_request.diff_preview_gate` is DEBUG, not in the journal); Claude may use Bash instead of Edit, which is gated the same way; `plan_approval.cleared reason=turn_boundary` |
| **R15-4g** Post-outline approval carries one turn | Send a complex prompt; on the ExitPlanMode message tap **📋 Pause & Outline Plan**. If Claude writes the outline and **ends its reply** without new buttons (or shows the `da:` Approve Plan buttons), tap **✅ Approve Plan**, then send `go ahead`. Then, in a fresh plan, repeat but send two messages (`wait` then `go ahead`) after tapping | Run 1: Claude proceeds on `go ahead` **without** a second approval. Run 2: the second message's ExitPlanMode shows normal buttons again (the carry lasted one turn) | Run 1: `plan_approval.carried` at the `go ahead` turn, then `control_request.discuss_approved`. Run 2: `carried` at `wait`, `cleared` at `go ahead` |
| **R15-4d** Approved background agent isn't disrupted (C4) | Send `Plan: launch a background agent that waits 40 seconds and then writes /tmp/r15-383/agent.txt; end your reply while it works.` Approve. Within 20 s send `Quick question: what time is it?` | The question is answered promptly and its reply starts with `⚠️ Not re-planned: the approved plan's background agents are still running. Plan mode resumes when they finish.`; `agent.txt` **is** written ~40 s later (the agent was not switched back into planning); the agent's wake message (`🔔 Background task finished …`) runs planned — if it wants to act it raises a new ExitPlanMode approval — and a follow-up after it is planned again with no ⚠️ line | `rearm_deferred reason=live_agents agents=1 plan_exit_turn=1` at the first final (and again at the question's turn close); when the agent ends: `rearm_deferral_ended reason=agents_done`, `rearm_sent reason=agents_done` and `rearm_ack mode=plan` **before** `turn.started reason=task_finished`; never a `rearm_sent` while the agent runs |
| **R15-4e** Non-plan chats untouched | `/planmode off` (acceptEdits). Send `create /tmp/r15-383/e.txt`, then within 45 s `delete it`. Then `/planmode auto` and repeat once. `/planmode on` at the end | Normal behaviour for each mode; no plan buttons | **zero** `claude.permission_mode.rearm_*` lines for these runs; `live_session.injected` present |
| **R15-4f** Kill switch | Set `[watchdog] rearm_plan_mode = false` (read per spawn, no restart needed). Rerun R15-4a steps 1-4 | Step 2 caption has **no** "Plan mode resumes…" clause; step 4: Claude writes `b.txt` **without** a new plan (rc14 behaviour) | no `rearm_sent`; `plan_approval.cleared` still present. Restore `true` afterwards |
| **R15-4h** plan-auto (Decision 6 default) | `/planmode plan-auto`. Send `Plan: start a Monitor on "for i in 1 2 3; do sleep 10; echo tick $i; done" and after each tick append the tick to /tmp/r15-383/ticks.txt.` Then after the ticks, send `summarise ticks.txt` | No approval buttons at any point; ticks are appended without a plan per tick; the follow-up plans (a plan appears in the progress/final) and is auto-approved | No `rearm_sent reason=idle` for the tick turns; one `rearm_sent reason=followup` before the follow-up; the follow-up's ExitPlanMode auto-approved (`control_response.auto_approved`; `control_request.auto_approve_exit_plan_mode` is DEBUG, so it isn't in the journal) — if Claude answers read-only without a plan, record it as not exercised |

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
| **R15-5a** Approve twice | Send `use the Write tool to create /tmp/r15-5a.txt containing x` (Bash is pre-approved on dev); when the Approve/Deny keyboard appears, `press_inline_button` **Approve twice back to back** (two calls, no wait) | First press toast `Approved`; second press toast `Already answered` (or `Approved`, if both were in flight); a silent `ℹ️ Already answered — approved` line; the tool runs once | Exactly **one** `control_response.sent approved=True request_id=<X>` and **one** `claude_control.sent approved=True request_id=<X>`; one `claude_control.already_handled request_id=<X> first_action=approve`; **no** `callback.failed` |
| **R15-5b** Outline, then Approve | A prompt that triggers ExitPlanMode (C3 shape); press **📋 Pause & Outline Plan**, then at once **✅ Approve Plan** on the same keyboard | Second press toast `Already answered`; silent line `ℹ️ Already answered — outline requested`; Claude writes the outline (not an approval) | One `control_response.sent approved=False`; `claude_control.already_handled first_action=discuss`; **no** `claude_control.sent approved=True` for `<X>` |
| **R15-5c** Stale tap | After the run finishes, `/new`, then replay the old callback. A stripped keyboard can't be pressed, and Telethon's `GetBotCallbackAnswerRequest` with arbitrary data fails (`MessageIdInvalidError` / `DataInvalidError`). What works: post a probe message **as the dev bot** via the Bot API (`sendMessage` with an inline button whose `callback_data` is `claude_control:approve:<X>`), then `press_inline_button` it | Toast `Already answered` (the id is still in the LRU) | `claude_control.already_handled`; no WARNING |
| **R15-5d** Unknown id | Replay a made-up id with the R15-5c probe-message method: `claude_control:approve:00000000-0000-4000-8000-000000000000` | Toast `This request has expired`; message `⚠️ Control request not found or session ended` | WARNING `claude_control.not_found reason=unknown` |
| **R15-5e** `da:` Deny → Approve (opportunistic) | If the escalation path is reachable (Pause & Outline, then Claude calls ExitPlanMode without an outline): press **❌ Deny** then **✅ Approve Plan** on the `da:` keyboard | Second press `Already answered — denied`; the next ExitPlanMode shows buttons again (not auto-approved) | No `control_request.discuss_approved` after the deny; `claude_control.already_handled first_action=deny`. Record "not exercised" if Claude always writes the outline first |

Pass = R15-5a–d match, plus C1/C2/C3/C6/T8/S9 unchanged. Post-rollout invariant (nsd, 24 h): every `claude_control.sent` has a `control_response.sent` for the same `request_id` within 1 s before it — unpaired count must be 0.

### #819 — Claude context-window use (`% ctx`) and 🗜️ compaction rows

Chats: **Claude** `5284581592`, **Codex** `4929463515` (Bot API ids with a leading `-`). Claude-only (the Codex half is [#832](https://github.com/littlebearapps/untether/issues/832)). Log sweep: `journalctl --user -u untether-dev -o cat --since "60 minutes ago" | grep -E "claude\.compaction|claude\.context\.(window_learned|over_window|window_miss)|runner\.completed|runner\.empty_result|session\.quarantined|stall_detected|stuck_after_tool_result|jsonl\.msgspec\.invalid"`. Expected: no `jsonl.msgspec.invalid` for `system`/`result`/`assistant` lines, and no empty-result or quarantine lines for compaction runs.

| # | Scenario | What to do | Pass criteria |
|---|---|---|---|
| R15-19a | **Header `% ctx` ([#819](https://github.com/littlebearapps/untether/issues/819))** | Claude chat, U2 prompt | Progress header gains `· NN% ctx` once the first answer frame arrives, including on the first run after a restart for a seeded model (only an unseeded, never-learned model waits for the final; rc5); final `done · claude · … · step N · NN% ctx`; the `🏷` footer line is byte-identical to a pre-rc15 final (no ctx segment). Log once: `claude.context.window_learned model=<id> context_window=<n>`; `runner.completed … context_pct=NN` |
| R15-19b | **Turn header** | Reply to R15-19a's answer within 60 s with a follow-up | Follow-up final header carries `% ctx` ≥ R15-19a's; `claude.live_session.injected` |
| R15-19c | **Manual `/compact`, live** | Short prompt, then within 60 s send `/compact` | A new message for the follow-up: `▸ 🗜️ Compacting context…` then `🗜️ Context compacted · Nk → Mk tokens (manual)` (no ✓ since rc17, #868); final `done` with the body `🗜️ Context compacted · …` and **no** `% ctx` in its header (D5); the next prompt is answered in the same session with a **lower** `% ctx`. Logs: `claude.compaction trigger=manual`, `runner.completed … compactions=1 compaction_trigger=manual`; **absent**: `runner.empty_result`, `session.quarantined`, `session.auto_resend_fresh` |
| R15-19d | **`/compact` outside the live window (unchanged behaviour)** | Wait > 60 s for idle close (`claude.live_session.stdin_closed reason=idle…`), then send `/compact` | rc15 does **not** change this path (bare-`/compact` handling is [#834](https://github.com/littlebearapps/untether/issues/834), planned for v0.36.2): the run is resumed with the preamble-prefixed text, Claude answers in prose, no 🗜️ row, no `runner.empty_result`. Records the baseline |
| R15-19e | **Auto compaction (best effort)** | `/model set <a Haiku model id>`; add `extra_args = ["--autocompact", "100k"]` under `[engines.claude]` in `~/.untether-dev/untether.toml` (hot reload; `--autocompact` is allowed by the [#209](https://github.com/littlebearapps/untether/issues/209) guard). Create five **distinct** files of ~140 KB in the test project (the CLI's `Read` dedupe keeps a re-read file out of the context, so one file re-read never fills it). Send "Read bigN.txt in full and reply with only its line count" as follow-ups, one file each. **Stop** after 10 prompts with no 🗜️ row (`% ctx` is measured against the model's 200k window, not the 100k autocompact window, so compaction fires at ~40 % and a "≥ 95 %" condition never triggers; an early `claude.compaction.failed error=too_few_groups` WARN on a one-turn session is expected) | Pass: one run shows `🗜️ … (auto)` inside the same streaming message (the 2026-10-01 capture fired on the third file, at 79k tokens), `% ctx` drops after it, no `progress_edits.stall_detected`, no `stuck_after_tool_result`, `claude.compaction trigger=auto`. If the stop condition hits with no compaction, mark it *not exercised* (not a fail) and note it in the attestation `--notes`. Revert the config and model afterwards |
| R15-19f | **Toggle** | Set `[progress] show_context_usage = false` in `~/.untether-dev/untether.toml` (hot reload), run U1 | No `% ctx` in progress or final; compaction rows are unaffected. Revert, run U1 again: the segment is back without a restart — including in a follow-up injected into the live session ([#863](https://github.com/littlebearapps/untether/issues/863)) |
| R15-19g | **Codex regression** | Codex chat U1 + U4 | Header has no `% ctx`; no new WARN/ERROR |

### #684 — withdrawn and unanswerable control requests

Tier 2 **C1–C6**, **B-LIVE-1…7**, Tier 6 **S9**, Tier 1 **U1** on Codex and OpenCode (smoke for the engine-agnostic `runner.py` frozenset edit). Claude `ut-dev` chat `5284581592` (Bot API `-5284581592`). Setup: `~/.untether-dev/untether.toml` `[watchdog] tool_timeout = 60` (the minimum; applies to new runs), `/planmode on`. **Restore 600 afterwards.** Logs: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "control_request\.|claude_control\.|approval_pending|live_session\.stdin_closed|session.summary"`.

| ID | Telegram action | Expected in Telegram | Expected in logs |
|---|---|---|---|
| **R15-6a** Healthy long wait (negative) | `use the Write tool to create /tmp/r15-6a.txt containing x`; don't tap for 150 s; then Approve | The keyboard stays; after Approve the tool runs | **no** `control_request.unanswerable` (`subprocess.approval_pending` only fires after the 600 s first-approval threshold, so don't expect it at 150 s); `session.summary … unanswerable_control_requests=0` |
| **R15-6b** Live wake turn (negative) | B-LIVE-7 shape (20 s background task, then ExitPlanMode in the wake turn); wait 150 s before tapping | The keyboard is on the wake turn's message; Approve → file written | No `unanswerable` |
| **R15-6c** Ask by text (negative) | A C4 prompt that raises AskUserQuestion; tap **Other**; wait 150 s; type an answer | The answer is routed | No `unanswerable`; `control_response.sent` and the `↩️ Answered: …` echo (the **Other** path writes no `ask_user_question.answering` line — that's only the plain-reply path) |
| **R15-6d** Outline hold (negative) | C3: tap **📋 Pause & Outline Plan**, let the outline arrive, wait 150 s | Buttons on the outline message | No `unanswerable` (the `has_outline_messages` surface) |
| **R15-6e** Stale `da:` (positive, opportunistic) | If C3 produces an outline-guard deny (`control_request.outline_guard_deny`) followed by an outline-ready hold-open, answer the real request | Normal approval flow | `control_request.da_superseded`, then the session idle-closes normally after the reply (`stdin_closed reason=idle_no_tasks`, not held). Record "not exercised" if the guard path doesn't occur |
| **R15-6f** Native cancel (opportunistic) | Grep the whole session for `control_request.cancelled_by_cli` | If present: the progress row reads `⏹️ Permission request withdrawn…`, the keyboard is gone, a tap toasts `No longer needed` | Untether never sends `interrupt`, so a live cancel is expected to be **not exercised**; unit + fake-CLI tests are the gate |
| **R15-6g** Kill switch | Set `[watchdog] detect_unanswerable_control_requests = false`; repeat (e) or a unit-level check | — | No `unanswerable` lines |
| **R15-6h** `initialize` re-send probe (zero-token, done offline 2026-10-01) | — | — | Fake-API harness on CLI 2.1.285: a mid-session `initialize` re-send while a `can_use_tool` is pending emits **no** second `system/init`; the envelope lists the pending id in `pending_permission_requests` (plus `pending_user_dialog_requests`), `session_state:"requires_action"`; one `system/background_tasks_changed` follows. Recorded in `docs/findings/2026-09-30-claude-sdk-control-permissions-context.md` (rc16 D2 input) |

Pass = R15-6a–d show no false positive, (e)/(f) positive or recorded "not exercised", `uv run pytest tests/test_claude_cli_schema_drift.py` passes against the installed CLI, and C1–C6, B-LIVE-1…7, S9 and U1 (Codex/OpenCode) are unchanged. Post-rollout (7 days, all 5 hosts): `journalctl --user -u untether -o cat --since "7 days ago" | grep -E "control_request\.(unanswerable|cancelled_by_cli|da_superseded)"` — expect `unanswerable` ≈ 0 and triage every hit; `claude.live_session.stdin_closed reason=abs_cap` should not rise.

### #751 — permission-mode audit and the requested-vs-effective re-arm

Tier 7 **Q8** (`/planmode`) and **Q16** (`/ping` with a cron), Tier 1 **U1** (Claude; Codex/OpenCode as a startup-message regression check), Tier 2 **C1** (tool approval still raises buttons in `default`), rc4 **R1** (hot-reload cron add). Claude `ut-dev` chat `5284581592`. Logs: `journalctl --user -u untether-dev -o cat --since "10 minutes ago" | grep -E "auto_semantics_changed|unattended_approval_risk|permission_mode_invalid|permission_mode.mismatch|auto_approve_tool"`.

Setup (dev only; restore afterwards): in `~/.untether-dev/untether.toml` set `[triggers] enabled = true` and add far-future crons (`schedule = "0 0 1 1 *"`): `r15-auto` (`project = "claude-test"`, `permission_mode = "auto"`), `r15-default` (`project = "claude-test"`, `permission_mode = "default"`), `r15-plan` (`project = "claude-test"`, `permission_mode = "plan"`), `r15-codex` (no project, `permission_mode = "auto"` — resolves to the codex default and must **not** appear).

| ID | Steps | Pass criteria |
|---|---|---|
| **R15-7a** Startup audit | With the setup above, `systemctl --user restart untether-dev` | Within seconds of `startup`, before any run: exactly **one** `claude.permission_mode.auto_semantics_changed reason=startup` with `entries = ["triggers.crons[r15-auto]"]` (dev engine config is `plan`, so no `engines.claude`; `r15-codex` absent); one `trigger.unattended_approval_risk phase=config` listing `cron:r15-default` (`default`, tool approval) and `cron:r15-plan` (`plan`, plan approval). Since rc17 the startup message also carries `_unattended approvals (auto-denied):_ \`cron:r15-default (default), cron:r15-plan (plan)\`` ([#836](https://github.com/littlebearapps/untether/issues/836), see R17-12) |
| **R15-7b** Reload semantics | (1) Toggle an unrelated key (e.g. `voice_transcription`) and save. (2) Add cron `r15-auto2` (`auto`, `claude-test`) and save | (1) no new audit WARN. (2) one `auto_semantics_changed reason=reload` listing `r15-auto` and `r15-auto2`; the R1 `triggers.manager.updated` line is also present |
| **R15-7c** Dispatch-time (inherited mode) | Add `r15-fire` (`project = "claude-test"`, `chat_id = -5284581592` (without it the run goes to the global `chat_id`, a placeholder `123` on dev), **no** `permission_mode`, `run_once = true`, `schedule = "* * * * *"`, prompt `use the Write tool to create /tmp/r15-fire.txt containing x` (Bash is pre-approved on dev)). In the Claude chat `/planmode off` (acceptEdits). Wait ≤ 60 s | The cron fires **on Claude** (the project's default engine, not the global Codex default — [#862](https://github.com/littlebearapps/untether/issues/862)); `trigger.unattended_approval_risk phase=dispatch trigger=cron:r15-fire mode=acceptEdits source=chat_pref outcome=denied` once; since rc17 ([#835](https://github.com/littlebearapps/untether/issues/835)) **no** approval button — the Write is denied (`permission.unattended_deny`, `🔒` row and footer); `/tmp/r15-fire.txt` absent. Then `/planmode on`. Note: if `[triggers]` started out `enabled = false`, enabling it needs a dev restart — hot-reload doesn't start the scheduler |
| **R15-7d** Runtime mismatch + re-arm (security) | In the Claude chat: `/model set <a Haiku model id>`, `/planmode auto`, send `use WebFetch to fetch https://www.iana.org and summarise it` (WebFetch is not in the default allowlist; `example.com` is allowlisted in `~/.claude/settings.json` on lba-1) | Progress shows `⚠️ Asked for auto mode — Claude Code is running default (auto mode isn't available for this model); approvals will be requested`; **an Approve/Deny button appears for WebFetch** (before the fix it was auto-approved); footer `… · default`; one `claude.permission_mode.mismatch requested=auto effective=default prompting_rearmed=true`; no `control_request.auto_approve_tool tool_name=WebFetch`. Tap ❌ Deny. A follow-up in the live session: **no** second row/WARN. (P2 kept the allowlist, so `run ls` still runs without a button — the documented #835 residual.) Restore with `/model clear` and `/planmode on` |
| **R15-7e** Negative control | `/model clear` (back to the default Opus/Sonnet), `/planmode auto`, send `say hi` | No `⚠️ Asked for` row and no `claude.permission_mode.mismatch`; footer `… · auto` |

Clean-up: remove the `r15-*` crons, set `[triggers] enabled = false`, `/planmode on`, restore `/model`. Offline evidence already recorded: probes P1 / P1b / P2 (findings §Q3a), `tests/test_permission_mode_mismatch.py`, `tests/test_unattended_approval_risk.py`.

### #747 — permission-mode wording in `/planmode` and `/config`

Tier 7 **Q2** (`/config`) and **Q8** (`/planmode`), Tier 2 **C1** / **C3** with the corrected preconditions. Claude `ut-dev` chat `5284581592` only. Restart dev from a shell **outside** any dev-bot session (#547). Before step a, note what `/planmode show` prints so it can be restored. Logs: `journalctl --user -u untether-dev -o cat --since "20 minutes ago" | grep -E "planmode\.(set|cleared)|config\.planmode\.set|startup\.command_menu\.updated|live_session\.options_changed"` — no `ERROR` / `Traceback`, no `callback.parse_failed`.

| ID | Send / tap | Pass criteria |
|---|---|---|
| **R15-8a** | `/planmode on` | "permission mode **on** for this chat: plan mode: Claude plans without editing files, and you approve the plan before changes start." / "applies from your next message (`--permission-mode plan`). Until then, the current run and any background wake-ups keep the old mode." |
| **R15-8b** | `/planmode show` | `permission mode: **on** (plan): plan mode: …` |
| **R15-8c** | `/planmode off`, then `/planmode show` | reply contains `acceptEdits` and "other tools ask you here first"; no "freely"; show has `(acceptEdits)` |
| **R15-8d** | `/planmode auto` | no "plan mode"; contains "classifier", "no plan phase" and the fallback clause; `--permission-mode auto` |
| **R15-8e** | `/planmode plan-auto`, then `/planmode clear`, then `/planmode show` | plan-auto summary; "override cleared: no override for this chat; it uses the engine config (`[engines.claude] permission_mode` …)"; show says **engine default** and the same text |
| **R15-8f** | `/config` | home line `Permission mode: engine default  · from engine config` (after e) |
| **R15-8g** | tap 📋 Permission mode | four bullets `off (acceptEdits) — …`, `on (plan) — …`, `plan-auto (plan) — …`, `auto — …`; the "Clear override → engine default" and "Changes apply from your next message…" lines; 📖 line present |
| **R15-8h** | tap **Off** | toast "Permission mode: off (acceptEdits)"; home shows `· edits run, others ask` |
| **R15-8i** | tap 📋 → **On** | toast "Permission mode: on (plan)"; home `· approve the plan first` |
| **R15-8j** | Telegram `/` menu | `planmode — Set Claude Code permission mode: on/plan-auto/auto/off` |
| **R15-8k** | with **off**: `use WebFetch to fetch https://www.iana.org` (Bash is pre-approved on dev) | an Approve / Deny message for WebFetch appears; tap **Deny**; Claude acknowledges the denial |
| **R15-8l** (informational) | with **on**: same prompt | expected: no Bash button, only the ExitPlanMode keyboard (Approve / Deny / 📋 Pause & Outline Plan) → **Deny**. Record whether the file exists (plan-mode Bash writes aren't probed; the text only claims "without editing files") |
| **R15-8m** | with **on**, `say hi`; when it answers (live-idle), `/planmode off`, then `say hi again` | the follow-up footer shows `acceptEdits`; log `claude.live_session.options_changed` |
| **R15-8n** (C5 precondition) | with **off** and diff preview on: `create /tmp/c5.txt containing hello` | record whether a Write approval with a diff preview appears. If yes, rewrite C5 to this recipe and drop its "pending R15-8" note; if no, leave the note and file a playbook issue |

Clean-up: restore the chat's previous mode (`/planmode clear` or what `show` printed before a); `rm -f /tmp/r15-8-probe /tmp/c1-probe /tmp/c5.txt`. Offline evidence: `tests/test_planmode_command.py`, `tests/test_config_command.py::TestPlanMode` / `TestPermissionModeHomeHints`.

### #296 — `/config` help links and the `⏰ Triggers` home button

Tier 7 **Q2** (`/config`) + Tier 4 **O3** (Listen via `/config` → 📡 Listen). Claude chat `5284581592`, Codex chat `4929463515`. Every dev restart must come from a shell **outside** any dev-bot session (#547). Logs: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "config\.triggers\.(paused|resumed)|callback\.parse_failed|handle\.worker_failed"` — the first two only when R15-12-2 ran; never `callback.parse_failed` / `handle.worker_failed`.

| ID | Scenario | What to do | Pass criteria |
|---|---|---|---|
| **R15-12-1** | Help links resolve | (a) Offline: `uv run python -m tests.test_config_help_links > /tmp/r15-12-urls.txt` (every page × engine incl. Triggers, Gemini, home footer, About), then `while read u; do printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 15 "$u")" "$u"; done < /tmp/r15-12-urls.txt`, and for each `#anchor` URL `curl -sL "${u%%#*}" \| grep -c "id=\"${u##*#}\""`. (b) Telegram presence: `/config` in the Claude chat, open each sub-page and confirm a `📖` line (Engine & model shows `Engines · Models`); Approval policy in the Codex chat | (a) every help URL **200**, every anchor count ≥ 1; `steer-follow-ups/` is **expected 404** until v0.36.0 stable syncs from master; allowlisted GitHub URLs 200. (b) every opened page shows its 📖 line. *Offline run 2026-10-01 (rc15 lane): 19 URLs, 18 × 200 + all 9 anchors = 1, `steer-follow-ups/` 404 as expected* |
| **R15-12-2** | Triggers home button | Back up: `cp ~/.untether-dev/untether.toml /tmp/r15-12-dev.toml.bak`. Set `[triggers] enabled = true` with one cron (`id="r15-probe"`, `schedule="0 4 1 1 *"`, `chat_id=-5284581592`, prompt `"noop"`), restart dev, `/config` in the Claude chat | Last row `⏰ Triggers` / `⏸ Pause triggers`; status line `⏰ Triggers: active`. Tapping `⏰ Triggers` edits in place to the page listing `r15-probe` with `📖 Learn more`. Pause → toast `⏸ Triggers paused` + `config.triggers.paused`; Resume → `config.triggers.resumed` |
| **R15-12-3** | Enabled, zero triggers (global count) | Remove the cron (keep `enabled = true`), restart dev, `/config` | Home shows `⏰ Triggers` alone; the page says "No crons or webhooks configured." with a 📖 link |
| **R15-12-cleanup** | Unconditional restore | `cp /tmp/r15-12-dev.toml.bak ~/.untether-dev/untether.toml && systemctl --user restart untether-dev` (outside-session rule); `grep -A1 '^\[triggers\]' ~/.untether-dev/untether.toml` | `enabled = false`; `/ping` in the Claude chat shows no `⏰ triggers:` line |
| **R15-12-4** | Emoji | `/config` in Claude + Codex chats | `📡` only on Listen; every Triggers label uses ⏰ |

R15-12-5 (🔧 More) is not applicable: Decision 1 deferred the More page to v0.36.2 (milestone renumbered from v0.35.6).

---

## rc17 scenarios (0.35.5rc17)

Dev bot only (`@untether_dev_bot`; R17-14 runs on the `untether-dev-ws` forum instance). Log checks: `journalctl --user -u untether-dev -o cat --since "30 minutes ago" | grep -E "<pattern>"`. Each subsection below comes from one rc17 plan and keeps that plan's scenario IDs. As for rc13/rc14, use `python3 -c "import time; time.sleep(N)"` rather than a bare `sleep N` for anything a subagent runs or anything long in the foreground.

Back up `~/.untether-dev/untether.toml` before any config-editing row and restore it afterwards; never `cat` it (it holds secrets — use `grep -c` / `sed -i`). Drive every row from an lba-1 terminal Claude Code session (Telegram MCP + Bash), never from inside a dev-bot chat, and restart `untether-dev` only from that terminal. Run the zero-token drift suite on lba-1 as part of the rc (`PROBED_CLI_VERSION` is 2.1.287; a skip on lba-1 counts as a fail): `uv run pytest tests/test_claude_cli_schema_drift.py tests/test_codex_cli_schema_drift.py -v -rs`.

No live scenario: [#823](https://github.com/littlebearapps/untether/issues/823) (`chat_id` on `telegram.http_error` / `network_error` / `api_error` — there's no safe way to force a non-benign 4xx/5xx; unit tests are the gate, plus R15-9-1 as a regression and the next `/monitor` soak checking every fleet `telegram.http_error` for `chat_id=`) and [#875](https://github.com/littlebearapps/untether/issues/875) (dependency floors and CI; covered by `pip-audit` in CI and the docs build).

### #820 — `lifecycle_exited` says how the live session ended

Chat: `ut-dev: Claude Code` (`-5284581592`). Logs: `journalctl --user -u untether-dev -o cat --since "15 min ago" | grep -E "stdin_closed|lifecycle_exited|exited_after_sigint|forced_teardown"`. Precondition for R17-02c: **no async hooks configured** in the dev project (an evident hook stretches the close grace to 35 s, `/cancel` gives up after 20 s and the line then correctly logs `cancelled`). Restore the config and `/new` afterwards. Regression: B-LIVE-1, B-LIVE-6, R15-21b.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R17-02a | `/new`; "Reply with OK." then wait ≈90 s | `OK`; nothing else | `stdin_closed reason=idle_no_tasks` → `lifecycle_exited reason=exited_after_close close_reason=idle_no_tasks` |
| R17-02b | with `[watchdog] post_result_bg_max_hold = 60`: `/new`; background Bash `python3 -c "import time; time.sleep(600)"`, reply STARTED (= R15-21b) | R15-21b's closing + `↩️ Reply to continue…` lines | `lifecycle_exited reason=exited_after_close close_reason=max_hold` |
| R17-02c | `/new`; start a background Agent ("read 20 files one by one, summarise each"), reply STARTED; then `/cancel` | `⏹ Stopped 1 background task: …` | `close_grace_expired` → `exited_after_sigint … stopped_clean=True` → `lifecycle_exited reason=sigint close_reason=cancel` (only with an async hook evident: `reason=cancelled`) |

### #872 — declared waits hold the live session

Setup: `[watchdog] post_result_bg_max_hold = 60` in `~/.untether-dev/untether.toml` (hot-reloads, but it is read **per spawn**, so every row starts with `/new`; restore `1800` and `/new` at the end). Chat: `ut-dev: Claude Code` (`-5284581592`). Logs: `journalctl --user -u untether-dev -o cat --since "15 min ago" | grep -E "hold_extended|stdin_closed|task.ended|lifecycle_exited"`. Regression: B-LIVE-1…7, R15-21a/b/f, C1–C6, U1–U4 (Claude).

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R17-01a | `/new`; "Run in the background with `run_in_background: true` and `timeout: 300000`: `sleep 180; echo WATCH-DONE`. Reply STARTED, then tell me what it printed when it finishes." | `STARTED`; **no** `⏳ Closing session` at ≈60 s; ≈180 s: `🔔 Background task finished` turn quoting `WATCH-DONE` | `claude.live_session.hold_extended source=bash_timeout declared_s=300.0`; **no** `stdin_closed reason=max_hold` before `claude.task.ended` |
| R17-01b | `/new`; same, but **no** `timeout` and `sleep 600` (silent) | at ≈60–90 s: `⏳ Closing session — 1 background task still running with no progress for 1 min: … Stopping it.` then `↩️ Reply to continue in the same session.` | `stdin_closed reason=max_hold`; no `hold_extended` (the R15-21b negative guard, unchanged) |
| R17-01c | `/new`; `timeout: 90000` with `sleep 600` | no Untether close at 60 s; at ≈90 s the **CLI** stops the task and Claude reports it was stopped at its background time limit (native notice) | `claude.task.ended` (stopped) at ≈90 s **before** any `stdin_closed`; then a wake turn — proves CLI enforcement on 2.1.287 |
| R17-01d *(opportunistic)* | `/new`; ask for a self-paced `/loop` whose next wake-up is ≈2 min out (e.g. "every 2 min print the time, 2 iterations") | `⏰ Scheduled wake-up` at ≈2 min, no closing notice at 60 s; `/cancel` afterwards | `hold_extended source=scheduled_wakeup`; no `stdin_closed reason=max_hold` before the wake turn |
| R17-01e | `bg_hold_declared_waits = false`; `/new`; repeat R17-01a | closing notice at ≈60 s | `stdin_closed reason=max_hold`; no `hold_extended` |

### #828 — a background subagent's sync denial is not a hook rewake

Chat: `ut-dev: Claude Code` (`-5284581592`). Tiers: `uv run pytest` + U1–U4/U6/U7 (Claude), B-LIVE-1/2, RC14-6 (positive control), R17-828.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R17-828 | In the dev project's `.claude/settings.json` add a sync `PreToolUse` hook, matcher `Bash`, command `jq -r .tool_input.command \| grep -q 'R17DENY' && { echo "R17 denied" >&2; exit 2; } \|\| exit 0`. `/new`; send: *"Launch ONE background agent (run_in_background) that first runs `sleep 15`, then the Bash command `echo R17DENY`, then `python3 -c "import time; time.sleep(30)"`, then reports 'agent done'. End your turn immediately."* (the `sleep 15` makes the denial land while the parent idles) | the launch reply; ≈45–60 s later one pushed `🔔 Background task finished — <agent description>` (never `🪝 Hook feedback — PreToolUse`) | `journalctl --user -u untether-dev -o cat --since "10 minutes ago" \| grep -E "claude.hook.(rewake_signal\|blocking_exit)\|claude.turn.(started\|completed\|hook_rewake)"` → ≥1 `claude.hook.blocking_exit hook_event=PreToolUse turn_open=False started_turn=2` (N+1 for the launch turn N); **zero** `claude.hook.rewake_signal` and zero `claude.turn.hook_rewake` for the run. Then re-run RC14-6 (Stop `asyncRewake`, `sleep 90`): `claude.hook.rewake_signal … started_turn=1` → `claude.turn.hook_rewake attributed=open` → pushed `🪝 Hook feedback — Stop`. Remove both hooks afterwards |

### #825 / #876 — late finishes and auto-backgrounded tasks get their own 🔔 header

Chat: `ut-dev: Claude Code` (`-5284581592`). Logs: `journalctl --user -u untether-dev -o cat --since "15 min ago" | grep -E "late_tasks_attributed|claude.turn.started|live_turn.(started|fold_decision|late_tasks_attributed)|claude.task.backgrounded|notification_ignored|stdin_closed"`. Tiers: `uv run pytest` + U1–U4/U6/U7 (Claude) + B-LIVE-1/2/5 + RC12-6 + RC13-1/2.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R17-825a | `/new`; *"Start two background Bash jobs with run_in_background: `python3 -c "import time; time.sleep(20); print('A')"` described 'job A' and `python3 -c "import time; time.sleep(26); print('B')"` described 'job B'. End your turn now. When job A finishes, run `python3 -c "import time; time.sleep(12)"` in the foreground, then report on whatever has finished."* | either `🔔 2 background tasks finished — job A · job B` (B ended during A's wake turn) or two separate `🔔 Background task finished` messages (the CLI gave B its own turn) — never a single-name header over a body reporting B | first shape: `claude.turn.late_tasks_attributed task_ids=[…]` + `live_turn.late_tasks_attributed`; any later turn for B is `live_turn.started push=False` and its `live_turn.fold_decision` is `fold` only for a short restatement (`tools` / `long_answer` = delivered as its own message). Second shape: `claude.turn.started reason=task_finished` ×2 |
| R17-825b *(#876)* | `/new`; *"Run this in the FOREGROUND (not run_in_background) with the Bash tool's `timeout: 10000`: `sleep 40; echo FG done`. When it finishes, tell me what it printed."* (the CLI moves a foreground command to the background at its timeout). Optional steer variant: `/steer` first, send the 60 s foreground command, then "what's 2+2?" ~10 s later; `/queue` afterwards | `🔔 Background task finished — <command description>` ≈30 s after the reply (not `🔔 Claude continued`); the session is not idle-closed while it runs | `claude.task.backgrounded source=task_updated` (or `snapshot`); no `claude.turn.notification_ignored … owned_by_subagent=False`; `claude.live_session.stdin_closed` only after `claude.task.ended`. If no `claude.task.backgrounded` appears (the CLI didn't move it), record *not exercised* — the fakes cover the logic |

### #821 — cost lines say when background agents' spend is included

Chat: `ut-dev: Claude Code` (`-5284581592`). Pre-step: set `[cost_budget] warn_run_above_usd = 0.01` in `~/.untether-dev/untether.toml` (hot-reloads; re-read per check) and note the old value. Tiers: `uv run pytest` + U1–U4 (Claude; U8 for the footer) + B-LIVE-1/2; U1 on Codex to confirm no `bg_*` fields and an unchanged footer.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R17-821 | `/new`; *"Launch TWO background agents (run_in_background). Each reads README.md and CHANGELOG.md, runs `python3 -c "import time; time.sleep(25)"`, then replies one sentence. End your turn now."* Then, as a negative control, `/new` and `what's 2+2?`. Restore `warn_run_above_usd` | launch final ends `💸 This run cost $X (over the $0.01 alert) — includes spend by 2 background agents since the previous reply`; each 🔔 wake final likewise (count = agents active in that window); with `show_api_cost` on, the 💰 line shows `· incl. 2 bg agents`. The 2+2 run: `💸 … (over the $0.01 alert)` with **no** background clause | `journalctl --user -u untether-dev -o cat --since "15 minutes ago" \| grep -E "cost.turn_delta\|cost.run_outlier"` → launch `bg_agents=2`; wake turns `bg_agents>=1 bg_agents_ended>=1`; the 2+2 run `bg_agents=0` |

### #838 — the pre-spawn guard covers Claude runs

Precondition: `[watchdog] max_concurrent_engine_runs = 1` in `~/.untether-dev/untether.toml` (hot-reloads; no restart). Claude chat `-5284581592` (`session_mode = "chat"`, Bash allowed), Codex chat `-4929463515`. Between R17-10a and R17-10b, and between R17-10c and R17-10d, **wait ≥ 70 s after the previous final and confirm `claude.live_session.stdin_closed` / `claude.live_session.closed` in the journal** — an idle Claude live session still counts toward the ceiling, and a message sent into it is injected rather than spawned, so the guard never runs. Logs: `journalctl --user -u untether-dev -o cat --since "15 minutes ago" | grep -E "prespawn|auto_clear|live_session.(injected|stdin_closed|closed)"`. Tiers: U1–U4, U6, U7 on Claude + Codex + OpenCode; B-LIVE-3.

| ID | Steps | Expected |
|---|---|---|
| R17-10a | Claude chat: `run python3 -c "import time; time.sleep(50)" with Bash in the foreground, then reply DONE`. Within 10 s, Codex chat: `say hi` | Codex reply `🛑 Too many engine runs in flight (1/1)…`; `subprocess.prespawn.concurrency_blocked engine=codex live_runs=1` (Claude's process is counted) |
| R17-10b | Codex chat: `run sleep 50 in the shell, then say DONE`. Within 10 s, Claude chat: `what is 2+2?` | Claude chat gets `🛑 Too many engine runs in flight (1/1)…`; `subprocess.prespawn.concurrency_blocked engine=claude`; **no** `subprocess.spawn … use_control_channel=True` in that second; no `session.auto_cleared`, and `session.auto_clear_skipped reason=prespawn_blocked`; the blocked final has no `💰` footer |
| R17-10c | After R17-10b's Codex run ends and ≥ 70 s have passed (Claude live session closed), Claude chat: `what was the last number you computed?` | Resumes the R17-10a session: `runner.start resume=<same id as R17-10a>`; the answer references the earlier context |
| R17-10d | Claude chat: `run python3 -c "import time; time.sleep(40)" in the background with run_in_background and end your turn immediately`; when the turn ends send `quick: 3+3?` | Answered via `claude.live_session.injected` (no guard block although `live_runs=1` and the ceiling is 1) |
| R17-10e | Restore: delete the `max_concurrent_engine_runs` line | Next spawn unaffected |

### #388 — approval buttons only work in their own chat

Required regression (control channel): C1–C6, T8, S9 in the Claude chat (`-5284581592`), plus B-LIVE-7 (an approval inside a wake turn renders and works in the same chat). R17-13a is **gated**: it needs a standalone Telethon client that can send arbitrary callback data (the pattern in memory `reference_add_bot_to_basic_group.md`, BWS session creds — never print them), run as Nathan's own allowed account from a terminal, never from a dev-bot chat. If it can't run, record R17-13a **not exercised** — the unit tests (`tests/test_claude_control.py`, `tests/test_callback_dispatch.py`) are the gate.

| ID | Steps | Expected | Logs |
|---|---|---|---|
| R17-13a *(gated)* | Claude chat: C1 recipe (`/planmode off`, `use the Write tool to create /tmp/r17-388.txt containing x`) → buttons appear. Read the request id from `journalctl --user -u untether-dev -o cat \| grep control_request.registered`. In the **Codex** chat (`-4929463515`) send `/config` (a bot message with buttons). Telethon: `GetBotCallbackAnswerRequest(peer=<codex chat>, msg_id=<that /config message>, data=b"claude_control:approve:<rid>")` | Telethon gets the toast `This request has expired`; the Claude chat's buttons stay live; `/tmp/r17-388.txt` absent | `claude_control.not_found reason=channel_mismatch channel_id=-4929463515 origin_channel_id=-5284581592`; **no** `control_response.sent` / `claude_control.sent` for that rid |
| R17-13b | Tap ❌ Deny in the Claude chat | Normal denial (C2 behaviour) | `claude_control.sent action=deny` |

### #822 — approval logs name the tool

Claude chat `-5284581592`. Run C1–C6, T8 and S9 per the control-channel row, then check: `journalctl --user -u untether-dev -o cat --since "10 minutes ago" | grep -E "control_request.received|control_response.sent|claude_control.sent|inline_keyboard_found|keyboard_attach|control_response.auto_"`.

| ID | Steps | Expected log |
|---|---|---|
| C1 (Write approve) | `/planmode off`, then `use the Write tool to create /tmp/c1-probe containing x`; tap Approve | `control_request.received tool_name=Write permission_mode=acceptEdits` → `inline_keyboard_found … tool_name=Write request_id=…` → `keyboard_attach … tool_name=Write` → `control_response.sent … tool_name=Write permission_mode=acceptEdits` → `claude_control.sent approved=True tool_name=Write` |
| C2 (WebFetch deny) | the C2 recipe; tap Deny | the same chain with `tool_name=WebFetch approved=False` |
| C3 (plan) | `/planmode on`, a complex prompt, tap Pause & Outline, then Approve | `claude_control.sent action=discuss tool_name=ExitPlanMode`; later `inline_keyboard_found … tool_name=ExitPlanMode`; `control_response.sent tool_name=ExitPlanMode permission_mode=plan` |
| R17-16 | In plan mode: `list every .py file under src using Glob` (the auto-approve path) | `control_request.received tool_name=Glob` followed by `control_response.auto_approved request_id=<same>`; no keyboard |
| Secret check | Over the whole window: `journalctl --user -u untether-dev --since -10min \| grep -E 'control_(request\|response)\.\|claude_control\.sent\|inline_keyboard_found\|keyboard_attach' \| grep -c c1-probe` | `0` (the path is tool input; the DEBUG `telegram.request` payloads legitimately contain it, so don't count those) |

Soak note: add `control_request.received` volume to the next `/monitor` checklist — read-heavy plan sessions roughly double the control-path INFO lines.

### #835 — unattended cron / webhook runs fail closed

Chat: `ut-dev: Claude Code` (`-5284581592`). Tiers: `uv run pytest`, C1–C6, T8, S9, R15-7a/c (dispatch WARN, now `outcome=denied`). Logs: `journalctl --user -u untether-dev -o cat --since "15 minutes ago" | grep -E "permission.unattended_deny|control_request.received|control_response.auto_denied|approval_pending|unattended_approval_risk|options_changed|runner.completed"`.

Setup (dev only; back up first: `cp ~/.untether-dev/untether.toml ~/.untether-dev/untether.toml.bak-r17-11`; never `cat` the config): set `[triggers] enabled = true` and add `[triggers.server] port = 9878` (127.0.0.1:9876 / 9877 are taken on lba-1 by other processes and staging). Enabling triggers needs **one** `systemctl --user restart untether-dev` from a terminal — hot reload doesn't start the scheduler or the webhook server. Crons use `project = "claude-test"`, `chat_id = -5284581592`, `run_once = true`, `schedule = "* * * * *"` (fires at the next minute boundary; append them with a heredoc and watch for `triggers.manager.updated`). Dev pre-approves Bash/Read, so recipes use **Write outside the project**.

| ID | Setup / steps | Expected (Telegram) | Logs |
|---|---|---|---|
| R17-11a | cron `r17-default` (`permission_mode = "default"`, prompt `use the Write tool to create /tmp/r17-835a.txt containing x, then say DONE`) | No approval buttons; progress row `🔒 Unattended run — denied Write: nobody to approve it`; the final explains it couldn't write and ends `🔒 unattended (cron:r17-default) · denied Write — nobody to approve` + `💡 set permission_mode on the cron …`; `/tmp/r17-835a.txt` absent | `control_request.received tool_name=Write unattended=cron:r17-default` → `permission.unattended_deny tool_name=Write reason=would_wait permission_mode=default` → `control_response.auto_denied`; **no** `subprocess.approval_pending`; `runner.completed` within ~2 min |
| R17-11b | cron `r17-plan` (`permission_mode = "plan"`, prompt `plan how to create /tmp/r17-835b.txt containing y, then do it`) | Final contains the plan; footer `denied ExitPlanMode` (plus `Write` if the model tried anyway); file absent | `permission.unattended_deny tool_name=ExitPlanMode reason=would_wait` (one, not a loop); any `Write`/`Bash` → `reason=plan_mode` |
| R17-11c | Precondition: Claude chat `/planmode off`, `/config` shows `acceptEdits`. Webhook `r17-wh` (`path = "/hooks/r17"`, `auth = "bearer"`, `secret = "r17-local-only"`, `project = "claude-test"`, `chat_id = -5284581592`, `prompt_template = "use the Write tool to create /tmp/r17-835c.txt containing x, then say DONE"`). Fire: `curl -sS -X POST http://127.0.0.1:9878/hooks/r17 -H "Authorization: Bearer r17-local-only" -H "Content-Type: application/json" -d '{}'` → `202` | Same as R17-11a, with `⚡ webhook:r17-wh` and `🔒 unattended (webhook:r17-wh)`; hint `💡 webhooks use the chat's permission mode …` | `trigger_source=webhook:r17-wh reason=would_wait permission_mode=acceptEdits` |
| R17-11d | Guard: Claude chat `/planmode off`, send R17-11a's prompt by hand (`/tmp/r17-835d.txt`) | Approve/Deny buttons appear (C1); tap ❌ Deny | **no** `permission.unattended_deny`; `control_request.received … unattended=None` |
| R17-11e | Cron `r17-live` (`permission_mode = "default"`, prompt `start a background Bash task with run_in_background: python3 -c "import time; time.sleep(90)", then use the Write tool to create /tmp/r17-835e.txt containing x, then say STARTED`). While the background task runs (≤ 90 s), **reply** to its final: `try the write once more` | The cron's Write is denied (`🔒` row); the reply closes the cron process (`⚙️ Settings changed — …`) and resumes the session attended: a Write **button** appears — tap ❌ Deny | `claude.live_session.options_changed`, then `runner.start resume=<r17-live session>`; no `unattended_deny` for the reply's run |
| R17-11f | cron `r17-pa` (`permission_mode = "plan-auto"`), same prompt as R17-11b with `/tmp/r17-835f.txt` | Regression: plan auto-approved and the Write goes through as attended `plan-auto` does today; no buttons, no `🔒` line | `control_request.auto_approve_exit_plan_mode`; no `unattended_deny`, no `approval_pending` |
| R17-11g *(optional)* | cron `r17-auto` (`permission_mode = "auto"`, prompt `list the files in this project, then say DONE`) | Normal answer, no `🔒` line (nothing reaches stage 6 in auto for routine work) | no `unattended_deny` |

Cleanup: restore the backup (`cp …bak-r17-11 …toml && rm …bak-r17-11`), restart dev once (triggers back to disabled), `/planmode on`, `rm -f /tmp/r17-835*.txt`. Offline evidence: `tests/test_unattended_fail_closed.py`, the step-0 auto probe in `docs/findings/2026-09-30-claude-sdk-control-permissions-context.md` (addendum 2026-10-02). Post-rollout (7 days, all hosts): `journalctl --user -u untether -o cat --since "7 days ago" | grep -E "permission.unattended_deny|permission_mode.mismatch"` — each hit is a cron to fix in config.

### #836 — unattended-approval crons in the startup message

Chat: the dev bot's startup chat. Tiers: Tier 7 Q16 (`/ping` with a cron), U1 (startup message for Claude/Codex/OpenCode). Logs: `journalctl --user -u untether-dev -o cat --since "5 minutes ago" | grep -E "unattended_approval_risk|startup.unattended_line_failed|auto_semantics_changed"`.

Setup (dev only; back up first: `cp ~/.untether-dev/untether.toml ~/.untether-dev/untether.toml.bak-r17-12`): `[triggers] enabled = true` (+ `[triggers.server] port = 9878`), far-future crons (`schedule = "0 0 1 1 *"`): `r15-default` (`project = "claude-test"`, `permission_mode = "default"`), `r15-plan` (`claude-test`, `plan`), `r15-auto` (`claude-test`, `auto`), `r15-codex` (no project, `auto` — resolves to the Codex global default), `r17-a` (`claude-test`, `acceptEdits`), `r17-slip` (`claude-test`, **no** `permission_mode`; dev's `[engines.claude] permission_mode` is `plan`).

| ID | Steps | Expected |
|---|---|---|
| R17-12a | `systemctl --user restart untether-dev` from a terminal (never from inside a bot session) | Startup message: `_triggers:_ \`enabled (0 webhooks, 6 crons)\`` then `_unattended approvals (auto-denied):_ \`cron:r15-default (default), cron:r15-plan (plan), cron:r17-a (acceptEdits)\` +1 more` (the fourth is `cron:r17-slip (inherits plan)`); no `auto` line; `r15-codex` absent. Journal: exactly one `trigger.unattended_approval_risk phase=config reason=startup`; no `startup.unattended_line_failed` |
| R17-12b | Give `r17-slip` `permission_mode = "plan-auto"` and remove `r15-default`, `r15-plan`, `r17-a`; restart | No `unattended approvals` line; the triggers line still present |
| R17-12c | Q16 `/ping` in the Claude chat | Unchanged trigger summary |

Cleanup: restore the backup, restart once.

### #743 — per-cron `model` and `reasoning`

Claude chat `-5284581592`, Codex chat `-4929463515`. Tiers: Tier 7 Q16, R5 (trigger source in footer), R1 (hot-reload cron add), Tier 4 O2 (reasoning in the footer), B-LIVE-x. Logs: `journalctl --user -u untether-dev -o cat --since "<t0>" | grep -E 'trigger\.cron\.(model|reasoning|permission_mode)_override|triggers?\.(manager\.updated|init_failed)|config\.reload\.triggers_failed|options_changed|runner\.completed'`.

Setup: back up (`cp ~/.untether-dev/untether.toml ~/.untether-dev/untether.toml.bak-r17-20`; never `cat` it); `[triggers] enabled = true` (+ `[triggers.server] port = 9878`) needs one dev restart from a terminal if it was off. Append crons with a heredoc; each reload logs `triggers.manager.updated`. Crons: `chat_id = -5284581592`, `run_once = true`, `schedule = "* * * * *"`. Preflight: `/model set opus` in the Claude chat.

| ID | Steps | Expected |
|---|---|---|
| R17-20a | cron `r17-20a`: `engine = "claude"`, `model = "haiku"`, `permission_mode = "plan-auto"`, `prompt = "reply with the single word CRONMODEL"`; wait for the minute | Footer shows a Haiku model + `⏰ cron:r17-20a`; `trigger.cron.model_override trigger_source=cron:r17-20a chat_model=opus trigger_model=haiku engine=claude`; `runner.completed … model=claude-haiku-…` |
| R17-20b | Right after, send `reply with the single word CHAT` | Footer shows Opus; no `trigger.cron.*_override` for this run; `/model` unchanged |
| R17-20c | Two crons in the same minute: `r17-20c1` (`model = "haiku"`), `r17-20c2` (no model), both `engine = "claude"`, `permission_mode = "plan-auto"` | c1's footer Haiku, c2's Opus; exactly one `trigger.cron.model_override` (c1) |
| R17-20d | cron `r17-20d`: `engine = "claude"`, `reasoning = "low"`, `permission_mode = "plan-auto"`, same prompt | Footer shows effort `low`; `trigger.cron.reasoning_override … trigger_reasoning=low` |
| R17-20e | **Validation:** append cron `r17-20e` with `engine = "codex"`, `reasoning = "max"` | `config.reload.triggers_failed` (WARN) naming `reasoning` and the allowed levels; the previous crons stay active (`/ping` still lists them). Remove the entry → the next reload succeeds |
| R17-20f | cron `r17-20f`: `model = "haiku"`, `permission_mode = "plan-auto"`, prompt `start a background Bash task with run_in_background: python3 -c "import time; time.sleep(90)", then reply STARTED`. While it runs, **reply** to its final: `what model are you` | `claude.live_session.options_changed`, the `⚙️ Settings changed — stopping 1 background task …` notice, and the reply's footer shows Opus (resumed session) |
| R17-20g *(gated — needs Nathan's OK)* | cron with `model = "r17-nonexistent"` | An error final names the model problem, not a hang; no retry loop |
| R17-20h | Codex chat: cron `engine = "codex"`, `chat_id = -4929463515`, `model = <a model from the Codex catalogue>`, `reasoning = "low"` | Footer shows that model + effort `low`; `runner.completed engine=codex model=…` |
| R17-20i | `/config → ⏰ Triggers` in the Claude chat while `r17-20a` is configured | Row shows `model=haiku · ` and (for `r17-20d`) `effort=low · ` before `last` |

Cleanup: restore the backup (`cp …bak-r17-20 …toml && rm …bak-r17-20`), `/model clear`, restart once if triggers were toggled; check `run_once_fired.json` holds only r17 ids (removing the crons cleans them on the next reload, #317).

### #826 — `/new` and `/cancel` scoped to the forum topic

Venue: the **`untether-dev-ws`** workspace-mode instance (forum supergroup `-1003669503877`, `topics.enabled = true`, `scope = "main"`, `session_mode = "chat"`, default engine Codex; same editable `.venv` as dev). `@untether_dev_bot` has no forum chat. After the code lands: `systemctl --user restart untether-dev-ws` from a terminal (never `untether.service`). Logs: `journalctl --user -u untether-dev-ws -o cat --since "10 minutes ago" | grep -E "new\.cancel_scope|new\.cancelled_running|cancel\.requested|loop\.cancelled_for_chat|session\.summary|runner\.completed"`.

Pre: Telegram MCP `list_topics` on `-1003669503877`; pick two topics **A** and **B** (create `r17-a` / `r17-b` with `create_forum_topic` if needed). The long run, sent in A: `run the shell command "sleep 120" and then reply DONE`. Note A's and B's thread ids.

| ID | Steps | Expected (chat) | Logs |
|---|---|---|---|
| R17-14a | while A runs: `/new` in **B** | B: `🧹 cleared stored sessions for this topic.` (no "cancelled run"); A later finishes with `DONE` | `new.cancel_scope scoped=True cancelled=0 skipped_other_threads=1 thread_id=<B>`; no `cancel.requested`, no `session.summary cancelled=True` for A |
| R17-14b | start A again; `/new` in **General** | General: `🧹 cleared stored sessions for you in this chat.` (or `no stored sessions to clear for this chat.`); A finishes with `DONE` | `new.cancel_scope … thread_id=None scoped=True cancelled=0 skipped_other_threads=1` |
| R17-14c | start A again; `/cancel` (no reply) in **B** | B: `nothing running in this topic.`; A finishes | no `cancel.requested` |
| R17-14d | while A runs: `/cancel` (no reply) in **A** | A's run ends cancelled | `cancel.requested thread_id=<A>` |
| R17-14e | start A again; `/new` in **A** | A: `🧹 cancelled run and cleared stored sessions for this topic.` | `new.cancel_scope scoped=True cancelled=1 skipped_other_threads=0`; `new.cancelled_running thread_id=<A> count=1` |
| R17-14f *(optional, Claude)* | In A: `/agent set claude`, `/config → 🔁 Loop mode` on, then `/loop 10m print the current time`. Then `/new` in **B**; then `/new` in **A** | Claude confirms the schedule in A; after B's `/new`, `jq '.entries[] \| {chat_id, thread_id}' ~/.untether-dev-ws/active_loops.json` still lists the loop with A's thread id; A's `/new` replies `🧹 … stored sessions for this topic.` and the entry is gone | no `loop.cancelled_for_chat` after B's `/new`; `loop.cancelled_for_chat scoped=True count=1` after A's `/new` |

Regression (non-forum, `@untether_dev_bot`): Tier 7 Q-smoke for `/new` and `/cancel`; in the Codex chat `-4929463515` start `sleep 60`, then `/new` → `🧹 cancelled run and cleared …`, log `new.cancel_scope scoped=False cancelled=1` (`chat_type=group` → chat-wide). Also RC14-2 (the #807 command barrier still replies), T1–T10 sampling, S7, O5, O6. Cleanup on dev-ws: `/agent clear` and Loop mode off in A if R17-14f ran.

### #871 — backticks in commands don't break the action list

Claude chat `-5284581592` unless noted. Regressions: U2, U3, T6, RC12-8, RC12-9.

| ID | Steps | Expected |
|---|---|---|
| R17-07a | `/verbose` off. Ask Claude to run four Bash commands one by one, including ``echo "today `date`"``, a heredoc whose body contains backticks, and `sleep 70` | The progress message has one line per action, each command one code span; the `▸ sleep 70 · 1m 0Ns · …` tail has no stray backtick |
| R17-07b | Repeat R17-07a and `/cancel` during `sleep 70` | The cancelled message keeps one action per line |
| R17-07c | `/verbose` on; R17-07a steps 1–2 | Detail lines are code spans and don't merge; `/verbose` off afterwards |
| R17-07d | Codex chat (`-4929463515`): ``run echo "a `b` c" and then ls`` | Two action lines |
| R17-07e | `/planmode off`, diff preview on: ``run echo "x `y` z" > /tmp/r17-e.txt``; then a multi-line heredoc command | The approval preview shows `$ ` + one code span; the heredoc shows `$` + a fenced `sh` block with its lines intact. Deny both; `rm -f /tmp/r17-e.txt` |

### #868 — emoji-led notes don't get a ✓

Claude chat `-5284581592`. Also re-check R15-19c (its expectation now says no ✓). Logs: `claude.compaction`, `claude.permission_mode.mismatch`.

| ID | Steps | Expected |
|---|---|---|
| R17-08a | U1, then `/compact` as a live follow-up | The row reads `🗜️ Context compacted · …` with no leading ✓ (`claude.compaction`) |
| R17-08b | `/model set haiku` + `/planmode auto`, then any prompt | `⚠️ Asked for auto mode …` with no ✓ (`claude.permission_mode.mismatch`); restore with `/model clear` + `/planmode off` |

### #870 — line-structured replies keep their line breaks

Claude chat `-5284581592` unless noted. Regressions: T11 (tables), RC12-8 (`<br>`), RC12-9 (filenames), U3, C5. Log check: no `can't parse entities` / `transport.send.failed`.

| ID | Steps | Expected |
|---|---|---|
| R17-06a | "Reply with exactly the following text and nothing else, preserving every line break and the leading spaces:" + a 10-line digest (emoji headers, two-space-indented `#N` items, two `Key: value` lines, no blank lines) | One line per source line; indented items visibly indented |
| R17-06b | Ask for ~120 words about TCP, hard-wrapped at 72 columns with single newlines | Reads as one paragraph; 0–2 mid-paragraph breaks is a pass |
| R17-06c | R17-06a in the Codex chat (`-4929463515`) | Same as R17-06a |
| R17-06d | `/at 60s Reply with exactly: <the R17-06a digest>` | The scheduled run's final keeps the line structure |

### #869 — startup trigger counts are pluralised

| ID | Steps | Expected |
|---|---|---|
| R17-09a | Preflight (read-only): `grep -c '^enabled = true'` under `[triggers]` in `~/.untether-dev/untether.toml`, plus `grep -c '^\[\[triggers.webhooks\]\]'` / `grep -c '^\[\[triggers.crons\]\]'`. Restart `untether-dev` from a terminal (or reuse the restart from R17-12a) | The startup `triggers:` line says `1 webhook` / `1 cron` wherever a count is 1. If no count is exactly 1, record **not exercised** (unit tests are the gate). RC14-4 regression: a spent one-shot still shows `, 1 spent one-shot` |

### #418 — `/export` attaches the transcript as a file

Updated U9 (Tier 1) and Q4 (Tier 7) on Claude (`-5284581592`), Codex (`-4929463515`) and OpenCode (`-5200822877`). Logs: `journalctl --user -u untether-dev --since -5min | grep -E "command.attachment|export"`. No live test for the >10 MB / failed-upload fallback (unit tests cover it); spot-check T3 (`/file get`), which shares `send_document`.

| ID | Steps | Expected |
|---|---|---|
| U9-a | A multi-tool prompt (U2), then `/export` | A reply with a 📎 `untether-export-<engine>-<sid>-<stamp>.md` captioned `📄 Session export — <engine> · N events · Markdown` / `Session: <id>`; `command.attachment_sent command=export filename=…md size_bytes=<n>` |
| U9-b | Download the file (MCP `download_media`) and open it | The full transcript: header, `**Usage:** … · last run` (`thread total` on Codex), every action, and the answer untruncated |
| U9-c | `/export json` | A `.json` document that `json.load`s and has `events`; `command.attachment_sent … filename=…json` |
| R17-15 | `write 120 numbered lines of lorem ipsum`, then `/export` | The file contains all 120 lines (the old export cut the answer at 2,000 characters and the message at 3,000) |
| Q4 | `/export` in a fresh chat with no run | `No session history available to export.` (text, no file); no `command.attachment_*` |

### #839 — hot-reload is keyed on file content

Tiers: O1–O9, S5 (upgrade path). Preflight: `grep -c '^watch_config = true' ~/.untether-dev/untether.toml` must be 1; `cp ~/.untether-dev/untether.toml ~/.untether-dev/untether.toml.bak-r17-18`. Logs: `journalctl --user -u untether-dev -o cat --since "<t0>" | grep -E 'config\.(reload|watch)'`. Regression: R15-13a + R15-13d (settings cache + invalid edit) and R1 (hot-reload cron add).

| ID | Steps | Expected |
|---|---|---|
| R17-18a | **Same-stat edit.** (1) `sed -i '/^\[transports\.telegram\]/a forward_coalesce_s = 1.5' ~/.untether-dev/untether.toml`, wait 5 s. (2) With Python, replace `forward_coalesce_s = 1.5` by `forward_coalesce_s = 2.5` in place and restore the original `st_mtime_ns` with `os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))` (same size, same mtime; print that the size is unchanged → `True`) | After (1): one `config.reload.transport_config_hot_reloaded keys=['forward_coalesce_s']`. After (2): a **second** `config.reload.applied digest=…` (a different digest), another `transport_config_hot_reloaded keys=['forward_coalesce_s']` and the "Hot-reloaded" notice in the dev chats |
| R17-18b | **No-op save.** `touch ~/.untether-dev/untether.toml` | No `config.reload.applied` within 5 s (`journalctl --user -u untether-dev --since "-10s" -o cat \| grep -c config.reload.applied` → 0) |
| R17-18c | **Atomic save still works.** `sed -i 's/forward_coalesce_s = 2.5/forward_coalesce_s = 3.5/'`, then send `ping` in the Claude chat | One reload; the `ping` run completes |
| R17-18d | **Cleanup.** `cp ~/.untether-dev/untether.toml.bak-r17-18 ~/.untether-dev/untether.toml && rm ~/.untether-dev/untether.toml.bak-r17-18` | One reload; the key is gone |

### #841 — credentials in URLs are masked in logs

Tier: T1 (voice) + the R15-10 shape. **Key-leak preflight:** `cp ~/.untether-dev/untether.toml ~/.untether-dev/untether.toml.bak-r17-19`; never `cat` the file; set `voice_transcription_api_key = "r17-dummy"` so no real key can leave the host in any step. Logs: `openai.transcribe.error`, `endpoint=https://***@`, and no `r17dummypw`. If the 401 surfaces on a different branch (e.g. `voice.transcribe.error`), the leak check still applies.

| ID | Steps | Expected |
|---|---|---|
| R17-19a | `sed -i` the dev TOML: `voice_transcription_base_url = "https://r17user:r17dummypw@api.groq.com/openai/v1"` (a public host passes the SSRF guard; the dummy Basic credentials replace the Bearer header, so Groq answers 401). Wait for the "Hot-reloaded" notice, then `send_voice` the T1 clip to the Claude chat | The reply is the sanitised failure text (no URL); the journal has `openai.transcribe.error … endpoint=https://***@api.groq.com/openai/v1` |
| R17-19b | **Leak check** over the whole window: `journalctl --user -u untether-dev -o cat --since "<t0>" \| grep -c -E 'r17dummypw\|r17user'` | **0**. Expected-present lines: `config.reload.transport_config_hot_reloaded keys=['voice_transcription_base_url', …]`, `voice.base_url.permitted phase=reload host=api.groq.com`, `ssrf.validated url=https://api.groq.com/openai/v1` (stripped) |
| R17-19c | Restore: `cp ~/.untether-dev/untether.toml.bak-r17-19 ~/.untether-dev/untether.toml && rm ~/.untether-dev/untether.toml.bak-r17-19`, then re-run T1 | T1 transcribes normally |

**Required for rc17:** Tier 7 + Tier 1 (Claude, Codex, OpenCode) + Tier 2 C1–C6 + B-LIVE-1…7 + R17-* (every row above, plus the regressions each subsection names). R17-13a is gated (Telethon; if it can't run, record it *not exercised* — the unit tests are the gate). R17-20g needs Nathan's OK before it runs.

## rc18 scenarios (0.35.5rc18)

Dev bot only (`@untether_dev_bot`, `ut-dev: Claude Code` `-5284581592`), driven from an lba-1 terminal session. Back up `~/.untether-dev/untether.toml` before R18-893/894 and restore it afterwards. Logs: `journalctl --user -u untether-dev -o cat --since "15 min ago" | grep -E "<pattern>"`.

No live scenario: [#889](https://github.com/littlebearapps/untether/issues/889) and [#890](https://github.com/littlebearapps/untether/issues/890) need a real latched usage limit. Unit tests are the gate, plus a passive `/monitor` check on nsd (it hits the 5 h cap most days): one `live_turn.fold_decision decision=error`, then `live_turn.capped_repeat_folded repeats=N`, no `live_turn.capped_repeat_edit_failed`, and error lines reading `live turn N · … · session cost:`.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R18-895a | Short prompt → wait for the final → `/new` within ~20 s | `🧹 closed the idle session and cleared stored sessions for you in this chat.` and no `cancelled ·` card | `new.cancel_scope … idle=1`, `handle.cancelled_after_delivery` |
| R18-895b | Prompt that runs `sleep 60` in the foreground → `/new` mid-run | `🧹 cancelled run and cleared …` plus a `cancelled ·` card | `new.cancel_scope … idle=0` |
| R18-894a | With triggers off and the service running, hot-edit `[triggers] enabled = true` plus a `run_once` cron with `project = "claude-test"` and no `chat_id` | `⚠️ Restart required … Restart-only keys touched: triggers.enabled` | `config.reload.restart_required key=triggers.enabled`, `trigger.cron.chat_fallback cron_id=… default_chat_id=123` |
| R18-893a | Restart `untether-dev` (from the terminal), then let the cron fire to the placeholder default chat | nothing | `triggers.dispatch.send_failed chat_id=123` → `triggers.cron.run_once_pending retry_window_s=900`; no `run_once_completed`; the id is absent from `run_once_fired.json` |
| R18-893b | Within the window, hot-add `chat_id = -5284581592` to the cron | `⏰ Scheduled: cron:<id>` then the run | `triggers.cron.run_once_retry` → `triggers.dispatch.starting` → `run_once_completed` |
| R18-893c | As R18-893a, then restart `untether-dev` inside the 15 min window, then hot-add `chat_id` | `⏰ Scheduled: cron:<id>` then the run | `run_once_pending.json` lists the id after the failure; after the restart the first tick logs `triggers.cron.run_once_retry pending_since=…`; then `run_once_completed`, and `run_once_pending.json` is `{"pending": {}}` |
| R18-891 | Two background Bash commands (`sleep 8`, `sleep 45`); when the first finishes, Claude replies with one short markdown sentence (`**A: 29/40 (Good)** – see \`notes_v2.md\``) | the panel note reads `↳ A: 29/40 (Good) – see notes_v2.md`, with no `**` or backticks | `background_status.folded`, `live_turn.fold_decision decision=fold`, no `background_status.ack_plain_failed` |
| R18-892 | A background agent `P4` (`sleep 40`); when it finishes, Claude SendMessages the same agent for another leg | the second header reads `🔔 Background task finished — P4: … (continued)`, and the panel's done row shows the sum of both legs | `claude.task.ended` → `claude.task.revived` → `claude.task.ended` (same task) |

## rc19 scenarios (0.35.5rc19)

These run on the dev bot only (`@untether_dev_bot`, in `ut-dev: Claude Code` `-5284581592`), driven from an lba-1 terminal session.
- Back up `~/.untether-dev/untether.toml` before R19-900 and R19-896, and restore it afterwards.
- Logs: `journalctl --user -u untether-dev -o cat --since "15 min ago" | grep -E "<pattern>"`.

Two parts have no live scenario:
- **#900's wake-overtakes-error ordering** needs a background task that outlives an errored result inside the 15 s close grace. It is covered by unit and fake-CLI tests (`test_900_errored_run_final_lands_before_the_wake_final`). For a passive `/monitor` check on nsd/mac: finals should appear in time order after usage-limit errors, with no session cost going down, and `final.error_delivered_early` should be logged.
- **#897** is gated by unit tests plus a clean restart.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R19-902a | Short prompt → wait for the final → within ~15 s `/at 5m qa-rc19-902 at test` → `/cancel` | one reply: `❌ cancelled 1 pending /at run.`, and the `/at` never fires | `cancel.idle_session_closed idle=1`, `at.cancelled`, `claude.live_session.closed close_reason=cancel` |
| R19-902b | Short prompt → wait for the final → `/cancel` | `nothing running in this chat — closed the idle session.` | `cancel.idle_session_closed` |
| R19-902c | `/cancel` while a turn is still running | the usual `cancelled · claude` card (unchanged) | `cancel.requested`, `handle.cancelled` |
| R19-897 | Restart `untether-dev` with a `stats.json` holding days older than 90 | nothing; `/stats` All Time is unchanged | `session_stats.rolled_up days=N` once, no `session_stats.*` warnings |
| R19-898 | Prompt → `/health` → restart `untether-dev` → `/health` | the same `today's API cost` both times (not $0.00) | `~/.untether-dev/daily_cost.json` has today's date; `cost_tracker.daily_loaded daily_total=…` |
| R19-896a | `[cost_budget] enabled = true`, `max_cost_per_day = 0.01`, `auto_cancel = true` (hot-reload) → send a prompt | `🛑 Daily budget reached ($X of $0.01). New runs are paused until midnight.` with a **Run anyway** button, and no run | `cost_budget.run_blocked scope=per_day attended=True` |
| R19-896b | Tap **Run anyway** | toast `Running once`; the notice gains `▶️ Running once despite the daily budget.` and loses the button; exactly one run, whose footer reads `🛑 Stopped: today's cost $X reached the daily budget $0.01` | `cost_budget.run_anyway`, `cost_budget.run_stopped`, `claude.live_session.closed close_reason=budget_stop` |
| R19-896c | Swap to `max_cost_per_run = 0.01` → a prompt that starts a 60 s background Bash and replies "started" | the reply's footer reads `🛑 Stopped: run cost $X passed the per-run budget $0.01`, then `⏳ Closing session — 1 background task still running at the cost budget: …`; no wake turn | `cost_budget.run_stopped`, `cost_budget.session_closed closed=True`, `close_reason=budget_stop` |
| R19-896d | As R19-896c, but send a follow-up while the first turn is still running | the follow-up does not run inside the stopped session (it gets the budget notice, or starts fresh) | no `write_user_message` after `cost_budget.run_stopped` |
| R19-903 | `/config` → Loop mode → On → Ask mode → Off | Loop stays on (check `telegram_chat_prefs_state.json`: `loop_enabled: true`). Clear both afterwards | `config.ask_questions.set` |
| R19-900 | `[engines.claude] extra_args = ["--max-budget-usd", "0.60"]` (restart) → a prompt that launches a 40 s background Agent and then reads many files | the `error · claude … error_max_budget_usd` final arrives before the session closes | `final.error_delivered_early`, then `claude.live_session.closed close_reason=error` |
| R19-904 | Reply to a bot final asking "what error subtype and cost are shown in the message I'm replying to?" (no tools); then reply to a plain bot message asking for an exact quote | the answer quotes text that exists only in the replied message | `subprocess.stdin.payload_sent` payload larger than the preamble plus the prompt |

## rc20 scenarios (0.35.5rc20)

These run on the dev bot only (`@untether_dev_bot`), driven from an lba-1 terminal session. The Claude chat is `ut-dev: Claude Code` `-5284581592` (`test-projects/test-claude/`); the Codex chat is `-4929463515`.
- Back up `~/.untether-dev/untether.toml` before R20-925/926, R20-927, R20-924e and R20-928c, and restore it afterwards. Restart dev only from a terminal, never from a bot session.
- Logs: `journalctl --user -u untether-dev -o cat --since "15 min ago" | grep -E "<pattern>"`.
- **CLI 2.1.289 gotcha:** Claude Code refuses a bare foreground `sleep N` (N ≳ 30). Where a recipe needs a long foreground command, ask for `for i in $(seq 1 N); do sleep 1; done` instead.
- Test hooks for R20-923 and R20-929 go in `test-projects/test-claude/.claude/settings.local.json` (written with the Write tool; the repo guard blocks writing a `.claude/settings.json`). Remove them afterwards.

Two parts have no live scenario:
- **#922's real cap** needs an account that is genuinely at its Fable share. R20-922a replays the verbatim result offline instead; R20-922c is Nathan's call.
- **#927 on macOS** is log-only (decision 38): after the Mac rollout, `service.detected manager=launchd unit=com.littlebearapps.untether`.

### #925 / #926 — Loop mode owns Claude's schedules

Setup: `[loop] max_iterations = 2`, `max_total_duration_hours = 1`. Logs: `grep -E "loop\.|hook_callback|scheduled_wakeup|live_session.stdin_closed|cron_suppressed|native_cron_fire|wake_cap"`. The 925a prompt: `[r20] Use the CronCreate tool to create a recurring cron with cron expression "*/1 * * * *" and prompt "[r20] reply with the word tick". Then reply in one line.`

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-925a | Loop **on**, `/new`, the 925a prompt | Reply says Untether scheduled it. ~1 min later `⏰ /loop · iter 1/2` + "tick"; then `iter 2/2` + "tick"; then nothing for 5 min | `control_response.hook_callback callback_id=ut_loop_cron_create decision=deny`, `loop.cli_job_denied loop_mode=on`, `loop.scheduled`; per fire `loop.live_session_closed_for_fire` (or none if already idle-closed) then `loop.fired_ok`; `loop.expired reason=max_iterations iterations_completed=2`. **No** `claude.turn.started reason=scheduled_wakeup`, **no** `fire_skipped_subprocess_alive` |
| R20-925b | After 925a: `[r20] reply with the word after` | First and only reply "after"; no "tick", no `🔔 Claude continued` | `runner.start resume=<sid>`, one `claude.turn.started` |
| R20-925c | `/new`, 925a with `max_iterations = 3`; just before a minute boundary send a 90 s prompt (`for i in $(seq 1 90); do sleep 1; done`) | The iteration due during the busy run is skipped (or runs once after); never two ticks back-to-back | During the 90 s turn: `loop.iteration_skipped_previous_running`. `loop.fire_deferred_session_busy` (debug) only for live-idle sessions with background work, closing sessions or limbo |
| R20-925d | While a loop is active: `[r20] call CronDelete with id <token from the log>` | Claude reports the CLI's "No scheduled job with id …" error (expected on CLI 2.1.289, which checks the id before hooks; the CronCreate deny reason tells it not to retry) and says the loop is stopped; no further iterations | `loop.cancelled reason=cron_delete` from the `tool_use` observer; usually no `callback_id=ut_loop_cron_delete` (the hook stays as a fallback for a CLI that runs hooks first). A foreign token logs `loop.cron_delete_foreign_token` and stops nothing |
| R20-925e | R17-01d recipe, Loop **on**, a 6-minute self-paced wait, 2 iterations | One `⏰ Scheduled wake-up` per wait; no duplicate `⏰ /loop · iter` after the session closes | `hold_extended source=scheduled_wakeup`; `loop.expired reason=cli_fired_live` |
| ~~R20-925f~~ | Retired (13b amendment 4) — replaced by R20-925h | | |
| R20-925g | Kill switch, Loop on: `[loop] own_schedule = false` (hot reload), `/new`, the 925a prompt, `/new` after 2 ticks | rc19-like native ticks. Restore `true` | no `hook_callback`; `loop.scheduled` only |
| R20-925h | Loop **off**, `/new`, the 925a prompt | Claude says scheduling is off and mentions Loop mode / `/at`; no ticks for 3 min | `hook_callback … decision=deny`, `loop.cli_job_denied loop_mode=off`, no `loop.scheduled`, no `claude.turn.started reason=scheduled_wakeup` |
| R20-925i | Loop off: `remind me in 3 minutes to say hello` | Declined with the `/at` suggestion; then `/at 3m say hello` fires once | `loop.cli_job_denied loop_mode=off`; `at.scheduled` / `at.firing` |
| R20-925j | Loop off, `/new`, `every 2 minutes print the time, 5 iterations, using ScheduleWakeup` | Two `⏰ Scheduled wake-up` messages (one more is possible if a wake lands inside the 60 s idle grace), then `⏰ Self-paced wake-up limit reached (2 wake-ups) …`; nothing further | `claude.live_session.wake_cap turns=2 cap=2`, `stdin_closed reason=wake_cap`; in a Loop-on chat a pending long wake-up also logs `loop.expired reason=wake_cap` |
| R20-925k | Legacy native job: `own_schedule = false`; Loop off; `/new`; the 925a prompt (native `*/1`); after one tick `/cancel`. Set `own_schedule = true`; send `[r20] reply with the word legacy` | **Up to two** ticks (the first can come before "legacy"). If a second fire lands while the session is open it is caught; otherwise the next resume repeats the pattern until one is. After a catch the session closes at idle; the next message runs with no ticks for 3 min and shows the `ℹ️ Claude's scheduling is off in this session …` note row | `claude.turn.native_cron_fire suppress=True`, `loop.cron_suppressed_marked source=native_fire`, `stdin_closed reason=cron_suppressed`; the next `runner.start` has `CLAUDE_CODE_DISABLE_CRON=***`, `claude.cron_suppressed` |
| R20-925l | Loop off, a self-paced 3-min wait pending, then `/new` | No wake-up ever arrives | `stdin_closed reason=new`; no later `scheduled_wakeup` |
| R20-925m | Kill switch, Loop off: `own_schedule = false`, the 925a prompt, `/new` after one tick | rc19 behaviour (native tick). Restore `true` | no `hook_callback`, no `wake_cap` |
| R20-926a | `[loop] max_iterations = 5`, Loop on. `/new`; the 925a prompt; wait for `iter 1/5`; `/cancel`; `[r20] reply with the word next` | `❌ cancelled 1 active loop.`; first reply exactly "next"; no ticks for 3 min | `loop.cancelled reason=user_cancel`; **no** `loop.cron_suppressed_marked`; `runner.start resume=<sid>` without `CLAUDE_CODE_DISABLE_CRON` |
| R20-926b | Same session: CronCreate `*/2` with prompt "[r20] reply with the word tock" | `⏰ /loop · iter 1/5` + "tock" within ~2 min | `loop.fired_ok` for the new token; **no** `loop.expired reason=do_not_resume` |
| R20-926c | Residual session (opportunistic; one dev stop/start from a terminal). (1) Loop off + `own_schedule = false`, `/new`, the 925a prompt → native job (session `S`); `/cancel` after one tick. (2) Stop dev; back up `~/.untether-dev/active_loops.json`; set `"do_not_resume": ["<S>"]` and delete the whole `do_not_resume_at` key (dropping only S's stamp leaves a v2 file, so the v1 migration never runs); start dev. (3) `own_schedule = true`; `[r20] reply with the word quiet` | First reply "quiet"; progress shows `ℹ️ Claude's scheduling is off in this session …`; no ticks for 3 min | `loop.cron_suppressed_marked source=restore_v1`; `claude.cron_suppressed session_id=<S>`; `runner.start` args contain `CLAUDE_CODE_DISABLE_CRON=***` |

Regression with the hooks on: U1–U4, U6, C1–C6, B-LIVE-1/2/7, R17-01d (a single self-paced wake still fires with Loop off), Q16 `/ping`. Soak on staging (lba-1) for 24 h before the other 4 hosts. Negative grep over 30 min: `Traceback|hook_callback.*error|initialize: hooks must|loop.hook_decision_failed|loop.restore.entry_invalid|loop.persist_failed` must be empty. Clean-up: restore the config and `active_loops.json` backups, Loop mode off, `/new`.

### #919 / #920 — approval reminders

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-919a | `ask me with AskUserQuestion whether to use TypeScript or JavaScript, with both as options, then wait for my answer`; don't answer for 10.5 min | `⏳ Waiting for your answer (10 min) — tap an option above or reply with your answer. The session is paused, not stuck.` / `❓ <question>` / `/cancel to stop.` — no `warning:`, `(running)` or `Last:` | `subprocess.approval_pending … reason=pending_approval last_action='warning:❓ …(running)'` (raw form kept) |
| R20-919b | `/planmode on`, `plan creating /tmp/r20-919b.txt containing hi, then exit plan mode`; leave Approve Plan for 10.5 min | `⏳ Waiting for you to approve the plan (10 min) — tap a button above. …` | as R20-919a |
| R20-919c | `/planmode off`, `create /tmp/r20-919c.txt containing hi` (Write outside the project); leave it 10.5 min | `⏳ Waiting for your approval to use Write (10 min) — tap Approve or Deny above. …` | `progress_edits.keyboard_attach tool_name=Write` |
| R20-919d | S1 recipe (`kill -STOP` the engine PID, later `kill -CONT`) | The `Last:` line shows a plain title (no `tool:`/`note:` prefix, no `(running)`) | `progress_edits.stall_detected … last_action='tool:…'` (raw) |
| R20-920a | R20-919a recipe; wait 10.5 min for the reminder, then tap an option | Within ≤ 30 s the reminder is gone (`get_history` no longer lists its id); the run completes; nothing pointing "above" remains | `progress_edits.approval_reminder_sent request_kind=question replaced=False` → `progress_edits.approval_reminder_retired reason=resolved deleted=True` |
| R20-920b | Repeat the wait; after the reminder send `/cancel` | Reminder deleted with the run's ephemeral clean-up; `cancelled` final | `approval_reminder_retired reason=run_end deleted=True` |
| R20-920c | R20-919c recipe; after the reminder tap **Deny** | Reminder gone ≤ 30 s | `reason=resolved` |
| R20-920d | Optional (~12 min): B-LIVE-7 recipe, but leave the wake turn's ExitPlanMode keyboard 10.5 min, then Approve | Reminder sent on the wake turn and removed after Approve or at turn close | `approval_reminder_sent` then `retired reason=resolved` or `run_end` |
| R20-920e | Optional (41 min): leave an AskUserQuestion for 41 min, then answer | Reminders at 10 and 40 min only; exactly one visible (the 40-min one replaced the 10-min one); no `(warned` suffix | two `approval_reminder_sent` ~1800 s apart, the second `replaced=True`; nothing at 33/36/39 min |

### #929 — a background agent's approval while the session is idle

Setup (dev-only hook, Write tool): `test-projects/test-claude/.claude/settings.local.json` →
`{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"jq -r .tool_input.command | grep -q R20ASK && echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"R20 test hook: confirm this command\"}}' || true"}]}]}}`.
Prompt P: *"Launch ONE background agent (Agent tool, run_in_background: true) that first runs `sleep 20`, then runs the Bash command `echo R20ASK-929`, then replies 'agent done'. End your turn immediately after launching it."*

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-929a | `/new`, `/planmode off`, send P; after the launch final, wait | ~20–30 s after the final a **pushed** `🔐 A background agent needs your approval` / `🤖 <agent desc>` / `Permission Request [CanUseTool] - tool: Bash (command=`echo R20ASK-929`)` / `🪝 R20 test hook: confirm this command` / `The agent is paused until you answer.` with ✅ Approve / ❌ Deny. Tap Approve → toast; the surface is gone within ≤ 30 s; later `🔔 Background task finished — …` | `control_request.received tool_name=Bash agent_id=a… decision_reason_type=hook` → `approval_surface.sent source=event` → `claude_control.sent approved=True` → `approval_surface.retired reason=resolved deleted=True`; **zero** `control_request.unanswerable` |
| R20-929b | Repeat, tap Deny | Surface removed; the agent reports the denial in its 🔔 wake | `claude_control.sent approved=False`; `retired reason=resolved` |
| R20-929c | Incident mirror: `/config` → permission mode **auto**, Diff preview **on**; `/new`, send P | Same surface as R20-929a | `control_request.received permission_mode=auto … decision_reason_type=hook`, `approval_surface.sent` |
| R20-929d | Repeat R20-929a but `/cancel` while the surface is up | Session closes; surface deleted; a stale copy's tap toasts `No longer needed` / expired | `approval_surface.retired reason=run_end` (or `completed` after `cancelled_by_cli`) |
| R20-929e | Optional (~11 min): repeat R20-929a, leave it 10.5 min | Re-sent at 10 min as `⏳ Still waiting for your approval (10 min) — a background agent is paused until you answer.`, the old copy deleted — one surface visible; tapping the new one works | `approval_surface.sent reposts=1 replaced=True` |
| R20-929f | Run in a **default**-mode chat (not `auto`, whose classifier allows the Write): `/planmode off`, P with step 2 "use the Write tool to create /tmp/r20-929f.txt containing x" | Surface for `tool: Write` with the diff preview; Approve → file written | `approval_surface.sent tool_name=Write` |

A request rescued after the 30 s grace (`approval_surface.sent source=rescue`) must show the command or key input above Approve / Deny, not just the tool name. Regression: C1 (in-turn Write keyboard on the progress message, **no** `approval_surface.sent`), C4 (AskUserQuestion), B-LIVE-1/2, B-LIVE-7 / R15-6b (wake-turn ExitPlanMode keyboard on the wake turn's message, no surface). Restore `/config` and remove the hook afterwards.

### #923 / #928 — hook-rewake labels and no-query results

Run with RC14-6 / R17-828 (one hooks setup).

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-923 | In `settings.local.json` add `PostToolUse`, matcher `Bash`: `{"type":"command","asyncRewake":true,"if":"Bash(echo R20REWAKE*)","command":"sleep 20; echo 'R20 finding: test' >&2; exit 2"}`. Send: *"Launch ONE background agent (run_in_background) that runs `sleep 15`, then the Bash command `echo R20REWAKE`, then `sleep 60`, then reports 'agent done'. End your turn immediately."* | ≈35 s later one **pushed** `🪝 Hook feedback — PostToolUse` mentioning the finding; ≈1 min later `🔔 Background task finished — <agent>` | `claude.hook.blocking_exit hook_event=PostToolUse turn_open=False rewake_candidate=True held_s≈20`, then `claude.turn.hook_rewake attributed=open_idle` (with `gap_s`), then `claude.turn.started reason=hook_rewake`. If no parent turn opens within 3 s (the subagent took the wake), record it and ask Nathan (05 decision 3, default keep). Negative control R17-828 → `rewake_candidate=False`, no `hook_rewake`; positive control RC14-6 → `rewake_signal started_turn=1` → `hook_rewake attributed=open` |
| R20-928a | *"Launch ONE background agent (run_in_background) that runs `sleep 20` and then replies 'R20 tail done'. End your turn immediately."* | Turn-1 final, then ≈25 s later one pushed `🔔 Background task finished — <agent>`; no other message; no empty `💬` status row | `grep -E "claude.turn.(started\|completed\|no_query)\|runner.completed\|live_turn.no_query_dropped"` → one `claude.turn.no_query` (`opened=False` = case R, `opened=True` = case I — record which); every live `claude.turn.completed` has `origin_kind=` and the wake turn shows `task-notification`; **zero** `runner.completed … num_turns=0 … answer_len=0` and zero `claude.turn.completed num_turns=0 … reason=unknown`. A `claude.turn.completed num_turns=0 reason=unknown origin_kind=<other\|None>` means tails lack the origin — stop (06 decision 3) |
| R20-928b | *"Start two background Bash jobs (run_in_background): `sleep 20; echo A` and `sleep 35; echo B`. Tell me each output when it finishes. End your turn."* | Two **pushed** `🔔 Background task finished` messages (A, then B) | no `claude.turn.task_end_paired` after a `claude.turn.no_query`; B's `live_turn.started` has `push=True` |
| R20-928c | `[progress] consolidate_wake_turns = false` (hot reload), repeat R20-928a | No empty `🔔 Claude continued` message | as R20-928a. Restore the setting |

Tiers: U1–U4 (Claude), B-LIVE-1/2/3/5/6, RC14-6, R17-828, R17-821 if reproducible, plus a Stop-at-limit smoke (R19-896) with a follow-up after a wake.

### #921 — cancel racing a queued follow-up

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-921a | (1) `/queue` (bare). (2) `Run this in the FOREGROUND (not background): for i in $(seq 1 45); do sleep 1; done; echo first-done. Then say done.` (3) ~10 s later `reply with the single word QUEUED-OK` — it shows `⏳ Queued …`. (4) ~10 s later `/cancel` (reply to the first run's progress message) | First run `cancelled · claude`. **No** `⚠️ The session ended before this message ran — please send it again.` anywhere. Within a few seconds the follow-up answers `QUEUED-OK` (a resumed run in the same session) | `cancel.requested`, `claude.live_session.steer_window_closed reason=cancel`, `claude.live_session.inject_unavailable user_msg_id=<3>`, `claude.live_session.followup_requeued`, then `handle.incoming user_msg_id=<3>` and `runner.completed ok=True`. **Absent:** `followup_not_run` |
| R20-921b | As R20-921a but `/steer` first, and send step 3 after the loop has started (it folds or steers), then `/cancel`. `/queue` afterwards | No "send it again". Either the steer folded into the cancelled turn (`↪️ Steered…` ack) or it runs after the cancel | no `followup_not_run`; no `followup_settle_timeout` |

Regression: B-LIVE-3, RC12-7, RC13-5(c), RC14-1, R19-896d. Post-rollout fleet check (7 days): `followup_not_run` only without a preceding `inject_unavailable` for the same `user_msg_id` within 5 s; a `followup_settle_timeout` is a finding only with `live_owner_present=False`.

### #922 — usage-credits cap wording

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-922a | Offline replay (no bot, zero tokens): feed the channelo verbatim result line (`is_error: true`, `result: "You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai/settings/usage?from=cc_cli_limit_message, to continue."`), then `{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}` through `decode_stream_json_line` + `translate_claude_event` in a `uv run python` snippet | Latch armed; the rejection's title is `⛔ Fable limit reached — may not clear on a timer; switch with /model or manage usage credits on claude.ai` | `claude.rate_limit_action_required_latched model=Fable kind=model source=result_text`, then `claude.rate_limit_event retry_after_source=action_required action_model=Fable` |
| R20-922b | Claude chat: U1 | Normal run; no false-positive latch | `runner.completed ok=True`; no `rate_limit_action_required_latched` |
| R20-922c | Gated (Nathan's call): only on an account genuinely at its Fable share, any prompt on a Fable model | Error final plus the latch | as R20-922a but `source=api_error api_error=model_requires_usage_credits api_error_status=429` |

### #927 — restart hints name the running service

Preflight: `grep -c '^watch_config = true' ~/.untether-dev/untether.toml` = 1; back up the config to `~/.untether-dev/untether.toml.bak-r20-927`.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-927a | Restart dev from a terminal | Normal startup message | `service.detected manager=systemd-user unit=untether-dev exec_pid_match=True` |
| R20-927b | Flip `[transports.telegram] message_overflow` (`split`↔`trim`) with the Edit tool; save | (1) ``🔄 Setting `message_overflow` changed — restart required to take effect. To apply: run `systemctl --user restart untether-dev` (or send `/restart`).`` (2) the `⚠️ Restart required …` notice whose "To apply," clause names `untether-dev`. Neither names the bare staging unit | `config.reload.transport_config_changed … restart_required=True`, `config.reload.restart_notify.sent` |
| R20-927c | `[triggers] enabled = false` → `true`; save | `⚠️ Restart required …` listing `triggers.enabled`, naming `untether-dev` | `config.reload.restart_required key=triggers.enabled` |
| R20-927d | Restore the backup (a second notice is expected), then run U1 in the Claude chat | U1 passes | `preamble.applied source=default`; the restored config's sha256 matches the backup |

Multi-instance (log only): a terminal restart of `untether-dev-hf` logs `service.detected … unit=untether-dev-hf`. Zero-token generic check from a plain ssh terminal: `.venv/bin/python -c "from untether.service_manager import restart_hint; print(restart_hint())"` prints ``restart Untether's service (or send `/restart` if a service manager keeps it running)``.

### #924 — the outbox sends only this run's files

Dev config already has `[transports.telegram.files] enabled = true` + `watch_config = true`. **Create stale files ≥ 30 s before the prompt** (ctime can't be backdated). Outbox: `test-projects/test-claude/.untether-outbox/`.

| ID | Steps | Expected Telegram | Log signatures |
|---|---|---|---|
| R20-924a | Shell: 3 files `r20-old-{1,2,3}.md` in the outbox; wait 30 s. Claude: `What is 2+2? Don't create or change any files.` | Final; **no documents**; one `📎 3 older files were already in .untether-outbox/ …` notice naming them + `.skipped/`; the files are now in `.untether-outbox/.skipped/` | `outbox.stale count=3 … archived=True`; no `outbox.sent` |
| R20-924b | One stale file, wait 30 s. `Write a file .untether-outbox/r20-fresh.md containing "fresh".` | `r20-fresh.md` delivered; stale notice for the one old file | `outbox.sent file=.untether-outbox/r20-fresh.md`, `outbox.stale count=1` |
| R20-924c | `Run exactly: printf x > r20-mv.md && touch -d 2020-01-01 r20-mv.md && mkdir -p .untether-outbox && mv r20-mv.md .untether-outbox/` | `r20-mv.md` **is delivered** (ctime rule) | `outbox.sent file=.untether-outbox/r20-mv.md` |
| R20-924d | `Start a background Bash task: sleep 20 && echo bg > .untether-outbox/r20-bg.md, then tell me you started it.` Wait for the wake turn | File delivered after the wake turn's final | `live_turn.started reason=task_finished…` then `outbox.sent file=.untether-outbox/r20-bg.md` |
| R20-924e | `outbox_stale_policy = "send"` (hot reload), repeat the 924a setup, wait 30 s, same prompt | The 3 old files **are** sent; revert the config | `config.reload.transport_config_hot_reloaded keys=['files']`; `outbox.sent` ×3; no `outbox.stale` |
| R20-924f | `Create 12 small files r20-n01.md … r20-n12.md in .untether-outbox/.` | 10 documents; `📎 2 more files weren't sent (limit outbox_max_files = 10): … — moved to .untether-outbox/.skipped/.` | `outbox.overflow count=2 max_files=10 archived=True` |
| R20-924g | Gated, Mac, read-only: on `mac`, `cd /tmp && echo x>a && touch -t 202001010000 a; stat -f '%m %c' a; mkdir -p o && mv a o/; stat -f '%m %c' o/a` | — | ctime after `mv` is now (APFS rename updates ctime); if not, note it in the docs and keep the rule |

Also run U1 on Claude and Codex, T3 (`/file get CLAUDE.md`), `/file get .untether-outbox/.skipped` after R20-924a (returns `.skipped.zip` with the 3 files), B-RESUME and U4. Clean up the outbox afterwards. No WARNING/ERROR `outbox.` lines (`outbox.stale_archive_failed` must be 0); a symlinked or out-of-project outbox logs `outbox.outside_root` and is never scanned.

**Required for rc20:** Tier 7 + Tier 1 (Claude, Codex, OpenCode) + Tier 2 C1–C6 + B-LIVE-1…7 + R20-* (every row above, plus the regressions each subsection names). R20-922c and R20-924g are gated; R20-926c is opportunistic; R20-920d/e and R20-929e are optional.

## 0.36.0rc1 scenarios / results

0.36.0rc1 is the 0.35.5 line renumbered as a minor ([#947](https://github.com/littlebearapps/untether/issues/947)). The runtime diff from rc20 is the version bump and wording only, so this run covers the **Minor** tiers, the rc20 gaps and the upgrade path from 0.35.4.

- **When and what:** 2026-10-05, on the dev bot, head `413288b`.
- **CLIs:** Claude 2.1.289, Codex 0.160.0, OpenCode 1.14.33, Pi 0.78.0.
- **Pre-flight:** `pytest` 6257 passed (88.26% coverage), `ruff` clean.
- **Marker:** `~/.untether-dev/integration-test-pass-0.36.0rc1.json`. It was first written with Q16 and R20-926c NOT RUN; both were then run later the same day with Nathan's approval, and the marker notes were updated.

| ID | Engine / chat | Result | Note |
|---|---|---|---|
| Q1–Q15 | DM | PASS | Q14 ran on the wrong engine (codex, not the chat's claude) → [#950](https://github.com/littlebearapps/untether/issues/950) |
| Q16 | Pi chat | PASS | Triggers temporarily on, with a far-future test cron for the Pi chat and a dev restart: `🏓 pong — up 10s` / `⏰ triggers: 1 cron (it-0360rc1-q16, 0 3 1 1 *)`. Config restored afterwards |
| `/config` deprecated wording | DM | PASS | `⚠️ amp, gemini — deprecated, no longer supported, may be removed in a future release. Prefer another engine.` |
| U1–U10 | Claude | PASS | |
| U1–U10 | Codex | PASS | The footer model is still `codex-mini-latest` (#846) |
| U1–U10 | OpenCode | PASS | Text parts are glued together ("CLAUDE.md.Files") → [#955](https://github.com/littlebearapps/untether/issues/955) |
| U1–U10 | Pi | PASS | U1's tool calls were blocked by the lba-1 `pi-permission-system` extension (environment, not Untether). Re-run as U2b with a temporary allow config: PASS. U8 has no token counts (#899) |
| C1–C5, C7 | Claude | PASS | |
| C6 | Claude | PARTIAL | The approved keyboard lingered 11 s and the denied one 6 s (rc20: 13–17 s). Pre-existing pacing, [#945](https://github.com/littlebearapps/untether/issues/945) |
| T1, T2, T3, T6, T8 | DM | PASS | |
| O1, O2, O8 | DM / Codex | PASS | O8: `/agent set pi` survived a dev restart |
| S2, S4, S7 | Codex / OpenCode / Pi | PASS | |
| S1 | Codex | PARTIAL | The stall warning fired at 15 min, not 5, because npm-wrapped engines always get the children threshold → [#953](https://github.com/littlebearapps/untether/issues/953). Claude S1 was not run |
| B-LIVE-1/2/7 | Claude | PASS | B-LIVE-2's follow-up was queued about 6 s behind a wake turn |
| R20-919b | Claude | PASS | The 10-minute tool-approval reminder names Write as an approval, with no `kind:title (running)` leak. The Deny tap cleared it within 20 s, so R20-920c passes too |
| R20-926c | Claude | PASS | Session `26e82577`, with a true v1 file (no `do_not_resume_at` key). Logs: `loop.cron_suppressed_marked source=restore_v1`, `claude.cron_suppressed`, and `runner.start` with `CLAUDE_CODE_DISABLE_CRON=***`. First reply "quiet"; no ticks for 3+ min; the note row is inferred (1 step, no tool use; the card is deleted on final). A first attempt that kept `do_not_resume_at` was invalid setup |
| #948 | Claude | REPRODUCED 1/2 | The card stayed "working" after cancel when the follow-up was queued first ([comment](https://github.com/littlebearapps/untether/issues/948#issuecomment-5988234252)) |
| #928 residual | Claude | PASS | None of 13 `runner.completed` across 3 live sessions had `turn_cost_usd=0.0` |
| Upgrade: config | offline | PASS | A 0.35.4-shaped config parses. `auto_semantics_changed` fires once for both `auto` crons (sunset 0.37.0) |
| Upgrade: state | offline | PASS | State files written by 0.35.4's code load cleanly, with one-shot migrations (chat-prefs `plan-auto`, loops v1 `do_not_resume`) |
| Upgrade: rollback | offline | PASS | 0.35.4 rejects `[loop] own_schedule`, as documented. About 20 other new keys and `plan-auto` also break a rollback but aren't documented → [#956](https://github.com/littlebearapps/untether/issues/956) |
| Logs | dev | PASS | Every warning and error is explained (deliberate bad model ids, S1 stall, T8 stale button, MCP `catalog_staleness`). 12 FDs, no zombies |

**Filed (rc21, v0.36.0):** [#950](https://github.com/littlebearapps/untether/issues/950) through [#956](https://github.com/littlebearapps/untether/issues/956):

- #950: `/at` uses the wrong engine.
- #951: the About page blocks the event loop for about 10 s, and Pi is missing.
- #952: a failed run on a non-Claude engine silently drops the session.
- #953: stall threshold.
- #954: the elapsed timer freezes.
- #955: OpenCode text joins.
- #956: rollback docs.

**Not filed (trivial, noted here):**

- A stale `💬 interim update` line stays in the background block after "all done".
- The path is repeated in the long-running action tail.
- Spurious `✓ Permission resolved` rows.

## 0.36.0rc2 scenarios / results

0.36.0rc2 fixes the 0.36.0rc1 integration-run and soak findings plus the open v0.36.0 bugs. This run is **targeted**: each fix was verified live where it can be triggered on demand, with a regression pass over the paths the fixes touched. The full **Minor** tiers followed on 2026-10-06 (see "0.36.0rc2 Minor tiers" below), and the marker was written from that run.

- **When and what:** 2026-10-05 22:48–23:50 UTC, on the dev bot (and the `untether-dev-ws` forum for [#826](https://github.com/littlebearapps/untether/issues/826)), feature branch `feature/v0.36.0rc2`.
- **CLIs:** Claude 2.1.289, Codex 0.160.0, OpenCode 1.14.33, Pi 0.78.0.
- **Pre-flight:** full `pytest` green (6315 passed before the last batch of fixes; re-run on the final head before merge), `ruff` clean.

| ID | Engine / chat | Result | Note |
|---|---|---|---|
| [#838](https://github.com/littlebearapps/untether/issues/838) | Claude + Codex | PASS | `[watchdog] max_concurrent_engine_runs = 1`: `/claude` in the Codex chat while a Claude run was live → `🛑 Too many engine runs in flight (1/1)` and `subprocess.prespawn.concurrency_blocked` from the Claude runner; a follow-up into the live Claude session was absorbed, not blocked. Already fixed in rc17 |
| [#826](https://github.com/littlebearapps/untether/issues/826) R17-14a–e | dev-ws forum (Codex) | PASS | `/new` in topic B and in General: `new.cancel_scope scoped=True cancelled=0 skipped_other_threads=1`; `/cancel` in B: "nothing running in this topic."; `/cancel` and `/new` in A cancel only A. Already fixed in rc17 |
| [#921](https://github.com/littlebearapps/untether/issues/921) R20-921a | Claude | PASS | `/queue`, a 45 s loop, a follow-up, then `/cancel`: the queued message was re-dispatched and answered (QUEUED-OK); no "please send it again" |
| [#924](https://github.com/littlebearapps/untether/issues/924) R20-924a | Claude | PASS | 3 old files in the outbox: none attached, one 📎 notice, moved to `.skipped/`, `outbox.stale count=3 archived=True` |
| [#929](https://github.com/littlebearapps/untether/issues/929) R20-929a | Claude | PASS | Re-run on rc2 because [#388](https://github.com/littlebearapps/untether/issues/388) touched the request registry: surface pushed, Approve went through, retired after 27.5 s, no `control_request.unanswerable` |
| [#987](https://github.com/littlebearapps/untether/issues/987) | Codex | PASS | One `⚠️ Codex is ignoring 1 unrecognized configuration setting` row in progress; `/export` shows `- ✓ ⚠️ …` |
| [#954](https://github.com/littlebearapps/untether/issues/954) | Claude | PASS | Silent 2500-word story: header 7s → 1m 02s → 1m 33s, repainting every 30 s with no events |
| [#951](https://github.com/littlebearapps/untether/issues/951) | Pi | PASS | `/config` → About in ~3 s, lists `pi 0.78.0` |
| [#984](https://github.com/littlebearapps/untether/issues/984) | Pi | PASS | `/browse` of a folder with one file and one dir: `1 dir · 1 file` |
| [#955](https://github.com/littlebearapps/untether/issues/955) / [#970](https://github.com/littlebearapps/untether/issues/970) | OpenCode | PASS | Pre-tool and post-tool text are separate paragraphs; `opencode.version.probe` ran once (1.14.33) and the run spawned normally. The 2.x refusal path is unit-tested only |
| [#952](https://github.com/littlebearapps/untether/issues/952) | Pi | PASS | `/model set` a bogus model, `ping`: run failed, `session.auto_clear_skipped reason=not_resume_failure`; after `/model clear` the next ping resumed the same session with no error |
| [#889](https://github.com/littlebearapps/untether/issues/889) | Claude | PASS | Resumed run with a non-existent model: `session: … · resumed · turns: 1 · cost: $0.00 · session cost: $2.03 · session api: 317056ms` |
| [#985](https://github.com/littlebearapps/untether/issues/985) | Claude | PARTIAL | Two background tasks: the block ends `✅ all 2 background tasks done` with no `💬` interim note, but Task A's `↳` ack still says "Task B … is still running" → [#989](https://github.com/littlebearapps/untether/issues/989) |
| [#986](https://github.com/littlebearapps/untether/issues/986) | Claude | PASS | 120 s foreground loop: `▸ for i in …; echo finished-986b · 1m 30s`, no repeat. (A bare `sleep 75` is blocked by Claude Code; use a loop) |
| [#948](https://github.com/littlebearapps/untether/issues/948) | Claude | PASS ×2 | Cancel tapped mid follow-up (steps updating) in a live session holding a background task: card ends `cancelled · claude · Ns`, no buttons, never reverts |
| [#388](https://github.com/littlebearapps/untether/issues/388) | Claude | PASS (single user) | `approval_originator_only = true` hot-reloaded; the originator's Approve (C1) and AskUserQuestion answer (C4) work. The refusal of a second user is unit-tested only (no second allowed account on the dev bot) |
| [#823](https://github.com/littlebearapps/untether/issues/823) | Codex | PASS | Progress message deleted by hand mid-run: `telegram.benign_rejection … kind=progress … reason_class=target_gone` |
| [#953](https://github.com/littlebearapps/untether/issues/953) S1 | Codex | PASS | `kill -STOP` on the engine with a shell command open: `⏳ Engine process is stopped (state T, 10 min)` at `stall_threshold_selected reason=running_tool threshold=600` (was 900 s and "Child processes idle"); `kill -CONT` + `/cancel` clean |
| [#904](https://github.com/littlebearapps/untether/issues/904) | Claude | PASS (reply half) | A reply to an earlier bot message reached the agent as `telegram_reply_context`; the selected-quote half needs the Telegram app |
| C1, C2, C4 | Claude | PASS | C1 approve (file written), C2 deny acknowledged, C4 option answered |
| T8 | Claude | PASS | Keyboards are removed when the run ends, so no stale button is left to tap |
| U1/U3/U4/U6/U9/U10 sampling | all 4 | PASS | Codex long answer split 3/3; resumes on Claude and Pi; cancels on Claude and Codex |
| Logs | dev | PASS | Only explained warnings/errors (the dev config's placeholder `chat_id = 123` startup message, the deliberate bad models, the S1 stall, `catalog_staleness`); 16 FDs, no zombies |

**Unit-test only (can't be triggered on demand):** [#905](https://github.com/littlebearapps/untether/issues/905) (stream-idle class vs a wake result), [#928](https://github.com/littlebearapps/untether/issues/928) residual (needs a slow `deleteMessage` after a sent final), [#959](https://github.com/littlebearapps/untether/issues/959) (clock-mapping race), [#789](https://github.com/littlebearapps/untether/issues/789) (voice mishear map), [#970](https://github.com/littlebearapps/untether/issues/970) 2.x refusal.

**Filed during rc2 (v0.36.0, label `0.36.0rc3`):** [#989](https://github.com/littlebearapps/untether/issues/989) (#985 attributed variant), [#990](https://github.com/littlebearapps/untether/issues/990) (steer fallback from General), [#991](https://github.com/littlebearapps/untether/issues/991) (#928 timeout edges), [#992](https://github.com/littlebearapps/untether/issues/992) (unattributed `reason=unknown` wakes on staging), [#993](https://github.com/littlebearapps/untether/issues/993) (OpenCode install pin / About probe command), [#994](https://github.com/littlebearapps/untether/issues/994) (`event_seq` double duty).

### 0.36.0rc2 Minor tiers (2026-10-06, head `a899252`)

Run on the dev bot by three parallel QA agents (one per set of chats) plus the coordinator for the restart tests, 00:49–01:18 UTC. CLIs: Claude 2.1.289/2.1.290, Codex 0.160.0, OpenCode 1.14.33, Pi 0.78.0.

- **Marker:** `~/.untether-dev/integration-test-pass-0.36.0rc2.json`, bound to `a899252`.
- **Rolled out:** lba-1, nsd, channelo and mac with `scripts/fleet-rollout.sh 0.36.0rc2 --only <host>`, and each bot answered the `/ping` sweep. sl was held back because a long Claude Code batch was running there.

| Tier | Result | Note |
|---|---|---|
| Tier 7 Q1–Q16 (DM) | PASS | Q14 `/at` fired on the DM's `/agent` engine (claude), confirming [#950](https://github.com/littlebearapps/untether/issues/950). Q16: triggers temporarily on, with a far-future cron for the Pi chat and a restart, gave `⏰ triggers: 1 cron (it-0360rc2-q16, 0 3 1 1 *)`; config restored afterwards |
| `/config` home / About / Engine & model | PASS | About: `Version: 0.36.0rc2` … `pi 0.78.0`; gemini and amp marked ⚠️ deprecated |
| Tier 1 U1–U10, Claude | PASS | U3 split into 8 messages, U4 resume, U6 cancel with no orphan, U9 export attached |
| Tier 1 U1–U10, Codex / OpenCode | PASS | U5 with valid model ids (the Codex ChatGPT account rejects some ids, an environment limit rendered with a 💡 hint) |
| Tier 1 U1–U10, Pi | PASS (env note) | U1 blocked by lba-1's `pi-permission-system` extension; U1b/U2b with a temporary project allow config: PASS. U8 still has no token counts (#899) |
| Tier 2 C1–C5, C7 | PASS | C3 outline → Approve / Deny / Let's discuss; C5 `📝 /tmp/c5.txt` / `+ hello` |
| Tier 2 C6 | PARTIAL | Keyboard lingered about 9 s / 7 s, the known pacing issue ([#945](https://github.com/littlebearapps/untether/issues/945)) |
| B-LIVE-1 / 7 | PASS | |
| B-LIVE-2 | PASS on re-run | First attempt: the subagent's bare `sleep 45` was blocked by Claude Code, and its early finish raced a steered follow-up, producing a `reason=unknown` wake (reproduction added to [#992](https://github.com/littlebearapps/untether/issues/992)). Use a loop in this recipe |
| Tier 3 T1 (TTS OGG via `send_voice`), T2, T3, T6, T7, T8, T11 | PASS | T6, T7 and T11 entities checked by re-rendering the raw engine answers through Untether's renderer |
| Tier 4 O1, O2, O4, O6, O8, O9 | PASS | O8: `/agent set pi` in the Codex chat survived a dev restart |
| Tier 4 O5 | PARTIAL | The first prompt after `/ctx set` was injected into the chat's idle live Claude session and ran in the old cwd ([#996](https://github.com/littlebearapps/untether/issues/996); predates rc2). `/browse` also ignores `/ctx` ([#997](https://github.com/littlebearapps/untether/issues/997)) |
| Tier 6 S2, S4, S7, S8, S9 | PASS | S7: 5 messages, all answered in order; S8: 4041 characters intact; S9: second tap → `Already answered` |
| Tier 6 S1 | PASS (rc2 run) | Codex SIGSTOP, see [#953](https://github.com/littlebearapps/untether/issues/953) above |
| Upgrade path | PASS | rc2 `load_settings` parses the live lba-1, nsd, channelo and mac configs. rc2's only new key is `approval_originator_only` (default off) and no state format changed. Rollback target 0.35.5rc20 is still on TestPyPI |
| Logs | PASS | No Traceback. Every warning and error is explained (placeholder startup `chat_id = 123`, a deliberate bad Pi model, the triggers `restart_required` notice, webhook port already bound by another instance, one benign `stdout_held_after_exit`); 11 FDs, 0 zombies |

**Filed:** [#996](https://github.com/littlebearapps/untether/issues/996) (`/ctx` vs a live session), [#997](https://github.com/littlebearapps/untether/issues/997) (`/browse` ignores `/ctx`), [#998](https://github.com/littlebearapps/untether/issues/998) (`/model clear` keeps the resumed session's model).

## 0.36.1rc1 scenarios / results (Antigravity, #558)

0.36.1rc1 ("Safe core") adds the Antigravity CLI (`agy`) engine ([#558](https://github.com/littlebearapps/untether/issues/558)), with [#975](https://github.com/littlebearapps/untether/issues/975) (a result held for a background task is an expected wait), [#976](https://github.com/littlebearapps/untether/issues/976) (`agy_version` in the logs) and [#993](https://github.com/littlebearapps/untether/issues/993) (the About versions line). The same change touches the base runner, the liveness watchdog, the stall monitor, cron options and the preamble, so the run is the **Minor** tiers plus the AG rows below, with a regression pass on Claude, Codex, OpenCode and Pi.

> **Status: NOT RUN.** This section was written with the code, before any live run. No row below has been run, no attestation marker exists for 0.36.1rc1 and nothing has been rolled out. Fill in the results table as rows are run; a row that is skipped is recorded as NOT RUN with a reason, never as a pass.

- **When and what:** not run yet. Dev bot only (`@untether_dev_bot`). The full run happens on the `feature/558-antigravity` head before the PR; the smoke rows are then repeated on the merged `dev` head for 0.36.1rc1, and the marker binds that SHA.
- **Prerequisites at 2026-10-10:** test project, group, chat ID, dev-config project block and `[triggers] enabled` are in place; `/usage` in the Antigravity chat answered (Gemini 5-hour group at 0 %). Still owed: one dev-bot restart onto the branch head, then the run. Auth route chosen: Google sign-in.
- **CLIs:** `agy` 1.3.2 on lba-1 when this was written; it updated itself to 1.3.3 on 2026-10-10 (drift suite 10/10 on 1.3.3 that day). `agy` updates itself, so record `agy --version` at the start and end of the run; if it changes mid-run, re-run the drift suite and note both versions.
- **Marker:** `~/.untether-dev/integration-test-pass-0.36.1rc1.json`, not written.

### Before the run

- `test-projects/test-antigravity/` exists as a git repo with an `AGENTS.md` and a `hello.py`, and **no** `.agents/` folder.
- The "ut-dev · Antigravity" group exists (Nathan + the dev bot), its ID is filled in under "Test chats" above, and the dev config binds it to the test project with `antigravity` as the engine. The dev bot has been restarted on the code under test from a normal shell, never from inside an Untether session.
- The drift suite is green the same day: `UNTETHER_AGY_DRIFT=1 uv run pytest tests/test_antigravity_cli_schema_drift.py`.
- `/usage` in the Antigravity chat first: pause if the 5-hour Gemini group is past 50 %. The run is about 40 short `agy` turns; record the auth route (API key or OAuth) and the turn count in the attestation notes.
- **Config table name:** the dev config uses the nested `[engines.*]` layout, and once any `[engines.*]` table exists a flat `[antigravity]` table is ignored (see [Engine-specific config tables](config.md#engine-specific-config-tables)). Every TOML step below is therefore written as `[engines.antigravity]`; written flat, AG-5 would run instead of being refused and AG-9 would run signed in.
- AG-12c and AG-12d fire test crons, so `[triggers] enabled` must be on in the dev config (turning it on needs a restart).
- Log tail in a second shell: `journalctl --user -u untether-dev -f -o cat | grep -E "antigravity|runner\.(start|completed)|usage\.token_delta|preamble|subprocess\.(background_wait|liveness_stall)|ERROR|WARNING"`.

### Required for 0.36.1rc1

1. **Tier 7** Q1–Q16 in the Antigravity chat and one other chat. Q8 stays `/planmode show` in the Claude chat; also send `/planmode show` in the Antigravity chat, which answers with the `Use /config → Permission mode instead.` hint (AG-8).
2. **Tier 1** U1–U10 in the Antigravity chat (Workspace mode; see the note under Tier 1) and in the Claude chat. **U1 + U2 + U6 on Codex, OpenCode and Pi**: the preamble is now built from each engine's capabilities, so the text those three receive has changed, and the base runner and stall monitor changed under all of them.
3. **Tier 2** C1, C2 and C4 in the Claude chat. Claude's preamble must be byte-identical: `preamble.applied preamble_len=3338 source=default` on a Claude run, AskUserQuestion buttons still appear (C4) and ExitPlanMode still renders.
4. **Tier 3** T1 (voice, in the Antigravity chat), T6, and T9 as `/antigravity list the files here` sent in the Claude chat.
5. **Tier 4** O1 (`/agent set antigravity` in the Codex chat, a prompt, then `/agent clear`) and O2 (Effort, in the Antigravity chat; see AG-12).
6. **Tier 6** S2 (Antigravity and Claude at the same time), **S2b** (two Antigravity chats or topics bound to the same project, run in parallel: both finish, neither reads the other's conversation), S6, and S8 (a 5,000-character prompt in the Antigravity chat, which goes to `agy` on stdin).
7. **AG-1 … AG-12 and AG-12a–e** below. AG-12a-long is opt-in.
8. **Upgrade path:** start the dev bot with a config that has no `[engines.antigravity]` table: no config error and the chat preferences load. A scratch chat whose stored Antigravity mode is hand-edited to `accept-edits` runs as Workspace, with one `antigravity.permission_mode.unknown` WARNING.
9. **Log sweep:** `journalctl --user -u untether-dev --since "<start>" -o cat | grep -E "WARNING|ERROR"` shows only the lines the rows provoke on purpose: `antigravity.permission_mode.gate_missing` (AG-5), `antigravity.no_project_dir` (AG-6), `antigravity.auth.*` (AG-9), `antigravity.conversation.*` (AG-12b), `antigravity.unattended_full_downgraded` (AG-12c, logged once per cron), `antigravity.workspace_config_present` and `antigravity.workspace_config.unattended_refused` (AG-12d), `antigravity.argv.invalid_value` (AG-12e) and the upgrade-path WARNING. No `subprocess.liveness_stall`, no `subprocess.stderr.hook_error`, no zombies, and `pgrep -fa "agy --input-format"` is empty after the run.
10. `git -C test-projects/test-antigravity status --porcelain` shows only the agent's own edits. Untether writes no `.agents/` folder; AG-12d's hand-written file is deleted after that row.

### AG scenarios

Run in the Antigravity chat unless a row says otherwise, and send exactly the quoted text. `<uuid>` is the conversation ID from the resume line. In `runner.start`'s `args`, a flag that takes a value is always joined to it (`--conversation=<uuid>`, `--effort=high`, `--model=<id>`), so `agy` can never read the value as another flag; the resume line shown in the chat keeps the spaced form `agy --conversation <uuid>`.

| ID | Steps | Expected in Telegram | Expected log signature |
|---|---|---|---|
| AG-1 plain run | `/config` → Permission mode → Clear override. `create a file called hello.txt containing hello world` | The final confirms and the file exists; the footer's last item is `workspace`; the resume line is `` `agy --conversation <uuid>` ``. On a host where `agy` signs in with a Google account, the first run in a chat also carries a one-off notice starting `⚠️ This host signs Antigravity in with a Google account.` and ending `(Shown once in this chat.)`. It does not appear on an API-key or ADC host, unless the `agy -p /config` check failed (a failed check is treated as a Google sign-in) | `antigravity.tos_notice.shown` once per chat on a Google sign-in host; `runner.start engine=antigravity` with args starting `--input-format stream-json --output-format stream-json --print-timeout 0 --disable-slash-commands`, **no prompt text and no `--dangerously-skip-permissions`**; `antigravity.version.probe`; `antigravity.session.started … agy_version=<the installed version>`; `antigravity.run.timing resumed=False`; `runner.completed engine=antigravity ok=True`; `session.summary … last_event_type=result` |
| AG-2 resume | Reply to AG-1's final: `append a second line saying bye to hello.txt` | Same ID in the resume line; the file has 2 lines; the footer tokens are this run's only (no `· thread total`) | Args have `--conversation=<same uuid>`; `antigravity.run.timing resumed=True`; `usage.token_delta engine=antigravity source=ledger runs=2` |
| AG-3 tool denial | ``Run the shell command `git status` and summarise it.`` | A `⚠️ Blocked: shell command (…)` progress row; the final is **not empty** and says Antigravity was blocked from using a shell command, with `/config → Permission mode → Full access` as the way to allow it | `antigravity.denied_actions actions=['command'] … permission_mode=workspace` |
| AG-4 Full access | `/config` → Permission mode → **Full access**; ``Run `python3 hello.py` and tell me the output.``; then Clear override and send `Reply with exactly: OK` | Toast `Permission mode: full access`. The output is shown; the footer's last item is `full access`; after Clear override it is `workspace` again | `config.antigravity_permission_mode.set mode=agf`; args contain `--dangerously-skip-permissions` for the first run only; no `antigravity.denied_actions` |
| AG-5 later modes refused | `/config` → Permission mode → tap **Ask me · soon**, then **Plan first · soon**. Then set `permission_mode = "plan"` under `[engines.antigravity]` in the dev config (hot-reload; if it only applies after a restart, record that as a finding), send `Reply with exactly: OK`, and remove the key | Two toasts, each `Needs Untether's Telegram approval gate — coming in a later 0.36.1 release`, and nothing stored. The run is refused before anything spawns: `Plan first needs Untether's approval gate, which arrives in a later 0.36.1 release — switch to Workspace or Full access in /config.` | `config.antigravity_permission_mode.unavailable` for each tap; `antigravity.permission_mode.gate_missing mode=plan` (WARNING); **no** `runner.start` |
| AG-6 no project | In a chat with no project bound (the DM): `/agent set antigravity`, `Reply with exactly: OK`, then `/agent clear` | `Antigravity needs a project — bind this chat with /ctx set … or a [projects.*] entry. It won't run in the bot's own directory, because it can edit files there.`; no run | `antigravity.no_project_dir` (WARNING, expected); no `runner.start` |
| AG-7 `/usage` | After AG-1: `/usage`, `/usage` again within 60 s, `/usage` a third time after 60 s, then `/usage debug` | Quota per group (5-hour and Weekly windows with reset times) under `📊 Antigravity quota`, then `From agy /usage (no quota spent).` and the last session's tokens; the debug output is escaped and has a `• agy version: …` line | `antigravity.quota.fetched` on the first and third calls only (60 s cache); never during a run |
| AG-8 `/config` labels, Effort, About | `/config` home; the Permission mode page; the Effort page; `/config` → About; `/planmode show`. Then `/model set <a Gemini Pro model id>`, open the Effort page again, and `/model clear` | Home: `Agent controls (Antigravity CLI)` and, with no override and no TOML value, `Permission mode: workspace (default)  · edits files, no shell — not a sandbox` (the source suffix is `(from untether.toml)` when the config sets the mode, and absent with a chat override). The Permission mode page says `Not a sandbox` and never "read-only"; its buttons are Workspace, Full access, `Ask me · soon`, `Plan first · soon`. The Effort page shows `Model: agy default · supports low, medium, high` with those three levels for the default model, and only the levels `agy` reports for a Pro model (Low / High when this was written). If the page says `Couldn't ask agy which levels this model supports — showing the usual three.`, the per-model check is NOT EXERCISED, not a fail. About's versions line includes `antigravity <version>`. `/planmode show`: `Plan mode is only available for Claude Code. … Use /config → Permission mode instead.` | `config.*` lines only; no WARNING |
| AG-9 signed out | Write a wrapper script under `test-projects/.ag-unauth/` that runs the real `agy` with `HOME` pointed at a new empty folder under `/tmp` (nothing under the real `~/.gemini` is touched). Set `cmd = "<that script>"` under `[engines.antigravity]`, send `Reply with exactly: OK`, then `/usage`, then remove `cmd` | An error final within about 10 s: `Antigravity CLI isn't signed in on this host — agy wanted a browser sign-in, which a bot can't complete. …`, ending with Untether's own help link (`https://littlebearapps.com/help/untether/switch-engines/#antigravity-cli`). That help link is expected; what must **not** appear is Google's OAuth sign-in URL or any query-string secret. `/usage` shows the same text within 15 s | `antigravity.auth.required`, then either `antigravity.auth.killed elapsed_ms<5000` or `agy`'s own exit (1.3.2 exits by itself in about 6 s, so the kill is a safety net); `runner.completed ok=False`; no OAuth sign-in URL or query-string secret in any log line |
| AG-10 cancel mid-run | `Write a detailed 1500-word essay about ocean tides, in plain prose.`, then `/cancel` at the first progress edit | A cancelled card, not an "interrupted" error; the next prompt works | The cancelled `runner.completed`; `subprocess.exit` for the `agy` pid; `pgrep -f "agy --input-format"` empty within 5 s |
| AG-11 long-chat resume latency | Six replies in one conversation: `Reply with exactly: t1` … `t6` | Six short finals | `antigravity.run.timing` with `total_ms` and `cumulative_input_tokens` on each turn. Bar: turn 6's `total_ms` is at most twice turn 2's (or at most 15 s) and the tokens only grow. Put the numbers in the attestation notes |
| AG-12 `/continue` + effort | `/config` → Effort → High; then `/continue reply with the word continued`; then clear the Effort override | Runs on this project's latest conversation; the footer ends `high · workspace`; the resume line carries a real conversation ID | Args contain `--effort=high` and a bare `--continue` (and no `--conversation=`); `antigravity.session.started` with a real conversation ID. (`session_lock.acquired` is logged at DEBUG, so it isn't in the journal at the default level) |
| AG-12a background hold, short ([#975](https://github.com/littlebearapps/untether/issues/975)) | `/config` → Permission mode → Full access. ``Start `sleep 90; echo done > bg.txt` in the background, reply STARTED straight away, then tell me when bg.txt exists.`` Wait about 2 minutes, then Clear override | **One** final after about 90 s, carrying both replies; `bg.txt` exists; no stall warning and no cancel in the chat while it waits. If `agy` prints anything during the wait, the row is retitled `⏳ background: …` | **No** `subprocess.liveness_stall`, no `progress_edits.stall_detected`, no `progress_edits.stall_auto_cancel`; one `runner.completed ok=True`. `subprocess.background_wait` is **not** expected here (see the note below) |
| AG-12a-long background hold, 10 minutes (opt-in) | As AG-12a with `sleep 700` in place of `sleep 90`. Wait about 12 minutes | One final after about 700 s; **no** stall warning at the 10-minute mark, where a plain running tool would get one | One `subprocess.background_wait … idle_seconds≥600 background=1`; no `subprocess.liveness_stall`; no `progress_edits.stall_detected`; one `runner.completed ok=True`. If `agy` printed during the wait, idle never reached 600 s: record the row as NOT EXERCISED |
| AG-12b stale resume | Send a two-line message: `` `agy --conversation 00000000-1111-2222-3333-444444444444` `` on the first line and `Reply OK` on the second | `That Antigravity conversation no longer exists (antigravity conversation not found), so it wasn't resumed — send your message again to start a new one.` (with the `ℹ️ The saved session couldn't be resumed, so it was cleared…` notice when it was the chat's saved session); the next message starts a new conversation | Args have `--conversation=00000000-1111-2222-3333-444444444444`; `antigravity.conversation.missing detected_by=stderr` (WARNING); no `runner.started.error`. `detected_by=init` (the error arrives after `agy`'s turn ends) is still a pass: note it |
| AG-12c unattended Full access | With the chat on Full access, add a dev cron for the Antigravity project **without** `permission_mode`, prompt ``Run `ls` and list the files``, due in the next minute. Then add `permission_mode = "full"` to the cron and let it fire again. Remove the cron and Clear override | First run: a `⚠️ Blocked` row and advice that names the cron (``set `permission_mode = "full"` on this cron``). Second run: the listing | First: `antigravity.unattended_full_downgraded trigger=cron:<cron id> origin=…` (WARNING, once per cron per process) and args without `--dangerously-skip-permissions`. Second: args with it |
| AG-12d planted config | By hand, add `test-projects/test-antigravity/.agents/hooks.json` with one named group holding a `Stop` hook that runs `true`. Send `Reply with exactly: OK` twice. Edit the hook command to `true # v2` and fire a 1-minute test cron for the project. Send `Reply with exactly: OK` in the chat, fire the cron again, then delete the file | First message: `⚠️ This project has agy hooks, plugins or MCP servers that Untether doesn't manage (.agents/hooks.json)…`. Second: no such row. First cron: refused with `agy's workspace config changed since someone last ran Antigravity in this chat's project…`. The chat message shows the ⚠️ row again, and the second cron then runs | `antigravity.workspace_config_present digest=…` twice, with two different digests; `antigravity.workspace_config.unattended_refused` for the first cron, with no `runner.start` for it |
| AG-12e invalid model id | `/model set --dangerously-skip-permissions`, then `Reply with exactly: OK`. Then `/model set gemini$flash` (a character outside the allowed set) and the same prompt. Then `/model clear` and the prompt once more | Both runs are refused before anything spawns: `🛑 That model id isn't valid for Antigravity, so nothing was started. A model id uses letters, digits and . _ - : / only, with no spaces and no leading dash (for example gemini-3.8-flash). Set one with /model set, or go back to the default with /model clear.` After `/model clear` the run works and its footer's last item is `workspace`, never `full access` | `antigravity.argv.invalid_value field=model reason=… preview=…` (WARNING; the preview never shows the raw value) for each refusal; **no** `runner.start` and no `--dangerously-skip-permissions` in any args. The refusal is recorded as `prespawn_blocked=invalid_argument`, so the chat's saved session is kept |

**AG-12a and the real thresholds.** The plan's AG-12a waited 90 s and expected both `subprocess.background_wait` in the log and a `⏳ Antigravity is waiting for 1 background task…` line in the chat. Neither can appear that early:

- The liveness watchdog logs `subprocess.background_wait` only after 600 s with no output from `agy` (`_LIVENESS_TIMEOUT_SECONDS`), and at most once every 1800 s after that.
- The stall monitor's chat line uses the 1800 s expected-wait threshold, checked once a minute, while Untether treats the hold as expected only for 1860 s (`agy`'s own 30-minute cap plus 60 s). That leaves a window of at most 60 s, so the chat line can't be relied on in a live run. It is covered by unit tests only (`test_stall_monitor_background_waiting_is_expected_wait`, `test_background_held_result_is_expected_wait`, `test_background_wait_expires_after_cap`).

So the row is split. AG-12a (required) proves the user-visible half in 2 minutes: one final with both replies and nothing stall-like. AG-12a-long (opt-in, about 12 minutes of mostly idle time) is the only live check of the `subprocess.background_wait` path.

**Unit-test only (can't be triggered on demand):** the chat line above; an unattended run held because the project's `agy` config could not be checked (a scan timeout or failure: `antigravity.workspace_config.unattended_refused reason=…`); the version guard's refusal of an `agy` older than 1.3.1; and the quota, credits and account-block error cards.

### Results

| ID | Engine / chat | Result | Note |
|---|---|---|---|
| Q1–Q16 | Antigravity + one other | NOT RUN | |
| U1–U10 | Antigravity | NOT RUN | |
| U1–U10 | Claude | NOT RUN | |
| U1, U2, U6 | Codex | NOT RUN | |
| U1, U2, U6 | OpenCode | NOT RUN | |
| U1, U2, U6 | Pi | NOT RUN | |
| C1, C2, C4 (+ `preamble_len=3338`) | Claude | NOT RUN | |
| T1, T6, T9 (`/antigravity`) | Antigravity / Claude | NOT RUN | |
| O1, O2 | Codex / Antigravity | NOT RUN | |
| S2, S2b, S6, S8 | Antigravity (+ Claude) | NOT RUN | |
| AG-1 | Antigravity | NOT RUN | |
| AG-2 | Antigravity | NOT RUN | |
| AG-3 | Antigravity | NOT RUN | |
| AG-4 | Antigravity | NOT RUN | |
| AG-5 | Antigravity | NOT RUN | |
| AG-6 | DM | NOT RUN | |
| AG-7 | Antigravity | NOT RUN | |
| AG-8 | Antigravity | NOT RUN | |
| AG-9 | Antigravity | NOT RUN | |
| AG-10 | Antigravity | NOT RUN | |
| AG-11 | Antigravity | NOT RUN | |
| AG-12 | Antigravity | NOT RUN | |
| AG-12a | Antigravity | NOT RUN | |
| AG-12a-long (opt-in) | Antigravity | NOT RUN | |
| AG-12b | Antigravity | NOT RUN | |
| AG-12c | Antigravity (cron) | NOT RUN | |
| AG-12d | Antigravity (chat + cron) | NOT RUN | |
| AG-12e | Antigravity | NOT RUN | |
| Upgrade path | dev | NOT RUN | |
| Logs | dev | NOT RUN | |
