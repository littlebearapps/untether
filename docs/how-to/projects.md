# Projects

Projects let you route messages to repos from anywhere using `/alias`.

## Register a repo as a project

```sh
cd ~/dev/happy-gadgets
untether init happy-gadgets
```

This adds a project to your config:

=== "untether config"

    ```sh
    untether config set projects.happy-gadgets.path "~/dev/happy-gadgets"
    ```

=== "toml"

    ```toml
    [projects.happy-gadgets]
    path = "~/dev/happy-gadgets"
    ```

## Target a project from chat

Send:

```
/happy-gadgets pinky-link two threads
```

## Project-specific settings

Projects can override global defaults:

=== "untether config"

    ```sh
    untether config set projects.happy-gadgets.path "~/dev/happy-gadgets"
    untether config set projects.happy-gadgets.default_engine "claude"
    untether config set projects.happy-gadgets.worktrees_dir ".worktrees"
    untether config set projects.happy-gadgets.worktree_base "master"
    ```

=== "toml"

    ```toml
    [projects.happy-gadgets]
    path = "~/dev/happy-gadgets"
    default_engine = "claude"
    worktrees_dir = ".worktrees"
    worktree_base = "master"
    ```

If you expect to edit config while Untether is running, enable hot reload:

=== "untether config"

    ```sh
    untether config set watch_config true
    ```

=== "toml"

    ```toml
    watch_config = true
    ```

## Set a default project

If you mostly work in one repo:

=== "untether config"

    ```sh
    untether config set default_project "happy-gadgets"
    ```

=== "toml"

    ```toml
    default_project = "happy-gadgets"
    ```

## Related

- [Context resolution](../reference/context-resolution.md)
- [Worktrees](worktrees.md)
