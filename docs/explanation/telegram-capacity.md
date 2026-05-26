# Telegram capacity and limits

How many concurrent agents can one Untether bot handle? Where does the system break first — Telegram or the host? This page explains the limits that actually constrain production use and where there's room to grow.

## TL;DR

**A single bot token comfortably handles dozens of concurrent agent runs.** The realistic ceiling on one token is about **25-40 concurrent actively-streaming agents** before Telegram's global broadcast cap becomes the binding constraint. Untether itself will usually run out of **host RAM** well before that — around **10-15 concurrent engine subprocesses** on a typical server. **Chat count itself is not a limit**: idle chats are free; only actively-streaming ones matter.

For normal Untether use (one or two tasks at a time), you're nowhere near any ceiling.

## Telegram Bot API limits

These are Telegram's published bot API limits. Untether's outbox is designed around them.

| Scope | Limit | Source |
|---|---|---|
| Per-chat (private or group) | ~1 message/sec | [Bots FAQ](https://core.telegram.org/bots/faq) |
| Per group (separate rule) | 20 messages/min to the same group | [Bots FAQ](https://core.telegram.org/bots/faq) |
| Global per-bot broadcast | ~30 messages/sec across all chats | [Bots FAQ](https://core.telegram.org/bots/faq) |
| `getUpdates` polling | **One poller per token** (second one gets 409 Conflict) | [tdlib#43](https://github.com/tdlib/telegram-bot-api/issues/43) |
| Chats per bot | **No documented cap** | — |
| Paid broadcast tier | 1000 msg/sec (requires 100k Stars + 100k MAU) | Bot API 7.7 |

Key nuances that aren't always obvious:

- `sendMessage`, `editMessageText`, and `answerCallbackQuery` all consume from the **same per-chat bucket**. An edit counts as much as a send.
- Community-observed soft ceiling: about **5 edits per minute per message** before Telegram issues a flood wait on that specific message.
- **Bot API 7.8 (June 2024)** added `parameters.scope` to 429 responses — either `"chat"` (isolate backoff to one chat) or `"global"` (freeze all methods). Older responses omit the field.

## How Untether handles these limits

Untether's `TelegramOutbox` (`src/untether/telegram/outbox.py`) serialises writes through a single async worker and applies per-chat pacing:

| Chat type | Default pace | Config key |
|---|---|---|
| Private | 1.0 msg/sec | `private_chat_rps` |
| Group | 20/60 ≈ one send per 3 sec | `group_chat_rps` |

Per-chat earliest-allowed-send time is tracked in `_next_at[chat_id]`. Operations are prioritised `send(0) < delete(1) < edit(2)` and the worker picks the highest-priority ready op whose chat isn't blocked.

**Progress edits are coalesced**: only deltas enqueue a new edit, and if a newer edit for the same message arrives before the outbox drains, the earlier edit is replaced (preserving `queued_at` for fairness). This is the mechanism that keeps Untether well under the ~5 edits/minute-per-message soft ceiling during long runs.

**429 backoff is currently global**: a single `retry_at` field on `TelegramClient` blocks every chat's outbox operations until the retry window expires. If one chat 429s (`edit` flood on a hot run), cold chats see a brief pause. See [#405](https://github.com/littlebearapps/untether/issues/405) for a planned per-chat `retry_at` that would honour Bot API 7.8's `scope` field and decouple hot chats from cold.

**Single poller**: Untether uses long polling, so only one instance can connect to a given bot token. Running two bots on the same token → `HTTP 409 Conflict` from Telegram. (The main process drops its handler and the second instance dies on startup.)

## Untether's own concurrency model

Separate from Telegram's limits, how many agent runs can actually be in flight?

**Per-chat runs are fully parallel.** Untether's run loop (`src/untether/telegram/loop.py`) spawns each dispatch as an independent task under a single TaskGroup — no semaphore, no queue. Multiple runs in one chat coexist fine. Multiple runs across many chats coexist fine.

**Session lock is narrow.** `SessionLockMixin` (`src/untether/runner.py`) serialises only *resumes of the same session* — the key is `f"{engine}:{resume_token}"`. New sessions never block each other, and resume-of-session-A never blocks a fresh run or a resume of session-B. The lock exists only to prevent two `claude --resume` invocations from fighting over the same session file on disk.

**No global `max_concurrent_runs` setting.** The platform is effectively unbounded in code; resource ceilings are what bite first.

**Pre-spawn RAM guard** ([#350](https://github.com/littlebearapps/untether/issues/350)) checks `/proc/meminfo` before every engine spawn:

| `MemAvailable` | Behaviour |
|---|---|
| ≥ 2 GB free | Proceed silently |
| 500 MB-2 GB free | Log `subprocess.prespawn.ram_warning`, proceed |
| < 500 MB free | **Block** — emit error `CompletedEvent` without spawning |

Thresholds are tunable under `[watchdog]` (`prespawn_ram_warn_mb`, `prespawn_ram_block_mb`).

## Operating envelope

| Concurrent active agents | Approximate Telegram call rate | Verdict |
|---|---|---|
| **1-5** | 1-5 calls/sec | Comfortable; well under every limit |
| **10-15** | 6-10 calls/sec | Telegram fine; host RAM becomes the live question |
| **15-25** | 10-15 calls/sec | Pre-spawn RAM guard may start warning or blocking new runs |
| **30-50** | 20-40 calls/sec | Approaches the global 30/sec cap; expect occasional `scope:global` 429s |
| **100+** | >50 calls/sec sustained | Hits global flood; needs multi-token sharding or paid broadcast |

These assume each agent run streams progress edits at a normal cadence with Untether's default coalescing.

## Where it breaks first (in order)

1. **Host RAM.** Each engine subprocess typically uses 400-600 MB resident. At ~15 concurrent runs (~7-9 GB) you'll start tripping the 500 MB-free pre-spawn block if other processes are active.
2. **Telegram global 30 msg/sec cap.** First visible around 25-40 concurrently-streaming agents. Shows up as `scope:global` 429s and brief outbox stalls.
3. **File descriptors.** Systemd user services typically have a ~4096 FD ulimit. Each engine subprocess (with its PTY, MCP connections, and pipes) uses 50-200 FDs. Not a real concern until 30+ concurrent.
4. **Chat count.** Never. Telegram has no documented cap and idle chats are free.

## Scaling patterns

In rough order of cost and effort, if you ever need more headroom:

1. **Tighter edit coalescing.** Cheap. Untether already coalesces edits; monitoring `journalctl | grep outbox` during heavy bursts reveals whether any single message is approaching the 5/min soft ceiling.
2. **Per-chat `retry_at`** ([#405](https://github.com/littlebearapps/untether/issues/405)). Honours Bot API 7.8's `scope` field so one hot chat doesn't freeze cold ones. Small change, meaningful improvement at 10+ concurrent runs.
3. **More host RAM.** The biggest practical lever. Each doubling of RAM roughly doubles the sustainable concurrent-run count before the pre-spawn guard trips.
4. **Multiple bot tokens, sharded by chat.** Cleanly multiplies the global 30 msg/sec cap. Already used informally for the dev vs staging split.
5. **Switch long polling → webhook + worker pool.** Removes the single-poller bottleneck and enables multi-process dispatch. Significant refactor; only worth it at very high sustained load.
6. **Paid broadcasts.** Raises the global cap from 30/sec to 1000/sec, but requires 100k Stars + 100k Monthly Active Users — not applicable to typical self-hosted Untether deployments.

## Troubleshooting

If you're seeing rate-limit symptoms — progress updates briefly freezing across multiple chats, or `TooManyRequests` in logs — see the [rate limit / flood wait section](../how-to/troubleshooting.md#telegram-rate-limit--flood-wait) of the troubleshooting guide.

## Related issues (for reference)

Recent capacity-adjacent hardening work:

- [#275](https://github.com/littlebearapps/untether/issues/275) — orphaned subprocess descendants (fixed in v0.35.1)
- [#281](https://github.com/littlebearapps/untether/issues/281) — webhook rate limiter fire-and-forget dispatch (v0.35.1)
- [#322](https://github.com/littlebearapps/untether/issues/322) — stuck-after-tool-result detector (v0.35.2)
- [#342](https://github.com/littlebearapps/untether/issues/342) — raised idle timeout to accommodate long reasoning (v0.35.2)
- [#350](https://github.com/littlebearapps/untether/issues/350) — pre-spawn RAM guard (v0.35.2)
- [#405](https://github.com/littlebearapps/untether/issues/405) — per-chat `retry_at` (planned)

## References

- [Telegram Bots FAQ — "My bot is hitting limits"](https://core.telegram.org/bots/faq)
- [Telegram Bot API reference](https://core.telegram.org/bots/api)
- [grammY: Scaling Up IV — Flood Limits](https://grammy.dev/advanced/flood)
- [grammY: Scaling Up II — High Load](https://grammy.dev/advanced/scaling)
- [tdlib/telegram-bot-api#43 — concurrent polling](https://github.com/tdlib/telegram-bot-api/issues/43)
- Transport implementation: [`docs/reference/transports/telegram.md`](../reference/transports/telegram.md)
