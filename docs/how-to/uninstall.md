# Uninstall Untether

## 1. Stop the service

If Untether is running as a systemd service, stop it first:

```sh
systemctl --user stop untether
systemctl --user disable untether
```

## 2. Remove the CLI

=== "uv"

    ```sh
    uv tool uninstall untether
    ```

=== "pipx"

    ```sh
    pipx uninstall untether
    ```

## 3. Remove configuration and state

Untether stores all config and state in `~/.untether/` (or the path set by `UNTETHER_CONFIG_PATH`):

```sh
rm -rf ~/.untether/
```

This deletes:

| File | Contains |
|------|----------|
| `untether.toml` | Bot token, chat ID, engine settings, transport config |
| `*_state.json` | Chat preferences, session resume tokens, topic bindings |
| `active_progress.json` | Orphan message references (restart recovery) |
| `stats.json` | Per-engine run counts and usage statistics |

!!! warning "Bot token"
    `untether.toml` contains your Telegram bot token in plaintext. Deleting the file removes it from disk.

## 4. (Optional) Delete the Telegram bot

Removing Untether does not delete the Telegram bot itself. If you no longer need it:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/deletebot`
3. Select your bot from the list

## 5. (Optional) Remove agent CLIs

If you no longer need the agent CLIs that Untether wrapped:

```sh
npm uninstall -g @anthropic-ai/claude-code
npm uninstall -g @openai/codex
npm uninstall -g opencode-ai
npm uninstall -g @mariozechner/pi-coding-agent
npm uninstall -g @google/gemini-cli
npm uninstall -g @sourcegraph/amp
```
