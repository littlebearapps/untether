# Untether

Telegram bridge for Claude Code, Codex, OpenCode, Pi, and other agent CLIs (Gemini CLI and Amp are deprecated — see below). Control your coding agents from anywhere — walking the dog, watching footy, at a friend's place.

**Repo**: [littlebearapps/untether](https://github.com/littlebearapps/untether)
**Based on**: [banteg/takopi](https://github.com/banteg/takopi) (upstream)

Untether adds interactive permission control, plan mode support, and several UX improvements on top of upstream takopi. All interactive features are Claude Code-specific; Codex, OpenCode, and other engines use standard non-interactive mode.

## Features (vs upstream takopi)

- **Interactive permission control** — bidirectional Telegram buttons for tool approval, plan mode, and clarifying questions
- **Pause & Outline Plan** — third button on plan approval; the outline (chat text, or the ExitPlanMode `plan` input on plan-file CLIs, #659) is posted with Approve/Deny/Let's discuss buttons (hold-open keeps session alive while user reads); the v2.1.72-74-era progressive cooldown was retired in #570 (upstream retry loop fixed, verified on CLI 2.1.215)
- **Agent context preamble** — configurable prompt preamble tells agents they're on Telegram and requests structured end-of-task summaries; `[preamble]` config section
- **`/planmode`** — toggle permission mode per chat (on/plan-auto/auto/off). `auto` is Claude Code's own classifier-gated auto mode; `plan-auto` is Untether's plan-gate sugar, renamed from `auto` in 0.35.5rc8 because it shadowed the CLI's mode (#741). Full mode table in `docs/reference/runners/claude/runner.md` → "Permission modes"
- **Permission modes that prompt, actually prompt** (#749, 0.35.5rc9) — `default`, `manual` and `acceptEdits` previously behaved like `bypassPermissions`: `--allowedTools Bash,Read,Edit,Write` pre-approved them at the CLI's stage 5, and the control handler blanket-approved whatever still reached stage 6. Both halves are now mode-aware — the allowlist is not sent, and every tool routes to a Telegram approval. Autonomous modes (`plan`, `plan-auto`, `auto`, `dontAsk`, `bypassPermissions`) are deliberately unchanged: gating them would raise a button per `Glob`/`Grep` in plan mode for no safety gain. `is_claude_prompting_mode()` in `runners/run_options.py` is the single classification point
- **`/listen`** — set listen mode (`all` / `mentions`) per chat or topic; controls when the bot responds in groups; renamed from `/trigger` in v0.35.3 (#297) to disambiguate from webhook/cron triggers — `/trigger` still works as a deprecated alias for one release cycle
- **Ask mode** — interactive AskUserQuestion with option buttons, sequential multi-question flows, and `/config` toggle; Claude-only
- **Early callback answering** — clears button spinners immediately instead of waiting for processing
- **Approval push notifications** — separate notify message when approval buttons appear
- **Ephemeral message cleanup** — approval-related messages auto-delete when run finishes
- **Bold formatting** — command responses use HTML bold for key values
- **`/usage`** — shows API usage and cost for the current session
- **`/export`** — exports session transcript as markdown or JSON
- **`/browse`** — navigate project files via inline keyboard buttons
- **Cost tracking and budget** — per-run and daily cost limits with configurable alerts
- **Subscription usage footer** — configurable `[footer]` to show 5h/weekly subscription usage instead of/alongside API costs
- **Graceful restart** — `/restart` command drains active runs before restarting; SIGTERM also triggers graceful drain
- **Compact startup message** — version number, conditional diagnostics (only shows mode/topics/triggers/engines when they carry signal), project count instead of full list
- **Workflow mode indicator** — startup message shows `mode: assistant`, `mode: workspace`, or `mode: handoff`; derived from `session_mode` + `topics.enabled`
- **Model/mode footer** — final messages show model name + permission mode (e.g. `🏷 sonnet · plan`) from `StartedEvent.meta`; all engines populate model info
- **`/verbose`** — toggle verbose progress mode per chat; shows tool details (file paths, commands, patterns) in progress messages
- **`/config`** — inline settings menu with navigable sub-pages; toggle plan mode, ask mode, verbose, engine, trigger via buttons
- **`[progress]` config** — global verbosity and max_actions settings in `untether.toml`
- **Pi context compaction** — `AutoCompactionStart`/`AutoCompactionEnd` events rendered as progress actions
- **Stall diagnostics & liveness watchdog** — `/proc` process diagnostics (CPU, RSS, TCP, FDs), progressive stall warnings with Telegram notifications, liveness watchdog for alive-but-silent subprocesses, stall auto-cancel (dead process, no-PID zombie, absolute cap) with CPU-active suppression (sleeping-process aware — shows tool name when main process waiting on child), tool-active repeat suppression (first warning fires, repeats suppressed while child CPU-active), MCP tool-aware threshold (15 min for network-bound MCP calls vs 10 min for local tools) with contextual "MCP tool running: {server}" messaging, `session.summary` structured log; `rate_limit_event` models the CLI's real quota snapshot (#790) — only `status=rejected` (not covered by overage) latches a throttle, until `resetsAt`; `allowed` heartbeats and bare events no longer fake a 60s wait (retires the #657 guess); `system/api_retry` back-offs render as `🔁 API error … retrying in Ns` and count as an expected wait (#792); live-idle holds never raise stall warnings and are reported as `peak_live_idle_seconds`, not `peak_idle_seconds` (#787), counting only idle gaps between turns (#811); `[watchdog]` config section with configurable `tool_timeout`, `mcp_tool_timeout`, and `stream_idle_auto_retry`/`stream_idle_max_retries` (#572 — bounded auto-resume of Type-A mid-generation API stalls, default off; Type-B never retries), `live_sessions`, `hold_for_async_hooks`/`async_hook_max_hold` (#812)
- **Auto-continue** — detects Claude Code sessions that exit after receiving tool results without processing them (upstream bugs #34142, #30333) and auto-resumes; suppressed on signal deaths (rc=143/SIGTERM, rc=137/SIGKILL) to prevent death spirals under memory pressure; configurable via `[auto_continue]` with `enabled` (default true) and `max_retries` (default 1)
- **Live sessions** (#776, 0.35.5rc11) — a Claude process stays live after its reply while background work runs. The runner keeps reading past `result`, tracks background work from the CLI's own `system/task_*` events (native task map; #374/#646/#662), and emits each later turn (background task finished / Monitor tick / ScheduleWakeup / injected follow-up) as a `TurnEvent` segment that the bridge delivers as its own Telegram message (`🔔 Background task finished — …`, `📡 Monitor — …`, `⏰ Scheduled wake-up`, `🪝 Hook feedback — …`; `TurnReason` = `task_finished`/`monitor_event`/`scheduled_wakeup`/`followup`/`hook_rewake`/`unknown`; approvals work inside wake turns). Follow-ups to a live session are written into its stdin (queue semantics, attributed via `command_lifecycle.command_uuid`) instead of `--resume` (#647). The lifecycle closes stdin gracefully when idle (60 s), after the background hold (1800 s, re-armed per turn) or at the 4 h cap; `/cancel`, `/new` and drain close idle sessions the same way with a notice; `/cancel` of an in-flight follow-up or wake turn renders it `cancelled`, not as an error (#806). A resume guard absorbs the CLI's 0-turn "stopped task" result on resume. Each run binds its **own** stream/PID via a per-run `RunStreamHandle` ContextVar — the bridge never reads the shared `runner.current_stream`/`last_pid` (#510, cross-chat stall/summary reads). An idle close that overruns its grace logs `claude.live_session.close_grace_expired` with a proc snapshot, sends SIGINT then SIGTERM, and is not quarantined when it was a clean idle close (#791). Wake turns are attributed only to top-level background tasks, retro-attributed when a task ends during an `unknown` turn, and a second turn for the same finish arrives silently (#785). A resumed background agent (same `task_id`, fresh `task_started`) is revived rather than left terminal, and a subagent's own background task that outlives its agent (`ClaudeTask.holds_session`) still holds the session open, so idle close never kills promised work (#801). Every turn final — follow-up, queued→injected, wake — carries `✓ turn complete` (#798), and a wake turn replies to the prompt whose turn launched its task (#795). Kill switch `[watchdog] live_sessions = false`. Claude control-channel mode only; see `docs/reference/runners/claude/runner.md` → "Live sessions"
- **Background-task status** (#777, 0.35.5rc13) — renders the live-session task map (`src/untether/background_status.py`, never re-parses events): a `⏳ background (N)` block in the progress message (`🤖 <desc> · <elapsed> · <tokens> tok · <tools> tools · <step>` / `🐚 <desc> · <elapsed>`) and, after the answer, one silent status message edited in place (≥30 s throttle, earlier on a completion) with `✅`/`❌`/`⏹️` rows, finalised when the set empties or the session closes (reason shown). Lists what holds the session (`live_shown()` — incl. a subagent's orphaned bg task once its agent ended). `/ping` shows `⏳ background: N tasks running`. `[progress] show_background_tasks` (default true), `background_tasks_max_rows` (default 5, `+N more`), hot-reloaded
- **Wake-ack consolidation** (#785 part 2) — short, tool-free wake turns (`task_finished`/`unknown`/`scheduled_wakeup`/`monitor_event`, ≤300 chars) fold into the status message as `↳ <ack>` lines instead of a new pushed message; a turn with tools, approvals, substantive text or the last task's report breaks out, and the first breakout of a batch always pushes; a batch where everything folded still pushes one `✅ all N background tasks done`. Up to 3 read-only collection calls (`Read`/`Glob`/`Grep`, `COLLECTION_TOOLS`) don't break a fold, and an unattributed ack is filed only under the task paired with its turn (#813). `[progress] consolidate_wake_turns = true`; off = rc12 per-turn delivery
- **Async-hook hold** (#812, 0.35.5rc14) — a live session stays open while a background command hook (`async`/`asyncRewake`, e.g. security-guidance's commit review) is still running, because the CLI drops an `asyncRewake` rewake once stdin closes. `--include-hook-events` is passed when a cached `claude --help` probe lists it (`claude.hook_events.probe`; not a reserved flag, deduped against `extra_args`); `system/hook_*` frames pair by `hook_id` (no UntetherEvents, not `last_event_type`). `has_pending_async_hooks()` is a sibling hold, never a background task (no footer/panel rows). Plain `async` hooks: the CLI withholds their `hook_response` until the next turn or stdin close, so Untether holds every unpaired hook while any hook process lives and releases them all once none has for 1 s (`claude.hook.hold_released reason=no_hook_process`; all-or-nothing — a per-hook PID binding lost a live rewake). A hook process is any CLI child except Bash-tool shells, children older than the oldest unpaired hook's `hook_started` by > 5 s (the CLI emits the frame, then spawns), and children in the CLI's own process group that are in the `system/init` baseline (pid + start time — MCP servers) or have MCP/LSP-looking argv (`proc_diag.hook_evidence_children()`); the CLI spawns hooks `detached`, so a hook is never exempt by baseline or name — not just `sh -c`, because bash/zsh (macOS) exec a single hook command. Children fall back to a /proc ppid scan without `task/*/children`; a forking wrapper is resolved to the CLI. A rewake arrives as a pushed `🪝 Hook feedback — <event>` turn (`hook_rewake`, never folded). Bound `[watchdog] async_hook_max_hold` (630 s, 0–3600) from the newest unpaired hook → one `claude.hook.hold_expired` per hold, then `claude.live_session.async_hook_killed` and `⏳ Closing session — a background hook (Stop or UserPromptSubmit) was still running; its feedback wasn't delivered.` — counts are live hook processes, never unpaired candidates. Hook close grace 35 s (also on a live hook process alone); `session.summary hooks_started`. Kill switch `[watchdog] hold_for_async_hooks = false`
- **Safeguard stops** (#814, 0.35.5rc14) — when Anthropic's safeguards stop a Claude response (assistant `stop_reason: "refusal"`, the `system/informational` "continuing once" notice, `model_refusal_fallback`/`_no_fallback`), a per-turn `🛡️ <model> safeguards stopped a response · retried once|switched to <model>|not retried` row, a `🛡️ safeguards stopped N response(s)` footer line (via `usage["safeguard"]`, so live turns get it too) with a one-time-per-session hint link (cyber safeguards article for category `cyber`, else the model-fallback docs), and `claude.safeguard_stop`. Never an error; a not-retried stop with no answer gets an explanatory body; stopped wake turns never fold. `system/model_fallback` renders a `↪️ Switched model` row (`claude.model_fallback`); other warning/notice `informational` banners get a generic row, info/suggestion log only
- **Steer follow-ups** (#775, 0.35.5rc13) — per-chat/topic `followup_mode` (`queue` default | `steer`), resolved topic → chat → `[transports.telegram] followup_mode` → `queue` (hot-reload; kept out of `EngineOverrides`). In steer mode a plain-text/voice message to a live Claude session is written straight into stdin (`steer_into_session`, under `LiveSession.lock` with a steer-window race guard closed on `/cancel`/close): mid-tool it folds into the running turn (`↪️ steer received` progress row, `↪️ Steered into the current run.` ack), after the last tool it runs as its own follow-up turn replying to the steer message; an idle live session gets no ack. `/steer <text>` / `/queue <text>` override once; bare `/steer` / `/queue` set the default; `/config` → `↪️ Follow-up` page. Files, media groups, forwards, commands and a pending AskUserQuestion always win/queue; non-Claude engines and no-live-run fall back with a notice. Helper `telegram/steer.py`; prefs `telegram/followup_mode.py`
- **Per-session cost deltas** (#778) — Claude's `total_cost_usd` is cumulative per session (also across `--resume`); `session_costs.json` records the last total per session so cost footer, budget, `cost.run_outlier` and daily totals use the per-run/per-turn delta
- **Empty-resume recovery (quarantine-and-fresh)** (#631, #632) — a resume that returns 0 turns/$0 (upstream dangling-tool_use defect) quarantines the session in `session_quarantine.json` and auto-resends the message once on a fresh session; sessions SIGTERM'd after a result (`forced_teardown_after_result`) are quarantined proactively so the next message diverts to a fresh session before any empty result is seen; `[auto_continue]` flags `empty_resume_fresh` and `quarantine_on_forced_teardown` (both default true); structured events `runner.empty_result` → `session.quarantined` → `session.auto_resend_fresh`/`session.resume_diverted_fresh`; Claude runner only
- **MCP catalog observability + proactive refresh** (#365) — `catalog_staleness.detected` structlog WARNING once per `(session, server, status)` tuple when Claude's `system.init` reports a non-`connected` MCP status (`detect_catalog_staleness`, default **on**); opt-in fire-and-forget `mcp_status` control_request after each `tool_result` to nudge Claude Code's catalog (`notify_catalog_refresh`, default **off**). Request IDs use the `ut_catalog_refresh_<session_id>_<seq>` namespace; drained via `ClaudeRunner._drain_catalog_refresh`. Logs `catalog.refresh_sent` INFO / `catalog.refresh_failed` WARN/ERROR. Claude runner only
- **File upload deduplication** — auto-appends `_1`, `_2`, … when target file exists, instead of requiring `--force`; media groups without captions auto-save to `incoming/`
- **Agent-initiated file delivery (outbox)** — agents write files to `.untether-outbox/` during a run; Untether sends them as Telegram documents on completion with `📎` captions; deny-glob security, size limits, file count cap, auto-cleanup; `[transports.telegram.files]` config
- **Progress persistence** — active progress messages persisted to `active_progress.json`; on restart, orphan messages edited to "⚠️ interrupted by restart" with keyboard removed; every exit path (cancel, error, recovery re-run) releases its entry, so a cancelled or failed message is never relabelled (#810)
- **Resume line formatting** — visual separation with blank line and ↩️ prefix in final message footer
- **`/continue`** — cross-environment resume; pick up the most recent CLI session from Telegram using each engine's native continue flag (`--continue`, `resume --last`, `--resume latest`); supported for Claude, Codex, OpenCode, Pi (Gemini deprecated; not AMP); a `/continue` run releases its session registries when it ends, so a later resume of that session no longer waits 30 s and diverts to a fresh session (#816)
- **Timezone-aware cron triggers** — per-cron `timezone` or global `default_timezone` with IANA names (e.g. `Australia/Melbourne`); DST-aware via `zoneinfo`; invalid names rejected at config parse time
- **Hot-reload trigger configuration** — editing `untether.toml` applies cron/webhook changes immediately without restart; `TriggerManager` holds mutable state that the cron scheduler and webhook server reference at runtime; `handle_reload()` re-parses `[triggers]` on config file change
- **Hot-reload Telegram bridge settings** — `voice_transcription` (incl. the #638 `voice_transcription_language` ISO-639-1 hint and the #691/#703 `voice_transcription_prompt` vocabulary bias, which ships a product-generic default), file transfer, `allowed_user_ids`, timing, and `show_resume_line` settings reload without restart; `TelegramBridgeConfig` unfrozen (slots kept) with `update_from()` wired into `handle_reload()`; restart-only keys (`bot_token`, `chat_id`, `session_mode`, `topics`, `message_overflow`) still warn; a refused voice `base_url` is re-checked at startup and on a voice-key reload (`voice.base_url.not_permitted`, log-only, #679)
- **`/at` command** — one-shot delayed runs: `/at 30m <prompt>` schedules a prompt to run in 60s–24h; `/cancel` drops pending delays before firing; lost on restart (documented) with a per-chat cap of 20 pending delays; `telegram/at_scheduler.py` holds task-group + run_job refs
- **`run_once` cron flag** — `[[triggers.crons]]` entries can set `run_once = true` to fire once then auto-disable; cron stays in TOML and re-activates on config reload or restart; the startup message counts only scheduled crons and shows spent one-shots separately (#809)
- **Trigger visibility (Tier 1)** — `/ping` shows per-chat trigger summary (`⏰ triggers: 1 cron (id, 9:00 AM daily (Melbourne))`); run footer shows `⏰ cron:<id>` / `⚡ webhook:<id>` for trigger-initiated runs; new `describe_cron()` utility renders common patterns in plain English
- **Graceful restart improvements (Tier 1)** — persists Telegram `update_id` to `last_update_id.json` so restarts don't drop/duplicate messages; `Type=notify` systemd integration via stdlib `sd_notify` (`READY=1` + `STOPPING=1`); `RestartSec=2`
- **`diff_preview` plan bypass (#283)** — after user approves a plan outline via "Pause & Outline Plan", the `_discuss_approved` flag short-circuits diff preview for subsequent Edit/Write tools so no second approval is needed
- **User-extensible env allowlist (#409)** — `[security] env_extra_allow` and `env_extra_prefix_allow` (in `untether.toml`) extend the engine-subprocess env allowlist with per-deployment names so users can thread credential-manager tokens (1Password, Doppler, Vault, Infisical, …) without forking `utils/env_policy.py`. Names are validated against `[A-Z_][A-Z0-9_]*`. Honoured by the Claude and Pi runners and by the `env_audit` probe. `BWS_ACCESS_TOKEN` was promoted into the built-in defaults at the same time. One `env_policy.user_extension` INFO log per process
- **Master trigger pause toggle (#294)** — `TriggerManager.pause()` / `resume()` / `is_paused` gate cron firing and webhook dispatch globally; webhook server returns `503 triggers paused` (with `Retry-After: 60`); `/health` endpoint reflects paused state. Wired into `/config` two ways: home-page button row (only when triggers configured) and a dedicated `📡 Triggers` page (`config:tg`) showing counts + Pause/Resume button. `/ping` switches to `⏸ triggers paused: … (suspended)` while paused. Pause is in-memory only — restart auto-resumes (safe default)

See `.claude/skills/claude-stream-json/` and `.claude/rules/control-channel.md` for implementation details.

## Deprecated engines (Gemini CLI, AMP)

Both are **deprecated** and targeted for **removal in 0.36.0**. They still load and
run; they are not supported.

- **`gemini`** — Google ended Gemini CLI support for individual and free accounts on
  **2026-06-18**, directing users to Antigravity CLI. On those accounts it fails with
  `IneligibleTierError: This client is no longer supported` and exits **1**. Under
  Untether the subprocess **hangs instead of exiting**, so the run stalls to the
  watchdog auto-cancel (~10 min) rather than erroring — known defect, not being fixed.
  Enterprise / Google Cloud licences may still work, unverified.
- **`amp`** — integration unmaintained. AMP remotely refuses out-of-date clients
  (`426 This version of Amp is no longer supported`) and exits **1**, so it fails fast.
  `amp threads list` is local and does NOT hit the version gate, so `/threads` can keep
  working while `amp -x` is refused. Decision is about our integration, not AMP itself.
  The AMP-only `/threads` command is deprecated alongside it.

**Working rule:** when a cross-engine sweep breaks either runner, `xfail`/`skip` the
test — do NOT fix the runner. Security fixes still apply. Both are excluded from every
integration-test tier. Full rule in `.claude/rules/runner-development.md` → "Deprecated
engines — sweep exemption".

Antigravity CLI (#558) is a **new engine**, not a `gemini` rename — it must not reuse
the `gemini` engine id.

## Architecture

```
Telegram <-> TelegramPresenter <-> RunnerBridge <-> Runner (claude/codex/opencode/pi/gemini/amp)
                                       |
                                  ProgressTracker
```

- **Runners** (`src/untether/runners/`) — engine-specific subprocess managers
- **RunnerBridge** (`src/untether/runner_bridge.py`) — connects runners to Telegram presenter, manages `ProgressEdits`
- **TelegramPresenter** (`src/untether/telegram/bridge.py`) — renders progress, inline keyboards, and answers
- **Commands** (`src/untether/telegram/commands/`) — command/callback handlers

## Key files

| File | Purpose |
|------|---------|
| `runners/claude.py` | Claude Code runner, interactive features |
| `runners/gemini.py` | Gemini CLI runner (⚠️ deprecated) |
| `runners/amp.py` | AMP CLI runner (Sourcegraph) (⚠️ deprecated) |
| `runner_bridge.py` | Connects runners to Telegram presenter, injects agent preamble, auto-continue with signal death suppression, empty-resume quarantine-and-fresh recovery |
| `session_costs.py` | Per-session cumulative-cost ledger (`session_costs.json`) for per-run/per-turn cost deltas (#778) |
| `live_followup.py` | Follow-up injection into a live Claude session (scheduler `inject_job` hook, #776) |
| `session_quarantine.py` | Persistent QuarantineStore (`session_quarantine.json`): poisoned-session markers, forced-teardown quarantine, resume divert (#631/#632) |
| `cost_tracker.py` | Per-run/daily cost tracking and budget alerts |
| `commands/claude_control.py` | Approve/Deny/Discuss callback handler |
| `commands/dispatch.py` | Callback dispatch and command routing |
| `markdown.py` | Progress/final message formatting, meta_line footer |
| `commands/planmode.py` | `/planmode` toggle command |
| `commands/usage.py` | `/usage` command |
| `commands/export.py` | `/export` command |
| `commands/browse.py` | `/browse` file browser |
| `commands/restart.py` | `/restart` graceful restart command |
| `commands/verbose.py` | `/verbose` toggle command |
| `commands/config.py` | `/config` inline settings menu |
| `commands/ask_question.py` | AskUserQuestion option button handler |
| `commands/topics.py` | `/new`, `/ctx`, `/topic` commands; `_cancel_chat_tasks()` helper |
| `commands/listen.py` | `/listen` command (listen-mode toggle); `/trigger` deprecated alias (#297) |
| `listen_mode.py` | `resolve_listen_mode()` and `should_trigger_run()` for response gating |
| `utils/proc_diag.py` | `/proc` process diagnostics for stall analysis (CPU, RSS, TCP, FDs, children) |
| `shutdown.py` | Graceful shutdown state and drain logic |
| `telegram/bridge.py` | Telegram message rendering |
| `telegram/loop.py` | Telegram update loop, signal handlers, drain-then-exit |
| `telegram/files.py` | File upload helpers, deduplication, deny globs, atomic writes |
| `telegram/outbox_delivery.py` | Agent-initiated file delivery: scan, send, cleanup outbox files |
| `commands.py` | Command result types |
| `scripts/validate_release.py` | Release validation (changelog format, issue links, version match) |
| `scripts/healthcheck.sh` | Post-deploy health check (systemd, version, logs, Bot API) |
| `triggers/manager.py` | TriggerManager: mutable cron/webhook holder for hot-reload; atomic config swap on TOML change; `crons_for_chat`, `webhooks_for_chat`, `remove_cron` helpers |
| `triggers/describe.py` | `describe_cron(schedule, timezone)` utility for human-friendly cron rendering |
| `telegram/at_scheduler.py` | `/at` command state: pending one-shot delays with cancel scopes, install/uninstall, cancel per chat |
| `telegram/commands/at.py` | `/at` command backend — parses Ns/Nm/Nh, schedules delayed run |
| `telegram/offset_persistence.py` | Persist Telegram `update_id` across restarts; `DebouncedOffsetWriter` |
| `sdnotify.py` | Stdlib `sd_notify` client for `READY=1`/`STOPPING=1` systemd signals |
| `triggers/server.py` | Webhook HTTP server (aiohttp); multipart parsing from cached body, fire-and-forget dispatch |
| `triggers/dispatcher.py` | Routes webhooks/crons to `run_job()` or non-agent action handlers |
| `triggers/cron.py` | Cron expression parser, timezone-aware scheduler loop |
| `triggers/actions.py` | Non-agent webhook actions: file_write (multipart short-circuit), http_forward, notify_only |
| `triggers/fetch.py` | Cron data-fetch: HTTP GET/POST, file read, response parsing, prompt building |
| `triggers/rate_limit.py` | Token-bucket rate limiter (per-webhook + global) |
| `triggers/ssrf.py` | SSRF protection for outbound HTTP requests (IP blocking, DNS validation, URL scheme check) |
| `triggers/auth.py` | Bearer token and HMAC-SHA256/SHA1 webhook auth verification |
| `triggers/settings.py` | CronConfig/WebhookConfig/CronFetchConfig/TriggersSettings models, timezone validation |
| `cliff.toml` | git-cliff config for changelog drafting |

## Reference docs

Detailed protocol specs and event cheatsheets for each integration:

| Doc | Path | Covers |
|-----|------|--------|
| Claude runner spec | `docs/reference/runners/claude/runner.md` | CLI invocation, stream-json protocol, control channel, permission modes |
| Claude stream-json | `docs/reference/runners/claude/stream-json-cheatsheet.md` | JSONL event shapes (`system`, `assistant`, `user`, `result`) with examples |
| Claude event mapping | `docs/reference/runners/claude/untether-events.md` | Claude JSONL → Untether event translation rules |
| Codex exec-json | `docs/reference/runners/codex/exec-json-cheatsheet.md` | Thread/item/turn JSONL event shapes with examples |
| Codex event mapping | `docs/reference/runners/codex/untether-events.md` | Codex JSONL → Untether event translation rules |
| OpenCode runner spec | `docs/reference/runners/opencode/runner.md` | CLI invocation, step-based event model, session IDs |
| OpenCode stream-json | `docs/reference/runners/opencode/stream-json-cheatsheet.md` | JSONL event shapes (`StepStart`, `ToolUse`, `Text`, `StepFinish`) |
| OpenCode event mapping | `docs/reference/runners/opencode/untether-events.md` | OpenCode JSONL → Untether event translation rules |
| Pi runner spec | `docs/reference/runners/pi/runner.md` | CLI invocation, file-based sessions, provider/model selection |
| Pi stream-json | `docs/reference/runners/pi/stream-json-cheatsheet.md` | JSONL event shapes (`SessionHeader`, `AgentStart`, `ToolExecution`) |
| Pi event mapping | `docs/reference/runners/pi/untether-events.md` | Pi JSONL → Untether event translation rules |
| Gemini runner spec | `docs/reference/runners/gemini/runner.md` | CLI invocation, stream-json, model selection |
| Gemini stream-json | `docs/reference/runners/gemini/stream-json-cheatsheet.md` | JSONL event shapes (`init`, `message`, `tool_use`, `tool_result`, `result`, `error`) |
| Gemini event mapping | `docs/reference/runners/gemini/untether-events.md` | Gemini JSONL → Untether event translation rules |
| AMP runner spec | `docs/reference/runners/amp/runner.md` | CLI invocation, stream-json, mode/model selection |
| AMP stream-json | `docs/reference/runners/amp/stream-json-cheatsheet.md` | JSONL event shapes (`system`, `assistant`, `user`, `result`) |
| AMP event mapping | `docs/reference/runners/amp/untether-events.md` | AMP JSONL → Untether event translation rules |
| Telegram transport | `docs/reference/transports/telegram.md` | Bot API client, outbox/rate-limiting, voice transcription, forum topics |
| Workflow modes | `docs/reference/modes.md` | Assistant, workspace, handoff — settings, commands, mode-agnostic features |

## Skills (project-scoped)

Domain-specific Claude Code skills for working on Untether:

| Skill | Path | Use when |
|-------|------|----------|
| Telegram Bot API | `.claude/skills/telegram-bot-api/` | Working on Telegram transport, inline keyboards, outbox, rate limiting, voice, topics |
| JSONL Subprocess Runner | `.claude/skills/jsonl-subprocess-runner/` | Working on runner base class, event translation, session locking, adding engines |
| Claude stream-json | `.claude/skills/claude-stream-json/` | Working on Claude runner, control channel, permission modes, auto-approve, outline gate |
| Codex/OpenCode/Pi | `.claude/skills/codex-opencode-pi/` | Working on non-Claude runners, comparing engine protocols |
| Untether Architecture | `.claude/skills/untether-architecture/` | Understanding overall data flow, config system, progress tracking, project system |
| Release Coordination | `.claude/skills/release-coordination/` | Preparing releases, version bumps, changelog drafting, issue audits, rollback procedures |

## Hooks (project-scoped)

Project hooks in `.claude/hooks.json` fire automatically:

| Hook | Trigger | What it does |
|------|---------|-------------|
| release-guard | Bash: `git push`, `git tag`, `gh pr merge`, `gh release` | Blocks pushes to master/main, tag creation, PR merging, releases; allows feature and dev branch pushes |
| release-guard-protect | Edit/Write to guard scripts, `hooks.json`, or `help-faq-protect.sh` | Prevents modification of release guard infrastructure and the FAQ-protect hook |
| release-guard-mcp | GitHub MCP write tools | Blocks `merge_pull_request` and writes to master/main; allows feature branches |
| help-faq-protect | Bash: `rm`, `git rm`, `mv`, `>` redirect targeting `docs/faq/faq.md` | Blocks deletion / move / truncate of the help-centre FAQ; edits via Edit/Write/append `>>` are allowed (#477, #483) |
| dev-workflow-guard | `systemctl` with `untether` | Blocks staging restarts during dev; guides to `untether-dev`; allows `staging.sh`/`pipx upgrade` path |
| runner-edit-context | Edit/Write to `runners/*.py` | 3-event contract, PTY lifecycle, test/doc reminders |
| schema-edit-context | Edit/Write to `schemas/*.py` | msgspec impact on parsing, fixture updates |
| telegram-edit-context | Edit/Write to `telegram/*.py` | Outbox model, callback_data limits, early answering |
| version-bump-checklist | Edit/Write to `pyproject.toml` (version change) | GitHub issues, CHANGELOG entry, `uv lock`, release checklist |

## Rules (project-scoped)

Rules in `.claude/rules/` auto-load when editing matching files:

| Rule | Applies to | Key constraints |
|------|-----------|----------------|
| `runner-development.md` | `runners/**`, `runner.py` | EventFactory usage, session locking, entry point registration |
| `telegram-transport.md` | `telegram/**` | Outbox-only writes, 64-byte callback data, ephemeral cleanup |
| `control-channel.md` | `runners/claude.py`, `claude_control.py` | PTY lifecycle, session registries, outline-gate mechanics |
| `testing-conventions.md` | `tests/**` | pytest+anyio, stub patterns, 80% coverage threshold |
| `release-discipline.md` | `CHANGELOG.md`, `pyproject.toml` | GitHub issue linking, changelog format, semantic versioning |
| `dev-workflow.md` | `src/untether/**` | Dev vs staging separation, never restart staging for testing, always use untether-dev |
| `context-quality.md` | AI context files (`CLAUDE.md`, `AGENTS.md`, etc.) | Cross-file consistency, path verification, version accuracy, command accuracy |
| `help-faq.md` | `docs/faq/**` | NEVER delete; keep FAQ current with feature changes; H2s must be question-shaped (#477) |
| `workflow-commands.md` | agentic loop commands (`.claude/commands/*.md`) | Routing table + 7 cross-cutting rules (Untether-mode, release-guard, dev/staging, reuse, confirm-gated writes, idempotency, redaction); cited by every workflow command. See `docs/LOOPS.md` for the loop registry |
| `kaizen.md` | `/kaizen`, `/kaizen-review` | Capture shape (8 tags + evidence + S/C/R), read-only-except-one-comment boundary, propose-only promotion; full rubric in `docs/kaizen/README.md` |

## Tests

3941 unit tests, 80% coverage threshold. Integration testing against `@untether_dev_bot` is **mandatory before every release** — see `docs/reference/integration-testing.md` for the full playbook with per-release-type tier requirements (patch/minor/major). All integration test tiers are fully automated by Claude Code via Telegram MCP tools and Bash.

Key test files:

- `test_claude_control.py` — 116 tests: control requests, response routing, registry lifecycle, auto-approve/auto-deny, tool auto-approve, custom deny messages, discuss action, early toast, outline gate (#570 retired the progressive cooldown), auto permission mode, diff_preview plan bypass, plus the **stage-6 approval invariant** (#749) — prompting modes (`default`/`manual`/`acceptEdits`) route every tool to Telegram while autonomous modes keep the two-tool set, the `ExitPlanMode`/`AskUserQuestion` exceptions survive, and `new_state()` is proven to arm `prompting_mode` rather than merely defining the helper
- `test_callback_dispatch.py` — 32 tests: callback parsing, dispatch toast/ephemeral behaviour, early answering, `CommandContext.file_deny_globs` for commands and callbacks incl. hot-reload (#389)
- `test_exec_bridge.py` — 291 tests: ephemeral notification cleanup, approval push notifications, progressive stall warnings, stall diagnostics, stall auto-cancel with CPU-active suppression (sleeping-process aware), tool-active repeat suppression, approval-aware stall threshold, MCP tool stall threshold, frozen ring buffer hung escalation, session summary, PID/stream threading, auto-continue detection (incl. the `saw_result` latch that stops the salvage predicate firing on healthy completed runs, #716), signal death suppression, Type-A stream-idle auto-retry (#572), empty-resume quarantine-and-fresh recovery, resume divert/clear, empty-result diagnostics, progress persistence released on every exit path incl. recovery re-runs (#810)
- `test_ask_user_question.py` — 56 tests: AskUserQuestion control request handling, question extraction, pending request registry, answer routing, option button rendering, multi-question flows, structured answer responses, ask mode toggle auto-deny, late-tap already-answered memo (TTL + entry cap + channel scoping, #698), tracked-action advance so the progress heartbeat can't re-render Q1 over Q2 (#709), concurrent final-tap bounds check (#710), HTML escaping at the `parse_mode="HTML"` boundary with a raw-by-default guard against double-escaping the markdown-rendered model title (#713), channel-scoped option taps so a tap in one chat cannot silently answer another chat's question, plus the dispatch early-toast hook's legacy-signature fallback (#715)
- `test_diff_preview.py` — 14 tests: Edit diff display, Write content preview, Bash command display, line/char truncation
- `test_cost_tracker.py` — 30 tests: cost accumulation, per-run/daily budget thresholds, warning levels, daily reset, auto-cancel flag, one-shot `config.cost_visibility_gap` warning (#658), budget-independent `cost.run_outlier` per-run spend signal with configurable threshold and notice opt-out (#702), run-shape fields on that signal — `num_turns`/`usd_per_turn`/durations/token block, omitted when absent or mistyped (#717)
- `test_export_command.py` — 16 tests: session event recording, markdown/JSON export formatting, usage integration, session trimming
- `test_browse_command.py` — 70 tests: path registry, directory listing, file preview, inline keyboard buttons, security (path traversal); explicit project root — chat binding, then `default_project`, no cwd fallback; deny globs + hidden-path denial (`.github`/`.gitignore` allowed) on args, listings and callbacks; symlink escape and loop handling; no existence oracle; per-chat registry ids and 64-byte `callback_data` (#389, #210)
- `test_meta_line.py` — 70 tests: model name shortening (incl. Claude 5 major-only IDs, the `fable` family and the `[1m]` context marker, #688), meta line formatting, ProgressTracker meta storage/snapshot, footer ordering (context/meta/resume)
- `test_error_hints.py` — 53 tests: end-of-life/unsupported-client hints ordered ahead of the generic `invalid_request_error` pattern, which AMP's `426` payload would otherwise match with a misleading "Invalid API request" hint; clap argv-drift hint (kept to clap's exact shapes so prose doesn't match) and the Codex retired-config-key hint outranking the generic EOL hint (#830)
- `test_threads_command.py` — `/threads` (AMP-only, deprecated): thread registry, formatting, callback-data bounds, plus a defensive zero-exit fatal-stderr guard (`amp threads list` exits 0 and does not hit AMP's version gate, so this is belt-and-braces rather than an observed failure)
- `test_runner_utils.py` — 43 tests: error formatting helpers, drain_stderr capture, enriched error messages, stderr sanitisation
- `test_shutdown.py` — 19 tests: shutdown state transitions, idempotency, reset, evidence-gated drain-timeout selection, self-restart argv matcher + descendant evidence scan (#690)
- `test_drain_notify.py` — 15 tests: drain start/timeout notices, per-chat dedupe, forum-topic thread routing + (channel, thread) dedupe (#665)
- `test_preamble.py` — 18 tests: default preamble injection, disabled preamble, custom text override, empty text disables, settings defaults
- `test_restart_command.py` — 3 tests: command triggers shutdown, idempotent response, command id
- `test_cooldown_bypass.py` — 27 tests: outline gate (hold-open with outline, auto-deny without), plan-input gate on plan-file CLIs (#659), no-text auto-deny, hold-open outline flow (#570 retired the time-based cooldown escalation)
- `test_verbose_progress.py` — 39 tests: format_verbose_detail() for each tool type, MarkdownFormatter verbose mode, compact regression
- `test_verbose_command.py` — 7 tests: /verbose toggle on/off/clear, backend id
- `test_config_command.py` — 245 tests: home page, plan mode/ask mode/verbose/engine/listen/model/reasoning sub-pages, toggle actions, callback vs command routing, button layout, engine-aware visibility, default resolution, deprecated-engine `⚠️` glyph + notice (still selectable, never hidden); Codex Approval policy copy/home hints describe the sandbox (safe = read-only, no `untrusted`, #830)
- `test_pi_compaction.py` — 6 tests: compaction start/end, aborted, no tokens, sequence
- `test_proc_diag.py` — 107 tests: format_diag, is_cpu_active, collect_proc_diag (Linux /proc reads), ProcessDiag defaults, macOS ps backend (TIME parser, process table, tree CPU, dispatch — #689), read_cmdline_argv; `describe_process` redacts argv[0] process titles and secret-ish tokens before truncation (#800); `cli_children()` (live direct children + argv, process group and start time, zombies skipped, ppid-scan fallback without `task/*/children`, forking-wrapper resolution, macOS `ps` pid/ppid/pgid/stat/etime/lstart backend) and `hook_evidence_children()` (Bash-tool shells, children older than the oldest unpaired hook, baseline (pid + start) and MCP/LSP exclusions only inside the CLI's process group — detached hooks never exempt; exec'd hook commands still count) and hook-script labels (#812)
- `test_exec_runner.py` — 52 tests: event tracking (event_count, recent_events ring buffer, PID in StartedEvent meta), JsonlStreamState defaults, watchdog approval-pending discriminator (registry probe first, non-positional ring scan fallback, same-tick `rate_limit_event`, #697); hook lifecycle frames don't overwrite `last_event_type` but count as liveness, ring label `hook:<subtype>` (#812)
- `test_build_args.py` — 187 tests: CLI argument construction for all 6 engines, model/reasoning/permission flags, verbatim pass-through of every genuine Claude permission mode (#741), and **mode-aware `--allowedTools`** (#749) — the flag is dropped for `default`/`manual`/`acceptEdits` and kept for every autonomous mode, an explicit `[engines.claude] allowed_tools` still wins (logging `claude.allowed_tools.prompting_mode_override` once per mode per process), and an empty list never emits a valueless flag; `--include-hook-events` gated on control-channel mode, the kill switch and the cached help probe, never duplicated from `extra_args` (#812); Codex safe → exec-level `--sandbox read-only` (before `resume`), no `--ask-for-approval` in any mode, `auto` = full auto, an unknown Codex mode WARNs once (#830); `extra_args` deny-list for Claude/Codex — bypass/managed/workspace flags in every spelling (`=`, clusters, attached short values, `--`), the Codex `-c` substring rule, documented passthrough still accepted, values never logged, one-shot `dangerously_skip_permissions` WARN (#209)
- `test_extra_args_guard.py` — 14 tests: the #209 shared tokeniser — `--flag=value` split, short clusters stop at value-taking and unknown letters, attached/`=` short values, case-sensitive shorts, a bare `--` reported with scanning continuing, value normalisation, dedupe, error text never carries values
- `test_runtime_loader.py` — 9 tests: setup summary/warnings per engine state; #209 D15 — a refused `extra_args` flag stops the default engine and disables any other (`load_error`, never available, startup `failed to load:`), other config errors keep the `bad_config` fallback
- `test_config_watch.py` — 4 tests: config status, watcher applies a reload; #209 a real `_reload_config` refuses a blocked flag (with a negative control) and the watcher keeps the previous runtime
- `test_claude_permission_modes.py` — 46 tests: Claude permission-mode semantics against CLI 2.1.228 — `auto` pass-through vs the `plan-auto` sugar, allowlist contents (incl. `manual`/`dontAsk`, and `default` which the CLI still accepts), cron↔`[engines.claude]` accept/reject parity, the **one-shot** chat-pref migration of the legacy `auto` spelling (incl. the regression guard that a deliberately chosen `auto` survives a reload), one-shot TOML WARN, plus a drift test that re-derives the mode set from the installed binary (#741/#742); also the `is_claude_prompting_mode` classification matrix over all 8 accepted values (#749) and the **#750 release-gate probe** — `--permission-prompt-tool` is hidden from `--help`, so the probe spawns the binary with the argument deliberately missing and reads commander's error, distinguishing "argument missing" (flag alive) from "unknown option" (flag gone) at zero token cost
- `test_telegram_files.py` — 66 tests: file helpers, deduplication, default upload paths, recursive deny-glob matching (root-level and deep `**`, right-anchored bare names, case-insensitive `.git`, `.env.example` pin, `full_match` oracle on 3.13+) (#831), `check_path_access` (lexical + resolved deny, absolute/`..` candidates, benign/escaping/dangling/looping symlinks, symlinked root, hidden + allowlist, no existence oracle) (#389, #390)
- `test_telegram_file_transfer_helpers.py` — 64 tests: `/file put` and `/file get` command handling, media groups, force overwrite, symlinked upload/download targets denied on the resolved path (incl. the `incoming -> .git/hooks` vector and per-file `.ssh` denial in media groups), symlinked run root dedup without `ValueError`, symlink-loop error, zip members checked on the real path with the requested names kept, `file_transfer.path_denied` log fields (#390)
- `test_loop_coverage.py` — 67 tests: update loop edge cases, message routing, callback dispatch, shutdown integration; `ForwardCoalescer` merges rapid prompts in order instead of dropping them and flushes on reply-target/context/voice/directive mismatch (#794); commands are a coalesce barrier — `/cancel`/`/new`/`/continue` drop the pending prompt with a notice, other commands flush it first, directives and `/steer <text>` stay prompts (#807)
- `test_telegram_topics_command.py` — 16 tests: `/new` cancellation (cancel helper, chat/topic modes, running task cleanup), `/ctx` binding, `/topic` command
- `test_trigger_server.py` — 34 tests: health, auth, event filter, multipart (file upload, form fields, size limit, filename sanitisation, auth rejection), rate limit burst 429, fire-and-forget dispatch
- `test_trigger_actions.py` — 37 tests: file_write (traversal, deny globs incl. deep `.git`/`.ssh` paths and case-insensitive `.git` (#831), symlink to `.env` denied on the resolved path (#390), size, conflicts, multipart short-circuit), http_forward (SSRF, retries, headers), notify_only
- `test_trigger_cron.py` — 27 tests: 5-field cron matching, timezone conversion (Melbourne, DST, per-cron/default override), step validation
- `test_trigger_settings.py` — 58 tests: CronConfig/WebhookConfig/CronFetchConfig/TriggersSettings validation, action fields, multipart defaults, timezone
- `test_trigger_ssrf.py` — 103 tests: IPv4/IPv6 blocking, URL validation, DNS resolution, allowlist overrides; structured `SSRFBlockedError` / `SSRFResolutionError` (message unchanged), `suggest_allowlist` (never link-local/metadata; each suggestion unblocks its address), userinfo redaction in `ssrf.*` logs (#679)
- `test_telegram_voice.py` — 48 tests: voice download/size/transcriber paths, language + vocabulary-prompt pass-through, #789 default-prompt shape (bare `Claude` first, no Gemini/Amp/Pi, no `Claude.md`, ≤300 chars, quoted verbatim in `docs/reference/transports/telegram.md`), #381 SSRF chokepoint; #679 actionable refusal (host + allowlist entry, no URL/port/userinfo echoed, hostile hostname not rendered, no suggestion for metadata/DNS failure) and the never-raising startup/reload `check_voice_endpoint`
- `test_trigger_fetch.py` — 28 tests: HTTP GET/POST, file read (deep `.git`/`.ssh` denial, symlink to a denied file, #831), parse modes, failure handling, prompt building
- `test_trigger_auth.py` — 16 tests: bearer token, HMAC-SHA256/SHA1, timing-safe comparison
- `test_trigger_rate_limit.py` — 4 tests: token bucket fill/drain, per-key isolation, refill timing
- `test_trigger_manager.py` — 35 tests: TriggerManager init/update/clear, webhook server hot-reload (add/remove/update routes, secret changes, health count), cron schedule swapping, timezone updates; rc4 helpers (crons_for_chat, webhooks_for_chat, cron_ids, webhook_ids, remove_cron, atomic iteration)
- `test_describe_cron.py` — 37 tests: human-friendly cron rendering (daily, weekday ranges, weekday lists, single day, timezone suffix, fallback to raw, AM/PM boundaries)
- `test_trigger_meta_line.py` — 6 tests: trigger source rendering in `format_meta_line()`, ordering relative to model/effort/permission
- `test_bridge_config_reload.py` — 20 tests: TelegramBridgeConfig unfrozen (slots preserved), `update_from()` copies all 11 fields, files swap, chat_ids/voice_transcription_api_key edge cases, trigger_manager field default, `RESTART_REQUIRED_FIELDS` ClassVar invariants (#318), `_notify_restart_required` broadcast to project chats + admin DMs with per-chat failure isolation (#318 follow-up)
- `test_at_command.py` — 38 tests: `/at` parse (valid/invalid suffixes, bounds, case-insensitive), `_format_delay`, schedule/cancel, per-chat cap, scheduler install/uninstall
- `test_offset_persistence.py` — 15 tests: Telegram update_id round-trip, corrupt JSON handling, atomic write, `DebouncedOffsetWriter` interval/max-pending semantics, explicit flush
- `test_sdnotify.py` — 7 tests: NOTIFY_SOCKET handling (absent/empty/filesystem/abstract-namespace), send error swallowing, UTF-8 encoding
- `test_session_quarantine.py` — 7 tests: QuarantineStore round-trip persistence, engine isolation, malformed/corrupt state-file resilience, age-based pruning to disk, singleton accessor + injection (#631/#632)
- `test_claude_task_map.py` — 38 tests: native background-task map from `system/task_*` events (#776) — schema decode incl. `command_lifecycle`, live/terminal statuses, snapshot reconciliation, Monitor as `local_bash`, native-vs-legacy handle precedence, ScheduleWakeup legacy handle, `claude.task.registered`/`ended` logs (#662); rc13: resumed-task revival (`task_started`/snapshot/`task_updated` for an ended id, 5 s snapshot grace, re-announceable end; #801), `holds_session` for a subagent's orphaned bg task, `origin_turn` stamping (#795), `last_step`, owner linking; the notification turn carries `announced_turns` (#813)
- `test_live_session_runner.py` — 23 tests: real ClaudeRunner over the control-channel fake `tests/fake_clis/fake_claude_live.py` — `TurnEvent` segments for bg Bash/Agent wake, Monitor ticks, scheduled wake-up and injected follow-ups (uuid attribution), subagent events not opening a turn, kill switch, resume guard, #505 inherited-fd regression; hook rewake turns and withheld plain-async hook responses (#812); a `/continue` run releases its session registries on idle close, with live sessions off and on `/cancel`, and cleanup — including `stream_end_events` / `process_error_events` — never clears a session another run owns (#816)
- `test_live_session_lifecycle.py` — 30 tests: idle close (rc=0, no quarantine), task hold until wake, max-hold notice + graceful close, pending ScheduleWakeup hold, pending-request pause, absolute cap, close-grace SIGINT→SIGTERM with a proc-diag snapshot first, clean idle close not quarantined while a stuck close with a live task still is (#791), close/injection race guard, errored first result closes the session at once, idle close backs off for a just-written follow-up; #812 async-hook hold (pending hold, bound → one `hold_expired` + `async_hook_killed` + closing notice, 35 s hook grace, all-or-nothing release once no hook process is left, the live mix of sync + plain-async + inline `asyncRewake` hooks — the rewake is delivered, and on expiry the labels count live hook processes; an exec'd hook command with no `sh -c` is held until its rewake, and so are a `UserPromptSubmit` hook alive at `system/init` and a hook with MCP-looking argv; baseline (incl. an `sh -c`-wrapped MCP server) and late MCP children never hold)
- `test_live_session_bridge.py` — 64 tests: stall monitor quiet while live-idle and run-level edits standing down during follow-up turns, `FollowupTurnRouter` (lazy progress created once under concurrency, approvals attach to the turn, interrupted-turn final, follow-up anchors, notice anchor), turn headers, closing-notice wording; cancelled follow-up turn renders `cancelled` with its cost accounted exactly once, even when the render is interrupted (#806), `peak_live_idle_seconds` counts idle gaps only (#811), `🪝 Hook feedback` header + hook closing notice (#812), a tool-free follow-up's header times the turn from its start, not `0s` (#815)
- `test_live_session_harness.py` — 11 tests: real `handle_message` + ClaudeRunner + fake live CLI — 🔔 wake message after turn 1 (#591 ordering), short vs tool-using wake turns, Monitor notify policy, alias release, max-hold notice; `hook_rewake` wake delivered as a pushed message (#812)
- `test_live_session_injection.py` — 17 tests: scheduler `inject_job` hook and live pump (regression: follow-up stuck behind a live run), queue-semantics `inject_when_idle`, FIFO, closing fallback, changed chat settings close instead of inject, end-to-end one-spawn follow-up
- `test_live_session_control.py` — 8 tests: unique-run counting with turn aliases, live-idle detection, drain closes idle sessions only, graceful `/cancel` of an idle live session, `/cancel` fallback dedupe
- `test_claude_hooks.py` — 26 tests: #812 hook frames paired by `hook_id` (SessionStart/Setup never hold, 256 cap), `has_pending_async_hooks` bound from the newest hook + one `claude.hook.hold_expired` per hold, kept apart from `has_live_background_work`, all-or-nothing `release_settled_async_hooks` (no per-hook PID binding), `capture_cli_baseline` once per process (non-detached children only, pid + start), the oldest-unpaired-hook timing exemption on `hook_clock()`, rewake attribution at turn open and at the result (`origin` task-notification)
- `test_claude_safeguard.py` — 15 tests: #814 refusal frame / informational notice / refusal-fallback tally (a paired stop counts once), outcome inference at the result, `claude.safeguard_stop`, `usage["safeguard"]` on CompletedEvent and live TurnEvent, footer + one-time hint, not-retried explanatory body, `model_fallback` row, real ClaudeRunner replay of the nsd transcript shape over `fake_claude_live.py`
- `test_session_costs.py` — 11 tests: cost ledger persistence/prune, deltas (ledger / seeded baseline / new session / unknown baseline), staging-evidence sequence, live follow-up turn budget sees the delta (#778)
- `test_noop_resume_harness.py` — 6 tests: end-to-end no-op empty-resume reproduction via the fake-claude CLI (`tests/fake_clis/fake_claude_noop_resume.py`) — real ClaudeRunner + handle_message drive quarantine-and-fresh recovery, healthy-resume negative control, linger-scenario emission shape (#634); also hosts the `trailing_user_after_result` scenario proving a post-`result` frame never reaches the stream (#716)
- `test_claude_cli_schema_drift.py` — 30 tests: zero-token drift probe of the installed CLI binary — `rate_limit_event` status/rateLimitType/overageStatus enums and snapshot keys match `CLAUDE_RATE_LIMIT_*` constants (#790), `system/api_retry` subtype + counter keys present (#792); skips when the CLI is absent or the minified pattern moves; #814 safeguard notice text, refusal `stop_details`, `model_refusal_*` subtypes, aborted `terminal_reason` values, `TaskOutput` in the removed-tools set (#813); #812 `--include-hook-events` + `hook_*` subtypes and the help probe, and `hook_started` emitted before a `detached` hook spawn; #209 refused flags still listed/recognised by the parser (`Input must be provided` / `argument missing`, never a prompt), commander cluster expansion, the bypass launch-gate literal, and a full long-flag snapshot (`CLAUDE_FLAGS_CLASSIFIED_2_1_285`, `--autocompact` allowed) that fails on any unclassified new flag
- `test_codex_cli_schema_drift.py` — 32 tests: zero-token drift probe of the installed Codex CLI — the exact `build_args` argv (safe/default × new/resume/continue) is accepted (`No prompt provided via stdin`, rc=1), `-a untrusted` rejected, exec `--sandbox` values, `-a` root-only, config-route `untrusted` rejected (#830); #209 refused flags exist at exec level, root-placement semantics (bypass flags live at the root, exec-only ones rejected there), root `-s` coexists with exec `--sandbox` while a duplicate exec `-s` errors, full long-flag snapshot (`CODEX_FLAGS_CLASSIFIED_0_157_1`); fresh temp `CODEX_HOME` per test, a timeout fails; skips when codex is absent
- `test_rendering.py` — 87 tests: markdown → Telegram entities incl. `<br>` (#786), filename code-formatting (#788), GFM pipe tables kept one row per line with the header bolded and the delimiter dropped, header repeated when `split_markdown_body` splits a table (#797)
- `test_logging_redaction.py` — 23 tests: structlog redaction incl. generic `Authorization: Bearer`, bare `Bearer`, JWTs and `api_key=`/`token=`/`secret=` values, with negatives for token counts and prose (#800)
- `test_background_status.py` — 93 tests: #777 rendering (tokens 52k/1.2M, elapsed, row cap `+N more`, escaping), `live_shown()` (orphaned subagent task listed once its agent ends), progress-block heartbeat, status-message lifecycle (throttle, early edit, persistence, finalise + close reason), manager open/re-open/hot-reload, `/ping` count, #785 fold decision matrix, fold/attribute/quiet-batch notice; #813 `COLLECTION_TOOLS`/`count_substantive_actions`, turn-keyed ack claims
- `test_wake_consolidation.py` — 11 tests: end-to-end #785 part 2 over `fake_claude_live.py` — acks fold and only the report pushes, tool-using ack breaks out, consolidation off = rc12, the `unknown`+`task_finished` pair pushes once, a quiet batch's report pushes even when already announced, all-folded batch sends one `✅ all N … done`, no-op turns after the report fold silently; five-agent interleaved `Read`-ack replay (#813)
- `test_steer_followup.py` — 63 tests: #775 prefs + resolver order (topic → chat → config → queue), settings validation/hot-reload, `/steer`/`/queue` parsing, `maybe_steer` matrix (mode × engine × live × payload × ask-pending, idle session = no ack), bare commands (chat/topic/non-Claude/admin), `/config` Follow-up page + toasts, anchor absorption
- `test_steer_runner.py` — 8 tests: real ClaudeRunner + fake CLI — mid-tool steer folds into one result with a steer row, ordered steers, post-last-tool steer becomes a follow-up turn, window-closed / no-live fallbacks, closing-session race under the lock
- `test_steer_harness.py` — 5 tests: real `handle_message` — one final honouring a mid-tool steer, post-last-tool steer replies to the steer message, steer after `/cancel` falls back; `/cancel` of an in-flight follow-up renders `cancelled` (#806)
- `test_steer_loop.py` — 12 tests: `run_main_loop` routing for steer/queue overrides, uploads and commands never steer, the override blocks #794 merges
- `test_exitplanmode_plan_approval.py` — 25 tests: #793 — the `📋 Plan (approved)` body comes only from an approved request (Telegram, `plan-auto`, discuss-approved), denied/stale bodies never shown, plan body taken from the plan file (the CLI's `input.plan` lags the file by one Write), `claude.plan.stale_input`, procedural denials (Pause & Outline, Let's discuss), symlink/size guards, cross-session isolation
- `test_final_footer_split.py` — 8 tests: #770 — cost/outlier/budget/usage lines land on the last chunk of a split final in footer order, resume `code` entity shifted correctly, trim mode, near-limit chunk, overflow becomes a trailing message
- `test_attestation_marker.py` — 5 tests: `scripts/run-integration-tests.sh` SHA-binding (#674) — `head_sha` auto-derived from the script repo, `--head-sha` + `UT_INTEGRATION_HEAD_SHA` overrides, `dev_bot_id` default + `UT_DEV_BOT_ID` override, tiers/notes preserved in the marker JSON
- `test_test_isolation.py` — 11 tests: #808 guard self-tests — per-test tmp `HOME_CONFIG_PATH` at every binding, `UNTETHER_CONFIG_PATH` dropped, non-loopback httpx refused unless `@pytest.mark.allow_network`, loopback allowed, a `src/` scan that fails on an uncovered `HOME_CONFIG_PATH` binding; the #506 settings parse cache is cleared between tests (order-independent `pytester` run under the real conftest, plus the clear helper)
- `test_settings_cache.py` — 28 tests: #506 content-keyed settings cache — hit/miss, same-size same-mtime edit, atomic rename, in-place append, env change, missing file, invalid config not cached, migrate-on-miss only, A→B→A write race (parse from captured bytes), LRU bound, kill switch, one INFO `config.loaded` per real parse, strict `load_settings` uncached, in-memory source keeps env precedence, bridge helpers see edits

## Development

Two instances run on lba-1 — staging (PyPI/TestPyPI) and dev (local editable source). See `docs/reference/dev-instance.md` for full quickref including the staging workflow. See `docs/reference/integration-testing.md` for the structured integration test playbook run against `@untether_dev_bot` before every release. All integration test tiers are fully automated by Claude Code via Telegram MCP tools (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`, `reply_to_message`, `send_voice`, `send_file`) and Bash (`journalctl`, `kill -TERM`, FD/zombie checks).

| | Staging (`@hetz_lba1_bot`) | Dev (`@untether_dev_bot`) |
|---|---|---|
| **Service** | `untether.service` | `untether-dev.service` |
| **Binary** | `~/.local/bin/untether` (pipx) | `.venv/bin/untether` (editable) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |
| **Source** | PyPI release or TestPyPI rc | Local `/home/nathan/untether/src/` |

### 3-phase release workflow (MANDATORY)

1. **Dev** — fix code, run unit tests, test via `@untether_dev_bot` (6 engine chats), run integration tests
2. **Fleet rollout (rc)** — bump to `X.Y.ZrcN`, merge feature branches to `dev` → CI publishes to TestPyPI, attest tests via `scripts/run-integration-tests.sh ${VERSION} --manual`, then `scripts/fleet-rollout.sh ${VERSION}` rolls the rc to all 5 hosts (lba-1 staging + nsd VPS + channelo VPS + sl VPS + Mac) in parallel
3. **Release** — bump to `X.Y.Z`, write changelog, PR from `dev` → `master`. After merge, `scripts/fleet-rollout.sh ${VERSION}` rolls the stable PyPI build to all 5 hosts in parallel. `release.yml` publishes to PyPI automatically; the master PR merge IS the approval.

**Branch model:** `feature/*` → PR → `dev` (TestPyPI) → PR → `master` (PyPI). Master always matches the latest PyPI release.

**NEVER skip integration testing for minor/major releases. NEVER skip the attestation gate.** `fleet-rollout.sh` enforces the gate — no production upgrades without a passing `@untether_dev_bot` test run on file.

**Claude Code's role in each phase:**
- **Dev**: edit code, run tests, push feature branches, create PRs to `dev`, run integration tests via Telegram MCP
- **Staging/Release**: prepare version bumps, changelog entries, and commit on feature branches — Nathan merges PRs to `dev` and `master`, creates tags, and approves PyPI deploys

Claude Code MUST NOT push to master, merge PRs, create version tags, or trigger releases. These are enforced by hooks and GitHub rulesets (see "Release guard" below).

### Dev/staging separation (CRITICAL)

- **NEVER restart `untether.service` (staging)** to test local code changes. Staging runs a PyPI/TestPyPI wheel — local edits have no effect on it. Restarting staging during development is always wrong.
- **ALWAYS use `untether-dev.service`** for testing. It runs the local editable source.
- **ALWAYS test via `@untether_dev_bot`** before merging/releasing. Staging (`@hetz_lba1_bot`) runs released wheels only.
- Staging is restarted after `scripts/staging.sh install` (TestPyPI rc) or `pipx upgrade untether` (PyPI release).

See `.claude/rules/dev-workflow.md` for full rules.

### Release guard (CRITICAL)

Multi-layer protection prevents accidental merges to master and PyPI publishes. Claude Code cannot circumvent these protections.

**GitHub server-side (unbypassable):**
- **Branch ruleset** "Protect master — no direct push" — all changes to master require a PR, no admin bypass
- **CODEOWNERS** — `* @littlebearapps/core` ensures Nathan reviews every PR to master

**Local hooks (defense-in-depth):**
- `release-guard.sh` — blocks `git push` to master/main, `git tag v*`, `gh release create`, `gh pr merge` to non-dev; feature and dev branch pushes allowed
- `release-guard-protect.sh` — blocks Edit/Write to guard scripts and `.claude/hooks.json`
- `release-guard-mcp.sh` — blocks GitHub MCP `merge_pull_request` and writes to master/main; feature and dev branches allowed

All three guard scripts use the current Claude Code PreToolUse output schema (`hookSpecificOutput` / `permissionDecision: "deny"`). Earlier versions used the legacy `{"decision":"block"}` shape, which Claude Code silently ignored — that bug is fixed.

**Claude Code MUST:**
- Push to feature branches: `git push -u origin feature/<name>`
- Create PRs to dev: `gh pr create --base dev --title "..." --body "..."`
- Merge PRs to dev (allowed): `gh pr merge <number> --squash` (TestPyPI/staging only)
- Let Nathan merge PRs to master — that merge is now the **single release gate**

Claude Code MUST NOT merge PRs targeting master — only dev merges are allowed.

**Single-gate release flow:** Once Nathan squash-merges a PR with a stable version (e.g. `0.35.2`, no `rc`/`a`/`b`/`dev` suffix) to master:
1. `auto-tag-on-master.yml` detects the version bump and pushes `v0.35.2`
2. `release.yml` fires on the tag, runs full CI (validate version, pytest, build, twine check), publishes to PyPI via OIDC trusted publishing, and creates the GitHub Release with wheel + sdist

No further manual approval is needed. The PR merge IS the release approval. Pre-release versions (e.g. `0.35.2rc1`) are skipped by `auto-tag-on-master.yml` so staging-PR merges don't accidentally publish.

**Self-guarding:** the hook scripts, `.claude/hooks.json`, and GitHub rulesets cannot be modified by Claude Code. Only Nathan can change these by editing files manually outside Claude Code.

```bash
# Dev cycle: edit source → restart dev → test via @untether_dev_bot
systemctl --user restart untether-dev
journalctl --user -u untether-dev -f

# Single-host staging (lba-1 only — legacy single-bot path)
scripts/staging.sh install X.Y.ZrcN
systemctl --user restart untether

# Fleet rollout to all 5 hosts (lba-1 + nsd + channelo + sl + mac)
scripts/run-integration-tests.sh X.Y.ZrcN --manual    # attest via @untether_dev_bot
scripts/fleet-rollout.sh X.Y.ZrcN                     # parallel upgrade
scripts/fleet-rollout.sh X.Y.ZrcN --dry-run           # preview
scripts/fleet-rollback.sh X.Y.(Z-1) --only mac        # revert one host

# Promote to stable (only after PyPI release)
scripts/staging.sh reset && systemctl --user restart untether

# Tests / lint
uv run pytest
uv run ruff check src/
```

See `.claude/rules/release-discipline.md` ("Fleet rollout (rc and stable)") and
`docs/plans/2026-05-13-fleet-monitoring-and-upgrades.md` for the full design.

## CI Pipeline

GitHub Actions CI runs on push to master/dev and on PRs:

| Job | What it checks |
|-----|---------------|
| format | `ruff format --check --diff` |
| ruff | `ruff check` with GitHub annotations |
| ty | Type checking (Astral's ty, informational — `continue-on-error`) |
| pytest | Tests on Python 3.12, 3.13, 3.14 with 80% coverage threshold |
| build | `uv build` + `twine check` + `check-wheel-contents` validation |
| lockfile | `uv lock --check` ensures lockfile is in sync |
| install-test | Clean wheel install + smoke-test imports (catches undeclared deps) |
| testpypi-publish | Publishes to TestPyPI on dev push (OIDC, `skip-existing: true`) |
| auto-tag-on-master | On master push: detects stable version bump in `pyproject.toml`, creates and pushes `vX.Y.Z` tag (skips pre-releases) |
| release-validation | PR-only: validates changelog format, issue links, date when version changes |
| pip-audit | Dependency vulnerability scanning (PyPA advisory DB) |
| bandit | Python SAST (security static analysis) |
| codeql | CodeQL code scanning (Python + Actions), blocks PRs on new alerts |
| docs | Zensical docs build |
| prerelease-deps | Weekly (Monday): tests with `--upgrade --prerelease=allow` (informational) |

All third-party actions are pinned to commit SHAs (supply chain protection). Top-level `permissions: {}` restricts to least-privilege.

Dependabot auto-merge (`dependabot-auto-merge.yml`) auto-squash-merges dependency updates after CI passes. GitHub Actions deps (CI-only, never shipped) are auto-merged for all version bumps including major. Python deps (shipped in wheel) are auto-merged for patch/minor only; major bumps get flagged for manual review.

Release pipeline (`release.yml`) uses PyPI trusted publishing with OIDC. The `pypi` GitHub Environment publishes automatically once `auto-tag-on-master.yml` creates a `vX.Y.Z` tag — the PR review on master IS the release approval, so no second reviewer prompt is needed. (Earlier versions had a manual `pypi` environment reviewer gate; that gate was removed in favour of the single-gate flow described under "Release guard".) The `testpypi` environment deploys automatically on dev push. `scripts/validate_release.py` enforces changelog/version consistency. `CODEOWNERS` (`* @littlebearapps/core`) requires team review on all PRs.

## Issue tracking & releases

### GitHub issues

Every bug fix and significant change MUST have a GitHub issue:
- **Bugs found during debugging**: create an issue before or alongside the fix
- **Issue body**: description, impact, affected files, fix reference
- **Labels**: `bug`, `enhancement`, `documentation` as appropriate; severity on bugs via `severity:critical|major|minor|trivial`; `priority: low|medium|high` (note space) for human triage
- **Auto-filed sources**: `auto:error-report` (untether-issue-watcher daemon, log-pattern errors — runs on all 5 fleet hosts (lba-1, nsd, channelo, sl, mac); each filing is host-tagged via the `HOST` env in the daemon's unit/plist) and `auto:monitor-audit` (`/monitor` command audit loop, bugs + enhancements — 5 per-host configs plus the `untether-fleet` meta-target for cross-host audits)
- **Closing**: reference the fixing PR or commit in a close comment

### Changelog

`CHANGELOG.md` must be updated with every version bump:
- **Format**: `## vX.Y.Z (YYYY-MM-DD)` with `### fixes`, `### changes`, `### breaking`, `### docs`, `### tests` subsections
- **Issue links**: every fix/change entry must reference its GitHub issue: `[#N](https://github.com/littlebearapps/untether/issues/N)`
- **Scope**: one changelog section per release, no retroactive edits to prior sections

### Version bumps (semantic versioning)

- **Patch** (0.23.x → 0.23.y): bug fixes, schema additions for new upstream events, dependency updates
- **Minor** (0.x.0 → 0.y.0): new features, new commands, new engine support, config additions
- **Major** (x.0.0 → y.0.0): breaking changes to config format, runner protocol, or public API

### Release checklist

Before tagging a release:
1. All related GitHub issues exist and are referenced in CHANGELOG.md
2. CHANGELOG.md has an entry for the new version with correct date
3. `pyproject.toml` version matches the changelog heading
4. Tests pass: `uv run pytest`
5. Lint clean: `uv run ruff check src/`
6. Lockfile synced: `uv lock --check`

## Documentation screenshots

48 screenshots in `docs/assets/screenshots/` with a tracking checklist in `CAPTURES.md`. README uses a composite hero collage (`hero-collage.jpg`) built with ImageMagick for mobile responsiveness. Doc files use HTML `<img>` tags with `width="360"` and `loading="lazy"` (works in both GitHub and MkDocs). 14 screenshots are still missing and commented out with `<!-- TODO: capture screenshot -->` markers.

## Help-centre FAQ

`docs/faq/faq.md` (17 H2 question-shaped Q/A pairs; renamed from `docs/faq/index.md` in #483 so the help-centre URL becomes `/help/untether/faq/`) backs the marketing-site **FAQPage Schema.org** pipeline shipped on `feature/help-seo-geo-items-1-4` in [`littlebearapps/littlebearapps.com`](https://github.com/littlebearapps/littlebearapps.com). Once the docs-sync mapping in `scripts/docs-sync.config.ts` registers `untether → docs/faq → category: faq`, the marketing site emits `<script type="application/ld+json">` `FAQPage` JSON-LD on every help-centre deploy, unlocking AI-citation surface (ChatGPT, Perplexity, Google AI Overviews) and SERP rich-snippet eligibility.

**The file MUST NOT be deleted or moved** — that silently breaks the docs-sync mapping and regresses the schema on the next deploy. The repo enforces this via the `help-faq-protect.sh` Bash hook which blocks `rm`, `git rm`, `mv`-away, and shell `>` truncation. **Edits ARE encouraged**: keep the FAQ in sync with new features as they land in `CHANGELOG.md`. See [`.claude/rules/help-faq.md`](.claude/rules/help-faq.md) for the full update cadence and shape rules. Tracking issues: [#477](https://github.com/littlebearapps/untether/issues/477) (creation), [#483](https://github.com/littlebearapps/untether/issues/483) (URL rename).

## Conventions

- Python 3.12+, anyio for async, msgspec for JSONL parsing, structlog for logging
- Ruff for linting, pytest with coverage for tests
- Runner backends registered via entry points in `pyproject.toml`
