# Commands & directives

This page documents Untether’s user-visible command surface: message directives, in-chat commands, and the CLI.

## Message directives

Untether parses the first non-empty line of a message for a directive prefix.

| Directive | Example | Effect |
|----------|---------|--------|
| `/<engine-id>` | `/codex fix flaky test` | Select an engine for this message. |
| `/<project-alias>` | `/happy-gadgets add escape-pod` | Select a project alias. |
| `@branch` | `@feat/happy-camera rewind to checkpoint` | Run in a worktree for the branch. |
| Combined | `/happy-gadgets @feat/flower-pin observe unseen` | Project + branch. |

Notes:

- Directives are only parsed at the start of the first non-empty line.
- Parsing stops at the first non-directive token.
- If a reply contains a `dir:` line, Untether ignores new directives and uses the reply context.

See [Context resolution](context-resolution.md) for the full rules.

## Context footer (`dir:`)

When a run has project context, Untether appends a footer line as part of the `🏷` info line:

- With branch: `dir: <project> @<branch>`
- Without branch: `dir: <project>`

This line is parsed from replies and takes precedence over new directives. For backwards compatibility, Untether also accepts the older `ctx:` format when parsing replies.

## Telegram in-chat commands

| Command | Description |
|---------|-------------|
| `/cancel` | Reply to the progress message to stop the current run. Without a reply it stops the single active run (or queued job), else cancels pending `/at` delays and loops — in a forum topic only that topic's, in General only General's ([#826](https://github.com/littlebearapps/untether/issues/826)). A Claude session idling after its answer isn't a run: `/cancel` closes it, still cancels the pending `/at` delays and loops, and always replies ([#902](https://github.com/littlebearapps/untether/issues/902)). Also drops a prompt still waiting in the forward-coalesce window with a `🗑️ Dropped N message(s) …` reply ([#807](https://github.com/littlebearapps/untether/issues/807)). |
| `/agent` | Show/set the default engine for the current scope. |
| `/model` | Show/set the model override for the current scope. |
| `/reasoning` | Show/set the reasoning override for the current scope. |
| `/listen` | Show/set listen mode (`all` / `mentions` / `clear`) — controls whether the bot responds to every message in a group chat or only to @-mentions. Renamed from `/trigger` in v0.35.3 ([#297](https://github.com/littlebearapps/untether/issues/297)); `/trigger` still works as a deprecated alias. |
| `/steer <text>` | Send `<text>` straight into the running Claude Code turn — Claude picks it up at its next tool step and folds it into the answer it is already working on (or, after its last tool call, answers it as the next turn, replying to your message). Replies `↪️ Steered into the current run.`; the run's progress shows `↪️ steer received: …` once Claude has read it. Bare `/steer` makes steer the default for this chat (or forum topic). Claude Code only, and only while a live session is running (a permission mode is set); otherwise the message queues and you get `↪️ … — queued instead.` See [steer follow-ups](../how-to/steer-follow-ups.md) ([#775](https://github.com/littlebearapps/untether/issues/775)). |
| `/queue <text>` | Send `<text>` as a normal queued follow-up — it waits until the running turn ends — even when the default is steer. Bare `/queue` makes queue (the built-in default) the default for this chat or topic. |
| `/file put <path>` | Upload a document into the repo/worktree (requires file transfer enabled). |
| `/file get <path>` | Fetch a file or directory back into Telegram. Agents can also send files automatically via `.untether-outbox/` — see [file transfer](../how-to/file-transfer.md#agent-initiated-delivery-outbox). |
| `/topic <project> @branch` | Create/bind a topic (topics enabled). |
| `/ctx` | Show context binding (chat or topic). |
| `/ctx set <project> @branch` | Update context binding. |
| `/ctx clear` | Remove context binding. |
| `/planmode` | Toggle Claude Code permission mode (on/plan-auto/auto/off/show/clear). `plan-auto` is plan mode with the plan gate auto-approved; `auto` is Claude Code's own classifier-gated mode. Claude Code only — Codex chats are pointed to `/config` → Approval policy (Gemini's page is called Approval mode). In a forum topic it still sets the whole chat's mode. |
| `/usage` | Claude Code: show subscription usage (5h window, weekly, per-model). Other engines (Codex, OpenCode, …): token totals for the chat's last session of that engine — session total, last run, run count and cost where reported; quota limits aren't available from those CLIs ([#417](https://github.com/littlebearapps/untether/issues/417)). The Claude view requires Claude Code OAuth credentials (see [troubleshooting](../how-to/troubleshooting.md#macos-and-linux-credential-differences)). `/usage debug` appends a `🔧 debug` block with last-fetch wall time and freshness label, last-error class+message, OAuth token expiry, and the cumulative `claude_usage.schema_mismatch` counter ([#410](https://github.com/littlebearapps/untether/issues/410)). |
| `/export` | Export the chat's most recently active session transcript as a Markdown or JSON file (`/export json`), attached as a document with a one-line summary caption ([#417](https://github.com/littlebearapps/untether/issues/417), [#418](https://github.com/littlebearapps/untether/issues/418)). The usage header says whether it's the last run's figure or Codex's thread total ([#859](https://github.com/littlebearapps/untether/issues/859)). |
| `/browse` | Browse project files with inline keyboard navigation (project chats or `default_project` only; respects `files.deny_globs` and hides dotfiles except `.github`/`.gitignore`). |
| `/ping` | Health check — replies with uptime since last (re)start. Shows a trigger summary if triggers target the current chat (`⏸ triggers paused` while paused), and `⏳ background: N tasks running` while a live Claude Code session holds background tasks ([#777](https://github.com/littlebearapps/untether/issues/777)). |
| `/health` | System + triggers + cost snapshot — RAM/swap, Untether process (PID, RSS, FDs, children), trigger counts, today's API cost, uptime. Compact HTML message (a header and up to seven lines); sections degrade gracefully when sources are unavailable. See [operations](../how-to/operations.md#health-snapshot). |
| `/restart` | Gracefully drain active runs and restart Untether. |
| `/verbose` | Toggle verbose progress mode (on/off/clear). Shows tool details in progress messages. |
| `/config` | Interactive settings menu — permission mode (titled **Permission mode** since [#741](https://github.com/littlebearapps/untether/issues/741)), ask mode, verbose, engine, model, reasoning, listen-mode and follow-up-mode (Claude Code: queue / steer, [#775](https://github.com/littlebearapps/untether/issues/775)) toggles with inline buttons. The `⏰ Triggers` page (`config:tg`) lists per-chat crons (`describe_cron(...)` schedule, project, engine, last-fired) and webhooks (path, auth, project, engine, last-fired), capped at 10 entries with an overflow marker, plus a master pause/resume toggle ([#271](https://github.com/littlebearapps/untether/issues/271), [#294](https://github.com/littlebearapps/untether/issues/294)); the home page links to it with a `⏰ Triggers` button whenever `[triggers]` is enabled ([#296](https://github.com/littlebearapps/untether/issues/296)). Each page's "📖 Learn more" link opens the matching help-centre article. |
| `/stats` | Per-engine session statistics — runs, actions, duration and last run for one period: `today` (default), `week` or `all` (e.g. `/stats week`). Includes `(N triggered, M manual)` per-engine breakdown when at least one count is nonzero ([#271](https://github.com/littlebearapps/untether/issues/271) Tier 3). Pass an engine name to filter (e.g. `/stats claude`). `/stats auth` shows each engine CLI's auth status instead. |
| `/auth` | `/auth codex` — headless device re-authentication for Codex: runs `codex login --device-auth` and sends the verification URL + device code. Bare `/auth` (or any other engine) shows a short help text. Codex-only; for each engine's auth status use `/stats auth`. |
| `/new` | Cancel any running task and clear stored sessions for the current scope (topic/chat). A prompt still waiting in the forward-coalesce window is dropped with a notice rather than carried into the new session ([#807](https://github.com/littlebearapps/untether/issues/807)). When the only thing open was Claude's idle live session after an answer, the reply says `🧹 closed the idle session and cleared …` instead of `cancelled run` ([#895](https://github.com/littlebearapps/untether/issues/895)). |
| `/continue [prompt]` | Resume the most recent session in the project directory. Picks up CLI-started sessions from Telegram. Optional prompt appended. Not supported for AMP. |
| `/threads [search <query>]` | List, view, resume or archive AMP threads via inline buttons. AMP-only — **deprecated** and no longer supported, together with the AMP engine ([#458](https://github.com/littlebearapps/untether/issues/458)). |
| `/at <duration> <prompt>` | Schedule a one-shot delayed run. Duration: `Ns`, `Nm` or `Nh`, between 60 s and 24 h. The engine and context a plain message would use are captured at schedule time and used at fire time (mirrors cron freeze-at-dispatch behaviour): the topic's or chat's `/agent` default, then the default of the `/ctx`-bound (or the chat's) project, then the global default ([#950](https://github.com/littlebearapps/untether/issues/950)). Pending delays are cancelled via `/cancel` and lost on restart. Per-chat cap of 20 pending delays. Trigger-source provenance is stamped as `at:<token>` and rendered in the run footer (`⏰ at:<token>`), and the run counts toward `/stats` as triggered ([#271](https://github.com/littlebearapps/untether/issues/271) follow-up). |

Notes:

- Outside topics, `/ctx` binds the chat context.
- In topics, `/ctx` binds the topic context.
- `/new` cancels running tasks and clears sessions but does **not** clear a bound context. In a forum supergroup (and a private chat with topics) it only cancels the runs and loops of the topic it was sent in; `/new` in General leaves topic runs alone. Non-forum groups stay chat-wide ([#826](https://github.com/littlebearapps/untether/issues/826)).
- A message that starts with a dot instead of a slash and names a command (`.new`, `.cancel`) isn't sent to the agent: Untether replies ``Did you mean `/new`? …`` so you can resend it ([#523](https://github.com/littlebearapps/untether/issues/523)).
- `/continue` uses the engine's native "continue" flag: `--continue` (Claude, OpenCode, Pi), `resume --last` (Codex), or `--resume latest` (Gemini, deprecated).
- Long-running tools (Bash, BashOutput, ScheduleWakeup, Monitor, …) surface a heartbeat-driven elapsed-time tail (`▸ Bash · 3m 47s · npm run build`) on the progress message after ~60s, regardless of `/verbose` state ([#481](https://github.com/littlebearapps/untether/issues/481)). Tune via `[progress] heartbeat_interval`.
- Loop mode (Claude only): there is no `/loop` Telegram command — it's a Claude Code feature. With Loop mode on, Untether declines Claude's own `CronCreate` and runs the schedule itself, firing each iteration with `claude --resume`; with it off, Claude can't schedule recurring or timed tasks and points you to Loop mode or `/at` ([#925](https://github.com/littlebearapps/untether/issues/925)). Self-paced `ScheduleWakeup` chains stop after `[loop] max_iterations` in both modes. Off by default; opt in per chat via `/config` → 🔁 **Loop mode**. Cost alerts come from `[cost_budget]` (checked after each fire), runaway-safety caps from `[loop]` ([#289](https://github.com/littlebearapps/untether/issues/289)).

## CLI

Untether’s CLI is an auto-router by default; engine subcommands override the default engine.

### Commands

| Command | Description |
|---------|-------------|
| `untether` | Start Untether (runs onboarding if setup/config is missing and you’re in a TTY). |
| `untether <engine>` | Run with a specific engine (e.g. `untether codex`). |
| `untether config path` | Print the resolved config file path. |
| `untether config list` | List config keys as flattened dot-paths. |
| `untether config get <key>` | Fetch a single config key (dot-path). |
| `untether config set <key> <value>` | Set a config value (value is auto-parsed). |
| `untether config unset <key>` | Remove a config key. |
| `untether init <alias>` | Register the current repo as a project (`--default` also sets it as `default_project`). |
| `untether chat-id` | Capture the current chat id. |
| `untether chat-id --project <alias>` | Save the captured chat id to a project. |
| `untether doctor` | Run configuration checks for the active transport (Telegram connectivity, file transfer, voice). |
| `untether onboarding-paths` | Print all possible onboarding paths. |
| `untether plugins` | List discovered plugins without loading them. |
| `untether plugins --load` | Load each plugin to validate types and surface import errors. |

### Common flags

| Flag | Description |
|------|-------------|
| `--version` | Show the version and exit. |
| `--onboard` | Force the interactive setup wizard before starting. |
| `--transport <id>` | Override the configured transport backend id. |
| `--debug` | Log engine JSONL, Telegram requests and rendered messages (to `debug.log` unless a log file is already set). |
| `--final-notify/--no-final-notify` | Send the final response as a new message vs an edit. |
