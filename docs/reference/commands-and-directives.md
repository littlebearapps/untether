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
| `/cancel` | Reply to the progress message to stop the current run. |
| `/agent` | Show/set the default engine for the current scope. |
| `/model` | Show/set the model override for the current scope. |
| `/reasoning` | Show/set the reasoning override for the current scope. |
| `/trigger` | Show/set trigger mode (mentions-only vs all). |
| `/file put <path>` | Upload a document into the repo/worktree (requires file transfer enabled). |
| `/file get <path>` | Fetch a file or directory back into Telegram. Agents can also send files automatically via `.untether-outbox/` — see [file transfer](../how-to/file-transfer.md#agent-initiated-delivery-outbox). |
| `/topic <project> @branch` | Create/bind a topic (topics enabled). |
| `/ctx` | Show context binding (chat or topic). |
| `/ctx set <project> @branch` | Update context binding. |
| `/ctx clear` | Remove context binding. |
| `/planmode` | Toggle Claude Code plan mode (on/auto/off/show/clear). |
| `/usage` | Show Claude Code subscription usage (5h window, weekly, per-model). Requires Claude Code OAuth credentials (see [troubleshooting](../how-to/troubleshooting.md#claude-code-credentials)). |
| `/export` | Export last session transcript as Markdown or JSON. |
| `/browse` | Browse project files with inline keyboard navigation. |
| `/ping` | Health check — replies with uptime. |
| `/restart` | Gracefully drain active runs and restart Untether. |
| `/verbose` | Toggle verbose progress mode (on/off/clear). Shows tool details in progress messages. |
| `/config` | Interactive settings menu — plan mode, ask mode, verbose, engine, model, reasoning, trigger toggles with inline buttons. |
| `/stats` | Per-engine session statistics — runs, actions, and duration for today, this week, and all time. Pass an engine name to filter (e.g. `/stats claude`). |
| `/auth` | Headless device re-authentication for Codex — runs `codex login --device-auth` and sends the verification URL + device code. `/auth status` checks CLI availability. Codex-only. |
| `/new` | Clear stored sessions for the current scope (topic/chat). |
| `/continue [prompt]` | Resume the most recent session in the project directory. Picks up CLI-started sessions from Telegram. Optional prompt appended. Not supported for AMP. |

Notes:

- Outside topics, `/ctx` binds the chat context.
- In topics, `/ctx` binds the topic context.
- `/new` clears sessions but does **not** clear a bound context.
- `/continue` uses the engine's native "continue" flag: `--continue` (Claude, OpenCode, Pi), `resume --last` (Codex), or `--resume latest` (Gemini).

## CLI

Untether’s CLI is an auto-router by default; engine subcommands override the default engine.

### Commands

| Command | Description |
|---------|-------------|
| `untether` | Start Untether (runs onboarding if setup/config is missing and you’re in a TTY). |
| `untether <engine>` | Run with a specific engine (e.g. `untether codex`). |
| `untether config` | Show config file path and content. |
| `untether init <alias>` | Register the current repo as a project. |
| `untether chat-id` | Capture the current chat id. |
| `untether chat-id --project <alias>` | Save the captured chat id to a project. |
| `untether doctor` | Validate Telegram connectivity and related config. |
| `untether plugins` | List discovered plugins without loading them. |
| `untether plugins --load` | Load each plugin to validate types and surface import errors. |

### Common flags

| Flag | Description |
|------|-------------|
| `--onboard` | Force the interactive setup wizard before starting. |
| `--transport <id>` | Override the configured transport backend id. |
| `--debug` | Write debug logs to `debug.log`. |
| `--final-notify/--no-final-notify` | Send the final response as a new message vs an edit. |
