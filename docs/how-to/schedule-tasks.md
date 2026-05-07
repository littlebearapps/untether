# Schedule tasks

There are several ways to run tasks on a schedule: the `/at` command for quick one-shot delays, Telegram's built-in message scheduling, Untether's trigger system (webhooks and cron), and Loop mode for Claude Code's `/loop` and `ScheduleWakeup`.

!!! note "Loop mode is opt-in"
    By default, Untether does **not** fire Claude Code's session-scoped schedules after a turn ends — the `claude --print` subprocess exits and the cron task dies with it (verified empirically against `claude` v2.1.129/2.1.132 — upstream docs claiming `--resume` restores tasks are incorrect in `--print` mode). To enable autonomous loop firing via Telegram, turn on **Loop mode** in `/config → 🔁 Loop mode`. See [Loop mode](#loop-mode) below.

## One-shot delays with /at

The `/at` command schedules a prompt to run after a delay — useful for reminders, follow-ups, or "run this in 30 minutes":

```
/at 30m Check the build
/at 2h Review the PR feedback
/at 60s Say hello
```

**Duration format:** `Ns` (seconds), `Nm` (minutes), or `Nh` (hours). Minimum 60 seconds, maximum 24 hours.

After scheduling, you'll see a confirmation:

!!! untether "Untether"
    ⏳ Scheduled: will run in 30m
    Cancel with /cancel.

When the delay expires, the prompt runs as a normal agent session. Use `/cancel` to cancel all pending delays in the current chat.

