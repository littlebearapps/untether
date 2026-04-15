# Glossary

Quick definitions for terms used throughout the Untether documentation.

## Core concepts

**Engine**
:   A coding agent CLI that Untether runs as a subprocess. Each engine is a separate tool — Claude Code, Codex, OpenCode, Pi, Gemini CLI, or Amp. Untether spawns the engine, reads its output, and renders progress in Telegram. You can switch engines per-message with directives like `/claude` or `/codex`.

**Runner**
:   The Untether component that manages an engine subprocess. Each engine has a dedicated runner (e.g. `ClaudeRunner`, `CodexRunner`) that translates between the engine's output format and Untether's internal events.

**Directive**
:   A prefix at the start of your Telegram message that tells Untether how to run the task. Engine directives (`/claude`, `/codex`), project directives (`/myapp`), and branch directives (`@feat/login`) can be combined in any order before your prompt.

**Project**
:   A registered repo on your machine. You register a project with `untether init <alias>` and then target it from Telegram with `/<alias>`. Projects let you switch between repos without restarting Untether.

**Resume token**
:   An identifier that the engine returns after a run finishes. It allows a future message to continue the same conversation — the agent remembers what it was working on. Resume tokens appear as lines like `codex resume abc123` at the bottom of a final message.

**Resume line**
:   The line in a Telegram message that shows the resume token (e.g. `codex resume abc123`). When visible, you can reply to that message to continue the conversation from that point. Resume lines can be hidden for a cleaner chat.

## Session and conversation

**Session mode**
:   Controls how follow-up messages are handled. **Chat mode** (`chat`) auto-resumes the previous conversation — just send another message. **Stateless mode** (`stateless`) treats every message as independent unless you reply to one with a resume line.

**Chat mode**
:   A session mode where Untether automatically continues the most recent conversation. Send a message and it picks up where the last run left off. Use `/new` to start fresh.

**Stateless mode**
:   A session mode where every message starts a new conversation unless you explicitly reply to a previous message that has a resume line.

**Workflow**
:   One of three presets chosen during onboarding: **assistant** (chat mode, clean output), **workspace** (chat mode with forum topics), or **handoff** (stateless with resume lines). Each preset configures session mode, topics, and resume line visibility.

## Interactive control (Claude Code)

**Permission mode**
:   The level of oversight Untether applies to Claude Code's actions. **Plan** shows Approve/Deny buttons for every tool call. **Auto** auto-approves tools and plan transitions. **Accept edits** (`off`) runs fully autonomously with no buttons.

**Approval buttons**
:   Inline Telegram buttons that appear when Claude Code wants to perform an action in plan mode. You tap **Approve** to allow the action, **Deny** to block it, or **Pause & Outline Plan** to require a written plan first. After an outline is written, you can also tap **Let's discuss** to talk about the plan before deciding.

**Progress message**
:   The Telegram message that Untether updates in real time as the agent works. It shows the engine, elapsed time, step count, and a list of recent tool calls. When the run finishes, it's replaced by the final answer.

**Diff preview**
:   A compact view of what Claude Code is about to change, shown alongside approval buttons. For file edits, it shows removed lines (`- old`) and added lines (`+ new`). For shell commands, it shows the command to be run.

## Projects and branches

**Branch**
:   A separate line of development in a git repository. Think of it as a copy of your code where you can make changes without affecting the main version. When done, changes from a branch can be merged back.

**Worktree**
:   A second checkout of the same repository in a different directory. Instead of switching branches (which changes files in your main directory), a worktree lets the agent work on a branch in a separate folder. Your main checkout stays untouched.

**Branch directive**
:   The `@branch-name` prefix in a Telegram message (e.g. `@feat/login`). It tells Untether to run the agent in a worktree for that branch, creating the branch and worktree if they don't exist.

## Messaging

**Final message**
:   The Telegram message Untether sends when a run completes. It contains the agent's answer, a footer with engine/model info, and optionally a resume line. This replaces the progress message.

**Meta line**
:   The footer at the bottom of a final message showing which engine, model, and permission mode were used (e.g. `sonnet · plan`), plus cost if available.

**Outbox**
:   Untether's internal message queue. All Telegram writes (sends, edits, deletes) pass through the outbox, which handles rate limiting and message coalescing automatically.

## Configuration

**`untether.toml`**
:   The main config file, usually at `~/.untether/untether.toml`. Controls the default engine, Telegram transport settings, project registrations, cost budgets, voice transcription, and all other options.

**Topic**
:   A Telegram forum thread. When topics are enabled, each forum thread can bind to a project and branch, with its own engine default and session. Requires a forum-enabled Telegram supergroup.

**Trigger**
:   A webhook or cron rule that starts a run without a Telegram message. Triggers let external systems (GitHub, CI, schedulers) send tasks to Untether.

**Hot-reload**
:   Applying configuration changes without restarting Untether. Requires `watch_config = true`. Hot-reloadable settings include trigger crons/webhooks, voice transcription, file transfer, and `allowed_user_ids`. Structural settings like `bot_token`, `chat_id`, and `session_mode` require a restart.

## Scheduling & triggers

**Delayed run**
:   A one-shot run scheduled via `/at <duration> <prompt>`. The prompt executes after the specified delay (60 seconds to 24 hours). Pending delays are held in memory and lost on restart. Per-chat cap of 20.

**Webhook action**
:   A lightweight action a webhook performs without spawning an agent run. Available actions: `file_write` (save POST body to disk), `http_forward` (relay payload to another URL), and `notify_only` (send a Telegram message). The default action (`agent_run`) starts a full agent session.
