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
- Untether splits long responses across multiple messages by default (~3500 chars per chunk)
- Trim mode (`message_overflow = "trim"`) truncates to a single message

## Inline keyboards

- `RenderedMessage.extra["reply_markup"]["inline_keyboard"]` for buttons
- Approval buttons: detect transitions via keyboard length changes
- Push notification: sent separately (`notify=True`) when approval buttons appear

## Outbox file delivery

Agents write files to `.untether-outbox/` during a run. On completion, `outbox_delivery.py` scans, validates (deny-glob, size limit, file count cap), sends as Telegram documents with `📎` captions, and cleans up. Configure via `[transports.telegram.files]`: `outbox_enabled`, `outbox_dir`, `outbox_max_files`, `outbox_cleanup`.

## Progress persistence

`progress_persistence.py` tracks active progress messages in `active_progress.json`. On startup, orphan messages from a prior instance are edited to "⚠️ interrupted by restart" with keyboard removed.

## Telegram update_id persistence (#287)

`offset_persistence.py` persists the last confirmed Telegram `update_id` to `last_update_id.json` (sibling to config). On startup, `poll_updates` loads the saved offset and passes `offset=saved+1` to `getUpdates` so restarts don't drop or re-process updates within Telegram's 24h retention window. Writes are debounced (5s interval, 100-update cap) via `DebouncedOffsetWriter` — see its docstring for the crash/replay tradeoff. Flush happens automatically in the `poll_updates` finally block.

## TelegramBridgeConfig hot-reload (#286)

`TelegramBridgeConfig` is unfrozen (slots preserved) as of rc4. `update_from(settings)` applies a reloaded `TelegramTransportSettings` to the live config; `handle_reload()` in `loop.py` calls it and refreshes the two cached copies in `TelegramLoopState`. `route_update()` reads `cfg.allowed_user_ids` live so allowlist changes take effect on the next message. Restart-only keys (`bot_token`, `chat_id`, `session_mode`, `topics`, `message_overflow`) still warn with `restart_required=true`.

## sd_notify (#287)

`untether.sdnotify.notify(message)` sends `READY=1`/`STOPPING=1` to systemd's notify socket (stdlib only — no dependency). `NOTIFY_SOCKET` absent → no-op False. `poll_updates` sends `READY=1` after `_send_startup` succeeds; `_drain_and_exit` sends `STOPPING=1` at drain start. Requires `Type=notify` + `NotifyAccess=main` in the systemd unit (see `contrib/untether.service`).

## /at command (#288)

`telegram/at_scheduler.py` is a module-level holder for the task group + `run_job` closure; `install()` is called from `run_main_loop` once both are available. `AtCommand.handle` calls `schedule_delayed_run(chat_id, thread_id, delay_s, prompt)` which starts an anyio task that sleeps then dispatches. Pending delays tracked in `_PENDING`; `/cancel` drops them via `cancel_pending_for_chat(chat_id)`. Drain integration via `at_scheduler.active_count()`. No persistence — restart cancels all pending delays (documented in issue body).

## Plan outline rendering

Plan outlines render as formatted Telegram text via `render_markdown()` + `split_markdown_body()`. Approval buttons (✅/❌/📋) appear on the last outline message. Outline and notification messages are cleaned up on approve/deny via `_OUTLINE_REGISTRY`.

## /new command

`/new` cancels all running tasks for the chat via `_cancel_chat_tasks()` (in `commands/topics.py`) before clearing stored sessions. This prevents process leaks from orphaned Claude/engine subprocesses.

## After changes

If this change will be released, run integration tests T1-T10 (Telegram transport), S7 (rapid-fire), S8 (long prompt) via `@untether_dev_bot`. See `docs/reference/integration-testing.md` — the "Changed area" table maps `telegram/*.py` changes to required tests.

**NEVER use `@hetz_lba1_bot` (staging) for initial dev testing. ALWAYS use `@untether_dev_bot` first.** Stage rc versions on `@hetz_lba1_bot` only after dev integration tests pass.
