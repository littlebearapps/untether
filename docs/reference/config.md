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
| `watch_config` | bool | `false` | Hot-reload config changes (transport excluded). |
| `default_engine` | string | `"codex"` | Default engine id for new threads. |
| `default_project` | string\|null | `null` | Default project alias. |
| `transport` | string | `"telegram"` | Transport backend id. |

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
| `bot_token` | string | (required) | Telegram bot token from @BotFather. |
| `chat_id` | int | (required) | Default chat id. |
| `allowed_user_ids` | int[] | `[]` | Allowed sender user ids. Empty disables sender filtering; when set, only these users can interact (including DMs). |
| `message_overflow` | `"trim"`\|`"split"` | `"split"` | How to handle long final responses. |
| `forward_coalesce_s` | float | `1.0` | Quiet window for combining a prompt with immediately-following forwarded messages; set `0` to disable. |
| `voice_transcription` | bool | `false` | Enable voice note transcription. |
| `voice_max_bytes` | int | `10485760` | Max voice note size (bytes). |
| `voice_transcription_model` | string | `"gpt-4o-mini-transcribe"` | OpenAI transcription model name. |
| `voice_transcription_base_url` | string\|null | `null` | Override base URL for voice transcription only. |
| `voice_transcription_api_key` | string\|null | `null` | Override API key for voice transcription only. |
| `session_mode` | `"stateless"`\|`"chat"` | `"stateless"` | Auto-resume mode. See [workflow modes](modes.md) — `"chat"` for assistant/workspace, `"stateless"` for handoff. |
| `show_resume_line` | bool | `true` | Show resume line in message footer. See [workflow modes](modes.md) — `false` for assistant/workspace, `true` for handoff. |

When `allowed_user_ids` is set, updates without a sender id (for example, some channel posts) are ignored.

### `transports.telegram.topics`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Enable forum-topic features. |
| `scope` | `"auto"`\|`"main"`\|`"projects"`\|`"all"` | `"auto"` | Where topics are managed. |

### `transports.telegram.files`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Enable `/file put` and `/file get`. |
| `auto_put` | bool | `true` | Auto-save uploads. |
| `auto_put_mode` | `"upload"`\|`"prompt"` | `"upload"` | Whether uploads also start a run. |
| `uploads_dir` | string | `"incoming"` | Relative path inside the repo/worktree. |
| `allowed_user_ids` | int[] | `[]` | Allowed senders for file transfer; empty allows private chats (group usage requires admin). |
| `deny_globs` | string[] | (defaults) | Glob denylist (e.g. `.git/**`, `**/*.pem`). |
| `outbox_enabled` | bool | `true` | Enable agent-initiated file delivery via `.untether-outbox/`. Requires `enabled = true`. |
| `outbox_dir` | string | `".untether-outbox"` | Relative outbox directory name (must not be absolute). |
| `outbox_max_files` | int (1–50) | `10` | Max files sent per run. |
| `outbox_cleanup` | bool | `true` | Delete sent files and remove empty outbox directory after delivery. |

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
    untether config set plugins.enabled '["untether-transport-slack", "untether-engine-acme"]'
    ```

=== "toml"

    ```toml
    [plugins]
    enabled = ["untether-transport-slack", "untether-engine-acme"]
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
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `verbosity` | `"compact"` \| `"verbose"` | `"compact"` | `compact` shows status + title only. `verbose` adds tool detail lines (file paths, commands, patterns). |
| `max_actions` | int (0–50) | `5` | Maximum action lines shown in the progress message. |

Per-chat override: `/verbose on` and `/verbose off` override the config default for the current chat without editing the TOML file. `/verbose clear` removes the override.

## `cost_budget`

=== "toml"

    ```toml
    [cost_budget]
    enabled = true
    max_cost_per_run = 2.00
    max_cost_per_day = 10.00
    warn_at_pct = 70
    auto_cancel = false
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `false` | Enable cost budget tracking. |
| `max_cost_per_run` | float\|null | `null` | Per-run cost limit (USD). |
| `max_cost_per_day` | float\|null | `null` | Daily cost limit (USD). |
| `warn_at_pct` | int | `70` | Warning threshold (0–100). |
| `auto_cancel` | bool | `false` | Auto-cancel runs that exceed the per-run limit. |

Budget alerts always appear regardless of `[footer]` settings.

## `watchdog`

=== "toml"

    ```toml
    [watchdog]
    liveness_timeout = 600.0
    stall_auto_kill = false
    stall_repeat_seconds = 180.0
    tool_timeout = 600.0
    mcp_tool_timeout = 900.0
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `liveness_timeout` | float | `600.0` | Seconds of no stdout before `subprocess.liveness_stall` warning (60–3600). |
| `stall_auto_kill` | bool | `false` | Auto-kill stalled processes. Requires zero TCP + CPU not increasing. |
| `stall_repeat_seconds` | float | `180.0` | Interval between repeat stall warnings in Telegram (30–600). |
| `tool_timeout` | float | `600.0` | Stall threshold (seconds) for running local tool calls like Bash, Read, Write (60–7200). Increase for long builds or benchmarks. |
| `mcp_tool_timeout` | float | `900.0` | Stall threshold (seconds) for running MCP tool calls (60–7200). MCP tools are network-bound and may legitimately run for 10–20+ minutes. |

