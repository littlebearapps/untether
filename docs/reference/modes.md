# Workflow modes

Untether supports three workflow modes inherited from [takopi](https://github.com/banteg/takopi). Each mode configures three settings that control session continuation and resume line display.

## Mode comparison

| Setting | Assistant | Workspace | Handoff |
|---------|-----------|-----------|---------|
| `session_mode` | `"chat"` | `"chat"` | `"stateless"` |
| `topics.enabled` | `false` | `true` | `false` |
| `show_resume_line` | `false` | `false` | `true` |

All other features — commands, engines, permission control, cost tracking, file delivery, stall detection — work identically across all three modes.

## Assistant

**Best for:** single developer, private chat.

Messages automatically continue the last session. Use `/new` to start a fresh session.

- **Session mode:** `chat` (auto-resume)
- **Topics:** disabled
- **Resume lines:** hidden (cleaner chat)
- **State file:** `telegram_chat_sessions_state.json`

```toml title="untether.toml"
[transports.telegram]
session_mode = "chat"
show_resume_line = false

[transports.telegram.topics]
enabled = false
```

## Workspace

**Best for:** teams, multiple projects or branches.

Same auto-resume as assistant, but scoped per Telegram forum topic. Each topic binds to a project and branch via `/ctx set <project> @<branch>`. Create new topics with `/topic <project> @<branch>`.

Requires a Telegram supergroup with forum topics enabled and the bot added as admin with "manage topics" permission.

- **Session mode:** `chat` (auto-resume within each topic)
- **Topics:** enabled — each topic gets its own resume tokens, default engine, trigger mode, and model/reasoning overrides
- **Resume lines:** hidden
- **State file:** `telegram_topics_state.json`

```toml title="untether.toml"
[transports.telegram]
session_mode = "chat"
show_resume_line = false

[transports.telegram.topics]
enabled = true
scope = "auto"
```

### Topic scope

The `scope` setting controls which chats allow topics:

| Scope | Behaviour |
|-------|-----------|
| `auto` (default) | Topics in project chats if projects exist, otherwise main chat |
| `main` | Main chat only |
| `projects` | Project chats only |
| `all` | Main chat and all project chats |

### Workspace-only commands

- `/ctx show` — display current topic's bound context
- `/ctx set <project> @<branch>` — bind topic to a project/branch
- `/ctx clear` — unbind topic context
- `/topic <project> @<branch>` — create a new forum topic for a project/branch

## Handoff

**Best for:** terminal-based workflow where you copy resume tokens.

Each message starts a new run. Continue a previous session by replying to its bot message or using `/continue`. Resume lines are always shown so you can copy them to a terminal.

- **Session mode:** `stateless` (reply-to-continue)
- **Topics:** disabled
- **Resume lines:** always shown
- **No state file** — `chat_session_store` is not initialised

```toml title="untether.toml"
[transports.telegram]
session_mode = "stateless"
show_resume_line = true

[transports.telegram.topics]
enabled = false
```

### Continuation in handoff mode

Since there is no auto-resume, you have three ways to continue a session:

1. **Reply-to-continue:** reply to a previous bot message in Telegram. Untether extracts the resume token from that message.
2. **`/continue`:** picks up the most recent CLI session using the engine's native continue flag.
3. **Copy to terminal:** copy the resume line from the bot message (e.g. `` `codex resume abc123` ``) and run it directly in a terminal.

## Changing modes

Edit `session_mode`, `show_resume_line`, and `topics.enabled` in your `untether.toml` and restart:

```bash
systemctl --user restart untether      # staging
systemctl --user restart untether-dev  # dev
```

There is no migration step — the new mode takes effect on restart.

## Mode-agnostic features

These work identically in all three modes:

- All 6 engine runners (Claude, Codex, OpenCode, Pi, Gemini, AMP)
- All commands except `/ctx` and `/topic` (workspace-only)
- Permission control (approve/deny/discuss, plan mode)
- AskUserQuestion with option buttons
- `/continue` cross-environment resume
- `/config` inline settings menu
- `/browse` file browser
- `/export` session transcript
- `/usage` cost stats
- File upload and outbox delivery
- Voice transcription
- Cost tracking and budget alerts
- Stall detection and watchdog
- Trigger mode (all vs mentions)
- Model and reasoning overrides
