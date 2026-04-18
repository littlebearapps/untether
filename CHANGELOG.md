# changelog

## v0.35.2 (unreleased)

### changes

- **feat:** restart-required vs hot-reloadable settings are now structurally surfaced. `TelegramTransportSettings.RESTART_REQUIRED_FIELDS` (new `ClassVar[frozenset[str]]` in `src/untether/settings.py`) is the single source of truth for which transport fields need a process restart (`bot_token`, `chat_id`, `session_mode`, `topics`, `message_overflow`); `telegram/loop.py:handle_reload()` now consumes that ClassVar instead of the previously-inlined `RESTART_ONLY_KEYS` set. When a restart-required key changes during hot-reload, the bot now posts a 🔄 notice ("Setting `X` changed — restart required to take effect; run: `systemctl --user restart untether`") in addition to the existing `config.reload.transport_config_changed` structlog warning, so the operator doesn't silently run on stale values. `docs/reference/config.md` gains a comprehensive "Hot-reload vs restart-required" section and per-field 🔄 markers in the transport / topics tables [#318](https://github.com/littlebearapps/untether/issues/318)
  - follow-up: `_notify_restart_required` broadcasts to every `runtime.project_chat_ids()` plus any `allowed_user_ids` admin DM instead of a single `cfg.chat_id` send — in project-routed deployments `cfg.chat_id` is the placeholder sentinel and every send failed with `chat not found`, so the user-visible warning never arrived. Per-chat failures are logged via `config.reload.restart_notify.failed` and skipped; `config.reload.restart_notify.sent` emits `targets` + `sent_count` for observability. Falls back to `cfg.chat_id` only when no routed targets exist.
- **feat:** `[[triggers.crons]]` now accepts an optional `permission_mode` field (`default` | `plan` | `auto` | `acceptEdits` | `bypassPermissions`) that overrides the chat / engine default for that cron's run only. Crons firing into plan-mode chats can now declare themselves autonomous via `permission_mode = "auto"` without flipping the whole chat to auto. Precedence: cron `permission_mode` > per-chat `/planmode` > engine config default. Claude-only for this release; Codex + Gemini completion is tracked in [#331](https://github.com/littlebearapps/untether/issues/331), and the broader all-engines + webhooks extension in [#332](https://github.com/littlebearapps/untether/issues/332) (v0.35.5). New `VALID_PERMISSION_MODES_BY_ENGINE` dict in `runners/run_options.py` lets the `CronConfig` validator reject typos for engines with known value sets while staying forward-compatible for engines whose permission wiring is pending. A new `trigger.cron.permission_mode_override` structlog INFO entry fires when the override actually changes the resolved value, for staging observability. [#330](https://github.com/littlebearapps/untether/issues/330)
- callback-answer instrumentation for inline-keyboard presses — every `answerCallbackQuery` now emits a `callback.answered` INFO event with `latency_ms` (HTTP round-trip), `total_ms` (since dispatcher entry), `early=true|false`, and `has_toast`. Lets staging greps distinguish "we were fast, Telegram was slow" from "we were slow" when `BotResponseTimeoutError` is reported client-side. Investigation of the existing `answer_early` path confirmed it already fires before any `backend.handle()` work; added a regression test (`test_early_answer_fires_before_slow_handle`) locking the ordering invariant in so future refactors can't reintroduce the timeout window. Telegram-transport reference docs gained a callback-answering section with the structured-log schema and triage guidance [#247](https://github.com/littlebearapps/untether/issues/247)

### fixes

