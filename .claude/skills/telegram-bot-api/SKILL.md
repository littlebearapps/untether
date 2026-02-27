---
name: telegram-bot-api
description: >
  Raw Telegram Bot API patterns used by Untether. Covers httpx async HTTP client,
  inline keyboards, callback queries, long polling, rate limiting, outbox model,
  forum topics, voice transcription, and media group coalescing.
  NOT aiogram, python-telegram-bot, or grammY — Untether uses raw HTTP.
triggers:
  - working on Telegram transport code
  - modifying inline keyboards or callback queries
  - changing rate limiting or outbox behaviour
  - adding new Bot API methods
  - working on voice transcription
  - modifying forum topic handling
  - working on media group coalescing
---

# Telegram Bot API (Raw HTTP)

Untether uses a **custom Telegram Bot API client** built on `httpx` (async) and `msgspec` (JSON). There is no Telegram SDK dependency.

## Key files

| File | Purpose |
|------|---------|
| `src/untether/telegram/client.py` | `TelegramClient` — all Bot API calls |
| `src/untether/telegram/outbox.py` | `TelegramOutbox` — queued send/edit/delete with rate limiting |
| `src/untether/telegram/bridge.py` | `TelegramPresenter` — renders progress, inline keyboards, answers |
| `src/untether/telegram/loop.py` | Long polling loop (`getUpdates`), callback dispatch |
| `src/untether/telegram/commands/` | Command and callback handlers |
| `docs/reference/transports/telegram.md` | Full transport reference |

## Bot API call pattern

All calls go through `TelegramClient`, which wraps `httpx.AsyncClient`:

```python
# Typical Bot API call (inside TelegramClient)
resp = await self._http.post(
    f"{self._base_url}/bot{self._token}/{method}",
    json=params,
)
data = msgspec.json.decode(resp.content, type=TelegramResponse)
```

- Base URL: `https://api.telegram.org`
- Auth: bot token in the URL path (`/bot<token>/`)
- All responses decoded with `msgspec.json.decode` into typed structs
- Error handling: check `ok` field, raise on HTTP or Telegram errors

## Inline keyboards and callback queries

Permission requests and plan mode buttons use Telegram inline keyboards:

```python
# reply_markup structure in RenderedMessage.extra
{
    "reply_markup": {
        "inline_keyboard": [
            [{"text": "Approve", "callback_data": "ctrl:approve:<request_id>"}],
            [{"text": "Deny", "callback_data": "ctrl:deny:<request_id>"}],
            [{"text": "Pause & Outline Plan", "callback_data": "ctrl:discuss:<request_id>"}],
        ]
    }
}
```

- Callback data format: `<prefix>:<action>:<id>` (max 64 bytes)
- Must call `answerCallbackQuery` promptly to clear the spinner
- Early answering: set `answer_early = True` on the backend to clear the spinner immediately with a toast

## Long polling (`getUpdates`)

```python
# In telegram/loop.py
updates = await client.get_updates(offset=last_offset + 1, timeout=30)
for update in updates:
    last_offset = update.update_id
    await handle_update(update)
```

- Bypasses the outbox (direct API call)
- Retries on `RetryAfter` by sleeping for the provided delay
- No webhooks — Untether is designed for single-instance long polling

## Outbox model

All writes (send, edit, delete) go through `TelegramOutbox`:

- **Single worker** processes one op at a time
- **Keyed deduplication**: one pending op per key; new ops overwrite payload but preserve `queued_at`
- **Priority scheduling**: `(priority, queued_at)` ordering
  - send=0 (highest), delete=1, edit=2 (lowest)
- **Coalescing**: rapid edits to the same message naturally coalesce (only latest payload runs)

Key formats (include `chat_id` to avoid cross-chat collisions):
- `("edit", chat_id, message_id)` for edits
- `("delete", chat_id, message_id)` for deletes
- `("send", chat_id, replace_message_id)` when replacing a progress message
- Unique key for normal sends

## Rate limiting

- Per-chat pacing: `private_chat_rps` (default 1.0 msg/s), `group_chat_rps` (default 20/60 msg/s)
- Global `next_at` timestamp — worker waits until `max(next_at, retry_at)`
- On 429: `RetryAfter` raised using `parameters.retry_after`; op requeued if no newer op superseded it
- Non-429 errors: logged and dropped (no retry)

## Replace progress messages

`send_message(replace_message_id=...)`:
1. Drops any pending edit for the progress message
2. Enqueues the send at highest priority
3. On success, enqueues a delete for the old progress message

## Voice transcription

```toml
[transports.telegram]
voice_transcription = true
voice_transcription_model = "gpt-4o-mini-transcribe"
```

1. Download voice payload from Telegram (`getFile` + HTTP fetch)
2. Transcribe with OpenAI-compatible API (or local Whisper server)
3. Route transcript through same command/directive pipeline as typed text

## Forum topics

Topics bind Telegram forum threads to a project/branch:
- Scope modes: `auto`, `main`, `projects`, `all`
- `/topic <project> @branch` creates and binds a topic
- Resume tokens persist per topic
- Bot needs **Manage Topics** permission

## Media group coalescing

Multiple documents sent as an album share a `media_group_id`:
- `MediaGroupBuffer` collects messages with the same `media_group_id`
- After `media_group_debounce_s` seconds of quiet, buffer flushes
- Processed as a single batch via `handle_media_group`

## Forwarded message coalescing

Comment + forwarded messages arrive as separate updates:
- Wait `forward_coalesce_s` seconds for additional forwards
- Forwards appended to the prompt; don't start their own runs
- Forwarded messages alone don't start runs

## Message overflow

- Default: trim to ~3500 chars (Telegram limit is 4096 after entity parsing)
- Split mode: multiple messages with "continued (N/M)" headers
- Configure via `message_overflow = "trim" | "split"`

## Approval push notifications

`edit_message_text` doesn't trigger phone push notifications. Untether sends a separate `notify=True` message ("Action required -- approval needed") when approval buttons appear. The `_approval_notified` flag resets when buttons disappear.

## Ephemeral message cleanup

Approval-related messages auto-delete:
- "Action required" notification — deleted when user clicks a button
- "Approved/Denied" feedback — deleted when the run finishes
- Tracked via `_approval_notify_ref` (in `ProgressEdits`) and `_EPHEMERAL_MSGS` (in `runner_bridge.py`)
