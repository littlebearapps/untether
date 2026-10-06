# Configuration

Untether reads configuration from `~/.untether/untether.toml`.

If you expect to edit config while Untether is running, set:

=== "untether config"

    ```sh
    untether config set watch_config true
    ```

=== "toml"

    ```toml
    watch_config = true
    ```

## Top-level keys

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `watch_config` | bool | `false` | Watch config file for changes; applies most settings immediately. See [Hot-reload vs restart-required](#hot-reload-vs-restart-required) below. |
| `default_engine` | string | `"codex"` | Default engine id for new threads. |
| `default_project` | string\|null | `null` | Default project alias. |
| `transport` | string | `"telegram"` | Transport backend id. |

Any key on the settings models below (everything except the engine tables and
`[triggers]`) can also be set from the environment as `UNTETHER__<SECTION>__<KEY>`
(e.g. `UNTETHER__WATCHDOG__LIVE_SESSIONS=false`), which wins over the TOML value.
See [environment variables](env-vars.md#config-overrides).

## Hot-reload vs restart-required

When `watch_config = true`, Untether watches `untether.toml` and applies most
changes immediately. A handful of settings require a process restart because
they're bound to resources (network sockets, bot token, session-mode machinery)
that can't be swapped live.

Changes are detected by file content, not timestamps
([#839](https://github.com/littlebearapps/untether/issues/839)): a save that
doesn't change anything (including `touch`) doesn't reload, while two quick
same-size edits are both applied. If a reload fails because of something outside
the file (say, an engine CLI missing from `PATH`), fix it and `touch` the config
to retry. A symlinked config is followed when the link is re-pointed; if the new
target lives in a different directory, restart Untether so later in-place edits
of that file are seen.

Editing a field marked 🔄 (**restart-required**) posts a warning to the project
chats and admin DMs ("⟳ Setting `chat_id` changed — restart required to take
effect.", followed by a "To apply:" line naming the service that's actually
running, [#927](https://github.com/littlebearapps/untether/issues/927)) and logs
`config.reload.transport_config_changed`. Other Telegram
transport changes apply without a message and log
`config.reload.transport_config_hot_reloaded`.

Separately from the watcher, the per-run settings (`[footer]`, `[progress]`,
`[watchdog]`, `[preamble]`, `[cost_budget]`, `[auto_continue]`, `[security]`, …)
are re-read on every use, even with `watch_config = false`, so an edit applies on
the next run or the next turn of a live session
([#269](https://github.com/littlebearapps/untether/issues/269)). Since 0.36.0 the
file is only re-parsed when its contents (or the `UNTETHER__*` env vars) change,
and each real parse logs one INFO `config.loaded reason=first_load|content_changed|env_changed`
line ([#506](https://github.com/littlebearapps/untether/issues/506)). Set
`UNTETHER_SETTINGS_CACHE=0` to turn the cache off (see
[environment variables](env-vars.md)).

The authoritative list for the Telegram transport lives on
`TelegramTransportSettings.RESTART_REQUIRED_FIELDS` (see `src/untether/settings.py`)
so code, docs, and UI can't drift; the other rows below are enforced where each
resource is created. Editing
`untether.toml` to update one of these while the service runs logs the warning
and sends the Telegram notice, but the new value won't take effect until you
restart.

| Section | Restart-required fields | Hot-reload |
|---|---|---|
| `transports.telegram` | `bot_token`, `chat_id`, `session_mode`, `topics`, `message_overflow` | everything else (`voice_*`, `show_resume_line`, `followup_mode`, `forward_coalesce_s`, `media_group_debounce_s`, `allowed_user_ids`, `allow_any_user`, `approval_originator_only`, `files.*` — except that outbox delivery is only wired at startup: if `files.enabled` or `outbox_enabled` was off when Untether started, turning it on needs a restart) |
| `transports.telegram.topics` | whole section (treated as one unit) | — |
| top-level `transport` | changing transport id | — |
| `progress` | `group_chat_rps` (read once when the Telegram client starts) | everything else (re-read per run) |
| engine tables, `projects`, `default_engine`, `default_project`, `plugins` | — | rebuilt on reload (a reload that breaks the **default** engine's config fails and the previous runtime keeps running) |
| `triggers` | turning `enabled` **on** (the cron scheduler and webhook server only start at startup; the reload logs `config.reload.restart_required key=triggers.enabled` and the Telegram reload notice flags it, [#894](https://github.com/littlebearapps/untether/issues/894)); everything under `[triggers.server]` (`host`, `port`, `rate_limit`, `max_body_bytes` are read when the server binds) | cron add/remove/edit, webhook add/remove/edit, `default_timezone`, `allow_unauthenticated_webhooks`, per-cron `timezone`/`run_once`/`permission_mode`/`model`/`reasoning`; turning `enabled` **off** clears every cron and webhook route (the server stays bound until restart) |

To restart, send `/restart` in Telegram (when a service manager relaunches Untether), or restart the service
directly. The restart notice names the unit or launchd label of the instance that's running; the default is:

```sh
systemctl --user restart untether                         # Linux (systemd), default unit
launchctl kickstart -k gui/$(id -u)/<your-launchd-label>  # macOS (launchd)
```

## `transports.telegram`

=== "untether config"

    ```sh
    untether config set transports.telegram.bot_token "..."
    untether config set transports.telegram.chat_id 123
    ```

=== "toml"

    ```toml
    [transports.telegram]
    bot_token = "..."
    chat_id = 123
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `bot_token` | string | (required) | 🔄 Telegram bot token from @BotFather. Restart-required. |
| `chat_id` | int | (required) | 🔄 Default chat id. Restart-required. |
| `allowed_user_ids` | int[] | (required, non-empty) | Allowed sender user ids. **Required for security as of v0.35.3** ([#377](https://github.com/littlebearapps/untether/issues/377)) — set to a non-empty list of Telegram user IDs (your own user id is the typical minimum). An empty list now triggers a hard `ConfigError` at startup unless you opt in to `allow_any_user = true` (see below). |
| `allow_any_user` | bool | `false` | **Dev/demo escape hatch** ([#377](https://github.com/littlebearapps/untether/issues/377)). Set to `true` to keep the prior insecure-default behaviour where any Telegram user who knows the bot username can send commands. Logged at INFO on every boot (`security.allow_any_user`) so the deviation is visible in `journalctl`. Use only for hackathons, demos, or local dev. |
| `approval_originator_only` | bool | `false` | Multi-user chats ([#388](https://github.com/littlebearapps/untether/issues/388)): when `true`, a Claude Code approval button (Approve / Deny / Pause & Outline / Let's discuss, including the background-agent approval message) and an AskUserQuestion answer (option buttons or a typed reply) are accepted only from the person whose message started the run. Anyone else gets `Only the person who started this run can answer this.` and a WARNING `callback.not_originator` / `ask_user_question.not_originator` naming both user ids. Runs with no human originator — cron, webhook, `/at` and loop fires — stay answerable by any allowed user. In a live Claude session the originator is the user who started the session, so a follow-up from someone else doesn't change it. Hot-reloads. |
| `message_overflow` | `"trim"`\|`"split"` | `"split"` | 🔄 How to handle long final responses. Restart-required. |
| `forward_coalesce_s` | float (≥ 0) | `1.0` | Quiet window for combining a prompt with immediately-following forwarded messages, and for merging rapid back-to-back prompts into one run ([#794](https://github.com/littlebearapps/untether/issues/794)); set `0` to disable. |
| `media_group_debounce_s` | float (≥ 0) | `1.0` | Quiet window for collecting the items of a Telegram media group (album) before handling them as one message. |
| `voice_transcription` | bool | `false` | Enable voice note transcription. |
| `voice_show_transcription` | bool | `true` | Echo the transcript back to the chat (`🎙 …`) before the run starts. |
| `voice_max_bytes` | int | `10485760` | Max voice note size (bytes). |
| `voice_transcription_model` | string | `"gpt-4o-mini-transcribe"` | OpenAI transcription model name. |
| `voice_transcription_base_url` | string\|null | `null` | Override base URL for voice transcription only. **SSRF-validated ([#381](https://github.com/littlebearapps/untether/issues/381)):** the resolved host must be public — loopback/private endpoints (e.g. a local Whisper server at `http://localhost:8000/v1`) are **rejected** unless allowlisted via `voice_transcription_url_allowlist`. Unset (the default public `api.openai.com` path) skips validation. A refused endpoint is logged at startup and after a hot-reload as `voice.base_url.not_permitted` (WARNING, naming the host and the allowlist entry to add), and a refused voice note's reply names the same fix ([#679](https://github.com/littlebearapps/untether/issues/679)). Credentials in the URL (`https://user:pass@host/v1`) are sent as HTTP Basic auth, which replaces the API-key header, and are masked in logs (error logs show the endpoint as `https://***@host/v1`, without its query string) ([#841](https://github.com/littlebearapps/untether/issues/841)). |
| `voice_transcription_url_allowlist` | string[] | `[]` | ([#381](https://github.com/littlebearapps/untether/issues/381)) CIDR/IP allowlist that opts specific private/loopback transcription endpoints back in past the SSRF guard — e.g. `["127.0.0.0/8"]` for a local Whisper server, or an Azure private-link range. Only consulted when `voice_transcription_base_url` is set. |
| `voice_transcription_api_key` | string\|null | `null` | Override API key for voice transcription only. |
| `voice_transcription_language` | string\|null | `null` | ([#638](https://github.com/littlebearapps/untether/issues/638)) Optional ISO-639-1 language hint (e.g. `"en"`) passed to the Whisper `language` param — stops wrong-language guesses on short voice notes. Unset = provider auto-detect. Hot-reloadable. |
| `voice_transcription_prompt` | string\|null | `null` (→ built-in) | ([#691](https://github.com/littlebearapps/untether/issues/691), [#703](https://github.com/littlebearapps/untether/issues/703)) Vocabulary-bias prompt (≤1000 chars) passed to the transcription `prompt` param — steers the decoder toward domain proper nouns (`"Trello, Untether, Claude Code"`). Effect is model-dependent; keep it to high-frequency nouns (overstuffing can induce hallucinated terms). **Unset = a shipped product-generic default** (a bare `Claude`, Claude Code, `CLAUDE.md`, `AGENTS.md`, Codex, OpenCode and Untether's own nouns — [#789](https://github.com/littlebearapps/untether/issues/789)); keep an override under Whisper's ~224-token window; a value **replaces** that default; `""` disables the bias and omits the parameter. Hot-reloadable. |
| `session_mode` | `"stateless"`\|`"chat"` | `"stateless"` | 🔄 Auto-resume mode. See [workflow modes](modes.md) — `"chat"` for assistant/workspace, `"stateless"` for handoff. Restart-required. |
| `show_resume_line` | bool | `true` | Show resume line in message footer. See [workflow modes](modes.md) — `false` for assistant/workspace, `true` for handoff. |
| `followup_mode` | `"queue"`\|`"steer"` | `"queue"` | ([#775](https://github.com/littlebearapps/untether/issues/775)) Default for messages sent while a Claude Code run is working: `queue` waits for the turn to end; `steer` writes the message into the running turn. Overridden per chat (`/config` → Follow-up, bare `/steer` / `/queue`), per forum topic (bare `/steer` / `/queue` in the topic) and per message (`/steer <text>`, `/queue <text>`). Claude Code only — other engines always queue. Hot-reloadable. See [steer follow-ups](../how-to/steer-follow-ups.md). |

When `allowed_user_ids` is set, updates without a sender id (for example, some channel posts) are ignored.

### `transports.telegram.topics`

🔄 **Restart-required as a whole section** — changes to either key only take effect after a restart because topic initialisation runs at startup.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | 🔄 Enable forum-topic features. Restart-required. |
| `scope` | `"auto"`\|`"main"`\|`"projects"`\|`"all"` | `"auto"` | 🔄 Where topics are managed. Restart-required. |

### `transports.telegram.files`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Enable `/file put` and `/file get`. |
| `auto_put` | bool | `true` | Auto-save uploads. |
| `auto_put_mode` | `"upload"`\|`"prompt"` | `"upload"` | Whether uploads also start a run. |
| `uploads_dir` | string | `"incoming"` | Relative path inside the repo/worktree. |
| `allowed_user_ids` | int[] | `[]` | Allowed senders for file transfer; empty allows private chats (group usage requires admin). |
| `deny_globs` | string[] | see below | Glob denylist for `/file put` / `/file get`, `/browse` and the outbox. `**` matches any number of directories, including none, so `**/*.pem` also denies a root-level `key.pem`, and `**/.env.*` a root-level `.env.example` ([#831](https://github.com/littlebearapps/untether/issues/831)). A bare name such as `.env` matches at any depth. Setting the key **replaces** the whole default list. |
| `outbox_enabled` | bool | `true` | Enable agent-initiated file delivery via `.untether-outbox/`. Requires `enabled = true`. |
| `outbox_dir` | string | `".untether-outbox"` | Relative outbox directory name (must not be absolute or contain `..`). An outbox that resolves outside the project, including through a symlinked path component, is never scanned or archived (`outbox.outside_root` warning, [#924](https://github.com/littlebearapps/untether/issues/924)). |
| `outbox_max_files` | int (1–50) | `10` | Max files sent per delivery. Extra files are reported in a notice and moved to `.untether-outbox/.skipped/` (never sent with a later run) ([#924](https://github.com/littlebearapps/untether/issues/924)). |
| `outbox_cleanup` | bool | `true` | Delete sent files and remove empty outbox directory after delivery. |
| `outbox_notify_skipped` | bool | `true` | ([#524](https://github.com/littlebearapps/untether/issues/524)) Notify the user when a non-deliverable outbox entry (a subdirectory, or a deny-globbed / oversize file) is skipped and archived to `.untether-outbox/.skipped/`, rather than dropping it silently. |
| `outbox_stale_policy` | `"archive"`\|`"send"` | `"archive"` | ([#924](https://github.com/littlebearapps/untether/issues/924)) Only entries written or copied into the outbox during the run (later of mtime/ctime, 5 s grace) are sent. `"archive"` moves older leftovers once to `.untether-outbox/.skipped/` with one notice; `"send"` is the old send-everything behaviour. Hot-reloads. |
| `outbox_deliver_directories` | `"off"`\|`"zip"` | `"off"` | ([#628](https://github.com/littlebearapps/untether/issues/628)) When `"zip"`, a subdirectory an agent writes into the outbox (e.g. a `screenshots/` folder from a quality audit) is bundled into a single `<name>.zip` document and delivered, instead of only being archived. Recursive deny-globs, symlink pruning, and per-member / total-input / member-count / final-size caps all apply; anything that fails them falls back to the `.skipped/` archive. |

Default `deny_globs`:

```toml
deny_globs = [
  ".git/**", ".env", "**/.env", "**/.env.*", ".envrc", "**/.envrc",
  "**/*.pem", "**/*.key", "**/id_rsa", "**/id_ed25519", "**/.ssh/**",
  "**/.netrc", "**/.npmrc", "**/.pypirc",
]
```

File size limits (not configurable):

- uploads: 20 MiB
- downloads / outbox: 50 MiB

## `projects.<alias>`

=== "untether config"

    ```sh
    untether config set projects.happy-gadgets.path "~/dev/happy-gadgets"
    untether config set projects.happy-gadgets.worktrees_dir ".worktrees"
    untether config set projects.happy-gadgets.default_engine "claude"
    untether config set projects.happy-gadgets.worktree_base "master"
    untether config set projects.happy-gadgets.chat_id -1001234567890
    ```

=== "toml"

    ```toml
    [projects.happy-gadgets]
    path = "~/dev/happy-gadgets"
    worktrees_dir = ".worktrees"
    default_engine = "claude"
    worktree_base = "master"
    chat_id = -1001234567890
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `path` | string | (required) | Repo root (expands `~`). Relative paths are resolved against the config directory. |
| `worktrees_dir` | string | `".worktrees"` | Worktree root (relative to `path` unless absolute). |
| `default_engine` | string\|null | `null` | Per-project default engine. |
| `worktree_base` | string\|null | `null` | Base branch for new worktrees. |
| `chat_id` | int\|null | `null` | Bind a Telegram chat to this project. |

Legacy config note: top-level `bot_token` / `chat_id` are auto-migrated into `[transports.telegram]` on startup.

## Plugins

### `plugins.enabled`

=== "untether config"

    ```sh
    untether config set plugins.enabled '["untether", "untether-transport-slack", "untether-engine-acme"]'
    ```

=== "toml"

    ```toml
    [plugins]
    enabled = ["untether", "untether-transport-slack", "untether-engine-acme"]
    ```

- `enabled = []` (default) means “load all installed plugins”.
- If non-empty, only distributions with matching names are visible (case-insensitive).

### `plugins.<id>`

Plugin-specific configuration lives under `[plugins.<id>]` and is passed to command plugins as `ctx.plugin_config`.

## `footer`

Controls what appears in the message footer after a run completes.

=== "toml"

    ```toml
    [footer]
    show_api_cost = false
    show_subscription_usage = true
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `show_api_cost` | bool | `true` | Show the API cost/tokens line (💰). |
| `show_subscription_usage` | bool | `false` | Show 5h/weekly subscription usage (⚡). Claude Code engine only. |

When `show_subscription_usage` is enabled, a compact line like `⚡ 5h: 45% (2h 15m) | 7d: 30% (4d 3h)` appears after every Claude Code run. Threshold-based warnings (≥70%) appear regardless of this setting.

## `preamble`

Controls the context preamble injected at the start of every agent prompt.

=== "toml"

    ```toml
    [preamble]
    enabled = true
    text = "Custom preamble text..."
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `true` | Inject preamble into prompts. |
| `text` | string\|null | `null` | Custom preamble text. `null` uses the built-in default. |

The default preamble tells agents they're running via Telegram, lists key constraints (only assistant text is visible), and requests a structured end-of-task summary.

## `progress`

Controls progress message rendering during agent runs.

=== "toml"

    ```toml
    [progress]
    verbosity = "verbose"
    max_actions = 8
    heartbeat_interval = 30
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `verbosity` | `"compact"` \| `"verbose"` | `"compact"` | `compact` shows status + title only. `verbose` adds tool detail lines (file paths, commands, patterns). |
| `max_actions` | int (0–50) | `5` | Maximum action lines shown in the progress message. |
| `min_render_interval` | float (0–30) | `2.0` | Minimum seconds between progress-message edits. |
| `group_chat_rps` | float (> 0, ≤ 10) | `0.333` (20/min) | Outgoing message rate limit per group chat (requests per second). Read once at startup — restart to apply. |
| `heartbeat_interval` | float (5–120) | `30.0` | Heartbeat tick that re-renders progress messages so long-running tools surface an elapsed-time tail (e.g. `▸ npm run build · 3m 47s`; the tail repeats nothing the title already shows, [#986](https://github.com/littlebearapps/untether/issues/986)) without waiting for the next JSONL event ([#481](https://github.com/littlebearapps/untether/issues/481)). It also repaints once nothing else has for a full interval, so the header's elapsed time keeps moving while an engine generates silently ([#954](https://github.com/littlebearapps/untether/issues/954)). |
| `show_background_tasks` | bool | `true` | Claude only. Show live background tasks (background agents, background Bash, Monitors): a `⏳ background (N)` block in the progress message, then one silent status message after the answer, edited in place and finalised when the tasks end ([#777](https://github.com/littlebearapps/untether/issues/777)). |
| `show_context_usage` | bool | `true` | Claude only. End the progress, final and live-turn header line with the context-window use, e.g. `done · claude · 1m 36s · step 10 · 62% ctx`. Omitted until the model's window is known (the first result on a model after a restart), and after a compaction until the next response ([#819](https://github.com/littlebearapps/untether/issues/819)). |
| `background_tasks_max_rows` | int (1–20) | `5` | Row cap for the background block and status message; extra tasks collapse into `+N more`. |
| `consolidate_wake_turns` | bool | `true` | Claude only. A background-task wake turn that runs no tools, raises no approval or question and answers in ≤300 characters is folded into that status message (an edit — no new message, no push) instead of arriving as its own `🔔` message. Tool use, a longer answer, an error, or the turn that finishes the last task (the report) still arrive as normal pushed messages. `false` restores one message per wake turn ([#785](https://github.com/littlebearapps/untether/issues/785)). Needs `show_background_tasks`. |

Per-chat override: `/verbose on` and `/verbose off` override the config default for the current chat without editing the TOML file. `/verbose clear` removes the override.

!!! tip "Hot-reload"
    Editing `[progress]` in `untether.toml` applies on the next run without restart ([#269](https://github.com/littlebearapps/untether/issues/269)). The default presenter and per-chat `/verbose` overrides both pick up the new values. The background-task keys are re-read at each turn of a live session and on each status-message refresh.

## `cost_budget`

=== "toml"

    ```toml
    [cost_budget]
    enabled = true
    max_cost_per_run = 2.00
    max_cost_per_day = 10.00
    warn_at_pct = 70
    auto_cancel = false
    warn_run_above_usd = 20.00
    notify_run_outlier = true
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Enable cost budget tracking. |
| `max_cost_per_run` | float\|null (≥ 0) | `null` | Per-run cost limit (USD). |
| `max_cost_per_day` | float\|null (≥ 0) | `null` | Daily cost limit (USD). The day runs from the host's local midnight; the running total is saved in `daily_cost.json` beside `untether.toml`, so it survives restarts ([#898](https://github.com/littlebearapps/untether/issues/898)). |
| `warn_at_pct` | int (0–100) | `70` | Warning threshold, as a percentage of the limit. |
| `auto_cancel` | bool | `false` | **Stop at limit**: once today's total reaches `max_cost_per_day`, new runs (prompts, follow-ups, crons, webhooks) are refused until local midnight, with a one-shot **Run anyway** button in chats; when a reply takes a live session's run total past `max_cost_per_run`, the reply is delivered and the session is closed. A reply in progress is never interrupted. Needs `enabled` (or the per-chat **Budget** toggle) ([#896](https://github.com/littlebearapps/untether/issues/896)). |
| `warn_run_above_usd` | float\|null (≥ 0) | `null` (→ `20.00`) | Per-run spend alert that fires **without** `enabled = true`. `0` disables it. For Claude, when background agents were active since the previous reply the alert adds `— includes spend by N background agents since the previous reply` ([#821](https://github.com/littlebearapps/untether/issues/821)). |
| `notify_run_outlier` | bool | `true` | Show the outlier as a chat line. The `cost.run_outlier` log event fires either way. |

Budget alerts always appear regardless of `[footer]` settings.

!!! tip "The outlier alert works with no budget configured"
    Everything above `warn_run_above_usd` is gated on `enabled = true`, so a
    deployment with no `[cost_budget]` block gets no spend signal at any
    amount — the one configuration where an alarm matters most
    ([#702](https://github.com/littlebearapps/untether/issues/702)). The
    per-run outlier alert is deliberately independent: it logs
    `cost.run_outlier` and adds one chat line for any single run above the
    threshold (default US$20), whatever `[footer] show_api_cost` is set to.
    Set `notify_run_outlier = false` to keep the log without the chat line.

!!! note "Cumulative session cost is not capped"
    Sessions can stack many runs via `/continue`, follow-up prompts, or
    a long back-and-forth dogfooding chat. A single session has been
    observed to cumulate over US$100 across 5 sub-runs even though each
    individual run was well under `max_cost_per_run`
    ([#517](https://github.com/littlebearapps/untether/issues/517)). If
    you need a ceiling that spans sessions, set `max_cost_per_day` —
    Untether tracks daily cost across all sessions and triggers
    `cost_budget.exceeded` once the day's spend crosses it. A
    `max_cost_per_session` knob is not currently provided; file a
    feature request if your workflow needs one.

    Since v0.36.0, Claude run costs are per run: Claude reports a running
    total for the whole session, and Untether records the difference since
    the previous run in `session_costs.json`. Earlier versions counted a
    resumed session's whole history against each run's budget, `/stats` and
    daily total ([#778](https://github.com/littlebearapps/untether/issues/778)).

## `watchdog`

=== "toml"

    ```toml
    [watchdog]
    liveness_timeout = 600.0
    stall_auto_kill = false
    stall_repeat_seconds = 180.0
    reap_orphans = true
    tool_timeout = 600.0
    mcp_tool_timeout = 900.0
    subagent_timeout = 900.0
    detect_unanswerable_control_requests = true
    detect_stuck_after_tool_result = false
    stuck_after_tool_result_timeout = 300.0
    stuck_after_tool_result_recovery_enabled = true
    stuck_after_tool_result_recovery_delay = 60.0
    detect_catalog_staleness = true
    notify_catalog_refresh = false
    catalog_refresh_min_interval_s = 5.0
    prespawn_ram_warn_mb = 2000
    prespawn_ram_block_mb = 500
    prespawn_ram_per_run_reserve_mb = 750
    max_concurrent_engine_runs = 0
    claude_stream_idle_timeout_ms = 300_000
    stream_idle_auto_retry = false
    stream_idle_max_retries = 1
    post_result_idle_enabled = true
    post_result_idle_timeout = 600.0
    bash_grace_seconds = 60.0
    pre_result_silence_timeout = 3600.0
    post_result_limbo_grace = 60.0
    post_result_bg_max_hold = 1800.0
    bg_hold_rearm_on_progress = true
    bg_hold_declared_waits = true
    live_sessions = true
    live_session_max_s = 14400.0
    rearm_plan_mode = true
    hold_for_async_hooks = true
    async_hook_max_hold = 630.0
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `liveness_timeout` | float | `600.0` | Seconds of no stdout before `subprocess.liveness_stall` warning (60–3600). |
| `stall_auto_kill` | bool | `false` | Auto-kill stalled processes. Requires zero TCP + CPU not increasing. |
| `stall_repeat_seconds` | float | `180.0` | Interval between repeat stall warnings in Telegram (30–600). |
| `reap_orphans` | bool | `true` | ([#590](https://github.com/littlebearapps/untether/issues/590)) After an engine subprocess exits (including a clean `rc=0`), kill the process-group members and descendants it left behind — chiefly leaked MCP server children. This also ends Bash tasks the agent backgrounded and that outlived the run. Set `false` to keep survivors. Applies from the next run. |
| `tool_timeout` | float | `600.0` | Stall threshold (seconds) for running local tool calls like Bash, Read, Write (60–7200). Increase for long builds or benchmarks. |
| `detect_unanswerable_control_requests` | bool | `true` | Log `control_request.unanswerable` (WARN, once per request) when a Claude approval or question has waited past `tool_timeout` with nothing on screen that can answer it — no approval/option button and no text-reply route — or no process to answer it ([#684](https://github.com/littlebearapps/untether/issues/684)). Detect-only: it never denies the request or closes the session. Applies to new runs. |
| `mcp_tool_timeout` | float | `900.0` | Stall threshold (seconds) for running MCP tool calls (60–7200). MCP tools are network-bound and may legitimately run for 10–20+ minutes. |
| `subagent_timeout` | float | `900.0` | Stall threshold (seconds) while the engine has active child processes, e.g. subagents (60–7200). Outside Claude Code, children count only while the process tree is using CPU ([#953](https://github.com/littlebearapps/untether/issues/953)). |
| `detect_stuck_after_tool_result` | bool | `false` | Enable the stuck-after-tool_result detector ([#322](https://github.com/littlebearapps/untether/issues/322)) — fires when a `tool_result` arrives and the engine goes silent for `stuck_after_tool_result_timeout` seconds while CPU-active (matches the upstream Claude-code / `mcp-remote` / undici wedge). Opt-in this release; will default `true` once the recovery path has more staging soak time. The detector is suppressed during legitimate long-running background primitives (`Monitor`, `Bash run_in_background=true`, `Agent run_in_background=true`, `ScheduleWakeup`, `RemoteTrigger`) via the per-session tracking infrastructure ([#346](https://github.com/littlebearapps/untether/issues/346) / [#347](https://github.com/littlebearapps/untether/issues/347)). |
| `stuck_after_tool_result_timeout` | float | `300.0` | Seconds of silence after a `tool_result` before the detector fires (60–1800). Matches undici's default idle-body timeout. |
| `stuck_after_tool_result_recovery_enabled` | bool | `true` | When the detector fires, attempt tiered recovery: Tier 2 SIGTERMs `mcp-remote`/`@modelcontextprotocol` adapter children (forces the SSE reader to error out and unblocks the parent engine); Tier 3 cancels the run via `cancel_event`. Set `false` to log only. Has no effect if `detect_stuck_after_tool_result = false`. |
| `stuck_after_tool_result_recovery_delay` | float | `60.0` | Seconds between Tier 2 MCP-adapter SIGTERM and Tier 3 cancel escalation (10–600). |
| `detect_catalog_staleness` | bool | `true` | MCP catalog observability ([#365](https://github.com/littlebearapps/untether/issues/365)) — emit `catalog_staleness.detected` structlog WARNING once per `(session, server, status)` tuple when Claude Code's `system.init` reports any MCP server with a non-`connected` status (e.g. `pending`, `failed`, `needs-auth`). Observability only — no kill or recovery action. Set `false` to silence the warning. Claude runner only. |
| `notify_catalog_refresh` | bool | `false` | Opt-in experimental ([#365](https://github.com/littlebearapps/untether/issues/365)) — after each `tool_result` batch, send an `mcp_status` control_request on Claude's stdin to nudge the catalog. Documented parent→CLI primitive from Anthropic's `claude-agent-sdk-python` (`get_mcp_status`). Logs `catalog.refresh_sent` INFO on success. Default `false` because the upstream refresh effect on the catalog UI is empirical; enable on staging to measure. Claude runner only. |
| `catalog_refresh_min_interval_s` | float | `5.0` | ([#497](https://github.com/littlebearapps/untether/issues/497)) Minimum seconds between two `mcp_status` refreshes in one session when `notify_catalog_refresh` is on (0–60; `0` sends one per `tool_result` batch). |
| `prespawn_ram_warn_mb` | int (0–65536) | `2000` | Pre-spawn RAM guard ([#350](https://github.com/littlebearapps/untether/issues/350)) — emit `subprocess.prespawn.ram_warning` when free RAM is below this threshold (MB) at engine spawn. `0` disables the warn tier. |
| `prespawn_ram_block_mb` | int (0–65536) | `500` | Refuse to spawn the engine subprocess (yields `CompletedEvent(ok=False, error="🛑 Insufficient RAM…")`) when free RAM is below this threshold (MB). `0` disables the block tier; `0` for both fully disables the guard. Must be strictly less than `prespawn_ram_warn_mb` when both are set. |
| `prespawn_ram_per_run_reserve_mb` | int (0–65536) | `750` | ([#589](https://github.com/littlebearapps/untether/issues/589)) Headroom reserved per engine run already in flight. The block threshold becomes `prespawn_ram_block_mb + this × live_runs`, so the bar rises with concurrency. Without it the guard is count-blind: N chats each pass the flat check independently and then collectively exhaust the host — the observed nsd failure (OOM killer 5× in one evening, two live Claude runs killed with `rc=-9`). `0` restores the flat pre-0.35.4rc8 threshold. |
| `max_concurrent_engine_runs` | int (0–64) | `0` | ([#589](https://github.com/littlebearapps/untether/issues/589)) Hard ceiling on concurrent engine subprocesses, independent of free RAM — useful on small VPS hosts where the accumulating MCP-child leak matters more than the instantaneous memory reading. Exceeding it fails the run with a readable Telegram message rather than letting the kernel SIGKILL a live session mid-task. `0` = unlimited (default, no behaviour change). Suggested: `2` on a 4 GB host, `3–4` on 8 GB. Applies to every engine including Claude Code ([#838](https://github.com/littlebearapps/untether/issues/838)); a Claude live session idling after its reply still holds its process, so it counts (the block message names such idle sessions). A block never clears the chat's saved session. |
| `claude_stream_idle_timeout_ms` | int | `300_000` | Sets `CLAUDE_STREAM_IDLE_TIMEOUT_MS` in the Claude Code subprocess env via `setdefault` ([#438](https://github.com/littlebearapps/untether/issues/438)). Range 30 s – 30 min. Long-form opus 4.7 1M plan-mode generations can legitimately idle the SSE stream past 5 min; deployments hitting upstream Anthropic API stalls (Type A — mid-generation) can raise this to `600_000` or `900_000` to ride out longer silences. Type-B failures (cold-start zero-byte, `num_turns ≤ 1 && duration_api_ms == 0`) are upstream API outages — raising this won't help; the failure error message now classifies both modes inline. Shell-set `CLAUDE_STREAM_IDLE_TIMEOUT_MS` still wins. |
| `stream_idle_auto_retry` | bool | `false` | ([#572](https://github.com/littlebearapps/untether/issues/572)) Bounded auto-retry for **Type-A** stream-idle timeouts: when a run fails with a mid-generation SSE stall after real output began, Untether resumes the session automatically (with a `🔁` notice) instead of surfacing a terminal error. Type-B (cold-start zero-byte) **never** retries — retrying hammers a down API. The retry is a normal resumed run, so cost-budget caps, quarantine divert and the RAM guard all apply; signal deaths (rc=143/137) are suppressed. Default off while the upstream API is unstable. |
| `stream_idle_max_retries` | int (1–3) | `1` | ([#572](https://github.com/littlebearapps/untether/issues/572)) Attempt cap for `stream_idle_auto_retry` (1–3). A retry that fails with Type-A again surfaces the classified error once the cap is reached. |
| `post_result_idle_enabled` | bool | `true` | Claude post-result idle watchdog ([#333](https://github.com/littlebearapps/untether/issues/333)) — closes Claude's stdin cleanly after `post_result_idle_timeout` of silence following a `result` event so multi-turn sessions don't sit alive (and billable) for the full upstream ~36 min idle window. Set `false` to disable (Claude will sit idle until the upstream CLI exits on its own). The clean exit is auto-continue safe — `last_event_type=result` is excluded from the auto-continue gate. |
| `post_result_idle_timeout` | float | `600.0` | Seconds the watchdog waits after a `result` event before closing stdin (30–3600). The first `result` also emits a `✓ turn complete` footer hint so users know the turn is done; when the watchdog actually fires it sends one Telegram message: `✓ turn complete · session closed after Nm idle`. Re-arms (instead of closing) if a control_request or AskUserQuestion is mid-flight, so a button click in flight is never orphaned. |
| `bash_grace_seconds` | float | `60.0` | Stall-warning grace window for Bash / BashOutput / KillShell tools ([#481](https://github.com/littlebearapps/untether/issues/481)). Range 5–300. While the most-recent action is one of these and within this window of its start, stall warnings (and the `_STALL_MAX_WARNINGS` auto-cancel arm) are suppressed — long builds and deploys are an expected wait, not a hung session. |
| `pre_result_silence_timeout` | float | `3600.0` | ([#592](https://github.com/littlebearapps/untether/issues/592)) Bounds the *pre-result* dead zone — a run whose stream goes silent **before its first `result` event** is SIGTERMed after this many seconds (0–86400; `0` disables). Suppressed while a permission/ask request is pending, so plan-approval waits stay safe. Catches zombie subprocesses that never produced output (an 8-day idle Claude on mac leaked its session lock and MCP children). |
| `post_result_limbo_grace` | float | `60.0` | ([#591](https://github.com/littlebearapps/untether/issues/591)) After a successful `result`, a *fully quiescent* limbo subprocess (no live background work, not CPU/tree-active) is SIGTERMed after this grace instead of waiting the full `post_result_idle_timeout` (0–600; `0` = wait the full timeout). A demonstrably-busy process is exempt ([#655](https://github.com/littlebearapps/untether/issues/655)). |
| `post_result_bg_max_hold` | float | `1800.0` | The background hold (0–7200, read per spawn). **Live sessions** ([#829](https://github.com/littlebearapps/untether/issues/829)): how long a live Claude session stays open with background work still running and **no background activity** (no turn, no agent progress frame, no subagent tool starting or ending, no output from a background Bash) before it closes with a notice. Working agents are no longer stopped at 30 min; a silent one still is — unless Claude declared a longer wait (`bg_hold_declared_waits`). **`live_sessions = false`** ([#647](https://github.com/littlebearapps/untether/issues/647)): upper bound on how long the post-result ceiling defers its SIGTERM while `/proc` evidence shows the subagent tree still working (`0` disables it), also bounded by the `BG_AGENT_MAX_KEEP_S` handle age-out. |
| `bg_hold_rearm_on_progress` | bool | `true` | ([#829](https://github.com/littlebearapps/untether/issues/829)) Claude live sessions only. Re-arm the background hold on background activity (see above). `false` restores the rc14 behaviour: the hold counts from the last turn, so a quietly working agent is stopped when it expires. Read per spawn, so a change applies from the next run (`/new`). |
| `bg_hold_declared_waits` | bool | `true` | ([#872](https://github.com/littlebearapps/untether/issues/872)) Claude live sessions only. The background hold never closes a session before a wait Claude declared has ended: a background command's `timeout` (passed with `run_in_background`; Claude Code stops the command at that limit, at most 2 h) or a pending scheduled wake-up (at most 1 h), plus a 60 s grace. When that wait ends, the quiet-time limit starts afresh so the turn reporting back (and its hooks) can finish. `live_session_max_s` still caps it. `false` restores the quiet-time rule only. Read per spawn (`/new`). |
| `live_sessions` | bool | `true` | ([#776](https://github.com/littlebearapps/untether/issues/776)) Claude only. Keep the session open after its answer while background work runs: background-task, Monitor and scheduled-wake-up turns are delivered as their own Telegram messages, and follow-ups are written into the open session instead of resuming it. With live sessions on, `post_result_limbo_grace` is the idle close (stdin closed gracefully, nothing quarantined) and `post_result_bg_max_hold` is how long background work may run with no activity. `false` restores the pre-0.36.0 "stop at the first answer" behaviour. |
| `live_session_max_s` | float | `14400.0` | ([#776](https://github.com/littlebearapps/untether/issues/776)) Absolute lifetime of one live Claude process from spawn (600–86400). The session is closed with a notice when reached, as a backstop against an endless `Monitor`. |
| `rearm_plan_mode` | bool | `true` | ([#383](https://github.com/littlebearapps/untether/issues/383)) Claude only, live sessions only. In a `plan` / `plan-auto` chat, approving a plan takes the open Claude session out of plan mode; with this on, Untether puts it back (the CLI's own `set_permission_mode`) when the reply ends, so your next message — and, in `plan` chats, a background wake-up — is planned again. `plan-auto` re-plans your messages only, not wake-ups. Background agents the approved reply launched finish first: the switch-back waits until they end or show no activity for `post_result_bg_max_hold` (capped at `live_session_max_s`), and replies meanwhile say `⚠️ Not re-planned`. Read at each new session. `false` = never sent (an approval then lasts the whole session). |
| `hold_for_async_hooks` | bool | `true` | ([#812](https://github.com/littlebearapps/untether/issues/812)) Claude live sessions only. Keep the session's stdin open while a hook the CLI runs in the background (`async` / `asyncRewake`) is still running, so an `asyncRewake` hook's findings arrive as their own `🪝 Hook feedback` turn instead of being dropped when stdin closes. Passes `--include-hook-events` when the installed CLI supports it. Read per spawn. `false` = no flag and no hold. |
| `async_hook_max_hold` | float | `630.0` | ([#812](https://github.com/littlebearapps/untether/issues/812)) Upper bound, in seconds, on that hold per hook, counted from the hook's start (0–3600). The default is the CLI's 600 s `asyncRewake` timeout plus its 30 s exit wait. |

!!! note "The post-result watchdog is a permanent mitigation ([#569](https://github.com/littlebearapps/untether/issues/569))"

    `post_result_idle_*`, the SIGTERM/SIGKILL subcountdown, and `detect_stuck_after_tool_result` are not transitional workarounds. Both upstream defects they mitigate — [claude-code#39700](https://github.com/anthropics/claude-code/issues/39700) (stream-json hangs and never exits) and [claude-code#30333](https://github.com/anthropics/claude-code/issues/30333) (ResultMessage never emitted) — are **CLOSED / NOT_PLANNED**. Disabling these knobs re-exposes hung sessions that never terminate, and the post-result limbo SIGTERM additionally bounds the MCP-child leak tracked in [#590](https://github.com/littlebearapps/untether/issues/590) / [#592](https://github.com/littlebearapps/untether/issues/592). See [Claude runner reference](runners/claude/runner.md#this-watchdog-is-permanent-not-a-transitional-workaround-569).

The stall monitor in `ProgressEdits` fires at 5 min (300s) idle, 10 min for local tools (`tool_timeout`), 15 min for MCP tools (`mcp_tool_timeout`) and active child processes (`subagent_timeout`; outside Claude Code only while the process tree is using CPU, so a permanent wrapper or MCP-server child doesn't count, [#953](https://github.com/littlebearapps/untether/issues/953)), and for pending approvals after 10 min, then every 30 min. Sessions blocked on an unanswered approval, inside an upstream rate-limit window (a `rejected` quota snapshot, [#790](https://github.com/littlebearapps/untether/issues/790)), or in a Claude CLI API-retry back-off ([#792](https://github.com/littlebearapps/untether/issues/792)) are treated as *expected waits* ([#495](https://github.com/littlebearapps/untether/issues/495)/[#499](https://github.com/littlebearapps/untether/issues/499)/[#500](https://github.com/littlebearapps/untether/issues/500)): they log a paced `subprocess.approval_pending` INFO instead of a `progress_edits.stall_detected` WARNING, are excluded from the frozen-ring escalation, and do not count toward the `stall_warnings` metric reported in `session.summary`. When a local tool is running and the child process is CPU-active, the first stall warning fires but repeat warnings are suppressed — they resume if CPU goes idle (indicating a genuinely stuck tool). A stopped (state T) or I/O-blocked (D) engine process is reported as `⏳ Engine process is stopped …` / `… blocked on I/O …` rather than as idle child processes (#953). The liveness watchdog in the subprocess layer fires at `liveness_timeout` with `/proc` diagnostics. When `stall_auto_kill` is enabled, auto-kill requires a triple safety gate: timeout exceeded + zero TCP connections + CPU ticks not increasing between snapshots.

### `[loop]`

Controls how Untether handles Claude Code's scheduling tools (`CronCreate`, `ScheduleWakeup`). With Loop mode on (opt in per chat via `/config → 🔁 Loop mode`), Untether runs Claude's schedules itself, with the caps below. With it off, Claude can't schedule recurring or timed tasks. ([#289](https://github.com/littlebearapps/untether/issues/289), [#925](https://github.com/littlebearapps/untether/issues/925))

=== "toml"

    ```toml
    [loop]
    enabled = false
    inline_threshold_seconds = 300
    redundancy_check_interval = 30
    max_iterations = 20
    max_total_duration_hours = 4
    min_interval_seconds = 60
    expiry_days = 7
    own_schedule = true
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Global default for Loop mode. Per-chat override available via `/config → 🔁 Loop mode`. Read when Claude tries to schedule, so a change applies to open sessions too. |
| `inline_threshold_seconds` | int (≥ 0) | `300` | `ScheduleWakeup` calls with `delaySeconds` ≤ this stay rendered live by the rc8 countdown — no Untether-side timer is registered. Longer waits (Loop mode on) also get an Untether timer, which fires only if the session has closed by then. |
| `redundancy_check_interval` | int (≥ 1) | `30` | Seconds between retries when a loop iteration is due but its session is busy (background work, an approval waiting). A recurring loop skips the iteration once the next one is due. |
| `max_iterations` | int (1–10000) | `20` | Runaway-safety cap on iteration count (NOT a cost cap). Also caps a chain of self-paced wake-ups, in both Loop modes: after this many in a row the session closes and the pending wake-up is dropped. |
| `max_total_duration_hours` | int (1–168) | `4` | Runaway-safety cap on wall-clock duration (NOT a cost cap). |
| `min_interval_seconds` | int (≥ 60) | `60` | Accepted but not enforced yet; the upstream cron floor (60 s) applies. |
| `expiry_days` | int (1–30) | `7` | Auto-expire loops this many days after creation (the default matches upstream's 7-day session-task expiry). |
| `own_schedule` | bool | `true` | Untether owns Claude's schedules in every Claude chat (a PreToolUse hook declines `CronCreate`; Loop mode decides whether Untether then runs it). `false` restores the earlier behaviour (0.35.4 through 0.35.5rc19) in both modes: Claude Code's own scheduled task runs uncapped while the session is open, and self-paced wake-ups aren't capped. Hot-reloads for new sessions. |

**Cost limits are NOT in `[loop]`** — they live in `[cost_budget]` and apply to loop fires automatically. See [Cost budgets](../how-to/cost-budgets.md) for setup.

State is persisted to `active_loops.json` (sibling of your `untether.toml`) so loops survive restarts. Alongside it are the per-loop cancel records and the sessions that may still hold an older Claude-side scheduled task; those resume with `CLAUDE_CODE_DISABLE_CRON=1` for up to 7 days so the task can't restart ([#926](https://github.com/littlebearapps/untether/issues/926)).

!!! warning "Rolling back to 0.35.4 (or 0.35.5rc19 and earlier)"
    Older versions reject any config key they don't know, in every section, not just `[loop]`. Remove `own_schedule` and the other keys added in v0.36.0, and change `plan-auto` back to `auto`, before downgrading, or the config won't load. See [Rolling back to 0.35.4](../how-to/update.md#rolling-back-to-0354) for the full list.

### `[auto_continue]`

Auto-continue detects when Claude Code exits after receiving tool results without processing them (upstream bugs [#34142](https://github.com/anthropics/claude-code/issues/34142), [#30333](https://github.com/anthropics/claude-code/issues/30333)) and automatically resumes the session. Detection is based on a protocol invariant: normal sessions always end with `last_event_type=result`, while premature exits show `last_event_type=user`.

Auto-continue only fires on a clean exit (`rc=0`). Signal deaths (rc=143/SIGTERM, rc=137/SIGKILL) and ordinary failures are excluded, to prevent death spirals under memory pressure ([#640](https://github.com/littlebearapps/untether/issues/640)).

This section also carries the empty-resume recovery and session-ownership knobs, since they mitigate the same upstream turn-state family.

=== "toml"

    ```toml
    [auto_continue]
    enabled = true
    max_retries = 1
    resend_empty_resume = true
    empty_resume_fresh = true
    quarantine_on_forced_teardown = true
    serialize_session_owner = true
    session_handoff_timeout_s = 30.0
    session_handoff_bg_timeout_s = 600.0
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `true` | Enable automatic session continuation for Claude Code. |
| `max_retries` | int (0–3) | `1` | Maximum consecutive auto-continue attempts per run. |
| `resend_empty_resume` | bool | `true` | ([#596](https://github.com/littlebearapps/untether/issues/596)) Auto-resend the original prompt once when a resume returns an empty 0-turn / $0 result, instead of asking the user to resend. Single-shot. |
| `empty_resume_fresh` | bool | `true` | ([#631](https://github.com/littlebearapps/untether/issues/631) W1) Make that retry a **fresh** session rather than the same one — the original session is poisoned, so resending into it can no-op again. |
| `quarantine_on_forced_teardown` | bool | `true` | ([#632](https://github.com/littlebearapps/untether/issues/632) W2) Mark sessions force-killed after a result as unsafe to resume, so the *next* message diverts fresh before any empty result is seen. Since 0.36.0 an idle live session that is merely slow to exit after its stdin closes is not quarantined ([#791](https://github.com/littlebearapps/untether/issues/791)). |
| `serialize_session_owner` | bool | `true` | ([#633](https://github.com/littlebearapps/untether/issues/633) W4) Never resume a session whose previous subprocess is still alive. Before spawning `--resume`, wait (bounded) for the prior owner to exit; if it will not, quarantine and start fresh rather than racing it. Two concurrent owners of one session id is what leaves the upstream turn dangling and produces the 0-turn empty resume. Set `false` for exact pre-0.35.4rc8 behaviour. Claude only. |
| `session_handoff_timeout_s` | float | `30.0` | Upper bound on that wait (0–300). Condition-based, so it resolves the instant the prior subprocess exits — this is only the give-up point. Keep comfortably above the post-result SIGTERM grace so a normal teardown wins the race. |
| `session_handoff_bg_timeout_s` | float | `600.0` | ([#647](https://github.com/littlebearapps/untether/issues/647)) Extended handoff wait when the prior owner still has live background work at the base `session_handoff_timeout_s` deadline (0–1800). The user is told why the reply is delayed, and the wait extends up to this bound before diverting to a fresh session. |

!!! tip "Layered defence"

    `serialize_session_owner` is *proactive* (stop the session being poisoned); `quarantine_on_forced_teardown` and `empty_resume_fresh` are *reactive* (recover once it has been). Leave all three on unless you are bisecting a regression.

### `[security]`

Runtime security knobs. Defaults are safe — operators only flip these when investigating a leak or opting out of a probe.

=== "toml"

    ```toml
    [security]
    env_audit = true
    env_extra_allow = ["OP_SERVICE_ACCOUNT_TOKEN", "DOPPLER_TOKEN"]
    env_extra_prefix_allow = ["VAULT_", "INFISICAL_"]
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `env_audit` | bool | `true` | One-shot `/proc/<claude_pid>/environ` sample on first `system.init` ([#361](https://github.com/littlebearapps/untether/issues/361)). Emits `claude.env_audit.leaked_var` WARNING per non-allowlisted name observed (dedup per session per name). Reuses `utils/env_policy.is_allowed`. Linux-only — silently no-ops elsewhere or when /proc is unreadable. Set `false` to opt out (e.g. on hardened hosts where `/proc/<pid>/environ` reads are sensitive). The companion `env -i` wrap on Claude exec ([#361](https://github.com/littlebearapps/untether/issues/361)) is always on and not configurable. |
| `env_extra_allow` | list[str] | `[]` | Per-deployment exact-match additions to the engine-subprocess env allowlist ([#409](https://github.com/littlebearapps/untether/issues/409)). Use for credential-manager tokens that aren't in the global defaults — e.g. `["OP_SERVICE_ACCOUNT_TOKEN", "DOPPLER_TOKEN", "INFISICAL_TOKEN"]`. Each entry must match `[A-Z_][A-Z0-9_]*` (uppercase, digits, underscore; cannot start with a digit). Empty / whitespace / lowercase entries are rejected at config-load time. Currently honoured by the Claude and Pi runners. The audit (`env_audit`) honours these too, so user-allowed names aren't false-flagged as leaks. Untether emits one `env_policy.user_extension` INFO log per process at first runner spawn so the addition is visible in journalctl. |
| `env_extra_prefix_allow` | list[str] | `[]` | Like `env_extra_allow` but for name *prefixes* — convenient for credential-manager families where many vars share a prefix. Examples: `["VAULT_"]` admits `VAULT_TOKEN`, `VAULT_ADDR`, `VAULT_NAMESPACE`. Each entry must match the same env-var name shape as `env_extra_allow`. |

## Engine-specific config tables

Each engine reads a table keyed by its id, written either as `[engines.<id>]`
(e.g. `[engines.claude]`) or as a top-level `[<id>]` (e.g. `[claude]`). The two
layouts don't mix: once an `[engines]` table exists (even an empty one, or a
single `[engines.<id>]`), every flat `[claude]` / `[codex]`-style table is ignored
(`UntetherSettings.engine_config`). Built-in engines are listed here; plugin engines should document
their own keys.

An invalid engine table stops Untether from starting when it belongs to the
default engine. For any other engine the table is ignored with a `setup.warning`
log and the engine runs on its defaults — except a refused `extra_args` flag,
which disables that engine (listed under `failed to load:` in the startup
message) rather than dropping its other settings.

### `codex`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `extra_args` | string[] | `["-c", "notify=[]"]` | Extra CLI args for `codex`, placed before `exec`. Setting the key replaces the default. Flags that bypass Codex's sandbox/approvals (`--dangerously-bypass-approvals-and-sandbox`/`--yolo`, `--approve-for-me`/`--not-so-yolo`, `--dangerously-bypass-hook-trust`, `-s`/`--sandbox danger-full-access`, a `-c` value mentioning `danger-full-access`, `:danger`, `bypass` or `dangerously`), the working-directory flags (`-C`/`--cd`, `--worktree`), the flags Untether manages (`-a`/`--ask-for-approval`, `--ignore-rules`, `--ignore-user-config`, `--json`, `--color`, `--skip-git-repo-check`, `--output-schema`, `-o`/`--output-last-message`) and a bare `--` are rejected at config load ([#209](https://github.com/littlebearapps/untether/issues/209)) — see [Security → Engine CLI flags](../how-to/security.md#engine-cli-flags-extra_args). |
| `profile` | string | (unset) | Passed as `--profile <name>` and used as the session title. |

=== "untether config"

    ```sh
    untether config set codex.extra_args '["-c", "notify=[]"]'
    untether config set codex.profile "work"
    ```

=== "toml"

    ```toml
    [codex]
    extra_args = ["-c", "notify=[]"]
    profile = "work"
    ```

### `claude`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `model` | string | (unset) | Optional model override. |
| `permission_mode` | string | (unset) | Default permission mode for Claude runs: `default`, `manual` (CLI alias of `default`), `acceptEdits`, `plan`, `plan-auto` (Untether only: `plan` with the plan approval auto-approved), `auto` (the CLI's classifier-gated mode — not plan mode since 0.36.0, [#741](https://github.com/littlebearapps/untether/issues/741)), `dontAsk` or `bypassPermissions`. Any other value is a config error naming the key ([#742](https://github.com/littlebearapps/untether/issues/742)). Unset = the non-interactive `-p` path with no control channel (no approvals, no plan mode, no live session). A per-chat `/planmode` or `/config` choice overrides it. |
| `allowed_tools` | string[] | `["Bash", "Read", "Edit", "Write"]` | Tools pre-approved via `--allowedTools`. **Since 0.36.0 the default is not sent in `default` / `manual` / `acceptEdits`** — pre-approving these would defeat the approval prompt those modes exist to give ([#749](https://github.com/littlebearapps/untether/issues/749)). Setting the key explicitly still applies in every mode, and logs `claude.allowed_tools.prompting_mode_override` once. |
| `extra_args` | string[] | `[]` | Extra CLI args passed to `claude` (e.g. `["--chrome"]` to opt into the Claude-in-Chrome extension). ([#209](https://github.com/littlebearapps/untether/issues/209)) Flags Untether manages internally (`-p`, `--print`, `--output-format`, `--input-format`, `--resume`/`-r`, `--continue`/`-c`, `--permission-mode`, `--permission-prompt-tool`, `--permission-prompts`, `--allowedTools`/`--allowed-tools` — use `allowed_tools`) and the approval bypasses (`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, a bare `--`) are rejected at config-load, in every spelling (`--flag=value`, short clusters like `-pc`). See [Security → Engine CLI flags](../how-to/security.md#engine-cli-flags-extra_args). |
| `dangerously_skip_permissions` | bool | `false` | Adds `--dangerously-skip-permissions`, which overrides `permission_mode` **and every `/planmode` choice** — no Telegram approvals are shown. Logs `claude.config.dangerously_skip_permissions` once at startup. |
| `use_api_billing` | bool | `false` | Keep `ANTHROPIC_API_KEY` in the Claude subprocess environment for API billing. When `false` it is removed, so Claude Code uses its subscription login. |

=== "untether config"

    ```sh
    untether config set claude.model "claude-sonnet-5-5"
    untether config set claude.allowed_tools '["Bash", "Read", "Edit", "Write"]'
    untether config set claude.extra_args '["--chrome"]'
    untether config set claude.dangerously_skip_permissions false
    untether config set claude.use_api_billing false
    ```

=== "toml"

    ```toml
    [claude]
    model = "claude-sonnet-5-5"
    allowed_tools = ["Bash", "Read", "Edit", "Write"]
    extra_args = ["--chrome"]    # e.g. opt into Claude-in-Chrome
    dangerously_skip_permissions = false
    use_api_billing = false
    ```

### `pi`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `model` | string | (unset) | Passed as `--model`. |
| `provider` | string | (unset) | Passed as `--provider`. |
| `extra_args` | string[] | `[]` | Extra CLI args for `pi`. |

=== "untether config"

    ```sh
    untether config set pi.model "..."
    untether config set pi.provider "..."
    untether config set pi.extra_args "[]"
    ```

=== "toml"

    ```toml
    [pi]
    model = "..."
    provider = "..."
    extra_args = []
    ```

### `opencode`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `model` | string | (unset) | Optional model override. |

=== "untether config"

    ```sh
    untether config set opencode.model "anthropic/claude-sonnet-5-5"
    ```

=== "toml"

    ```toml
    [opencode]
    model = "anthropic/claude-sonnet-5-5"
    ```

### `gemini`

!!! warning "Deprecated — no longer supported"
    The Gemini CLI engine still loads but is deprecated and no longer supported: it gets no fixes, is excluded from testing, and may be removed in a future release. The CLI has reached end of life upstream for individual accounts and hangs under Untether until the watchdog cancels the run. Plan a move to another engine.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `model` | string | (unset) | Optional model override, passed as `--model`. |
| `skip_trust` | bool | `true` | Pass `--skip-trust` so headless runs work outside `~/.gemini/trustedFolders.json` ([#471](https://github.com/littlebearapps/untether/issues/471)). Gemini CLI rejects runs from any directory not in the trust list — even with `--approval-mode yolo` — and there is no interactive prompt path in headless usage. Set `false` to enforce Gemini's project-local extension/MCP trust gate. |

=== "untether config"

    ```sh
    untether config set gemini.model "gemini-2.5-pro"
    untether config set gemini.skip_trust true
    ```

=== "toml"

    ```toml
    [gemini]
    model = "gemini-2.5-pro"
    skip_trust = true
    ```

!!! note "Approval mode"
    Gemini CLI's approval mode (read-only / edit files / full access) is toggled per chat via `/config` → **Approval mode**, not the config file. Codex CLI's approval policy (full auto / safe) is similarly toggled via `/config` → **Approval policy**: **safe** runs `codex exec` with `--sandbox read-only` (no `--ask-for-approval`, which `codex exec` ignores — [#830](https://github.com/littlebearapps/untether/issues/830)), and **full auto** adds no sandbox flag. See [inline settings](../how-to/inline-settings.md).

### `amp`

!!! warning "Deprecated — no longer supported"
    The AMP engine still loads but is deprecated and no longer supported: it gets no fixes, is excluded from testing, and may be removed in a future release. The AMP backend refuses Untether's runs (HTTP `426`). Plan a move to another engine.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `mode` | string | (unset) | Execution mode, passed as `--mode`. Values: `deep`, `free`, `rush`, `smart`. |
| `model` | string | (unset) | Display label shown in the message footer. Overridden by `mode` if both are set. |
| `dangerously_allow_all` | bool | `false` | Pass `--dangerously-allow-all` to skip AMP's permission prompts. **Default flipped to `false` in v0.35.3** ([#206](https://github.com/littlebearapps/untether/issues/206)) — set to `true` only if you specifically want AMP runs without its built-in permission system. Untether's own permission layer (when configured) remains the primary control. |
| `stream_json_input` | bool | `false` | Pass `--stream-json-input` for stdin-based prompt delivery. |

=== "untether config"

    ```sh
    untether config set amp.mode "deep"
    untether config set amp.dangerously_allow_all true
    ```

=== "toml"

    ```toml
    [amp]
    mode = "deep"
    dangerously_allow_all = true
    ```

## Triggers

Webhook and cron triggers that start agent runs from external events. See the
full [Triggers reference](triggers/triggers.md) for auth, templating, and
routing details.

=== "toml"

    ```toml
    [triggers]
    enabled = true

    [triggers.server]
    host = "127.0.0.1"
    port = 9876
    rate_limit = 60
    max_body_bytes = 1_048_576

    [[triggers.webhooks]]
    id = "github-push"
    path = "/hooks/github"
    project = "myapp"
    engine = "claude"
    auth = "hmac-sha256"
    secret = "whsec_abc..."
    prompt_template = "Review push to {{ref}} by {{pusher.name}}"

    [[triggers.crons]]
    id = "daily-review"
    schedule = "0 9 * * 1-5"
    project = "myapp"
    engine = "claude"
    prompt = "Review open PRs and summarise status."
    ```

### `[triggers]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Master switch. No server or cron loop starts when `false`. |
| `default_timezone` | string\|null | `null` | Default IANA timezone for all crons (e.g. `"Australia/Melbourne"`). Per-cron `timezone` overrides. |
| `allow_unauthenticated_webhooks` | bool | `false` | ([#382](https://github.com/littlebearapps/untether/issues/382)) Opt-in escape hatch permitting a webhook with `auth = "none"` to bind on a **non-loopback** host. By default such a route is refused at initial bind and dropped on hot-reload — an unauthenticated public webhook is a remote-agent-run primitive — while polling, commands, and crons keep running. Loopback binds are always allowed regardless. Set `true` only for trusted local demos. |

!!! tip "Hot-reload"
    When `watch_config = true`, changes to webhooks, crons, schedules, and timezones
    are applied automatically without restart. `[triggers.server]` settings (`host`,
    `port`, `rate_limit`, `max_body_bytes`) and turning `enabled` on still require a
    restart.
    See the [Triggers reference — Hot-reload](triggers/triggers.md#hot-reload) for details.

### `[triggers.server]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `host` | string | `"127.0.0.1"` | Bind address. Use a reverse proxy for internet exposure. |
| `port` | int | `9876` | Listen port (1–65535). |
| `rate_limit` | int (≥ 1) | `60` | Max requests per minute (global + per-webhook). |
| `max_body_bytes` | int | `1048576` | Max request body size in bytes (1024–10485760). |

### `[[triggers.webhooks]]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `id` | string | (required) | Unique identifier. |
| `path` | string | (required) | URL path (e.g. `/hooks/github`). Must start with `/`, contain only letters, digits, `/`, `_`, `.`, `-`, and not be `/health`. |
| `project` | string\|null | `null` | Project alias for working directory. |
| `engine` | string\|null | `null` | Engine override. If unset: the `project`'s `default_engine` when a project is set, otherwise the global `default_engine` ([#862](https://github.com/littlebearapps/untether/issues/862)). |
| `chat_id` | int\|null | `null` | Telegram chat. Falls back to the transport default `chat_id`, even when `project` is set (the project's bound chat is not used; a warning is logged when they differ). |
| `auth` | string | `"bearer"` | `"bearer"`, `"hmac-sha256"`, `"hmac-sha1"`, or `"none"`. |
| `secret` | string\|null | `null` | Auth secret. Required when `auth` is not `"none"`. |
| `action` | `"agent_run"`\|`"file_write"`\|`"http_forward"`\|`"notify_only"` | `"agent_run"` | What the webhook does. Non-agent actions use `file_path` + `on_conflict`, `forward_url` + `forward_headers` + `forward_method`, or `message_template`, plus `notify_on_success` / `notify_on_failure` — see the [Triggers reference](triggers/triggers.md). |
| `prompt_template` | string\|null | `null` | Prompt with `{{field.path}}` substitutions. Required when `action = "agent_run"`. |
| `event_filter` | string\|null | `null` | Only process matching event type headers. |
| `accept_multipart` | bool | `false` | Accept `multipart/form-data` file uploads, saved to `file_destination`. |
| `max_file_size_bytes` | int | `52428800` | Max uploaded file size (1024–104857600). |

### `[[triggers.crons]]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `id` | string | (required) | Unique identifier. |
| `schedule` | string | (required) | 5-field cron expression. |
| `project` | string\|null | `null` | Project alias for working directory. |
| `engine` | string\|null | `null` | Engine override. If unset: the `project`'s `default_engine` when a project is set, otherwise the global `default_engine` ([#862](https://github.com/littlebearapps/untether/issues/862)). |
| `chat_id` | int\|null | `null` | Telegram chat. Falls back to the transport default `chat_id`, even when `project` is set (the project's bound chat is not used; a warning is logged when they differ). |
| `prompt` | string\|null | (required if no `prompt_template`) | Prompt sent to the engine. |
| `prompt_template` | string\|null | `null` | Prompt with `{{field}}` substitutions, used with `fetch`. |
| `fetch` | table\|null | `null` | Pre-fetch step (`type = "http_get"`, `"http_post"` or `"file_read"`) whose result is available to `prompt_template` — see the [Triggers reference](triggers/triggers.md). |
| `timezone` | string\|null | `null` | IANA timezone (e.g. `"Australia/Melbourne"`). Overrides `default_timezone`. |
| `permission_mode` | string\|null | `null` | Permission mode for this cron's runs. For `engine = "claude"` it must be one of the `[claude] permission_mode` values. Codex honours `"safe"` (read-only sandbox, [#830](https://github.com/littlebearapps/untether/issues/830)); any other value runs Codex in full auto and logs `codex.permission_mode.unknown`. Gemini (deprecated) passes it through as `--approval-mode`; other engines ignore it. |
| `model` | string\|null | `null` | ([#743](https://github.com/littlebearapps/untether/issues/743)) Model for this cron's run only; wins over the chat's `/model`. Free-form (no leading `-`, no whitespace). Set `engine` too. |
| `reasoning` | string\|null | `null` | ([#743](https://github.com/littlebearapps/untether/issues/743)) Effort / reasoning level for this cron's run only; validated against the engine's levels when `engine` is set, else against `low`…`max`. |
| `run_once` | bool | `false` | Fire once, then stay disabled. The fired state is saved to `run_once_fired.json` beside `untether.toml`, so a reload or restart does not re-activate it; remove the cron from the config (or rename its `id`) to re-arm it. |
