# Context resolution

This page documents how Untether resolves **run context** (project, worktree/branch, engine) from messages.
For step-by-step usage, see [Projects](../how-to/projects.md) and [Worktrees](../how-to/worktrees.md).

## Overview

Projects let you give a repo an alias (used as `/alias` in messages) and opt into
worktree-based runs via `@branch`.

- If no projects are configured, Untether runs in the startup working directory.
- If a project is configured, `@branch` resolves/creates a git worktree and runs
  the task in that worktree.
- Progress/final messages include a `ctx:` footer when project context is active.

## Config schema (relevant subset)

All config lives in `~/.untether/untether.toml`.
See [Config](config.md) for the full reference.

=== "untether config"

    ```sh
    untether config set default_engine "codex"
    untether config set default_project "z80"
    untether config set transport "telegram"
    untether config set transports.telegram.bot_token "..."
    untether config set transports.telegram.chat_id 123
    untether config set projects.z80.path "~/dev/z80"
    untether config set projects.z80.worktrees_dir ".worktrees"
    untether config set projects.z80.default_engine "codex"
    untether config set projects.z80.worktree_base "master"
    untether config set projects.z80.chat_id -123
    ```

=== "toml"

    ```toml
    default_engine = "codex"       # optional
    default_project = "z80"        # optional
    transport = "telegram"         # optional, defaults to "telegram"

    [transports.telegram]
    bot_token = "..."              # required
    chat_id = 123                  # required

    [projects.z80]
    path = "~/dev/z80"             # required (repo root)
    worktrees_dir = ".worktrees"   # optional, default ".worktrees"
    default_engine = "codex"       # optional, per-project override
    worktree_base = "master"       # optional, base for new branches
    chat_id = -123                 # optional, project chat id
    ```

Legacy config note: top-level `bot_token` / `chat_id` are auto-migrated into
`[transports.telegram]` on startup.

Note on `worktrees_dir`:

- The default `.worktrees` lives inside the repo root. You'll see it as an
  untracked directory (with nested git worktrees) unless you ignore it.
- Options:
  - add `.worktrees/` to your repo `.gitignore`, or
  - set `worktrees_dir` to a path outside the repo (e.g. `~/.untether/worktrees/<alias>`).
  - add it to `.git/info/exclude` if you prefer a local-only ignore.

Validation rules:

- `projects` is optional.
- Each project entry must include `path` (string, non-empty).
- `default_project` must match a configured project alias.
- Project aliases cannot collide with engine ids or reserved commands (`/cancel`).
- `default_engine` and per-project `default_engine` must be valid engine ids.
- `projects.<alias>.chat_id` must be unique and must not match `transports.telegram.chat_id`.
- `transport` defaults to `"telegram"` when omitted; override per-run with `--transport`.

## `untether init`

`untether init <alias>` registers the current repo as a project alias.

Important behavior:

- The stored `path` is the **main checkout** of the repo, even if you run
  `untether init` inside a worktree. Untether resolves the repo root via the git
  common dir and writes that path to `[projects.<alias>].path`.
- `worktree_base` is set from the current repo using this resolution order:
  `origin/HEAD` → current branch → `master` → `main`.

## Directives and context resolution

Untether parses the first non-empty line of a message for a directive prefix.

Supported directives:

- `/<engine-id>` or `/<engine-id>@bot`: chooses the engine
- `/<project-alias>`: chooses a project alias
- `@branch`: chooses a git branch/worktree

Rules:

- Directives must be a contiguous prefix of the line; parsing stops at the first
  non-directive token.
- At most one engine directive, one project directive, and one `@branch` are
  allowed (duplicates are errors).
- If a reply contains a `ctx:` line, Untether **ignores new directives** and uses
  the reply context.

## Context footer (`ctx:`)

When a run has project context, Untether appends a footer line rendered as inline
code (backticked):

- With branch: `` `ctx: <project> @<branch>` ``
- Without branch: `` `ctx: <project>` ``

The `ctx:` line is parsed from replies and takes precedence over new directives.

When a message arrives in a chat whose `chat_id` matches `projects.<alias>.chat_id`,
Untether defaults the project context to that alias unless a reply `ctx:` or explicit
`/<project-alias>` directive is present.

In non-topic chats, `/ctx` can bind a chat context. That bound context is treated as
ambient and takes precedence over the default project mapping until cleared.

## Worktree resolution

When `@branch` is present:

```
worktrees_root = <project.path> / <worktrees_dir>
worktree_path = worktrees_root / <branch>
```

Branch validation:

- Must be non-empty
- Must not start with `/`
- Must not contain `..` path segments
- May include `/` (nested directories)
- The resolved worktree path must stay within `worktrees_root`

Worktree creation rules:

1) If `worktree_path` exists:
   - It must be a git worktree or Untether errors.
2) If it does not exist:
   - If local branch exists: `git worktree add <path> <branch>`
   - Else if remote `origin/<branch>` exists:
     `git worktree add -b <branch> <path> origin/<branch>`
   - Else:
     `git worktree add -b <branch> <path> <base>`

Base branch selection:

1) `projects.<alias>.worktree_base` (if set)
2) `origin/HEAD` (if present)
3) current checked out branch
4) `master` if it exists
5) `main` if it exists
6) otherwise error

When `@branch` is omitted:

- Untether runs in `<project.path>` (the main checkout).

## Examples

Start a new thread in a worktree:

```
/z80 @feat/streaming fix flaky test
```

Reply to a progress message to continue in the same context:

```
ctx: z80 @feat/streaming
```
