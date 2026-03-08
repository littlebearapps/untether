# changelog

## v0.34.2 (2026-03-08)

### fixes

- stall monitor loops forever after laptop sleep — no auto-cancel, `/cancel` requires reply [#99](https://github.com/littlebearapps/untether/issues/99)
  - stall auto-cancel: dead process detection (immediate), no-PID zombie cap (3 warnings), absolute cap (10 warnings)
  - early PID threading: `last_pid` set at subprocess spawn, polled by `run_runner_with_cancel` before `StartedEvent`
  - standalone `/cancel` fallback: cancels single active run without requiring reply; prompts when multiple runs active
  - `queued_for_chat()` method on `ThreadScheduler` for standalone cancel of queued jobs

## v0.34.1 (2026-03-07)

### fixes

- session stall diagnostics: add `/proc` process diagnostics (CPU, RSS, TCP, FDs, children), progressive stall warnings, liveness watchdog, event timeline tracking, and session completion summary [#97](https://github.com/littlebearapps/untether/issues/97)
  - new `utils/proc_diag.py` module: `collect_proc_diag()`, `format_diag()`, `is_cpu_active()`
  - `JsonlStreamState` tracks `last_stdout_at`, `event_count`, `last_event_type`, `recent_events` ring buffer, `stderr_capture`
  - PID auto-injected into `StartedEvent.meta` via base class (all engines)
  - progressive `_stall_monitor`: repeating warnings every 3 min with fresh `/proc` snapshots and Telegram notifications
  - liveness watchdog: detects alive-but-silent subprocesses after 10 min with diagnostics; optional auto-kill (off by default, triple safety gate)
  - `session.summary` structured log on every session completion
  - `[watchdog]` config section: `liveness_timeout`, `stall_auto_kill`, `stall_repeat_seconds`
- stream threading broken: `_ResumeLineProxy` hides `current_stream` from `ProgressEdits`, causing `event_count=0` and `last_event_type=None` for all engines [#98](https://github.com/littlebearapps/untether/issues/98)
  - add `current_stream` property to `_ResumeLineProxy` and `_PreludeRunner`
  - set `self.current_stream = stream` in Claude's overridden `run_impl`
  - use `stream.stderr_capture` instead of separate `stderr_lines` in Claude's `run_impl`

## v0.34.0 (2026-03-07)

### fixes

- ExitPlanMode stuck after cancel + resume: stale outline_guard not cleaned up [#93](https://github.com/littlebearapps/untether/issues/93)
  - extract `_cleanup_session_registries()` helper, call from `run_impl` finally block
- stall monitor fails to detect stalls when no events arrive after session start; no Telegram notification [#95](https://github.com/littlebearapps/untether/issues/95)
  - initialise `_last_event_at` from `clock()` instead of `0.0` so threshold works from session start
  - send `⏳ No progress for N min` Telegram notification on stall detection (previously journal-only)

### changes

- show token-only cost footer for Gemini and AMP — `_format_run_cost()` no longer requires `total_cost_usd`; renders `💰 26.0k in / 71 out` when only token data is available [#94](https://github.com/littlebearapps/untether/issues/94)
  - Gemini `_build_usage()`: extract `cached` → `cache_read_tokens` and `duration_ms` from StreamStats
  - AMP `_accumulate_usage()`: accumulate `cache_creation_input_tokens` and `cache_read_input_tokens`
- add Gemini CLI approval mode toggle in `/config` — "read-only" (default, write tools blocked) or "full access" (`--approval-mode=yolo`); tied into existing plan mode infrastructure via shared `permission_mode` field [#90](https://github.com/littlebearapps/untether/issues/90)
  - home page shows "Approval mode" label and button when engine is Gemini
  - sub-page with Read-only/Full access toggle
  - `PERMISSION_MODE_SUPPORTED_ENGINES` constant for engine-aware gating

## v0.33.5 (2026-03-07)

### fixes

- downgrade `control_response.failed` ClosedResourceError from error to warning — race condition when Telegram callback arrives after session stdin closes; `write_control_response()` now returns `bool` and `send_claude_control_response()` propagates it [#61](https://github.com/littlebearapps/untether/issues/61)
  - also downgrade `auto_approve_failed` and `auto_deny_failed` for consistency
- add subprocess watchdog — detects orphaned child processes (e.g. MCP servers) holding stdout pipes open after parent exits; kills process group after grace period [#91](https://github.com/littlebearapps/untether/issues/91)
- add stall monitor — warns when no progress events arrive for 5 minutes; clears on recovery [#92](https://github.com/littlebearapps/untether/issues/92)
- handle `ClosedResourceError` in `iter_bytes_lines()` on abrupt pipe close

## v0.33.4 (2026-03-06)

### fixes

- add render debouncing to batch rapid progress events — configurable `min_render_interval` (default 2.0s) prevents flooding Telegram edits [#88](https://github.com/littlebearapps/untether/issues/88)
  - first render is never debounced; subsequent renders sleep for remaining interval
  - `group_chat_rps` now configurable in `[progress]` (default 20/60, matching Telegram limit)
- make approval notification sends non-blocking — `transport.send()` for push notifications runs in a background task instead of stalling the render loop [#88](https://github.com/littlebearapps/untether/issues/88)

### docs

- document `KillMode=process` → `KillMode=control-group` fix for systemd service files — orphaned MCP servers accumulate across restarts, consuming 10+ GB [#88](https://github.com/littlebearapps/untether/issues/88)

## v0.33.3 (2026-03-06)

### fixes

- block ExitPlanMode after cooldown expires when no outline has been written — adds outline guard check before time-based cooldown [#87](https://github.com/littlebearapps/untether/issues/87)
  - `_OUTLINE_PENDING` + `max_text_len_since_cooldown < 200` guard fires regardless of cooldown expiry
  - strengthened deny/escalation messages with consequence warnings and concrete framing

## v0.33.2 (2026-03-06)

### fixes

- warn at startup when `allowed_user_ids` is empty — any chat member can run commands without filtering [#84](https://github.com/littlebearapps/untether/issues/84)
- sanitise subprocess stderr before exposing to Telegram — redact absolute file paths and URLs [#85](https://github.com/littlebearapps/untether/issues/85)
- truncate prompts to 100 chars in INFO logs to reduce sensitive data exposure [#86](https://github.com/littlebearapps/untether/issues/86)

## v0.33.1 (2026-03-06)

### fixes

- fall back to plain commonmark renderer when `linkify-it-py` is missing instead of crash-looping on startup [#83](https://github.com/littlebearapps/untether/issues/83)

## v0.33.0 (2026-03-06)

### changes

- add effort control for Claude Code — `--effort` flag with low/medium/high levels via `/reasoning` and `/config` [#80](https://github.com/littlebearapps/untether/issues/80)
- show model version numbers in footer — e.g. `opus 4.6` instead of `opus` [#80](https://github.com/littlebearapps/untether/issues/80)
- show effort level in meta line between model and permission mode (e.g. `opus 4.6 · medium · plan`) [#80](https://github.com/littlebearapps/untether/issues/80)
- rename all user-facing "Claude" to "Claude Code" for product clarity [#81](https://github.com/littlebearapps/untether/issues/81)
  - error messages, button labels, config descriptions, notification text
  - engine IDs (`"claude"`) and model/subscription references unchanged

### fixes

- signal error hints (SIGTERM/SIGKILL/SIGABRT) no longer hardcode `/claude` — now engine-agnostic [#81](https://github.com/littlebearapps/untether/issues/81)
- config reasoning page showed bare "Claude" instead of "Claude Code" due to `.capitalize()` [#81](https://github.com/littlebearapps/untether/issues/81)
- `/usage` HTTP errors now show descriptive messages (e.g. "Rate limited by Anthropic — too many requests") instead of bare status codes [#81](https://github.com/littlebearapps/untether/issues/81)
- `/usage` now handles ConnectError and TimeoutException with specific recovery guidance [#81](https://github.com/littlebearapps/untether/issues/81)
- add error hints for "finished without a result event" and "finished but no session_id" — covers all 6 engines [#81](https://github.com/littlebearapps/untether/issues/81)

### docs

- update 27 documentation files with Claude Code naming
- update troubleshooting guide with new error hint categories (process/session errors)
- update inline settings guide — reasoning now shows Claude Code and Codex as supported
- update model-reasoning guide with Claude Code effort levels

### tests

- add 8 new error hint tests (signal engine-agnostic, cross-engine process/session errors)
- update model version tests for `_short_model_name()` (e.g. `opus 4.6`)
- add effort/meta line tests for `format_meta_line()`
- update config command tests for Claude Code reasoning support

## v0.32.1 (2026-03-06)

### fixes

- missing `linkify-it-py` dependency crashes service on startup after 0.32.0 upgrade [#79](https://github.com/littlebearapps/untether/issues/79)
  - `markdown-it-py` linkify feature requires optional `linkify-it-py` package
  - changed dependency to `markdown-it-py[linkify]` to include the extra

### docs

- cross-platform process management instructions — platform tabs for restart/logs, contextualise systemd as Linux-specific

## v0.32.0 (2026-03-06)

### changes

- add Gemini CLI runner with `--approval-mode` passthrough for plan mode support [#991](https://github.com/littlebearapps/untether/issues/991)
- add Amp CLI runner with mode selection and `--stream-json-input` support [#988](https://github.com/littlebearapps/untether/issues/988), [#989](https://github.com/littlebearapps/untether/issues/989)
- add `/threads` command for Amp thread management [#993](https://github.com/littlebearapps/untether/issues/993)
- track Amp subagent `parent_tool_use_id` in action detail [#992](https://github.com/littlebearapps/untether/issues/992)
- redesign `/config` home page with grouped sections (Agent controls, Display, Routing), inline hints, and help links
- add version information footer to `/config` home page
- compact startup message — only show enabled features (topics, triggers), merge engine and default on one line

### fixes

- Gemini CLI `-p` flag compatibility (changed from boolean to string argument) [#75](https://github.com/littlebearapps/untether/issues/75)
- Amp CLI `-x` flag requires prompt as direct argument [#76](https://github.com/littlebearapps/untether/issues/76)
- Amp CLI uses `--mode` not `--model` for model override [#77](https://github.com/littlebearapps/untether/issues/77)
- Amp `/threads` table parsing — `threads list`/`search` don't support `--json` [#78](https://github.com/littlebearapps/untether/issues/78)
- standardise unrecognised-event debug logging across all engine runners
- add structured logging for cost budget alerts and exceeded events
- improve atomic JSON state write error handling and logging
- add timeout and generic exception handlers to voice transcription
- add structured logging for plugin load errors
- improve config cleanup error logging with error type details

### docs

- update README engine compatibility table with Gemini CLI and Amp columns
- add `[gemini]` and `[amp]` configuration sections to config reference
- various doc formatting and link updates

### tests

- add comprehensive tests for redesigned `/config` command (+199 lines)
- simplify startup message generation tests
- add cross-engine test coverage for Gemini and Amp runners

## v0.31.0 (2026-03-05)

### changes

- merge API cost and subscription usage into unified "Cost & usage" config page [#67](https://github.com/littlebearapps/untether/issues/67)
- make `/auth` codex-only, move auth status to `/stats auth` [#68](https://github.com/littlebearapps/untether/issues/68)
- add docs link to `/config` home page [#69](https://github.com/littlebearapps/untether/issues/69)

### fixes

- widen device code regex for real codex output format [#40](https://github.com/littlebearapps/untether/issues/40)
- improve `/auth` info message wording [#70](https://github.com/littlebearapps/untether/issues/70)
- put Cost & usage and Trigger on same row in `/config` [#71](https://github.com/littlebearapps/untether/issues/71)
- 5 optimisations from 4-engine test sweep [#72](https://github.com/littlebearapps/untether/issues/72)

### docs

- add triggers/webhooks/cron architecture and how-to documentation
- expand trigger mode and group chat documentation

## v0.30.0 (2026-03-04)

### changes

- add `/stats` command — persistent per-engine session statistics (runs, actions, duration) with today/week/all periods [#41](https://github.com/littlebearapps/untether/issues/41)
  - `SessionStatsStore` with JSON persistence in config dir
  - auto-prune data older than 90 days
  - recording hook in `runner_bridge.py` on run completion
- add `/auth` command — headless engine re-authentication via Telegram [#40](https://github.com/littlebearapps/untether/issues/40)
  - runs `codex login --device-auth` and sends verification URL + device code
  - `/auth status` checks CLI availability
  - concurrent guard and 16-minute timeout
- add API cost and subscription usage toggles to `/config` menu
  - per-chat persistent settings for `show_api_cost` and `show_subscription_usage`

### fixes

- diff preview on approval buttons was dead code — Edit/Write/Bash were always auto-approved before reaching the diff preview path [#52](https://github.com/littlebearapps/untether/issues/52)
  - when `diff_preview` is enabled, previewable tools now route through interactive approval
  - default behaviour (diff_preview off) unchanged

### tests

- 16 new diff preview gate tests (parametrised across tools and settings)
- 18 new session stats storage tests (record, aggregate, persist, prune, corrupt file)
- 13 new stats command tests (formatting, duration, handle with args)
- 13 new auth command tests (ANSI stripping, device code parsing, concurrent guard, status)

## v0.29.0 (2026-03-03)

### changes

- add diff preview toggle to `/config` menu — per-chat persistent setting to enable/disable diff previews in tool approval messages [#58](https://github.com/littlebearapps/untether/issues/58)
  - Claude-only; default is on (matches existing behaviour)
  - stored in `EngineOverrides`, gated via `EngineRunOptions` ContextVar
  - home page layout: new "Diff preview" button alongside Verbose

### fixes

- remove redundant local import of `get_run_options` in `claude.py` that shadowed the module-level import

### tests

- 25 new tests: diff preview config page (18), gating logic (4), engine override merge (2), toast labels (3)
- updated home button test to assert `config:dp` presence for Claude

## v0.28.1 (2026-03-03)

### changes

- add 20 new API/LLM error hints for graceful failure during provider outages [#54](https://github.com/littlebearapps/untether/issues/54)
  - subscription limits: Claude "out of extra usage" / "hit your limit" — tells user session is saved, wait for reset
  - billing errors: OpenAI `insufficient_quota`, `billing_hard_limit_reached`; Google `resource_exhausted`
  - API overload: Anthropic `overloaded_error` (529), generic "server is overloaded"
  - server errors: 500 `internal_server_error`, 502 `bad gateway`, 503 `service unavailable`, 504 `gateway timeout`
  - rate limits: `too many requests` (extends existing `rate limit` pattern)
  - network: `connecttimeout`, DNS failure, network unreachable
  - auth: `openai_api_key`, `google_api_key` (extends existing `anthropic_api_key`)

### fixes

- deduplicate error messages when answer and error share the same first line (e.g. Claude subscription limits showed "You're out of extra usage" twice) [#55](https://github.com/littlebearapps/untether/issues/55)
- remove Approve/Deny buttons from AskUserQuestion option keyboards — only option buttons and "Other (type reply)" shown [#56](https://github.com/littlebearapps/untether/issues/56)
- push notification for AskUserQuestion now says "Question from Claude" instead of "Action required — approval needed" [#57](https://github.com/littlebearapps/untether/issues/57)

### tests

- 19 new tests for API error hint patterns: subscription limits, billing, overload, server errors, network, ordering
- 2 new tests for error/answer deduplication in runner_bridge [#55](https://github.com/littlebearapps/untether/issues/55)
- negative assertions for Approve/Deny absence in option button test [#56](https://github.com/littlebearapps/untether/issues/56)

## v0.28.0 (2026-03-02)

### changes

- interactive ask mode — AskUserQuestion renders option buttons in Telegram, sequential multi-question flows (1 of N), "Other (type reply)" fallback, and structured `updatedInput` responses [#51](https://github.com/littlebearapps/untether/issues/51)
  - `/config` toggle: "Ask mode" sub-page (Claude-only) to enable/disable interactive questions
  - dynamic preamble encourages or discourages AskUserQuestion based on toggle state
  - auto-deny when toggle is OFF — Claude proceeds with defaults instead of asking
- Gemini CLI and Amp engine runners added (coming soon — not yet released for production use)

### fixes

- synthetic Approve Plan button now returns an error when session has already ended, instead of silently succeeding [#50](https://github.com/littlebearapps/untether/issues/50)
  - session-alive check in `da:` button handler (`claude_control.py`)
  - stale `_REQUEST_TO_SESSION` entries cleaned up during session end
- ReadTimeout in usage footer no longer kills final message delivery — chat appeared frozen when Anthropic usage API was slow [#53](https://github.com/littlebearapps/untether/issues/53)

### tests

- 27 new tests for ask mode: option button rendering, multi-question flow management, structured answer responses, config toggle, auto-deny when OFF
- 4 new tests for synthetic approve after session ends (#50): dead approve, dead deny, active approve, session cleanup

### docs

- updated inline-settings how-to, interactive-control tutorial, README, and CLAUDE.md for ask mode
- added ask mode to `/config` command description and features list
- Gemini CLI and Amp listed as "coming soon" in README engines table

## v0.27.1 (2026-03-02)

### fixes

- add ReadTimeout error hint for transient network timeouts [#15](https://github.com/littlebearapps/untether/issues/15)
- resolve all ty type checker warnings (109 → 0)

### docs

- fix PyPI logo rendering — use absolute raw GitHub URL so SVG displays on PyPI
- add Upgrading section to README with uv/pipx upgrade + restart commands
- point project URLs to GitHub for PyPI verified details

## v0.27.0 (2026-03-01)

### fixes

- per-chat outbox pacing — progress edits to different chats no longer serialise through a single global timer; each chat tracks its own rate-limit window independently [#48](https://github.com/littlebearapps/untether/issues/48)
  - `_next_at[chat_id]` dict replaces scalar `next_at`
  - new `_pick_ready(now)` selects from unblocked chats; `retry_at` stays global (429)
  - 7 group chats now update in parallel (~0s total) vs old 7 × 3s = 21s delay

### changes

- `/config` model sub-page — view current model override and clear it; button always visible on home page [#47](https://github.com/littlebearapps/untether/issues/47)
- `/config` reasoning sub-page — select reasoning level (minimal/low/medium/high/xhigh) via buttons; only visible when engine supports reasoning (Codex) [#47](https://github.com/littlebearapps/untether/issues/47)

### tests

- 7 per-chat pacing tests: independent chats, private vs group intervals, global retry_at, cross-chat priority, same-chat pacing, 7 concurrent chats, chat_id=None independence
- 54 model + reasoning /config tests: sub-page rendering, toggle actions, engine-aware visibility, toast mappings, override persistence, cross-field preservation

## v0.26.0 (2026-03-01)

### changes

- `/config` inline settings menu — BotFather-style inline keyboard for toggling plan mode, verbose, engine, and trigger; edits message in-place [#47](https://github.com/littlebearapps/untether/issues/47)
  - confirmation toasts on toggle actions (e.g. "Plan mode: off")
  - auto-return to home page after setting changes
  - engine-aware plan mode — hidden for non-Claude engines

### docs

- comprehensive tutorials and how-to guides — 15 new/expanded guides covering daily use, interactive control, messaging, cost management, security, and operations
- inline settings how-to (`docs/how-to/inline-settings.md`)

### tests

- add 62-test suite for `/config` (toast permutations, engine-aware visibility, auto-return, callback dispatch)

## v0.25.3 (2026-03-01)

### fixes
- increase SIGTERM→SIGKILL grace period from 2s to 10s — gives engines time to flush session transcripts before forced kill [#45](https://github.com/littlebearapps/untether/issues/45)
- add `error_during_execution` error hint — users see actionable recovery guidance when a session fails to load [#45](https://github.com/littlebearapps/untether/issues/45)
- auto-clear broken session on failed resume — when a resumed run fails with 0 turns, the saved token is automatically cleared so the next message starts fresh [#45](https://github.com/littlebearapps/untether/issues/45)
  - new `clear_engine_session()` on `ChatSessionStore` and `TopicStateStore`
  - `on_resume_failed` callback threaded through `handle_message` → `_run_engine` → `wrap_on_resume_failed`

### tests
- add `ErrorReturn` step type to `ScriptRunner` mock for simulating engine failures
- add 4 auto-clear unit tests (zero-turn error, success, partial turns, new session)
- add SIGTERM→SIGKILL 10s timeout assertion test
- add 2 `error_during_execution` hint tests (resumed and new session variants)
- integration-tested across Claude, Codex, and OpenCode via untether-dev

## v0.25.2 (2026-03-01)

### fixes

- add actionable error hints for SIGTERM/SIGKILL/SIGABRT signals — users now see recovery guidance instead of raw exit codes [#44](https://github.com/littlebearapps/untether/issues/44)

### docs

- add `contrib/untether.service` example with `KillMode=process` and `TimeoutStopSec=150` for graceful shutdown [#44](https://github.com/littlebearapps/untether/issues/44)
- update `docs/reference/dev-instance.md` with systemd configuration section and graceful upgrade path
- update `CLAUDE.md` with graceful upgrade comment

### tests

- add 5 signal hint tests (SIGTERM, SIGKILL, SIGABRT, case insensitivity, no false positives)

## v0.25.1 (2026-03-01)

### changes

- default `message_overflow` changed from `"trim"` to `"split"` — long final responses now split across multiple Telegram messages instead of being truncated [#42](https://github.com/littlebearapps/untether/issues/42)

## v0.25.0 (2026-02-28)

### changes

- `/verbose` command and `[progress]` config — per-chat verbose toggle shows tool details (file paths, commands, patterns) in progress messages; global verbosity and max_actions settings [#25](https://github.com/littlebearapps/untether/issues/25)
- Pi context compaction events — render `AutoCompactionStart`/`AutoCompactionEnd` as progress actions with token counts [#26](https://github.com/littlebearapps/untether/issues/26)
- `UNTETHER_CONFIG_PATH` env var — override config file location for multi-instance setups [#27](https://github.com/littlebearapps/untether/issues/27)
- ExceptionGroup unwrapping, transport resilience, and debug logging improvements [#30](https://github.com/littlebearapps/untether/issues/30)

### fixes

- outline not visible in Pause & Outline Plan flow — outline was scrolled off by max_actions truncation and lost in final message [#28](https://github.com/littlebearapps/untether/issues/28)
- footer double-spacing — sulguk trailing `\n\n` caused blank lines between footer items (context/meta/resume) [#29](https://github.com/littlebearapps/untether/issues/29)

### docs

- add dev instance quickref (`docs/reference/dev-instance.md`) documenting production vs dev separation
- add dev workflow rule (`.claude/rules/dev-workflow.md`) preventing accidental production restarts
- update CLAUDE.md and README with verbose mode, Pi compaction, and config path features

### tests

- add test suites for verbose command, verbose progress formatting, config path env var, cooldown bypass, and Pi compaction (44 new tests)

## v0.24.0 (2026-02-27)

### changes

- agent context preamble — configurable `[preamble]` injects Telegram context into every runner prompt, informing agents they're on Telegram and requesting structured end-of-task summaries; engine-agnostic (Claude, Codex, OpenCode, Pi) [#21](https://github.com/littlebearapps/untether/issues/21)
- post-outline Approve/Deny buttons — after "Pause & Outline Plan", Claude writes the outline then Approve/Deny buttons appear automatically in Telegram; no need to type "approved" [#22](https://github.com/littlebearapps/untether/issues/22)

### fixes

- improved discuss denial message for resumed sessions — explicitly tells Claude to rewrite the outline even if one exists in prior context [#23](https://github.com/littlebearapps/untether/issues/23)
- discuss cooldown state cleaned up on session end — prevents stale cooldown leaking into resumed runs [#23](https://github.com/littlebearapps/untether/issues/23)

### docs

- update plan-mode how-to with post-outline approval flow
- update control-channel rule with new registries and discuss-approval mechanism
- update CLAUDE.md feature list with preamble and discuss buttons
- update site URL to `https://littlebearapps.com/tools/untether/`

## v0.23.5 (2026-02-27)

### changes

- enrich error reporting in Telegram messages and structlog across all engines [#14](https://github.com/littlebearapps/untether/issues/14)
  - Claude errors now show session ID, resumed/new status, turn count, cost, and API duration
  - non-zero exit codes show signal name (e.g. `SIGTERM` for rc=-15) and captured stderr excerpt
  - stream-ended-without-result errors include session context
  - `runner.completed` structlog includes `num_turns`, `total_cost_usd`, `duration_api_ms`
- compact startup message formatting with hard breaks [#14](https://github.com/littlebearapps/untether/issues/14)

### docs

- comprehensive documentation audit and upgrade [#13](https://github.com/littlebearapps/untether/issues/13)
  - add how-to guides: interactive approval, plan mode, cost budgets, webhooks & cron
  - expand schedule-tasks guide with cron and webhook trigger coverage
  - remove orphaned `docs/user-guide.md` redirect stub
  - fix stale version reference (0.19.0 → 0.23.4) in install tutorial and llms-full.txt
  - regenerate `llms.txt` and `llms-full.txt` with 18 previously missing doc pages
  - add AI IDE context files: `AGENTS.md`, `.cursorrules`, `.github/copilot-instructions.md`
  - update `.codex/AGENTS.md` with correct project commands
  - add `ROADMAP.md` with near/mid/future directional plans
  - update README documentation section with new guide links
  - update `zensical.toml` nav with new how-to guides

## v0.23.4 (2026-02-26)

### fixes

- fix `test_doctor_voice_checks` env var leak from pydantic_settings [#12](https://github.com/littlebearapps/untether/issues/12)
  - `UntetherSettings.model_validate()` auto-loads `UNTETHER__*` env vars, causing `voice_transcription_api_key` to leak into test
  - added `monkeypatch.delenv()` for the pydantic_settings env var before constructing test settings

### docs

- add macOS Keychain credential info to install tutorial, troubleshooting guide, and command reference [#7](https://github.com/littlebearapps/untether/issues/7)

## v0.23.3 (2026-02-26)

### fixes

- add `rate_limit_event` to Claude stream-json schema (CLI v2.1.45+) [#8](https://github.com/littlebearapps/untether/issues/8)
  - new `StreamRateLimitMessage` and `RateLimitInfo` msgspec structs
  - event is decoded cleanly and silently skipped (informational only)
  - eliminates noisy `jsonl.msgspec.invalid` warning in logs

## v0.23.2 (2026-02-26)

### fixes

- fix crash when Claude OAuth credentials file missing (macOS Keychain, API key auth) [#7](https://github.com/littlebearapps/untether/issues/7)
  - `_maybe_append_usage_footer()` now catches `FileNotFoundError` and `httpx.HTTPStatusError`
  - post-run messages are delivered to Telegram even when usage data is unavailable
- add macOS Keychain support for `/usage` command and subscription usage footer [#7](https://github.com/littlebearapps/untether/issues/7)
  - on macOS, Claude Code stores OAuth credentials in the Keychain, not on disk
  - `_read_access_token()` now tries the file first, then falls back to macOS Keychain

## v0.23.1 (2026-02-26)

### changes

- restructure startup message: one field per line, always show all status fields
  - list project names instead of count
  - always show mode, topics, triggers, resume lines, voice, and files status
  - add voice and files enabled/disabled status
- update PyPI description and keywords to reflect current feature set

## v0.23.0 (2026-02-26)

### changes

- refresh startup message: dog emoji, version number, conditional diagnostics, project count
  - only shows mode/topics/triggers/engines lines when they carry signal
  - removes `resume lines:` field (config detail, not actionable)
- add model + permission mode footer on final messages (`🏷 sonnet · plan`)
  - all 4 engines (Claude, Codex, OpenCode, Pi) populate `StartedEvent.meta` with model info
  - Claude also includes `permissionMode` from `system.init`
  - Codex/OpenCode use runner config since their JSONL streams don't include model metadata
- route telegram callback queries to command backends [#116](https://github.com/banteg/takopi/issues/116)
  - callback data format: `command_id:args...` routes to registered command plugins
  - extracts `message_thread_id` from callback for proper topic context
  - enables plugins to build interactive UX with inline keyboards

## v0.22.2 (2026-02-25)

### fixes

- remove defunct Telegram notification scripts that caused CI/release workflows to report failure [#9](https://github.com/littlebearapps/untether/issues/9)
- skip `uuid.uuid7` test on Python < 3.14 (only available in 3.14+) [#10](https://github.com/littlebearapps/untether/issues/10)
- fix PyPI metadata: PEP 639 SPDX license, absolute doc links, remove deprecated classifier [#11](https://github.com/littlebearapps/untether/issues/11)

## v0.22.1 (2026-02-10)

### fixes

- preserve ordered list numbering when nested list indentation is malformed in telegram render output [#202](https://github.com/banteg/takopi/pull/202)

## v0.22.0 (2026-02-10)

### changes

- support Codex `phase` values and unknown action kinds in commentary rendering [#201](https://github.com/banteg/takopi/pull/201)

## v0.21.5 (2026-02-08)

### fixes

- dedupe redelivered telegram updates to prevent duplicate runs in DMs [#198](https://github.com/banteg/takopi/pull/198)

### changes

- read package version from metadata instead of a hardcoded `__version__` constant

### docs

- rotate telegram invite link

## v0.21.4 (2026-01-22)

### changes

- add allowed user gate to telegram [#179](https://github.com/banteg/takopi/pull/179)

## v0.21.3 (2026-01-21)

### fixes

- ignore implicit topic root replies in telegram [#175](https://github.com/banteg/takopi/pull/175)

## v0.21.2 (2026-01-20)

### fixes

- clear chat sessions on cwd change [#172](https://github.com/banteg/takopi/pull/172)

### docs

- add untether-slack plugin to reference [#168](https://github.com/banteg/takopi/pull/168)

## v0.21.1 (2026-01-18)

### fixes

- separate telegram voice transcription client [#166](https://github.com/banteg/takopi/pull/166)
- disable telegram link previews by default [#160](https://github.com/banteg/takopi/pull/160)

### docs

- align engine terminology in telegram and docs [#162](https://github.com/banteg/takopi/pull/162)
- add untether-discord plugin to plugins reference [#164](https://github.com/banteg/takopi/pull/164)

## v0.21.0 (2026-01-16)

### changes

- add `untether config` subcommand [#153](https://github.com/banteg/takopi/pull/153)
- make telegram /ctx work everywhere [#159](https://github.com/banteg/takopi/pull/159)
- improve telegram command planning and testability [#158](https://github.com/banteg/takopi/pull/158)
- simplify telegram loop and jsonl runner [#155](https://github.com/banteg/takopi/pull/155)
- refactor telegram schemas and parsing with msgspec [#156](https://github.com/banteg/takopi/pull/156)

### tests

- improve coverage and raise threshold to 80% [#154](https://github.com/banteg/takopi/pull/154)
- stabilize mutmut runs and extend telegram coverage [#157](https://github.com/banteg/takopi/pull/157)

### docs

- add opengraph meta fallbacks [#150](https://github.com/banteg/takopi/pull/150)

## v0.20.0 (2026-01-15)

### changes

- add telegram mentions-only trigger mode [#142](https://github.com/banteg/takopi/pull/142)
- add telegram /model and /reasoning overrides [#147](https://github.com/banteg/takopi/pull/147)
- coalesce forwarded telegram messages [#146](https://github.com/banteg/takopi/pull/146)
- export plugin utilities for transport development [#137](https://github.com/banteg/takopi/pull/137)

### fixes

- handle forwarded uploads for telegram [#149](https://github.com/banteg/takopi/pull/149)
- preserve directives for voice transcripts [#141](https://github.com/banteg/takopi/pull/141)
- resolve claude.cmd via shutil.which on windows [#124](https://github.com/banteg/takopi/pull/124)

### docs

- add untether-scripts plugin to plugins list [#140](https://github.com/banteg/takopi/pull/140)

## v0.19.0 (2026-01-15)

### changes

- overhaul onboarding with persona-based setup flows [#132](https://github.com/banteg/takopi/pull/132)
- add queued cancel placeholder for Telegram runs [#136](https://github.com/banteg/takopi/pull/136)
- prefix Telegram voice transcriptions for agent awareness [#135](https://github.com/banteg/takopi/pull/135)

### docs

- refresh onboarding docs with new widgets and hero flow [#138](https://github.com/banteg/takopi/pull/138)
- fix docs site mobile layout and font consistency [#139](https://github.com/banteg/takopi/pull/139)
- link to untether.dev docs site

## v0.18.0 (2026-01-13)

### changes

- add per-chat and per-topic default agent via `/agent set` command [#109](https://github.com/banteg/takopi/pull/109)
- add session resume shorthand for pi runner [#113](https://github.com/banteg/takopi/pull/113)
- expose `sender_id` and `raw` fields on `MessageRef` for plugins [#112](https://github.com/banteg/takopi/pull/112)

### fixes

- recreate stale topic bindings when topic is deleted and recreated [#127](https://github.com/banteg/takopi/pull/127)
- use stdout session header for pi runner [#126](https://github.com/banteg/takopi/pull/126)

### docs

- restructure docs into diataxis format and switch to zensical [#121](https://github.com/banteg/takopi/pull/121) [#125](https://github.com/banteg/takopi/pull/125)

## v0.17.1 (2026-01-12)

### fixes

- fix telegram /new command crash [#106](https://github.com/banteg/takopi/pull/106)
- track telegram sessions for plugin runs [#107](https://github.com/banteg/takopi/pull/107)
- align telegram prompt upload resume flow [#105](https://github.com/banteg/takopi/pull/105)

## v0.17.0 (2026-01-12)

### changes

- add chat session mode (`session_mode = "chat"`) for auto-resume per chat without replying, reset with `/new` [#102](https://github.com/banteg/takopi/pull/102)
- add `message_overflow = "split"` to send long responses as multiple messages instead of trimming [#101](https://github.com/banteg/takopi/pull/101)
- add `show_resume_line` option to hide resume lines when auto-resume is available [#100](https://github.com/banteg/takopi/pull/100)
- add `auto_put_mode = "prompt"` to start a run with the caption after uploading a file [#97](https://github.com/banteg/takopi/pull/97)
- expose `thread_id` to plugins via run context [#99](https://github.com/banteg/takopi/pull/99)
- use tomli-w for config serialization [#103](https://github.com/banteg/takopi/pull/103)
- add `voice_transcription_model` setting for local whisper servers [#98](https://github.com/banteg/takopi/pull/98)

### docs

- document chat sessions, message overflow, and voice transcription model settings

## v0.16.0 (2026-01-12)

### fixes

- harden telegram file transfer handling [#84](https://github.com/banteg/takopi/pull/84)

### changes

- simplify runtime, config, and telegram internals [#85](https://github.com/banteg/takopi/pull/85)
- refactor telegram boundary types [#90](https://github.com/banteg/takopi/pull/90)

### docs

- add tips section to user guide
- rework readme

## v0.15.0 (2026-01-11)

### changes

- add telegram file transfer support [#83](https://github.com/banteg/takopi/pull/83)

### docs

- document telegram file transfers [#83](https://github.com/banteg/takopi/pull/83)

## v0.14.1 (2026-01-10)

### changes

- add topic scope and thread-aware replies for telegram topics [#81](https://github.com/banteg/takopi/pull/81)

### docs

- update telegram topics docs and user guide for topic scoping [#81](https://github.com/banteg/takopi/pull/81)

## v0.14.0 (2026-01-10)

### changes

- add telegram forum topics support with `/topic` command for binding threads to projects/branches, persistent resume tokens per topic, and `/ctx` for inspecting or updating bindings [#80](https://github.com/banteg/takopi/pull/80)
- add inline cancel button to progress messages [#79](https://github.com/banteg/takopi/pull/79)
- add config hot-reload via watchfiles [#78](https://github.com/banteg/takopi/pull/78)

### docs

- add user guide and telegram topics documentation [#80](https://github.com/banteg/takopi/pull/80)

## v0.13.0 (2026-01-09)

### changes

- add per-project chat routing [#76](https://github.com/banteg/takopi/pull/76)

### fixes

- hardcode codex exec flags [#75](https://github.com/banteg/takopi/pull/75)
- reuse project root for current branch when resolving worktrees [#77](https://github.com/banteg/takopi/pull/77)

### docs

- normalize casing in the readme and changelog

## v0.12.0 (2026-01-09)

### changes

- add optional telegram voice note transcription (routes transcript like typed text) [#74](https://github.com/banteg/takopi/pull/74)

### fixes

- fix plugin allowlist matching and windows session paths [#72](https://github.com/banteg/takopi/pull/72)

### docs

- document telegram voice transcription settings [#74](https://github.com/banteg/takopi/pull/74)

## v0.11.0 (2026-01-08)

### changes

- add entrypoint-based plugins for engines/transports plus a `untether plugins` command and public API docs [#71](https://github.com/banteg/takopi/pull/71)

### fixes

- create pi sessions under the run base dir [#68](https://github.com/banteg/takopi/pull/68)
- skip git repo checks for codex runs [#66](https://github.com/banteg/takopi/pull/66)

## v0.10.0 (2026-01-08)

### changes

- add transport registry with `--transport` overrides and a `untether transports` command [#69](https://github.com/banteg/takopi/pull/69)
- migrate config loading to pydantic-settings and move telegram credentials under `[transports.telegram]` [#65](https://github.com/banteg/takopi/pull/65)
- include project aliases in the telegram slash-command menu with validation and limits [#67](https://github.com/banteg/takopi/pull/67)

### fixes

- validate worktree roots instead of treating nested paths as worktrees [#63](https://github.com/banteg/takopi/pull/63)
- harden onboarding with clearer config errors, safe backups, and refreshed command menu wording [#70](https://github.com/banteg/takopi/pull/70)

### docs

- add architecture and lifecycle diagrams
- call out the default worktrees directory [#64](https://github.com/banteg/takopi/pull/64)
- document the transport registry and onboarding changes [#69](https://github.com/banteg/takopi/pull/69)

## v0.9.0 (2026-01-07)

### projects and worktrees

- register repos with `untether init <alias>` and target them via `/project` directives
- route runs to git worktrees with `@branch` — untether resolves or creates worktrees automatically
- replies preserve context via `ctx: project @branch` footers, no need to repeat directives
- set `default_project` to skip the `/project` prefix entirely
- per-project `default_engine` and `worktree_base` configuration

### changes

- transport/presenter protocols plus transport-agnostic `exec_bridge`
- move telegram polling + wiring into `untether.telegram` with transport/presenter adapters
- list configured projects in the startup banner

### fixes

- render `ctx:` footer lines consistently (backticked + hard breaks) and include them in final messages

### breaking

- remove `untether.bridge`; use `untether.runner_bridge` and `untether.telegram` instead

### docs

- add a projects/worktrees guide and document `untether init` behavior in the readme

## v0.8.0 (2026-01-05)

### changes

- queue telegram requests with rate limits and retry-after backoff [#54](https://github.com/banteg/takopi/pull/54)

### docs

- improve documentation coverage [#52](https://github.com/banteg/takopi/pull/52)
- align runner guide with factory pattern
- add missing pr links in the changelog

## v0.7.0 (2026-01-04)

### changes

- migrate logging to structlog with structured pipelines and redaction [#46](https://github.com/banteg/takopi/pull/46)
- add msgspec schemas for jsonl decoding across runners [#37](https://github.com/banteg/takopi/pull/37)

## v0.6.0 (2026-01-03)

### changes

- interactive onboarding: run `untether` to set up bot token, chat id, and default engine via guided prompts [#39](https://github.com/banteg/takopi/pull/39)
- lockfile to prevent multiple untether instances from racing the same bot token [#30](https://github.com/banteg/takopi/pull/30)
- re-run onboarding anytime with `untether --onboard`

## v0.5.3 (2026-01-02)

### changes

- default claude allowed tools to `["Bash", "Read", "Edit", "Write"]` when not configured [#29](https://github.com/banteg/takopi/pull/29)

## v0.5.2 (2026-01-02)

### changes

- show not installed agents in the startup banner (while hiding them from slash commands)

### fixes

- treat codex reconnect notices as non-fatal progress updates instead of errors [#27](https://github.com/banteg/takopi/pull/27)
- avoid crashes when codex tool/file-change events omit error fields [#27](https://github.com/banteg/takopi/pull/27)

## v0.5.1 (2026-01-02)

### changes

- relax telegram ACL to check chat id only, enabling use in group chats and channels [#26](https://github.com/banteg/takopi/pull/26)
- improve onboarding documentation and add tests [#25](https://github.com/banteg/takopi/pull/25)

## v0.5.0 (2026-01-02)

### changes

- add an opencode runner via the `opencode` cli with json event parsing and resume support [#22](https://github.com/banteg/takopi/pull/22)
- add a pi agent runner via the `pi` cli with jsonl streaming and resume support [#24](https://github.com/banteg/takopi/pull/24)
- document the opencode and pi runners, event mappings, and stream capture tips

### fixes

- fix path relativization so progress output does not strip sibling directories [#23](https://github.com/banteg/takopi/pull/23)
- reduce noisy debug logging from markdown_it/httpcore

## v0.4.0 (2026-01-02)

### changes

- add auto-router runner selection with configurable default engine [#15](https://github.com/banteg/takopi/pull/15)
- make auto-router the default entrypoint; subcommands or `/{engine}` prefixes override for new threads
- add `/cancel` + `/{engine}` command menu sync on startup
- show engine name in progress and final message headers
- omit progress/action log lines from final output for cleaner answers [#21](https://github.com/banteg/takopi/pull/21)

### fixes

- improve codex exec error rendering with stderr extraction [#18](https://github.com/banteg/takopi/pull/18)
- preserve markdown formatting and resume footer when trimming long responses [#20](https://github.com/banteg/takopi/pull/20)

## v0.3.0 (2026-01-01)

### changes

- add a claude code runner via the `claude` cli with stream-json parsing and resume support [#9](https://github.com/banteg/takopi/pull/9)
- auto-discover engine backends and generate cli subcommands from the registry [#12](https://github.com/banteg/takopi/pull/12)
- add `BaseRunner` session locking plus a `JsonlSubprocessRunner` helper for jsonl subprocess engines
- add jsonl stream parsing and subprocess helpers for runners
- lazily allocate per-session locks and streamline backend setup/install metadata
- improve startup message formatting and markdown rendering
- add a debug onboarding helper for setup troubleshooting

### breaking

- runner implementations must define explicit resume parsing/formatting (no implicit standard resume pattern)

### fixes

- stop leaking a hidden `engine-id` cli option on engine subcommands

### docs

- add a runner guide plus claude code docs (runner, events, stream-json cheatsheet)
- clarify the claude runner file layout and add guidance for jsonl-based runners
- document "minimal" runner mode: started+completed only, completed-only actions allowed

## v0.2.0 (2025-12-31)

### changes

- introduce runner protocol for multi-engine support [#7](https://github.com/banteg/takopi/pull/7)
  - normalized event model (`started`, `action`, `completed`)
  - actions with stable ids, lifecycle phases, and structured details
  - engine-agnostic bridge and renderer
- add `/cancel` command with progress message targeting [#4](https://github.com/banteg/takopi/pull/4)
- migrate async runtime from asyncio to anyio [#6](https://github.com/banteg/takopi/pull/6)
- stream runner events via async iterators (natural backpressure)
- per-thread job queues with serialization for same-thread runs
- render resume as `codex resume <token>` command lines
- various rendering improvements including file edits

### breaking

- require python 3.14+
- remove `--profile` flag; configure via `[codex].profile` only

### fixes

- serialize new sessions once resume token is known
- preserve resume tokens in error renders [#3](https://github.com/banteg/takopi/pull/3)
- preserve file-change paths in action events [#2](https://github.com/banteg/takopi/pull/2)
- terminate codex process groups on cancel (posix)
- correct resume command matching in bridge

## v0.1.0 (2025-12-29)

### features

- telegram bot bridge for openai codex cli via `codex exec`
- stateless session resume via `` `codex resume <token>` `` lines
- real-time progress updates with ~2s throttling
- full markdown rendering with telegram entities (markdown-it-py + sulguk)
- per-session serialization to prevent race conditions
- interactive onboarding guide for first-time setup
- codex profile configuration
- automatic telegram token redaction in logs
- cli options: `--debug`, `--final-notify`, `--version`