- **security:** Claude and Pi engine subprocesses no longer inherit the parent's full environment — only allowlisted variables (basic OS essentials, AI/cloud provider keys, Claude/MCP namespaces, Node/Python/UV/NPM runtime vars) pass through via the new `utils/env_policy.filtered_env()` helper. Random third-party tokens that happen to live in the parent env (AWS, Stripe, DigitalOcean, DATABASE_URL, personal app tokens, etc.) are no longer available to engine subprocesses or their MCP servers — reduces the blast radius of any tool-call or MCP that exfiltrates process env. PR #323's four `setdefault` reinforcements for the stuck-after-tool_result watchdog are preserved on top of the filtered env. Other engines (Codex, Gemini, OpenCode, AMP) keep the default inherit-everything behaviour for this release; extending to them is tracked as part of [#332](https://github.com/littlebearapps/untether/issues/332) (v0.35.5). Adding a new engine or MCP that relies on an unfamiliar variable is documented at the top of `utils/env_policy.py` [#198](https://github.com/littlebearapps/untether/issues/198)
- **security:** CI matrix values (`matrix.command`, `matrix.sync_args`) now pass through `env:` instead of direct `${{ }}` interpolation in `run:` blocks, eliminating a theoretical shell-injection vector should matrix values ever become dynamic (e.g. from PR labels) [#195](https://github.com/littlebearapps/untether/issues/195)
- **security:** `bot_token` is now `pydantic.SecretStr` in `TelegramTransportSettings` — masks the value in `repr()`, `str()`, tracebacks, and any accidental structlog serialisation. Raw value is unwrapped via `.get_secret_value()` at the transport boundary (`require_telegram`, `backend.lock_token`/`build_and_run`, `cli/doctor`, `cli/onboarding_cmd`). A field_validator preserves the pre-change NonEmptyStr contract (whitespace-only tokens still rejected, since SecretStr bypasses `str_strip_whitespace`) [#196](https://github.com/littlebearapps/untether/issues/196)
- **security:** `_HANDLED_REQUESTS` in `runners/claude.py` switched from a `set` cleared wholesale at 100 entries to an LRU `OrderedDict` (max 200, oldest-first eviction) — closes the small window where a duplicate Telegram callback delivered just after a `.clear()` would be misclassified as "request not found" rather than "duplicate" [#197](https://github.com/littlebearapps/untether/issues/197)
- **security:** Codex auth subprocess output is now `html.escape()`'d before being wrapped in `<pre>` in the HTML-mode Telegram reply — prevents a crafted error message from injecting Telegram entities (`<b>`, `<a>`, etc) into the rendered response [#199](https://github.com/littlebearapps/untether/issues/199)
- **security:** voice transcription error paths (`telegram/voice.py`) and command-dispatch error paths (`telegram/commands/dispatch.py`) now send sanitised text via shared `utils/error_display.user_safe_error()` — strips URLs and absolute paths, caps length, and falls back when sanitised text is empty. Full exception detail still goes to structlog [#200](https://github.com/littlebearapps/untether/issues/200) [#201](https://github.com/littlebearapps/untether/issues/201)
- **security:** removed global bandit skips for B603/B607 in `pyproject.toml`; the three remaining subprocess sites (`telegram/backend.py:_detect_cli_version`, `telegram/commands/usage.py` macOS Keychain lookup, `utils/git.py:_run_git`) are annotated inline with `# nosec` + per-site justification — CI now flags any NEW subprocess call site by default [#202](https://github.com/littlebearapps/untether/issues/202)
- **security:** `_EPHEMERAL_MSGS` and `_OUTLINE_REGISTRY` in `runner_bridge.py` gain companion timestamp maps and a `sweep_stale_registries()` helper that prunes entries older than 1 hour. Sweep piggy-backs on `ProgressEdits._stall_monitor`'s existing 60-second tick — handles runs that crash or exit abnormally without firing the normal cleanup path [#203](https://github.com/littlebearapps/untether/issues/203)
- **security:** `telegram/client_api.py:download_file` validates `file_path` (from Telegram `getFile`) against `://`, `..`, and leading `/` before URL construction — a tampered or spoofed getFile response that returned an attacker-controlled URL as `file_path` could otherwise redirect the subsequent HTTP GET away from `api.telegram.org` [#204](https://github.com/littlebearapps/untether/issues/204)
- engine subprocess cleanup now walks the process tree and signals descendants in separate process groups — previously `os.killpg(proc.pid, SIGTERM)` only reached the parent's direct pgroup, so grandchildren spawned with fresh sessions (Node's `child_process.spawn()` pattern, used by `workerd` via `@cloudflare/vitest-pool-workers`) survived a SIGTERM'd Claude Code session. On lba-1 this orphaned 316 `workerd` processes consuming 37 GB of RAM after 6 cascading Claude Code signal deaths. `_signal_process` now snapshots descendants via `proc_diag.find_descendants()` **before** `killpg` (so `/proc/<pid>/task/*/children` is still readable), runs the existing pgroup kill, then `os.kill(pid, sig)` on each captured PID best-effort (swallowing `ProcessLookupError`/`PermissionError`). SIGKILL escalation walks the tree again. Graceful fallback to legacy pgroup-only behaviour on non-Linux hosts or `/proc` read errors. Related upstream: [anthropics/claude-code#43944](https://github.com/anthropics/claude-code/issues/43944), [cloudflare/workers-sdk#8837](https://github.com/cloudflare/workers-sdk/issues/8837) [#275](https://github.com/littlebearapps/untether/issues/275)
  - `proc_diag._find_descendants` renamed to public `find_descendants` (private alias kept for back-compat with existing test imports)
- webhook server now degrades gracefully when it can't bind its port — previously a port conflict (e.g. another process on the default 9876) crashed the entire bot (polling, commands, crons included) via an uncaught `OSError` propagating through the `anyio` task group, triggering a systemd restart loop. `run_webhook_server` now catches `OSError` from `TCPSite.start()`, logs a structured `triggers.server.bind_failed` event with `host`/`port`/`hint`/`fix` fields, and returns normally so the rest of the bot stays up [#320](https://github.com/littlebearapps/untether/issues/320)
- cost footer accuracy and engine cost parity — 60-second TTL cache on the Claude subscription-usage fetch (`utils/usage_cache.py`) with stale-while-error fallback smooths transient 429s and rate-limit windows; a one-shot `claude_usage.schema_mismatch` warning logs missing expected fields so upstream API drift is noticed instead of silently dropping the footer; `_format_run_cost` now renders zero-turn completions (`if turns is not None:` instead of `if turns:`); Gemini runner extracts `stats.total_cost_usd` into usage when present; AMP `AmpResult` schema gains a `total_cost_usd` field and the runner surfaces it through the usage dict when AMP emits one; added an OpenCode regression test locking in that token counts still render when cost is zero (free-tier runs) [#316](https://github.com/littlebearapps/untether/issues/316)
  - persisting the daily cost accumulator across restarts was part of the issue's "nice-to-have" scope and is deferred to a follow-up to keep this change focused on accuracy + parity
- `run_once = true` crons now persist their fired state to `run_once_fired.json` (sibling of `untether.toml`) — no longer re-fire on config hot-reload or process restart. Previously the TOML entry re-entered the active list on every reload because `remove_cron()` was in-memory only; editing any unrelated config setting would cause every already-fired one-shot to run again. `TriggerManager` now takes an optional `config_path` argument, loads the fired set on init, persists on `remove_cron()`, and auto-cleans fired-state entries whose cron id no longer appears in the TOML so ids can be safely reused. Related: #269 (hot-reload), #294 (master pause toggle) [#317](https://github.com/littlebearapps/untether/issues/317)
- Pi footer now shows the model name when the user relies on the default config model (no `/model set` override and no `pi.model` in `untether.toml`). The Pi CLI's `message_end` event carries `"model": "..."` alongside provider/usage; the runner now extracts this and emits a supplementary `StartedEvent` once per session so `ProgressTracker.note_event` merges it into the tracker meta. Priority preserved: `run_options.model` > `self.model` > JSONL fallback. Completes the work begun in #235 [#225](https://github.com/littlebearapps/untether/issues/225)
  - follow-up: `JsonlSubprocessRunner.handle_started_event` was silently dropping the supplementary `StartedEvent` as a same-session duplicate, so the extracted model never reached `ProgressTracker.note_event`. The filter now emits duplicates through when the event carries `meta`; true duplicates (no meta) are still dropped. Unit tests in `tests/test_runner_utils.py` previously passed because they called `translate_pi_event` directly, bypassing the base-runner filter — added a regression test covering the duplicate-with-meta path.
- detect and recover from Claude Code hanging after an MCP `tool_result` via stream-json / sdk-cli — root cause is upstream [claude-code#39700](https://github.com/anthropics/claude-code/issues/39700) / [#41086](https://github.com/anthropics/claude-code/issues/41086) combined with the undici idle-body timeout in `mcp-remote` ([geelen/mcp-remote#226](https://github.com/geelen/mcp-remote/issues/226), [#107](https://github.com/geelen/mcp-remote/issues/107)) talking to Cloudflare's remote MCP servers. The symptom "MCP tool may be hung: cloudflare-observability" was misleading — the MCP had already returned its result; the engine was silent after ingesting it [#322](https://github.com/littlebearapps/untether/issues/322)
  - new engine-agnostic `_classify_jsonl_event()` in `runner.py` recognises tool_result-equivalent events across all six engines (Claude, Codex, OpenCode, Pi, Gemini, AMP); `JsonlStreamState` gains a `last_tool_result_at` latch cleared only on an assistant-turn event
  - new `ProgressEdits._detect_stuck_after_tool_result()` fires when the latch has been set for ≥ `stuck_after_tool_result_timeout` (default 300 s, matches undici's 5-minute idle-body timeout) with `cpu_active=True`, frozen ring buffer ≥ 3, and no pending approval — ExitPlanMode-, Bash-, and subagent-safe
  - tiered recovery in `ProgressEdits._handle_stuck_after_tool_result()`: Tier 1 logs `progress_edits.stuck_after_tool_result` with diag; Tier 2 SIGTERMs MCP-adapter children whose `/proc/<pid>/cmdline` contains `mcp-remote` or `@modelcontextprotocol` (forces the SSE reader to error out and unblocks the parent engine); Tier 3 cancels via `cancel_event` after `stuck_after_tool_result_recovery_delay` (default 60 s) with a specific Telegram notice
  - `runners/claude.py:env()` now sets `CLAUDE_ENABLE_STREAM_WATCHDOG=1`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS=60000`, `MCP_TOOL_TIMEOUT=120000`, and `MAX_MCP_OUTPUT_TOKENS=12000` via `setdefault` — reduces incidence while the detector is the safety net; user overrides via shell env or `~/.claude/settings.json` still win
  - four new `[watchdog]` config fields: `detect_stuck_after_tool_result` (default `false` for this release, will default `true` once validated), `stuck_after_tool_result_timeout`, `stuck_after_tool_result_recovery_enabled`, `stuck_after_tool_result_recovery_delay`
  - `utils/proc_diag.py:read_cmdline()` helper for identifying adapter children; 17 new tests across engine-matrix classifier, detector gates, and tier-1/2/3 state machine

### docs

- document `[triggers.server]` port-conflict troubleshooting in `docs/reference/triggers/triggers.md` with `ss -tlnp` diagnosis step and the `port = <N>` remediation [#320](https://github.com/littlebearapps/untether/issues/320)

## v0.35.1 (2026-04-15)

### fixes

- diff preview approval gate no longer blocks edits after a plan is approved — the `_discuss_approved` flag now short-circuits diff preview as well as `ExitPlanMode`, so once the user approves a plan outline the next `Edit`/`Write` runs without a second approval prompt [#283](https://github.com/littlebearapps/untether/issues/283)
- `scripts/healthcheck.sh` exits prematurely under `set -e` — `pass()`/`fail()` used `((var++))` which returns the pre-increment value, tripping `set -e` on the first call so only the first check ever ran and the script always exited 1. Also, the error-log count piped journalctl through `grep -c .`, which counted `-- No entries --` meta lines as matches, producing false-positive log-error counts on clean systems. Now uses explicit `var=$((var+1))` assignment and filters meta lines with `grep -vc '^-- '` [#302](https://github.com/littlebearapps/untether/issues/302)

- fix multipart webhooks returning HTTP 500 — `_process_webhook` pre-read the request body for size/auth/rate-limit checks, leaving the stream empty when `_parse_multipart` called `request.multipart()`. Now the multipart reader is constructed from the cached raw body, so multipart uploads work end-to-end; also short-circuits the post-parse raw-body write so the MIME envelope isn't duplicated at `file_path` alongside the extracted file at `file_destination` [#280](https://github.com/littlebearapps/untether/issues/280)
- fix webhook rate limiter never returning 429 — `_process_webhook` awaited the downstream dispatch (Telegram outbox send, `http_forward` network call, etc.) before returning 202, which capped request throughput at the dispatch rate (~1/sec for private Telegram chats) and meant the `TokenBucketLimiter` never saw a real burst. Dispatch is now fire-and-forget with exception logging, so the rate limiter drains the bucket correctly and a burst of 80 requests against `rate_limit = 60` now yields 60 × 202 + 20 × 429 [#281](https://github.com/littlebearapps/untether/issues/281)
- **security:** validate callback query sender in group chats — reject button presses from unauthorised users; prevents malicious group members from approving/denying other users' tool requests [#192](https://github.com/littlebearapps/untether/issues/192)
  - also validate sender on cancel button callback — the cancel handler was routed directly, bypassing the dispatch validation
- **security:** escape release tag name in notify-website CI workflow — use `jq` for proper JSON encoding instead of direct interpolation, preventing JSON injection from crafted tag names [#193](https://github.com/littlebearapps/untether/issues/193)
- **security:** sanitise flag-like prompts in Gemini and AMP runners — prompts starting with `-` are space-prefixed to prevent CLI flag injection; moved `sanitize_prompt()` to base runner class for all engines [#194](https://github.com/littlebearapps/untether/issues/194)
- **security:** redact bot token from structured log URLs — `_redact_event_dict` now strips bot tokens embedded in Telegram API endpoint strings, preventing credential leakage to log files and aggregation systems [#190](https://github.com/littlebearapps/untether/issues/190)
- **security:** cap JSONL line buffer at 10 MB — unbounded `readline()` on engine stdout could consume all available memory if an engine emitted a single very long line (e.g. base64 image in a tool result); now truncates and logs a warning [#191](https://github.com/littlebearapps/untether/issues/191)

- reduce stall warning false positives during Agent subagent work — tree CPU tracking across process descendants, child-aware 15 min threshold when child processes or elevated TCP detected, early diagnostic collection for CPU baseline, total stall warning counter that persists through recovery, improved "Waiting for child processes" notification messages [#264](https://github.com/littlebearapps/untether/issues/264)
- `/ping` uptime now resets on service restart — previously the module-level start time was cached across `/restart` commands; now `reset_uptime()` is called on each service start [#234](https://github.com/littlebearapps/untether/issues/234)
- add 38 missing structlog calls across 13 files — comprehensive logging audit covering auth verification, rate limiting, SSRF validation, codex runner lifecycle, topic state mutations, CLI error paths, and config validation in all engine runners [#299](https://github.com/littlebearapps/untether/issues/299)
- **systemd:** stop Untether being the preferred OOM victim — systemd user services inherit `OOMScoreAdjust=200` and `OOMPolicy=stop` defaults, which made Untether's engine subprocesses preferred earlyoom/kernel OOM killer targets ahead of CLI `claude` (`oom_score_adj=0`) and orphaned grandchildren actually consuming the RAM. `contrib/untether.service` now sets `OOMScoreAdjust=-100` (documents intent; the kernel clamps to the parent baseline for unprivileged users, typically 100) and `OOMPolicy=continue` (a single OOM-killed child no longer tears down the whole unit cgroup, which previously broke every live chat at once). Docs in `docs/reference/dev-instance.md` updated. Existing installs need to copy the unit file and `systemctl --user daemon-reload`; staging picks up the change on the next `scripts/staging.sh install` cycle [#275](https://github.com/littlebearapps/untether/issues/275)

### changes

- **timezone support for cron triggers** — cron schedules can now be evaluated in a specific timezone instead of the server's system time (usually UTC) [#270](https://github.com/littlebearapps/untether/issues/270)
  - per-cron `timezone` field with IANA timezone names (e.g. `"Australia/Melbourne"`)
  - global `default_timezone` in `[triggers]` — per-cron `timezone` overrides it
  - DST-aware via Python's `zoneinfo` module (zero new dependencies)
  - invalid timezone names rejected at config parse time with clear error messages

- **SSRF protection for trigger outbound requests** — shared utility at `triggers/ssrf.py` blocks private/reserved IP ranges, validates URL schemes, and checks DNS resolution to prevent server-side request forgery in upcoming webhook forwarding and cron data-fetch features [#276](https://github.com/littlebearapps/untether/issues/276)
  - blocks loopback, RFC 1918, link-local, CGN, multicast, reserved, IPv6 equivalents, and IPv4-mapped IPv6 bypass
  - DNS resolution validation catches DNS rebinding attacks (hostname → private IP)
  - configurable allowlist for admins who need to hit local services
  - timeout and response-size clamping utilities

- **non-agent webhook actions** — webhooks can now perform lightweight actions without spawning an agent run [#277](https://github.com/littlebearapps/untether/issues/277)
  - `action = "file_write"` — write POST body to disk with atomic writes, path traversal protection, deny-glob enforcement, and on-conflict handling
  - `action = "http_forward"` — forward payload to another URL with SSRF protection, exponential backoff on 5xx, and header template rendering
  - `action = "notify_only"` — send a templated Telegram message with no agent run
  - `notify_on_success` / `notify_on_failure` flags for Telegram visibility on all action types
  - default `action = "agent_run"` preserves full backward compatibility

- **multipart form data support for webhooks** — webhooks can now accept `multipart/form-data` POSTs with file uploads [#278](https://github.com/littlebearapps/untether/issues/278)
  - file parts saved with sanitised filenames, atomic writes, deny-glob and path traversal protection
  - configurable `file_destination` with template variables, `max_file_size_bytes` (default 50 MB)
  - form fields available as template variables alongside file metadata

- **data-fetch cron triggers** — cron triggers can now pull data from external sources before rendering the prompt [#279](https://github.com/littlebearapps/untether/issues/279)
  - `fetch.type = "http_get"` / `"http_post"` — fetch URL with SSRF protection, configurable timeout and headers
  - `fetch.type = "file_read"` — read local file with path traversal protection and deny-globs
  - `fetch.parse_as` — parse response as `json`, `text`, or `lines`
  - fetched data injected into `prompt_template` via `store_as` variable (default `fetch_result`)
  - `on_failure = "abort"` (default) sends failure notification; `"run_with_error"` injects error into prompt
  - all fetched data prefixed with untrusted-data marker

- **hot-reload for trigger configuration** — editing `untether.toml` `[triggers]` applies changes immediately without restarting Untether or killing active runs [#269](https://github.com/littlebearapps/untether/issues/269) ([#285](https://github.com/littlebearapps/untether/pull/285))
  - new `TriggerManager` class holds cron and webhook config; scheduler reads `manager.crons` each tick; webhook server resolves routes per-request via `manager.webhook_for_path()`
  - supports add/remove/modify of crons and webhooks, auth/secret changes, action type, multipart/file settings, cron fetch, and timezones
  - `last_fired` dict preserved across swaps to prevent double-firing within the same minute
  - unauthenticated webhooks logged at `WARNING` on reload (previously only at startup)
  - 13 new tests in `test_trigger_manager.py`; 2038 existing tests still pass

- **hot-reload for Telegram bridge settings** — `voice_transcription`, file transfer, `allowed_user_ids`, `show_resume_line`, and message-timing settings now reload without a restart [#286](https://github.com/littlebearapps/untether/issues/286)
  - `TelegramBridgeConfig` unfrozen (keeps `slots=True`) and gains an `update_from(settings)` method
  - `handle_reload()` now applies changes in-place and refreshes cached loop-state copies; restart-only keys (`bot_token`, `chat_id`, `session_mode`, `topics`, `message_overflow`) still warn with `restart_required=true`
  - `route_update()` reads `cfg.allowed_user_ids` live so allowlist changes take effect on the next message

- **`/at` command for one-shot delayed runs** — schedule a prompt to run between 60s and 24h in the future with `/at 30m Check the build`; accepts `Ns`/`Nm`/`Nh` suffixes [#288](https://github.com/littlebearapps/untether/issues/288)
  - pending delays tracked in-memory (lost on restart — acceptable for one-shot use)
  - `/cancel` drops pending `/at` timers before they fire
  - per-chat cap of 20 pending delays; graceful drain cancels pending scopes on shutdown
  - new module `telegram/at_scheduler.py`; command registered as `at` entry point

- **`run_once` cron flag** — `[[triggers.crons]]` entries can set `run_once = true` to fire once then auto-disable; the cron stays in the TOML and re-activates on the next config reload or restart [#288](https://github.com/littlebearapps/untether/issues/288)

- **trigger visibility improvements (Tier 1)** — surface configured triggers in the Telegram UI [#271](https://github.com/littlebearapps/untether/issues/271)
  - `/ping` in a chat with active triggers appends `⏰ triggers: 1 cron (daily-review, 9:00 AM daily (Melbourne))`
  - trigger-initiated runs show provenance in the meta footer: `🏷 opus 4.6 · plan · ⏰ cron:daily-review`
  - new `describe_cron(schedule, timezone)` utility renders common cron patterns in plain English; falls back to the raw expression for complex schedules
  - `RunContext` gains `trigger_source` field; `ProgressTracker.note_event` merges engine meta over the dispatcher-seeded trigger so it survives
  - `TriggerManager` exposes `crons_for_chat()`, `webhooks_for_chat()`, `cron_ids()`, `webhook_ids()` helpers

- **faster, cleaner restarts (Tier 1)** — restart gap reduced from ~15-30s to ~5s with no lost messages [#287](https://github.com/littlebearapps/untether/issues/287)
  - persist last Telegram `update_id` to `last_update_id.json` and resume polling from the saved offset on startup; Telegram retains undelivered updates for 24h, so the polling gap no longer drops or re-processes messages
  - `Type=notify` systemd integration via stdlib `sd_notify` (`socket.AF_UNIX`, no dependency) — `READY=1` is sent after the first `getUpdates` succeeds, `STOPPING=1` at the start of drain
  - `RestartSec=2` in `contrib/untether.service` (was `10`) — faster restart after drain completes
  - `contrib/untether.service` also adds `NotifyAccess=main`; existing installs must copy the unit file and `systemctl --user daemon-reload`

### docs

- add update and uninstall guides + README transparency section [#305](https://github.com/littlebearapps/untether/issues/305)
  - new `docs/how-to/update.md` and `docs/how-to/uninstall.md` covering pipx, pip, and source installs, plus config/data/systemd cleanup
  - README: "What Untether accesses" section (network, filesystem, process, credentials), update/uninstall one-liners in Quick Start, and cross-links throughout install/how-to pages
- comprehensive v0.35.1 documentation audit — 8 gap fills across 121 files [#306](https://github.com/littlebearapps/untether/issues/306)
  - `group-chat.md`: document callback sender validation in groups (#192)
  - `security.md`: cross-reference button validation, fix misleading SSRF allowlist claim, add bot token auto-redaction tip (#190)
  - `plan-mode.md`: document auto-approval after plan approval (#283)
  - `interactive-approval.md`: admonition linking to plan bypass behaviour
  - `commands-and-directives.md`: `/ping` description now mentions uptime reset and trigger summary (#234)
  - `runners/amp/runner.md`: add `sanitize_prompt()` note matching Pi/Gemini runners (#194)
  - `troubleshooting.md`: document 10 MB engine output line cap (#191)
  - `glossary.md`: add delayed run, webhook action, and hot-reload entries

## v0.35.0 (2026-03-31)

### fixes

- render plan outline as formatted text instead of raw markdown — outline messages now use `render_markdown()` + `split_markdown_body()` so headings, bold, code, and lists display properly in Telegram [#139](https://github.com/littlebearapps/untether/issues/139)
- add approve/deny buttons to the last outline message — users no longer need to scroll back up past long outlines to find the buttons [#140](https://github.com/littlebearapps/untether/issues/140)
- delete outline messages on approve/deny — outline and notification messages are cleaned up immediately via module-level `_OUTLINE_REGISTRY`, and stale approval keyboard on the progress message is suppressed [#141](https://github.com/littlebearapps/untether/issues/141)
- scope AskUserQuestion pending requests by channel_id — `_PENDING_ASK_REQUESTS` and `_ASK_QUESTION_FLOWS` were global dicts with no chat scoping; a pending ask in one chat would steal the next message from any other chat, causing cross-chat contamination and lost messages [#144](https://github.com/littlebearapps/untether/issues/144)
  - added `channel_id` contextvar (`get_run_channel_id`/`set_run_channel_id`) to `utils/paths.py`
  - `get_pending_ask_request()` and `get_ask_question_flow()` now accept `channel_id` and filter by it
  - session cleanup now also clears stale pending asks and flows
- standalone override commands (`/planmode`, `/model`, `/reasoning`) now preserve all `EngineOverrides` fields instead of resetting unrelated overrides [#124](https://github.com/littlebearapps/untether/issues/124)
- register input for system-level auto-approved control requests (Initialize, HookCallback, McpMessage, RewindFiles, Interrupt) so `updatedInput` is included in the response — prevents ZodError in Claude Code [#123](https://github.com/littlebearapps/untether/issues/123)
- reduce Telegram API default timeout from 120s to 30s — a single ReadTimeout on `editMessageText` could make the bot appear unresponsive for up to 2 minutes; `getUpdates` long-poll now uses a dedicated timeout of `timeout_s + 20` so network failures are detected faster [#145](https://github.com/littlebearapps/untether/issues/145)
- OpenCode error runs now show the error message instead of an empty body — `CompletedEvent.answer` falls back to `state.last_tool_error` when no prior `Text` events were emitted; covers both `StepFinish` and `stream_end_events` paths [#146](https://github.com/littlebearapps/untether/issues/146), [#150](https://github.com/littlebearapps/untether/issues/150)
- Pi `/continue` now captures the session ID from `SessionHeader` — `allow_id_promotion` was `False` for continue runs, preventing the resume token from being populated [#147](https://github.com/littlebearapps/untether/issues/147)
- post-outline approval no longer fails with "message to be replied not found" — the "Approve Plan" button on outline messages uses the real ExitPlanMode `request_id`, so the regular approve path now sets `skip_reply=True` when outline messages were just deleted; also suppresses the redundant push notification after outline cleanup [#148](https://github.com/littlebearapps/untether/issues/148)
- sanitise `text_link` entities with invalid URLs before sending to Telegram — localhost, loopback, file paths, and bare hostnames are converted to `code` entities instead, preventing silent 400 errors that drop the entire final message [#157](https://github.com/littlebearapps/untether/issues/157)
- fix duplicate approval buttons after "Pause & Outline Plan" — both the progress message and outline message showed approve/deny buttons simultaneously; now only the outline message has approval buttons (with Cancel), progress keeps cancel-only; outline state resets properly for future ExitPlanMode requests [#163](https://github.com/littlebearapps/untether/issues/163)
- hold ExitPlanMode request open after outline so post-outline Approve/Deny buttons persist — instead of auto-denying (which caused Claude to exit ~7s later), the control request is never responded to, keeping Claude alive while the user reads the outline [#114](https://github.com/littlebearapps/untether/issues/114), [#117](https://github.com/littlebearapps/untether/issues/117)
  - buttons use real `request_id` from `pending_control_requests` for direct callback routing
  - 5-minute safety timeout cleans up stale held requests
- suppress stall auto-cancel when CPU is active — extended thinking phases produce no JSONL events but the process is alive and busy; `is_cpu_active()` check prevents false-positive kills [#114](https://github.com/littlebearapps/untether/issues/114)
- fix stall notification suppression when main process sleeping — CPU-active suppression now checks `process_state`; when main process is sleeping (state=S) but children are CPU-active (hung Bash tool), notifications fire instead of being suppressed; stall message now shows tool name ("Bash tool may be stuck") instead of generic "session may be stuck" [#168](https://github.com/littlebearapps/untether/issues/168)
- suppress redundant cost footer on error runs — diagnostic context line already contains cost data, footer no longer duplicates it [#120](https://github.com/littlebearapps/untether/issues/120)
- clarify /config default labels and remove redundant "Works with" lines [#119](https://github.com/littlebearapps/untether/issues/119)
- Codex: always pass `--ask-for-approval` in headless mode — default to `never` (auto-approve all) so Codex never blocks on terminal input; `safe` permission mode still uses `untrusted` [#184](https://github.com/littlebearapps/untether/issues/184)
- OpenCode: surface unsupported JSONL event types as visible Telegram warnings instead of silently dropping them — prevents silent 5-minute hangs when OpenCode emits new event types (e.g. `question`, `permission`) [#183](https://github.com/littlebearapps/untether/issues/183)
- stall warnings now succinct and accurate for long-running tools — truncate "Last:" to 80 chars, recognise `command:` prefix (Bash tools), reassuring "still running" message when CPU active, drop PID diagnostics from Telegram messages, only say "may be stuck" when genuinely stuck [#188](https://github.com/littlebearapps/untether/issues/188)
  - frozen ring buffer escalation now uses tool-aware "still running" message when a known tool is actively running (main sleeping, CPU active on children), instead of alarming "No progress" message
- OpenCode model name missing from footer when using default model — `build_runner()` now reads `~/.config/opencode/opencode.json` to detect the configured default model so the `🏷` footer always shows the model (e.g. `openai/gpt-5.2`) even without an `untether.toml` override [#221](https://github.com/littlebearapps/untether/issues/221)
- OpenCode model override hint — `/config` and engine model sub-page now show `provider/model (e.g. openai/gpt-4o)` instead of the unhelpful "from provider config", guiding users to use the required provider-prefixed format [#220](https://github.com/littlebearapps/untether/issues/220)
- Codex footer missing model name — Codex runner always includes model in `StartedEvent.meta` so the footer shows the model even when no override is set [#217](https://github.com/littlebearapps/untether/issues/217)
- `/planmode` command worked in non-Claude engine chats — now gated to Claude-only with a helpful message; Codex/Gemini users are directed to `/config` → Approval policy [#216](https://github.com/littlebearapps/untether/issues/216)
- `/usage` showed Claude subscription data in non-Claude engine chats — now gated to subscription-supported engines with an engine-specific error message [#215](https://github.com/littlebearapps/untether/issues/215)
- `/export` showed duplicate "Session Started" headers for resumed sessions — deduplicated so only the first `StartedEvent` renders [#218](https://github.com/littlebearapps/untether/issues/218)
- Gemini CLI prompt injection — prompts starting with `-` were parsed as flags when passed via `-p <value>`; now uses `--prompt=<value>` to bind the value directly [#219](https://github.com/littlebearapps/untether/issues/219)
- `/new` command now cancels running processes before clearing sessions — previously only cleared resume tokens, leaving old Claude/Codex/OpenCode processes running (~400 MB each), worsening memory pressure and triggering earlyoom kills [#222](https://github.com/littlebearapps/untether/issues/222)
- auto-continue no longer triggers on signal deaths (rc=143/SIGTERM, rc=137/SIGKILL) — earlyoom kills have `last_event_type=user` which matched the upstream bug detection, causing a death spiral where 4 killed sessions were immediately respawned into the same memory pressure [#222](https://github.com/littlebearapps/untether/issues/222)
- `/new` command triggers engine run instead of clearing sessions when `topics.enabled=false` — `/new` was only handled in `_dispatch_builtin_command` when topics were enabled; moved `/new` out of the `topics.enabled` gate to handle all modes (topic, chat session, stateless), mirroring how `/ctx` already works; also removed unreachable early routing code [#236](https://github.com/littlebearapps/untether/issues/236)
- Gemini engine stuck at "starting · 0s" — Gemini CLI outputs a non-JSON warning (`MCP issues detected...`) on stdout before the first JSONL event, corrupting the line; `decode_jsonl()` now strips non-JSON prefixes by finding the first `{` and retrying parse [#231](https://github.com/littlebearapps/untether/issues/231)
- `/config` Ask mode toggle inverted — `_toggle_row` default was `False` but display default was "on", causing the button to show "Ask: off" when the effective state was on; pressing it appeared to do nothing [#232](https://github.com/littlebearapps/untether/issues/232)
- diff preview approval buttons not rendered after outline flow — `_outline_sent` flag in `ProgressEdits` stripped ALL subsequent approval buttons, not just outline-related ones; now only strips buttons for `DiscussApproval` actions [#233](https://github.com/littlebearapps/untether/issues/233)
- prevent duplicate control response for already-handled requests [#229](https://github.com/littlebearapps/untether/issues/229) ([#230](https://github.com/littlebearapps/untether/issues/230))
- fix `render_markdown` entity overflow when text ends with a fenced code block — entity offsets now clamped to the UTF-16 text length after trailing newline stripping, preventing Telegram 400 errors [#59](https://github.com/littlebearapps/untether/issues/59)
- `/config` now reflects project-level `default_engine` — previously showed Claude-specific buttons (Plan mode, Ask mode, etc.) for chats routed to Codex/Pi via project config [#60](https://github.com/littlebearapps/untether/issues/60)
- non-Claude runners (Codex, Pi) now populate model name in `StartedEvent.meta` — footer previously showed permission mode only (e.g. `🏷 plan`) without the model [#62](https://github.com/littlebearapps/untether/issues/62)
- fix liveness watchdog false positive auto-cancel on long-running sessions — actively working sessions with CPU activity and TCP connections were being killed during extended thinking/processing phases [#115](https://github.com/littlebearapps/untether/issues/115)
- fix reply-to resume when emoji prefix is present — the `↩️` prefix on resume footer lines broke all 6 engine regexes; `extract_resume()` now strips emoji prefixes before matching [#134](https://github.com/littlebearapps/untether/issues/134)
- `/config` sub-pages now show resolved on/off values instead of "default" — body text now matches the toggle button state using `_resolve_default()`, removing the confusing mismatch [#152](https://github.com/littlebearapps/untether/issues/152)
- expired control requests now auto-denied after 5-minute timeout — previously the timeout cleanup removed local tracking but did not send a deny response, leaving the Claude subprocess blocked indefinitely on stdin [#32](https://github.com/littlebearapps/untether/issues/32)
- `/export` no longer returns sessions from wrong chat — session recording was not scoped by channel_id, so `/export` in one chat could return another engine's session data [#33](https://github.com/littlebearapps/untether/issues/33)
- fix `KillMode=control-group` bypassing drain and causing 150s restart delay — `contrib/untether.service` now uses `KillMode=mixed` which sends SIGTERM to the main process first (drain works), then SIGKILL to remaining cgroup processes (orphaned MCP servers, containers cleaned up instantly) [#166](https://github.com/littlebearapps/untether/issues/166)
  - `process`: orphaned children survive across restarts, accumulating memory (#88)
  - `control-group`: kills all processes simultaneously, bypassing drain (#166)
  - `mixed`: best of both — graceful drain then forced cleanup
- AMP CLI `-x` flag regression — double-dash separator in `build_args()` caused AMP to interpret `-x` as a subcommand name instead of a flag, breaking execute mode for all prompts [#245](https://github.com/littlebearapps/untether/issues/245)

### docs

- update integration test chat IDs from stale `ut-dev:` to current `ut-dev-hf:` chats [#238](https://github.com/littlebearapps/untether/issues/238)
- investigation: orphaned `workerd` processes from Bash tool children are upstream Claude Code bug — Untether's process group cleanup is correct; Claude Code spawns Bash tool shells in their own session group which Untether cannot reach; no TTY/SIGHUP cascade in headless mode [#257](https://github.com/littlebearapps/untether/issues/257)

### changes

- logging audit: fill gaps in structlog coverage — elevate settings loader failures from DEBUG to WARNING (footer, watchdog, auto-continue, preamble), add access control drop logging, add executor `handle.engine_resolved` info log, elevate outline cleanup failures to WARNING, add credential redaction for OpenAI/GitHub API keys, add file transfer success logging, bind `session_id` in structlog context vars, add media group/cost tracker/cancel debug logging [#254](https://github.com/littlebearapps/untether/issues/254)
- CI: expand ruff lint rules from 7 to 18 — add ASYNC, LOG, I (isort), PT, RET, RUF (full), FURB, PIE, FLY, FA, ISC rule sets; auto-fix 42 import sorts, clean 73 stale noqa directives, fix unused vars and useless conditionals; per-file ignores for test-specific patterns [#255](https://github.com/littlebearapps/untether/issues/255)
- Gemini: default to `--approval-mode yolo` (full access) when no override is set — headless mode has no interactive approval path, so the CLI's read-only default disabled write tools entirely, causing multi-minute stalls as Gemini cascaded through sub-agents [#244](https://github.com/littlebearapps/untether/issues/244), [#248](https://github.com/littlebearapps/untether/issues/248)
- expand error hints coverage — add model not found, context length exceeded, authentication, content safety, CLI not installed, SSL/TLS, invalid request, disk/permission, AMP-specific auth, Gemini result status, and account suspension error categories [#246](https://github.com/littlebearapps/untether/issues/246)
- `/continue` command — cross-environment resume; pick up the most recent CLI session from Telegram using each engine's native continue flag (`--continue`, `resume --last`, `--resume latest`); supported for Claude, Codex, OpenCode, Pi, Gemini (not AMP) [#135](https://github.com/littlebearapps/untether/issues/135)
  - `ResumeToken` extended with `is_continue: bool = False`
  - all 6 runners' `build_args()` updated to handle continue tokens
  - `/continue` handled as reserved command in Telegram loop
  - new how-to guide: `docs/how-to/cross-environment-resume.md`
- `/config` UX overhaul — 2-column toggle pattern replaces all 3-button rows with single `[✓ Feature: on]` toggle + `[Clear]` for better mobile tap targets; merged Engine + Model into single page; max 2 buttons per row on home page; plan mode 2+1 split layout [#132](https://github.com/littlebearapps/untether/issues/132)
- resume line toggle — per-chat `show_resume_line` override via `/config` settings; configurable via EngineOverrides [#128](https://github.com/littlebearapps/untether/issues/128)
- cost budget settings — per-chat `budget_enabled` and `budget_auto_cancel` overrides on Cost & Usage page in `/config` [#129](https://github.com/littlebearapps/untether/issues/129)
- model metadata improvements — shorten model display names in footer: `claude-opus-4-6[1m]` → `opus 4.6 (1M)`, `auto-gemini-3` → `gemini-3`; all engines populate model info from `StartedEvent.meta` [#132](https://github.com/littlebearapps/untether/issues/132)
- resume line formatting — visual separation with blank line and `↩️` prefix in final message footer [#127](https://github.com/littlebearapps/untether/issues/127)
- agent-initiated file delivery — agents write files to `.untether-outbox/` during a run; Untether sends them as Telegram documents on completion with `📎 filename (size)` captions; flat scan, deny-glob security, size limits, auto-cleanup [#143](https://github.com/littlebearapps/untether/issues/143)
  - new module `telegram/outbox_delivery.py` with `scan_outbox()`, `cleanup_outbox()`, `deliver_outbox_files()`
  - `ExecBridgeConfig` gains `send_file` callback + `outbox_config` (transport-agnostic)
  - preamble updated with outbox instructions for all 6 engines
  - config: `outbox_enabled`, `outbox_dir`, `outbox_max_files`, `outbox_cleanup` in `[transports.telegram.files]`
- orphan progress message cleanup on restart — active progress messages are persisted to `active_progress.json`; on startup, orphan messages from a prior instance are edited to show "⚠️ interrupted by restart" with no keyboard [#149](https://github.com/littlebearapps/untether/issues/149)
  - new module `telegram/progress_persistence.py` with `register_progress()`, `unregister_progress()`, `load_active_progress()`, `clear_all_progress()`
  - `runner_bridge.py` registers on progress send, unregisters on ephemeral cleanup
  - `telegram/loop.py` cleans up orphans before sending startup message
- expand pre-run permission policies for Codex CLI and Gemini CLI in `/config` [#131](https://github.com/littlebearapps/untether/issues/131)
  - Codex: new "Approval policy" page — full auto (default) or safe (`--ask-for-approval untrusted`)
  - Gemini: expanded approval mode from 2 to 3 tiers — read-only, edit files (`--approval-mode auto_edit`), full access
  - both engines show "Agent controls" section on `/config` home page with engine-specific labels
- suppress stall Telegram notifications when CPU-active; heartbeat re-render keeps elapsed time counter ticking during extended thinking phases [#121](https://github.com/littlebearapps/untether/issues/121)
- temporary debug logging for hold-open callback routing — will be removed after dogfooding confirms [#118](https://github.com/littlebearapps/untether/issues/118) is resolved
- auto-continue mitigation for Claude Code bug — when Claude Code exits after receiving tool results without processing them (bugs [#34142](https://github.com/anthropics/claude-code/issues/34142), [#30333](https://github.com/anthropics/claude-code/issues/30333)), Untether detects via `last_event_type=user` and auto-resumes the session [#167](https://github.com/littlebearapps/untether/issues/167)
  - `AutoContinueSettings` with `enabled` (default true) and `max_retries` (default 1) in `[auto_continue]` config section
  - detection based on protocol invariant: normal sessions always end with `last_event_type=result`
  - sends "⚠️ Auto-continuing — Claude stopped before processing tool results" notification before resuming
- emoji button labels and edit-in-place for outline approval — ExitPlanMode buttons now show ✅/❌/📋 emoji prefixes; post-outline "Approve Plan"/"Deny" edits the "Asked Claude Code to outline the plan" message in-place instead of creating a second message [#186](https://github.com/littlebearapps/untether/issues/186)
- redesign startup message layout — version in parentheses, split engine info into "default engine" and "installed engines" lines, italic subheadings, renamed "projects" to "directories" (matching `dir:` footer label), added bug report link [#187](https://github.com/littlebearapps/untether/issues/187)
- show token usage counts for non-Claude engines — completion footer now displays `💰 26.0k in / 71 out` for Codex, OpenCode, Pi, Gemini, and Amp when token data is available [#36](https://github.com/littlebearapps/untether/issues/36)
- include CLI versions in startup diagnostics — startup message now shows detected engine CLI versions for easier debugging of outdated or mismatched tools [#38](https://github.com/littlebearapps/untether/issues/38)

### tests

- 8 new outline UX tests: markdown rendering with entities, approval keyboard on last chunk, multi-chunk keyboard placement, ref tracking, deletion on approval transition, deletion on keyboard change, safety-net cleanup, no double-deletion [#139](https://github.com/littlebearapps/untether/issues/139), [#140](https://github.com/littlebearapps/untether/issues/140), [#141](https://github.com/littlebearapps/untether/issues/141)
- 22 new outbox delivery tests: scan (empty, single, sorted, max_files, deny globs, size limit, empty file, symlink, subdir), cleanup (delete, keep unsent, already gone), delivery (send, cleanup, no-cleanup, empty, send failure), integration (after completion, disabled, error run) [#143](https://github.com/littlebearapps/untether/issues/143)
- 4 new cross-chat ask isolation tests: pending ask scoped by channel, correct channel returned, flow scoped by channel, translate registers with channel_id [#144](https://github.com/littlebearapps/untether/issues/144)
- 99 new `/continue` tests: 46 auto-router assertions (continue token handling, engine routing) + 53 build-args assertions (continue flags for all 6 engines) [#135](https://github.com/littlebearapps/untether/issues/135)
- 195 `/config` tests covering home page, all sub-pages, toggle actions, callback routing, button layout, engine-aware visibility [#132](https://github.com/littlebearapps/untether/issues/132)
- 7 new OpenCode error message tests: Error event with no prior text, process_error_events, stream_end_events, last_tool_error fallback on StepFinish, last_text takes priority over tool error, tool error status captures last_tool_error, stream_end_events fallback [#146](https://github.com/littlebearapps/untether/issues/146), [#150](https://github.com/littlebearapps/untether/issues/150)
- 3 new Pi /continue tests: allow_id_promotion flag, session ID promotion from SessionHeader, normal resume no promotion [#147](https://github.com/littlebearapps/untether/issues/147)
- 3 new timeout tests: default 30s timeout, getUpdates per-request timeout, sendMessage uses default [#145](https://github.com/littlebearapps/untether/issues/145)
- 3 new discuss-approval skip_reply tests: approve and deny results set skip_reply=True, dispatch callback skip_reply sends without reply_to [#148](https://github.com/littlebearapps/untether/issues/148)
- 8 new progress persistence tests: register/load roundtrip, unregister, missing file, corrupt file, non-dict, multiple entries, clear all, clear nonexistent [#149](https://github.com/littlebearapps/untether/issues/149)
- 2 new dual-button tests: outline strips approval from progress, outline state resets on approval disappear [#163](https://github.com/littlebearapps/untether/issues/163)
- hold-open outline flow: new tests for hold-open path, real request_id buttons, pending cleanup, approval routing [#114](https://github.com/littlebearapps/untether/issues/114)
- stall suppression: tests for CPU-active auto-cancel, notification suppression when cpu_active=True, notification fires when cpu_active=False [#114](https://github.com/littlebearapps/untether/issues/114), [#121](https://github.com/littlebearapps/untether/issues/121)
- cost footer: tests for suppression on error runs, display on success runs [#120](https://github.com/littlebearapps/untether/issues/120)
- 10 new auto-continue tests: detection function (bug scenario, non-claude engine, cancelled session, normal result, no resume, max retries) + settings validation (defaults, bounds) [#167](https://github.com/littlebearapps/untether/issues/167)
- 2 new stall sleeping-process tests: notification not suppressed when main process sleeping (state=S), stall message includes tool name [#168](https://github.com/littlebearapps/untether/issues/168)
- 8 new `_read_opencode_default_model` tests: valid config, missing file, invalid JSON, empty model, no model key, build_runner fallback, untether config priority, no OC config [#221](https://github.com/littlebearapps/untether/issues/221)
- engine command gate tests: `/planmode` Claude-only, `/usage` subscription-engine-only [#215](https://github.com/littlebearapps/untether/issues/215), [#216](https://github.com/littlebearapps/untether/issues/216)
- export dedup test: duplicate started events deduplicated in markdown export [#218](https://github.com/littlebearapps/untether/issues/218)
- Gemini `--prompt=` build_args test [#219](https://github.com/littlebearapps/untether/issues/219)
- Gemini integration test stall diagnosed — root cause was missing `--approval-mode yolo` in test chat config; Gemini CLI defaults to read-only mode with write tools disabled; set full access via `/config` for `ut-dev-hf: gemini` test chat; U1 now passes in 56s (was 8–18 min stall) [#244](https://github.com/littlebearapps/untether/issues/244)
- 10 new `/new` cancellation tests: `_cancel_chat_tasks` helper (None, empty, matching, other chats, already cancelled, multiple), chat `/new` with running task, cancel-only no sessions, no tasks no sessions, topic `/new` with running task [#222](https://github.com/littlebearapps/untether/issues/222)
- 12 new auto-continue signal death tests: `_is_signal_death` (SIGTERM, SIGKILL, negative, normal, None), `_should_auto_continue` (rc=143, rc=137, rc=-9, rc=-15 blocked; rc=0, rc=None, rc=1 allowed), `proc_returncode` default on `JsonlStreamState` [#222](https://github.com/littlebearapps/untether/issues/222)

### docs

- document OpenCode lack of auto-compaction as a known limitation — long sessions accumulate unbounded context with no automatic trimming; added to runner docs and integration testing playbook [#150](https://github.com/littlebearapps/untether/issues/150)

## v0.34.4 (2026-03-09)

### fixes

- preamble hook awareness: add constraint to default preamble instructing Claude that if hooks fire at session end, the final response must still contain the user's requested content — hook concerns are secondary and should be noted after main content, never instead of it [#107](https://github.com/littlebearapps/untether/issues/107)
  - addresses content displacement when Claude Code plugin Stop hooks (e.g. PitchDocs context-guard) consume the final Telegram message with meta-commentary instead of user-requested content
- `UNTETHER_SESSION` env var: Claude runner now sets `UNTETHER_SESSION=1` in subprocess environment, enabling Claude Code hooks to detect Untether sessions and adjust behaviour (e.g. PitchDocs context-guard skips blocking Stop hooks in Telegram) [#107](https://github.com/littlebearapps/untether/issues/107)

### docs

- audit: PitchDocs context-guard interference analysis — root cause (false positive from `git status --porcelain` on untracked hook infrastructure), cross-project comparison (BIP/Scout/Brand Copilot/littlebearapps.com), recommendations for both Untether and PitchDocs [#107](https://github.com/littlebearapps/untether/issues/107)

## v0.34.3 (2026-03-08)

### fixes

- tool-aware stall threshold: 10-minute threshold (`_STALL_THRESHOLD_TOOL = 600s`) when a tool action is started but not completed, preventing false stall warnings during long-running Bash commands, Agent tasks, and TaskOutput waits [#105](https://github.com/littlebearapps/untether/issues/105)
  - three-tier system: normal (5 min), running tool (10 min), pending approval (30 min)
  - `_has_running_tool()` checks most recent action state
  - stall threshold selection logged at info level with reason
- progress message edit failure: log warning and fall back to sending a new message when the initial "queued" → "starting" edit fails, preventing stuck "queued" messages [#103](https://github.com/littlebearapps/untether/issues/103)
- approval keyboard edit failure: use `wait=True` for keyboard transitions (approval buttons appearing), log keyboard attach at info level and edit failures at warning level for diagnostics [#104](https://github.com/littlebearapps/untether/issues/104)
  - `transport.edit.failed` warning in `TelegramTransport.edit()` when `wait=True` edit returns `None`
  - `progress_edits.keyboard_attach` info log on keyboard transitions
  - `progress_edits.keyboard_edit_failed` warning when keyboard edit fails
  - transport errors upgraded from debug to warning level
- `/usage` 429 rate limit: downgrade from error to warning level, preventing untether-issue-watcher noise for transient rate limits [#89](https://github.com/littlebearapps/untether/issues/89)

### changes

- session cleanup structured reporting: `_cleanup_session_registries()` now logs cleaned registry names at info level for post-mortem analysis [#93](https://github.com/littlebearapps/untether/issues/93)
  - session registration (`claude_runner.registered`, `session_stdin.registered`) upgraded to info level
- JSONL decode failure logged at warning level with truncated line content (first 200 chars)
- runner spawn now logs CLI args in `runner.start` event
- no-events session warning: `session.summary.no_events` logged when a non-cancelled session completes with zero events

### tests

- new test coverage for tool-aware stall threshold, keyboard edit failure recovery, edit-fail fallback send, session cleanup tracking, stderr sanitisation [#85](https://github.com/littlebearapps/untether/issues/85), build args validation, loop coverage

## v0.34.2 (2026-03-08)

### fixes

- stall monitor loops forever after laptop sleep — no auto-cancel, `/cancel` requires reply [#99](https://github.com/littlebearapps/untether/issues/99)
  - stall auto-cancel: dead process detection (immediate), no-PID zombie cap (3 warnings), absolute cap (10 warnings)
  - early PID threading: `last_pid` set at subprocess spawn, polled by `run_runner_with_cancel` before `StartedEvent`
  - standalone `/cancel` fallback: cancels single active run without requiring reply; prompts when multiple runs active
  - `queued_for_chat()` method on `ThreadScheduler` for standalone cancel of queued jobs
  - approval-aware stall threshold: 30 min when waiting for user approval (inline keyboard detected), 5 min otherwise

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
