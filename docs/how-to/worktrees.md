# Worktrees

Run tasks on multiple branches in parallel without touching your main checkout. Untether creates isolated git worktrees so you can kick off work on `@feat/auth` and `@fix/memory-leak` at the same time — all from Telegram.

## Enable worktree-based runs for a project

Add a `worktrees_dir` (and optionally a base branch) to the project:

=== "untether config"

    ```sh
    untether config set projects.happy-gadgets.path "~/dev/happy-gadgets"
    untether config set projects.happy-gadgets.worktrees_dir ".worktrees"
    untether config set projects.happy-gadgets.worktree_base "master"
    ```

=== "toml"

    ```toml
    [projects.happy-gadgets]
    path = "~/dev/happy-gadgets"
    worktrees_dir = ".worktrees"      # relative to project path
    worktree_base = "master"          # base branch for new worktrees
    ```

## Run in a branch worktree

Send a message like:

```
/happy-gadgets @feat/memory-box freeze artifacts forever
```

<!-- SCREENSHOT: Telegram message showing a worktree run with the @branch directive and project context in the footer -->

## Ignore `.worktrees/` in git status

If you use the default `.worktrees/` directory inside the repo, add it to a gitignore.
One option is a global ignore:

```sh
git config --global core.excludesfile ~/.config/git/ignore
echo ".worktrees/" >> ~/.config/git/ignore
```

## Context persistence

When project/worktree context is active, Untether includes a `ctx:` footer in messages.
When you reply, this context carries forward (you usually don’t need to repeat `/<project-alias> @branch`).

## Related

- [Context resolution](../reference/context-resolution.md)
