<h1 align="center">Untether</h1>

<p align="center">
  <strong>Control your AI coding agents from Telegram.</strong><br>
  Stream progress, approve actions, manage projects — from anywhere.
</p>

<p align="center">
  <a href="https://github.com/littlebearapps/untether/actions/workflows/ci.yml"><img src="https://github.com/littlebearapps/untether/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://pypi.org/project/untether/"><img src="https://img.shields.io/pypi/v/untether" alt="PyPI" /></a>
  <a href="https://pypi.org/project/untether/"><img src="https://img.shields.io/pypi/pyversions/untether" alt="Python" /></a>
  <a href="https://github.com/littlebearapps/untether/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License" /></a>
</p>

---

Untether is a Telegram bridge for AI coding agents. It connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [OpenCode](https://github.com/opencode-ai/opencode), and [Pi](https://github.com/nicholasgasior/pi) to Telegram so you can send coding tasks, watch progress live, and approve actions — all from your phone.

Walk the dog, watch the footy, sit at a friend's place. Your agents keep working. You stay in control.

## Table of contents

- [Quick start](#quick-start)
- [Why Untether?](#why-untether)
- [Supported engines](#supported-engines)
- [Features](#features)
- [Commands](#commands)
- [Configuration](#configuration)
- [Requirements](#requirements)
- [Engine guides](#engine-guides)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Acknowledgements](#acknowledgements)
- [Licence](#licence)

## Quick start

```sh
uv tool install untether        # recommended
# or
pipx install untether            # alternative
```

```sh
untether                        # run setup wizard
```

The wizard creates a Telegram bot, picks your workflow, and connects your chat. Then send a message to your bot:

> fix the failing tests in src/auth

That's it. Your agent runs on your machine, streams progress to Telegram, and you can reply to continue the conversation.

## Why Untether?

| Problem | Untether's solution |
|---------|-------------------|
| You have to sit at your desk while agents work | Stream progress to Telegram — watch from anywhere |
| Agents need permission to run tools | Approve or deny actions with inline buttons on your phone |
| You switch between Claude, Codex, and other agents | One bot, multiple engines — switch with `/claude`, `/codex`, `/opencode`, or `/pi` |
| Managing multiple repos from chat is messy | Register projects, target them with `/myproject`, branch with `@feat/thing` |
| No cost visibility | Per-run and daily cost tracking with configurable budgets |
| Can't continue terminal sessions remotely | Stateless resume — pick up any session in chat or terminal |

## Supported engines

| Engine | Install | What it's good at |
|--------|---------|-------------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `npm i -g @anthropic-ai/claude-code` | Complex refactors, architecture, long context |
| [Codex](https://github.com/openai/codex) | `npm i -g @openai/codex` | Fast edits, shell commands, quick fixes |
| [OpenCode](https://github.com/opencode-ai/opencode) | `npm i -g opencode-ai@latest` | 75+ providers via Models.dev, local models |
| [Pi](https://github.com/mariozechner/pi-coding-agent) | `npm i -g @mariozechner/pi-coding-agent` | Multi-provider auth, conversational |

Use your existing Claude or ChatGPT subscription — no extra API keys needed (unless you want API billing).

## Features

### Progress streaming

Watch your agent work in real time. See tool calls, file changes, and elapsed time as they happen.

### Interactive permissions (Claude Code)

When Claude Code needs to run a tool, Untether shows **Approve / Deny / Pause & Outline Plan** buttons in Telegram. Routine tools (Read, Grep, Glob) are auto-approved. Dangerous operations require your explicit approval with a diff preview.

### Plan mode

Toggle plan mode per chat with `/planmode`. Claude outlines its approach before making changes. Choose between:

- **on** — full plan mode with manual approval
- **auto** — plan mode with auto-approved transitions
- **off** — no plan phase

### Projects and worktrees

Register repos with `untether init myproject`, then target them from chat:

> /myproject @feat/new-api add the endpoint

Each branch runs in an isolated git worktree. Multiple projects and branches can run in parallel.

### Cost and usage tracking

```toml
[footer]
show_api_cost = false           # hide API cost line (default: true)
show_subscription_usage = true  # show 5h/weekly window usage (default: false)

[cost_budget]
enabled = true
max_cost_per_run = 2.00
max_cost_per_day = 10.00
```

See subscription usage or API costs in the progress footer. Use `/usage` for a detailed breakdown. Budget alerts fire at configurable thresholds, and can optionally auto-cancel runs.

### Conversation modes

| Mode | Best for | How it works |
|------|----------|-------------|
| **Assistant** | Day-to-day use | Ongoing chat with auto-resume. `/new` to start fresh. |
| **Workspace** | Teams and multi-project | Forum topics bound to repos and branches. |
| **Handoff** | Terminal-first workflow | Reply-to-continue with resume lines you can paste into terminal. |

### More features

- **Voice notes** — dictate tasks, Untether transcribes and sends to the agent
- **File transfer** — upload files to your repo or download results back
- **Scheduled tasks** — cron expressions and webhook triggers
- **Forum topics** — map Telegram topics to projects and branches
- **Session export** — `/export` for markdown or JSON transcripts
- **File browser** — `/browse` to navigate project files with inline buttons
- **Plugin system** — extend with custom engines, transports, and commands

## Commands

| Command | What it does |
|---------|-------------|
| `/cancel` | Stop the running agent |
| `/agent` | Show or set the engine for this chat |
| `/model` | Override the model for an engine |
| `/planmode` | Toggle plan mode (on/auto/off) |
| `/usage` | Show API costs for the current session |
| `/export` | Export session transcript |
| `/browse` | Browse project files |
| `/new` | Clear stored sessions |
| `/file put/get` | Transfer files |
| `/topic` | Create or bind forum topics |
| `/restart` | Gracefully restart Untether (drains active runs first) |

Prefix any message with `/<engine>` to pick an engine for that task, or `/<project>` to target a repo:

> /claude /myproject @feat/auth implement OAuth2

## Configuration

Untether reads `~/.untether/untether.toml`. The setup wizard creates this for you, or configure manually:

```toml
default_engine = "codex"

[transports.telegram]
bot_token = "123456789:ABC..."
chat_id = 123456789
session_mode = "chat"

[projects.myapp]
path = "~/dev/myapp"
default_engine = "claude"

[cost_budget]
enabled = true
max_cost_per_run = 2.00
max_cost_per_day = 10.00
```

See the [full configuration reference](https://untether.cc/reference/config/) for all options.

## Requirements

- **Python 3.12+** — `uv python install 3.14`
- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- At least one agent CLI on PATH: `codex`, `claude`, `opencode`, or `pi`

## Engine guides

Detailed setup and usage for each engine:

- [Claude Code guide](https://untether.cc/reference/runners/claude/runner/) — permission modes, plan mode, cost tracking, interactive approvals
- [Codex guide](https://untether.cc/reference/runners/codex/exec-json-cheatsheet/) — profiles, extra args, exec mode
- [OpenCode guide](https://untether.cc/reference/runners/opencode/runner/) — model selection, 75+ providers, local models
- [Pi guide](https://untether.cc/reference/runners/pi/runner/) — multi-provider auth, model and provider selection
- [Configuration reference](https://untether.cc/reference/config/) — full walkthrough of `untether.toml`
- [Troubleshooting guide](https://untether.cc/how-to/troubleshooting/) — common issues and solutions

## Documentation

Full documentation is available at **[untether.cc](https://untether.cc/)**.

- [Install and onboard](https://untether.cc/tutorials/install/) — setup wizard walkthrough
- [First run](https://untether.cc/tutorials/first-run/) — send your first task
- [Projects and branches](https://untether.cc/tutorials/projects-and-branches/) — multi-repo workflows
- [Multi-engine workflows](https://untether.cc/tutorials/multi-engine/) — switching between agents
- [Architecture](https://untether.cc/explanation/architecture/) — how the pieces fit together

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and guidelines.

## Acknowledgements

Untether is a fork of [takopi](https://github.com/banteg/takopi) by [@banteg](https://github.com/banteg), which provided the original Telegram-to-Codex bridge. Untether extends it with interactive permission control, multi-engine support, plan mode, cost tracking, and many other features.

## Licence

[MIT](LICENSE)
