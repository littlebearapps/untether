# Schedule tasks

There are several ways to run tasks on a schedule: the `/at` command for quick one-shot delays, Telegram's built-in message scheduling, and Untether's trigger system (webhooks and cron).

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
