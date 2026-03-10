# Verbose progress mode

Untether shows progress messages as the agent works, updating in real time. Control how much detail you see — from compact summaries to full tool details — so you can follow along from your phone or get a quick glance from [Telegram](https://telegram.org) on any device.

## Enable verbose mode

Send `/verbose on` to see full details for each action:

```
/verbose on
```

In verbose mode, progress messages include file paths, command text, glob patterns, and other tool-specific details alongside the action status.

## Compact mode

Send `/verbose off` to switch back to compact summaries:

```
/verbose off
```

Compact mode shows only the action status and title — no extra detail. This is the default.

## Compare the two

Here's the same action shown in both modes:

!!! note "Compact"
    ```
    ...tool: edit: Update import order
    ```

!!! note "Verbose"
    ```
    ...tool: edit: Update import order
       file: src/untether/runner_bridge.py
       - from untether.events import EventFactory
       + from untether.events import EventFactory, StartedEvent
    ```

Verbose mode adds context lines underneath each action, so you can see exactly what the agent is doing without waiting for the final answer.

## Set global default in config

To make verbose the default for all chats:

=== "untether config"

    ```sh
    untether config set progress.verbosity "verbose"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [progress]
    verbosity = "verbose"  # verbose | compact
    ```

## Adjust max action lines

Control how many actions appear in the progress message. Actions beyond this limit are collapsed:

=== "untether config"

    ```sh
    untether config set progress.max_actions 10
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [progress]
    max_actions = 10  # 0-50, default 5
    ```

Set to `0` to hide the action list entirely, or increase it to see more history.

## Per-chat override

The `/verbose` toggle overrides the global config for the current chat. This override persists until you clear it or restart Untether.

## Clear override

Remove the per-chat setting to revert to the global config value:

```
/verbose clear
```

## Related

- [Configuration](../reference/config.md) — full config reference for progress settings
- [Chat sessions](chat-sessions.md) — session management and per-chat state