!!! note "Not persistent"
    Pending `/at` delays are held in memory. They are lost if Untether restarts. For persistent scheduled tasks, use [cron triggers](#cron-triggers) instead.

!!! note "Engine and project frozen at schedule time"
    When you run `/at`, Untether snapshots the chat's current project mapping and engine at that moment. That snapshot is what fires when the delay expires — changing `/agent`, `/ctx`, or `/planmode` afterwards does **not** affect already-scheduled delays. Cancel with `/cancel` and re-schedule if you change your mind. ([#362](https://github.com/littlebearapps/untether/issues/362))

## Loop mode

Claude Code has a built-in `/loop <interval> <prompt>` command (and a no-interval `/loop <prompt>` dynamic mode driven by `ScheduleWakeup`) for self-pacing autonomous work. Untether's **Loop mode** observes those tool calls at the JSONL layer, captures the user's intent, and re-fires each iteration when due — even after the subprocess exits. ([#289](https://github.com/littlebearapps/untether/issues/289))

**Default OFF** — opt-in per chat via `/config → 🔁 Loop mode`. When OFF, behaviour matches the pre-v0.35.4 baseline: `/loop` registers a schedule during the turn but nothing fires after the subprocess exits.

### How it works

1. You type `/loop 5m check the deploy` in a Claude session.
2. Claude calls `CronCreate(cron="*/5 * * * *", prompt="check the deploy", recurring=true)`.
3. Untether observes the `tool_use` event and registers an Untether-side timer.
4. The subprocess exits cleanly. Upstream's session-scoped cron dies with it.
5. Each fire interval, Untether spawns `claude --resume <session_id>` with a wrapped re-issue prompt: `Loop iteration N: check the deploy. Do the task now; do not summarize old results unless necessary.`
6. State persists to `active_loops.json` (sibling of `untether.toml`) — loops survive Untether restarts.

### Runaway-safety caps

The `[loop]` config has caps in case a loop runs longer than expected:

- `max_iterations = 20` — cap on iteration count (NOT a cost cap)
- `max_total_duration_hours = 4` — wall-clock cap (NOT a cost cap)
- `expiry_days = 7` — auto-expire 7 days after creation (matches upstream)

These bound loop duration regardless of cost. They are *not* a substitute for setting a budget — see "Cost considerations" below.

### Cost considerations

Autonomous loops consume API credits or your Claude subscription quota. A 24-hour `/loop 1m` can fire up to 1440 times. Cost per fire depends on conversation length:

- Short conversations: ~$0.01–$0.05 per fire (cache-warm).
- Long conversations: cache may evict between fires, costing $0.10–$0.50 per fire.

**Set a daily budget BEFORE turning on Loop mode** in `/config → 💰 Cost & usage` (or `[cost_budget].max_cost_per_day` in `untether.toml`). The same daily cost cap applies to loop fires automatically — there is no separate per-loop budget. See [Cost budgets](cost-budgets.md) for setup.

### Cancelling a loop

`/cancel` drops all active loops for the current chat and writes a do-not-resume sentinel so the upstream session-scoped cron — if it ever survives — cannot be re-fired by Untether. `/new` does the same (treats `/new` as "wipe this chat's state").

## Telegram scheduling

Telegram's native message scheduling works with Untether out of the box.

In Telegram, long-press the send button and choose **Schedule Message** to run tasks at a specific time. You can also set up recurring schedules (daily/weekly) for automated workflows.

This is the simplest approach — no server or config changes needed.

<!-- TODO: capture screenshot -->
<!-- <img src="../assets/screenshots/scheduled-message.jpg" alt="Telegram scheduled message picker showing the Schedule Message option" width="360" loading="lazy" /> -->

!!! tip "How to schedule"
    In Telegram, **long-press the send button** (iOS) or tap the **clock icon** (Android/Desktop) and choose **Schedule Message**. Pick a date and time, then tap **Send**. Untether receives the message at the scheduled time and starts the run automatically.

## Cron triggers

For more control, use Untether's built-in cron system. Cron triggers fire on a schedule and start agent runs automatically.

=== "toml"

    ```toml
    [triggers]
    enabled = true

    [[triggers.crons]]
    id = "daily-review"
    schedule = "0 9 * * 1-5"
    project = "myapp"
    engine = "claude"
    prompt = "Review open PRs and summarise their status."
    ```

This runs every weekday at 9:00 AM (server time) in the `myapp` project using
Claude Code. Add `timezone = "Australia/Melbourne"` to evaluate in a specific
timezone, or set `default_timezone` in `[triggers]` for all crons. See
[Webhooks and cron](webhooks-and-cron.md#timezone) for details.

Common schedules:

| Expression | Meaning |
|-----------|---------|
| `0 9 * * *` | Daily at 9:00 AM |
| `0 9 * * 1-5` | Weekdays at 9:00 AM |
| `*/30 * * * *` | Every 30 minutes |
| `0 */4 * * *` | Every 4 hours |

Add `run_once = true` to fire a cron exactly once, then auto-disable. Fired state persists to `run_once_fired.json` (sibling of your `untether.toml`), so a reload or restart will **not** re-fire it. Remove the cron from your TOML to clean up.

### Autonomous crons in plan-mode chats (Claude) {#autonomous-crons}

By default a cron inherits the chat's permission mode, so if you've set `/planmode plan` on a Claude chat the scheduled run will pause for your approval too. That's rarely what you want for an 8 AM summariser that runs while you're asleep.

Set `permission_mode = "auto"` on the cron to make that run autonomous without flipping the whole chat:

```toml
[[triggers.crons]]
id = "overnight-review"
schedule = "0 6 * * *"
chat_id = -1001234567890
engine = "claude"
prompt = "Review overnight PRs and reply with a summary."
permission_mode = "auto"
```

Precedence (Claude): cron `permission_mode` > per-chat `/planmode` > engine config default. Every autonomous run logs `trigger.cron.permission_mode_override`. Valid values: `default`, `plan`, `auto`, `acceptEdits`, `bypassPermissions`. Claude-only for now; other engines silently ignore the field ([#332](https://github.com/littlebearapps/untether/issues/332) tracks full coverage).

## Trigger provenance and history

Trigger-initiated runs are visibly distinct from manual ones — every run footer carries a provenance marker:

* `⏰ cron:<id>` — fired by a cron trigger
* `⚡ webhook:<id>` — fired by a webhook trigger
* `⏰ at:<token>` — fired by `/at`

`/stats` reports a per-engine `(N triggered, M manual)` breakdown next to each engine line and on the totals row when at least one count is nonzero ([#271](https://github.com/littlebearapps/untether/issues/271) Tier 3).

`/config → 📡 Triggers` (`config:tg`) lists every cron and webhook configured for the current chat — for crons: `describe_cron(schedule, timezone)`, project, engine, last-fired relative time; for webhooks: path, auth scheme, project, engine, last-fired. Lists are scoped to the current chat, capped at 10 entries with a `…and N more (see untether.toml)` overflow marker. The page also hosts the master pause/resume toggle (see below). See [Inline settings](inline-settings.md#triggers-page) for the navigation walkthrough.

Last-fired times are persisted to `triggers_history.json` (sibling of `untether.toml`) so the values survive a restart. Renaming a trigger ID in TOML leaves a stale entry that operators can manually delete (no auto-prune to avoid losing data on transient TOML errors).

## Pausing all triggers

When you need to silence the bot for maintenance, demos, or a noisy upstream, the master pause toggle suspends all cron firing and webhook dispatch globally without changing your config ([#294](https://github.com/littlebearapps/untether/issues/294)).

* **From `/config`:** open `📡 Triggers` (or use the one-button toggle row on the home page when triggers are configured) and tap **Pause**.
* **While paused:** the cron scheduler skips its tick (`run_once` crons are not consumed during the pause and fire on the next matching tick after resume); the webhook server returns `503 triggers paused` with `Retry-After: 60` instead of dispatching; `/health` reports `{"status":"paused","paused":true}` for external monitors; `/ping` shows `⏸ triggers paused: … (suspended)`.
* **Restart auto-resumes** — pause is in-memory only by design; restarting the bot is a safe escape hatch.

Tap **Resume** in the same page to clear the pause.

## Webhook triggers

Webhooks let external services (GitHub, Slack, PagerDuty) trigger agent runs via HTTP POST.

=== "toml"

    ```toml
    [triggers]
    enabled = true

    [[triggers.webhooks]]
    id = "github-push"
    path = "/hooks/github"
    auth = "hmac-sha256"
    secret = "whsec_abc..."
    event_filter = "push"
    project = "myapp"
    prompt_template = "Review push to {{ref}} by {{pusher.name}}"
    ```

See [Webhooks and cron](webhooks-and-cron.md) for the full setup guide, including authentication, prompt templating, and testing.

## Related

- [Webhooks and cron](webhooks-and-cron.md) — full trigger setup guide with examples
- [Triggers reference](../reference/triggers/triggers.md) — complete configuration reference
