# Topics

Topics bind Telegram **forum threads** to a project/branch context. Each topic keeps its own session and default engine, which is ideal for teams or multi-project work.

!!! tip "Workspace workflow"
    If you chose the **workspace** workflow during [onboarding](../tutorials/install.md), topics are already enabled. This guide covers advanced topic configuration and usage.

## Why use topics

- Keep each thread tied to a repo + branch
- Avoid context collisions in busy team chats
- Set a default engine per topic with `/agent set`

## Requirements checklist

- The chat is a **forum-enabled supergroup** (enable Topics in group settings — this auto-converts to supergroup)
- The bot is an **admin** in the group
- The bot has **Manage Topics** permission (`can_manage_topics`) — needed for creating/editing topics; without it, the bot logs a warning but can still operate in existing topics
- **Group privacy** is disabled for the bot via @BotFather (`/setprivacy` → Disable) — otherwise the bot only sees commands and @mentions, not plain text messages
- After changing privacy, **remove and re-add** the bot to the group for the change to take effect
- If you want topics in project chats, set `projects.<alias>.chat_id`

!!! note "Setting up workspace from scratch"
    If you didn't choose workspace during onboarding and want to enable topics now:

    1. Create a group and enable topics in group settings
    2. Add your bot as admin with "Manage Topics" permission
    3. Update your config to enable topics (see below)

## Enable topics

=== "untether config"

    ```sh
    untether config set transports.telegram.topics.enabled true
    untether config set transports.telegram.topics.scope "auto"
    ```

=== "toml"

    ```toml
    [transports.telegram.topics]
    enabled = true
    scope = "auto" # auto | main | projects | all
    ```

### Scope explained

- `auto` (default): uses `projects` if any project chats exist, otherwise `main`
- `main`: topics only in the main `chat_id`
- `projects`: topics only in project chats (`projects.<alias>.chat_id`)
- `all`: topics available in both the main chat and project chats

## Create a bound topic

Send this anywhere in the forum group:

```
/topic <project> @branch
```

Examples:

- In the main chat: `/topic backend @feat/api`
- In a project chat: `/topic @feat/api` (project is implied)

Untether creates a **new** topic named after the context, binds it, and posts the binding as the topic's first message. A branch is required. If a topic for that project and branch already exists, Untether renames it back to the context name and says so instead of creating a second one.

!!! untether "Untether"
    created topic `backend @feat/api`.

To bind the topic you're already in, use `/ctx set <project> @branch` inside it (below).

<!-- TODO: capture screenshot -->
<!-- <img src="../assets/screenshots/forum-topic-context.jpg" alt="Forum topic bound to project and branch with renamed title and context footer" width="360" loading="lazy" /> -->

## Inspect or change the binding

- `/ctx` shows the current binding
- `/ctx set <project> @branch` updates it
- `/ctx clear` removes it

Note: Outside topics (private chats or main group chats), `/ctx` binds the chat context instead of a topic.

## Reset a topic session

Use `/new` inside the topic to cancel any running task and clear stored sessions for that thread. Only this topic's run (and its `/loop` schedules) is cancelled — other topics keep running. `/new` or `/cancel` in General likewise only touches General's work ([#826](https://github.com/littlebearapps/untether/issues/826)).

`/cancel` without a reply follows the same rule: in a topic it stops that topic's run, or replies "nothing running in this topic." when only other topics are busy. When nothing is running it also drops the topic's pending `/at` runs and loops and says what it dropped (`❌ cancelled 1 pending /at run and 1 active loop.`); a Claude session that is only idling after its answer is closed rather than counted as a run (`nothing running in this topic — closed the idle session.`). It always replies ([#902](https://github.com/littlebearapps/untether/issues/902)). `/new` cancels runs and loops but leaves pending `/at` runs alone.

!!! note "Scheduled runs"
    Cron and webhook runs have no topic: they run in General, so only `/new` or `/cancel` in General cancels them. An `/at` run belongs to the topic it was scheduled from. Claude Code `/loop` schedules belong to the topic whose run created them, and each loop iteration is posted back in that topic.

## Set a default engine per topic

Use `/agent set` inside the topic:

```
/agent set claude
```

## Set the follow-up mode per topic

For Claude Code, send `/steer` (no text) inside a topic to have messages sent there while Claude is working steered into the run, or `/queue` to go back to waiting for the turn to end. It applies to that topic only. See [Steer follow-ups](steer-follow-ups.md).

Notices about a run — such as `🔄 Restarting — waiting for your run to finish…` during a restart — are posted in the topic that owns the run ([#665](https://github.com/littlebearapps/untether/issues/665)).

## State files

Topic bindings and sessions live in:

- `telegram_topics_state.json`

## Common issues and fixes

- **"topics commands are only available..."**
  - Your `scope` does not include this chat. Update `topics.scope`.
- **"chat is not a supergroup" / "topics enabled but chat does not have topics"**
  - Convert the group to a supergroup and enable topics.
- **"topics enabled but bot is not an admin"** (startup error)
  - Promote the bot to admin and grant Manage Topics. An admin without Manage Topics starts, but logs `topics.manage_topics.missing` and can't create topics.
- **"topics enabled but no project chats are configured"** (startup error)
  - With `scope = "projects"`, set `projects.<alias>.chat_id` for your forum chats, or use `scope = "main"`.

## Related

- [Projects and branches](../tutorials/projects-and-branches.md)
- [Route by chat](route-by-chat.md)
- [Chat sessions](chat-sessions.md)
- [Multi-engine workflows](../tutorials/multi-engine.md)
- [Switch engines](switch-engines.md)
