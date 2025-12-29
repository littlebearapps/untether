# Telegram Codex Bridge (Codex)

Route Telegram replies back into Codex sessions using non-interactive
`codex exec` + `codex exec resume`.

The bridge does not persist routes. It resumes only when the incoming
message (or the replied-to bot message) contains an explicit `resume: <uuid>`
line.

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

## Option 1: exec/resume

Run:

```bash
uv run exec-bridge
```

Optional flags:

- `--final-notify/--no-final-notify` (default notify via new message)
- `--debug/--no-debug` (default no debug logging; use `--debug | tee debug.log` to capture)
- `--cd PATH` (pass through to `codex --cd`)
- `--model NAME` (pass through to `codex exec`)

Progress updates are always sent silently.
Pending updates are always ignored on startup.
Progress updates are throttled to roughly every 2 seconds.

To resume an existing thread without a database, reply with (or include) the session id shown at the end of the bot response:

`resume: \`019b66fc-64c2-7a71-81cd-081c504cfeb2\``

## Files
- `src/codex_telegram_bridge/constants.py`: limits and config path constants
- `src/codex_telegram_bridge/config.py`: config loading and chat-id parsing helpers
- `src/codex_telegram_bridge/exec_render.py`: renderers for codex exec JSONL events
- `src/codex_telegram_bridge/rendering.py`: markdown rendering
- `src/codex_telegram_bridge/telegram_client.py`: Telegram Bot API client
- `src/codex_telegram_bridge/exec_bridge.py`: codex exec + resume bridge
