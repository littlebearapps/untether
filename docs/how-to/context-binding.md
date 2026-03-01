# Context binding

Untether can bind a chat or forum topic to a specific project and branch, so every message in that scope runs in the right directory automatically — no need to prefix with `/<project>` each time. Manage bindings from Telegram, available on iPhone, iPad, Android, Mac, Windows, Linux, and Telegram Web.

## Check current context

Send `/ctx` to see what project and branch are active for the current scope:

```
/ctx
```

!!! untether "Untether"
    **Project:** backend
    **Branch:** feat/api-v2
    **Source:** topic binding

If no context is bound, Untether shows the default project (if configured) or the startup directory.

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

When a branch is specified and worktrees are enabled for the project, Untether creates or reuses a worktree for that branch. The agent runs inside the worktree directory.

!!! tip "Branch shorthand"
    If you're already bound to a project, you can set just the branch: `/ctx set @new-branch`.

## Clear binding

Remove the context binding to revert to the default:

```
/ctx clear
```

The chat or topic returns to using the default project (if configured) or the global startup directory.

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
3. **`default_project`** — configured in your `untether.toml`
4. **Startup directory** — the working directory when Untether started

The first match wins. A topic binding always takes priority over a chat-level binding, which takes priority over the global default.

## Related

- [Projects](projects.md) — register repos as projects
- [Worktrees](worktrees.md) — branch-based worktree runs
- [Topics](topics.md) — forum topic setup and management
- [Context resolution](../reference/context-resolution.md) — full resolution logic reference
