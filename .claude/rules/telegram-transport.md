---
applies_to: "src/untether/telegram/**"
---

# Telegram Transport Rules

## Outbox model

ALL Telegram writes (send, edit, delete) MUST go through `TelegramOutbox`. Never call Bot API methods directly from command handlers or bridge code.

- Sends: `transport.send(channel_id, message, options)`
- Edits: `transport.edit(ref, message)`
- Deletes: `transport.delete(ref)`

The outbox handles coalescing, priority scheduling, and rate limiting automatically.

## Callback data

- Max 64 bytes (Telegram enforced)
- Format: `prefix:action:id` (e.g. `ctrl:approve:req_123`)
- Must call `answerCallbackQuery` promptly to clear the button spinner

## Early callback answering

For time-sensitive callbacks (approval buttons), use early answering:
- Set `answer_early = True` on the callback backend
- Provide `early_answer_toast()` returning the toast text ("Approved", "Denied", etc.)
- Dispatch calls `answerCallbackQuery` before processing the action

## Ephemeral messages

Messages that should auto-delete when a run finishes:
- Register via `register_ephemeral_message(channel_id, anchor_msg_id, ref)` in `runner_bridge.py`
- `ProgressEdits.delete_ephemeral()` cleans them up on run completion

## Rate limiting

- Per-chat pacing: private 1.0 msg/s, groups 20/60 msg/s
- On 429: `RetryAfter` raised, op requeued unless superseded
- Non-429 errors: logged and dropped

## Message limits

- Telegram message limit: 4096 chars after entity parsing
- Untether trims to ~3500 chars by default
- Split mode (`message_overflow = "split"`) sends multiple messages

## Inline keyboards

- `RenderedMessage.extra["reply_markup"]["inline_keyboard"]` for buttons
- Approval buttons: detect transitions via keyboard length changes
- Push notification: sent separately (`notify=True`) when approval buttons appear
