# Context binding

Bind a chat or forum topic to a specific project and branch, so every message runs in the right directory automatically — no need to prefix with `/<project>` each time. Set it once from [Telegram](https://telegram.org) and forget about it.

## Check current context

Send `/ctx` to see what project and branch are active for the current scope:

```
/ctx
```

!!! untether "Untether"
    bound ctx: backend @feat/api-v2<br>
    resolved ctx: backend @feat/api-v2 (source: bound)

`bound ctx` is what `/ctx set` stored for this chat or topic; `resolved ctx` is what the next message will actually use, and `source` says where it came from: `bound` (this chat's binding), `ambient` (inside a forum topic, the topic's binding merged with the chat's project), `default_project` (the chat's project or the global `default_project`) or `none`. If you send `/ctx` as a reply to a message with a `dir:` line, it shows `reply_ctx`. If nothing is bound, `bound ctx` reads `none` and a `note:` line shows how to bind one. Inside a forum topic the reply also shows the topics scope and the topic's stored sessions.

## Bind to a project

Use `/ctx set` with a project alias to bind the current chat or topic:

```
/ctx set myproject
```

All subsequent messages in this chat run in that project's directory. You no longer need to prefix messages with `/myproject`.

## Bind to project + branch

Add `@branch` to also bind to a specific git branch:

```
/ctx set myproject @feature-branch
```

When a branch is specified, Untether creates or reuses a worktree for that branch under the project's `worktrees_dir`, and the agent runs inside it. If the project's main checkout is already on that branch, the agent runs in the project directory instead. See [Worktrees](worktrees.md).

!!! tip "Branch shorthand"
    Inside a forum topic of a project chat you can set just the branch: `/ctx set @new-branch`, and the chat's project is implied. Outside a topic, a bare `@branch` uses the global `default_project` and is refused (`project is required`) if none is set. In a topic of a chat that isn't bound to a project, a bare `@branch` is always refused; name the project.

## Clear binding

Remove the context binding to revert to the default:

```
/ctx clear
```

The chat or topic falls back to the chat's project (if the chat is a project chat via `projects.<alias>.chat_id`), then the default project (if configured), then the global startup directory.

!!! note "Claude sessions that are still open"
    If Claude Code is still holding a session open after its answer (for background tasks), `/ctx set`, `/ctx clear` or rebinding the topic closes that session, and your next message resumes it in the new project or branch, as after `/model` or `/planmode`. Background tasks still running in it are stopped with a "Settings changed" notice. Before v0.36.0 the next message could run in the old working directory ([#996](https://github.com/littlebearapps/untether/issues/996)).

## Create a bound topic

In a forum-enabled group, use `/topic` to create a new forum topic pre-bound to a project and branch:

```
/topic myproject @branch
```

The topic is created with the context already set — you can start sending messages immediately without running `/ctx set`. Untether names the topic to reflect the binding.

!!! note "Requires topics"
    The `/topic` command only works in forum-enabled supergroups where the bot has Manage Topics permission. See [Topics](topics.md) for setup.

## Resolution order

When Untether receives a message, it resolves context using the first match from this list:

1. **Topic binding** — set via `/ctx set` or `/topic` inside a forum thread
2. **Chat binding** — set via `/ctx set` in a private or group chat
3. **Chat's project** — the project whose `projects.<alias>.chat_id` is this chat (see [Route by chat](route-by-chat.md))
4. **`default_project`** — configured in your `untether.toml`
5. **Startup directory** — the working directory when Untether started

The first match wins. A topic binding always takes priority over a chat-level binding, which takes priority over the chat's project and then the global default. A reply to a message carrying a `dir:` line, or a `/<project>` / `@branch` directive at the start of a message, overrides all of these for that one message (see [Context resolution](../reference/context-resolution.md)).

## Related

- [Projects](projects.md) — register repos as projects
- [Worktrees](worktrees.md) — branch-based worktree runs
- [Topics](topics.md) — forum topic setup and management
- [Context resolution](../reference/context-resolution.md) — full resolution logic reference