The stall monitor in `ProgressEdits` fires at 5 min (300s) idle, 10 min for local tools, 15 min for MCP tools, and 30 min for pending approvals. When a local tool is running and the child process is CPU-active, the first stall warning fires but repeat warnings are suppressed — they resume if CPU goes idle (indicating a genuinely stuck tool). The liveness watchdog in the subprocess layer fires at `liveness_timeout` with `/proc` diagnostics. When `stall_auto_kill` is enabled, auto-kill requires a triple safety gate: timeout exceeded + zero TCP connections + CPU ticks not increasing between snapshots.

### `[auto_continue]`

Auto-continue detects when Claude Code exits after receiving tool results without processing them (upstream bugs [#34142](https://github.com/anthropics/claude-code/issues/34142), [#30333](https://github.com/anthropics/claude-code/issues/30333)) and automatically resumes the session. Detection is based on a protocol invariant: normal sessions always end with `last_event_type=result`, while premature exits show `last_event_type=user`.

Auto-continue is suppressed on signal deaths (rc=143/SIGTERM, rc=137/SIGKILL) to prevent death spirals under memory pressure.

=== "toml"

    ```toml
    [auto_continue]
    enabled = true
    max_retries = 1
    ```

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `true` | Enable automatic session continuation for Claude Code. |
| `max_retries` | int | `1` | Maximum consecutive auto-continue attempts per run (1–5). |

## Engine-specific config tables

Engines use **top-level tables** keyed by engine id. Built-in engines are listed
here; plugin engines should document their own keys.

### `codex`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `extra_args` | string[] | `["-c", "notify=[]"]` | Extra CLI args for `codex` (exec-only flags are rejected). |
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
| `allowed_tools` | string[] | `["Bash", "Read", "Edit", "Write"]` | Auto-approve tool rules. |
| `dangerously_skip_permissions` | bool | `false` | Skip Claude Code permissions prompts. |
| `use_api_billing` | bool | `false` | Keep `ANTHROPIC_API_KEY` for API billing. |

=== "untether config"

    ```sh
    untether config set claude.model "claude-sonnet-4-5-20250929"
    untether config set claude.allowed_tools '["Bash", "Read", "Edit", "Write"]'
    untether config set claude.dangerously_skip_permissions false
    untether config set claude.use_api_billing false
    ```

=== "toml"

    ```toml
    [claude]
    model = "claude-sonnet-4-5-20250929"
    allowed_tools = ["Bash", "Read", "Edit", "Write"]
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
    untether config set opencode.model "claude-sonnet"
    ```

=== "toml"

    ```toml
    [opencode]
    model = "claude-sonnet"
    ```

### `gemini`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `model` | string | (unset) | Optional model override, passed as `--model`. |

=== "untether config"

    ```sh
    untether config set gemini.model "gemini-2.5-pro"
    ```

=== "toml"

    ```toml
    [gemini]
    model = "gemini-2.5-pro"
    ```

!!! note "Approval mode"
    Gemini CLI's approval mode (read-only / edit files / full access) is toggled per chat via `/config` → **Approval mode**, not the config file. Codex CLI's approval policy (full auto / safe) is similarly toggled via `/config` → **Approval policy**. See [inline settings](../how-to/inline-settings.md).

### `amp`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `mode` | string | (unset) | Execution mode, passed as `--mode`. Values: `deep`, `free`, `rush`, `smart`. |
| `model` | string | (unset) | Display label shown in the message footer. Overridden by `mode` if both are set. |
| `dangerously_allow_all` | bool | `true` | Pass `--dangerously-allow-all` to skip permission prompts. |
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

### `[triggers.server]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `host` | string | `"127.0.0.1"` | Bind address. Use a reverse proxy for internet exposure. |
| `port` | int | `9876` | Listen port (1--65535). |
| `rate_limit` | int | `60` | Max requests per minute (global + per-webhook). |
| `max_body_bytes` | int | `1048576` | Max request body size in bytes (1 KB--10 MB). |

### `[[triggers.webhooks]]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `id` | string | (required) | Unique identifier. |
| `path` | string | (required) | URL path (e.g. `/hooks/github`). |
| `project` | string\|null | `null` | Project alias for working directory. |
| `engine` | string\|null | `null` | Engine override. |
| `chat_id` | int\|null | `null` | Telegram chat. Falls back to transport default. |
| `auth` | string | `"bearer"` | `"bearer"`, `"hmac-sha256"`, `"hmac-sha1"`, or `"none"`. |
| `secret` | string\|null | `null` | Auth secret. Required when `auth` is not `"none"`. |
| `prompt_template` | string | (required) | Prompt with `{{field.path}}` substitutions. |
| `event_filter` | string\|null | `null` | Only process matching event type headers. |

### `[[triggers.crons]]`

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `id` | string | (required) | Unique identifier. |
| `schedule` | string | (required) | 5-field cron expression. |
| `project` | string\|null | `null` | Project alias for working directory. |
| `engine` | string\|null | `null` | Engine override. |
| `chat_id` | int\|null | `null` | Telegram chat. Falls back to transport default. |
| `prompt` | string | (required) | Prompt sent to the engine. |
