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
- If a reply contains a `ctx:` line, Untether ignores new directives and uses the reply context.

See [Context resolution](context-resolution.md) for the full rules.

## Context footer (`ctx:`)

When a run has project context, Untether appends a footer line rendered as inline code:

- With branch: `` `ctx: <project> @<branch>` ``
- Without branch: `` `ctx: <project>` ``

This line is parsed from replies and takes precedence over new directives.

## Telegram in-chat commands

| Command | Description |
|---------|-------------|
| `/cancel` | Reply to the progress message to stop the current run. |
| `/agent` | Show/set the default engine for the current scope. |
| `/model` | Show/set the model override for the current scope. |
| `/reasoning` | Show/set the reasoning override for the current scope. |
| `/trigger` | Show/set trigger mode (mentions-only vs all). |
| `/file put <path>` | Upload a document into the repo/worktree (requires file transfer enabled). |
| `/file get <path>` | Fetch a file or directory back into Telegram. |
| `/topic <project> @branch` | Create/bind a topic (topics enabled). |
| `/ctx` | Show context binding (chat or topic). |
| `/ctx set <project> @branch` | Update context binding. |
| `/ctx clear` | Remove context binding. |
| `/planmode` | Toggle Claude Code plan mode (on/auto/off/show/clear). |
| `/usage` | Show Claude Code subscription usage (5h window, weekly, per-model). |
| `/export` | Export last session transcript as Markdown or JSON. |
| `/browse` | Browse project files with inline keyboard navigation. |
| `/ping` | Health check — replies with uptime. |
| `/restart` | Gracefully drain active runs and restart Untether. |
| `/new` | Clear stored sessions for the current scope (topic/chat). |

Notes:

- Outside topics, `/ctx` binds the chat context.
- In topics, `/ctx` binds the topic context.
- `/new` clears sessions but does **not** clear a bound context.

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
