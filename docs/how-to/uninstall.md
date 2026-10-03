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
| `active_loops.json` | Pending Claude Code loop iterations (Loop mode) |
| `stats.json` | Per-engine run counts and usage statistics |
| `session_costs.json`, `session_quarantine.json` | Per-session cost baselines and quarantined Claude sessions |
| `daily_cost.json` | Today's running API cost total, used by `max_cost_per_day` and `/health` |
| `triggers_history.json`, `run_once_fired.json`, `run_once_pending.json` | Last-fired times for crons and webhooks, spent `run_once` crons, `run_once` crons waiting on a send retry |
| `last_update_id.json`, `untether.lock` | Telegram update offset and the single-instance lock |

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
