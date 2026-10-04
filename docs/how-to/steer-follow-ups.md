# Steer a running Claude run

When Claude Code is halfway through a task and you think of something else — "also check the logs", "use the staging database instead" — you have two choices for the message you send from [Telegram](https://telegram.org):

- **queue** (the default) — the message waits until Claude's current turn finishes, then runs as the next turn.
- **steer** — the message goes straight into the run that is already working. Claude reads it at its next step (the next time a tool finishes) and folds it into the answer it is producing.

Steer is **Claude Code only** and needs a live session, which means a permission mode is set (`/planmode` or `/config` → Permission mode) and `[watchdog] live_sessions` is on (the default). Other engines always queue.

## Steer one message

Send `/steer` followed by your text while a run is working:

```
/steer also tell me the hostname
```

You'll get `↪️ Steered into the current run.` as a reply. Once Claude has read it, the run's progress message shows a `↪️ steer received: also tell me the hostname` row, and the final answer covers both the original request and the steer.

If Claude has already finished its turn when the message arrives (the session is idle), there is nothing to steer: the message simply runs as the next turn and its answer replies to it — no `Steered` acknowledgement.

If the steer arrives after Claude's last tool call (it is already writing its answer), Claude finishes the first answer and then answers the steer as its next turn — you get a second reply, threaded under your `/steer` message. Nothing is lost either way.

## Queue one message

`/queue <text>` sends a message the normal way even when steer is the default:

```
/queue when you're done, write a summary
```

## Make steer the default

- **Chat** — send `/steer` with no text, or open `/config` → **↪️ Follow-up** and pick **Steer**. Every plain message you send while Claude is working is steered.
- **Forum topic** — send `/steer` inside the topic to set it for that topic only.
- **Everywhere** — set the default in `untether.toml` (applies without a restart when `watch_config = true`):

```toml
[transports.telegram]
followup_mode = "steer"
```

Send `/queue` (no text) or pick **Queue** in `/config` to switch back. **Clear override** in `/config` returns the chat to the `untether.toml` default. In a group, only admins can change the default with a bare `/steer` or `/queue`; anyone allowed to use the bot can still send `/steer <text>` or `/queue <text>`.

Voice notes follow the default: a transcribed voice note sent to a steer chat is steered.

## What always queues

- Files, photos and albums (they need saving before the prompt exists)
- Forwarded messages
- Commands
- A message sent while Claude is asking you a question — a plain-text reply answers the question instead

## When a steer can't be delivered

The message is queued as normal and Untether tells you why:

| Reply | Meaning |
|---|---|
| `↪️ Steer isn't supported on codex — queued instead.` | The chat's engine isn't Claude Code. |
| `↪️ No live Claude run to steer — queued instead.` | Nothing is running, the session has no permission mode (legacy mode can't take input mid-run), live sessions are off (`[watchdog] live_sessions = false`), or there is no session to steer into (for example handoff mode with nothing stored). |
| `↪️ The current run is ending — queued instead.` | The run was cancelled or is shutting down. |

With steer as the chat default you only see these while something is actually running, once per run — a steer chat with nothing running just starts a normal run. An explicit `/steer <text>` tells you too, with two exceptions: while Claude is waiting on a question (`AskUserQuestion`) the text is queued without a note, and if your model or mode changed since the session started, the queued run posts its own notice when it restarts the session with the new settings.

## When a follow-up didn't run

In either mode, one notice can appear on a follow-up after the run ends:

| Reply | Meaning |
|---|---|
| `⚠️ The session ended before this message ran — please send it again.` | The follow-up was written into the session, but the session ended before Claude started it. It did not run. |

Cancelling a run doesn't drop a follow-up already queued behind it: it runs next, so you don't need to send it again.

## Related

- [Inline settings menu](inline-settings.md) — the `/config` Follow-up page
- [Interactive approval](interactive-approval.md) — a steer sent while an approval is pending is read after the tool is approved or denied
- [Commands reference](../reference/commands-and-directives.md)
- [Claude runner: live sessions](../reference/runners/claude/runner.md#live-sessions-776)
