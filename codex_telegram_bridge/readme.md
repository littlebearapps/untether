# Telegram Codex Bridge (Codex)

Route Telegram replies back into Codex sessions. Includes three options:

1. Non-interactive `codex exec` + `codex exec resume`.
2. `codex mcp-server` with MCP stdio JSON-RPC.
3. tmux injection for interactive Codex sessions.

All options store a mapping from `(chat_id, bot_message_id)` to a route so replies can be routed correctly.

## Install

1. Ensure `uv` is installed.
2. From this folder, run the entrypoints with `uv run` (uses `pyproject.toml` deps).
3. Put your Telegram credentials in `~/.codex/telegram.toml`.

Example `~/.codex/telegram.toml`:

```toml
bot_token = "123:abc"
chat_id = 123456789
```

`chat_id` is used both for allowed messages and startup notifications.

Optional keys (by mode):

- common: `bridge_db`, `allowed_chat_ids`, `startup_chat_ids`
- exec/resume: `startup_message`, `codex_cmd`, `codex_workspace`, `codex_exec_args`, `max_workers`
- MCP server: `codex_mcp_cmd`, `codex_workspace`, `codex_sandbox`, `codex_approval_policy`

## Option 1: exec/resume

Run:

```bash
uv run exec-bridge
```

Optional flags:

- `--progress-edit-every FLOAT` (default `2.5`)
- `--progress-silent/--no-progress-silent` (default silent)
- `--final-notify/--no-final-notify` (default notify via new message)
- `--ignore-backlog/--process-backlog` (default ignore pending updates)

## Option 2: MCP server

Run:

```bash
uv run mcp-bridge
```

Optional flags:

- `--ignore-backlog/--process-backlog` (default ignore pending updates)

## Option 3: tmux

Reply injector:

```bash
uv run tmux-reply
```

Optional flags:

- `--ignore-backlog/--process-backlog` (default ignore pending updates)

Notifier (call from your existing hook):

```bash
uv run tmux-notify --tmux-target "codex1:0.0" --text "$TURN_TEXT"
```

Add `--chat-id` if `chat_id` is not set in `~/.codex/telegram.toml`.

## Files

- `src/codex_telegram_bridge/bridge_common.py`: shared Telegram client, chunking, and routing store
- `src/codex_telegram_bridge/exec_bridge.py`: codex exec + resume bridge
- `src/codex_telegram_bridge/mcp_bridge.py`: MCP stdio JSON-RPC bridge
- `src/codex_telegram_bridge/tmux_notify.py`: tmux notifier helper
- `src/codex_telegram_bridge/tmux_reply_bot.py`: tmux reply injector
