# Schedule tasks

There are two ways to run tasks on a schedule: Telegram's built-in message scheduling (no config needed) and Untether's trigger system (webhooks and cron).

## Telegram scheduling

Telegram's native message scheduling works with Untether out of the box.

In Telegram, long-press the send button and choose **Schedule Message** to run tasks at a specific time. You can also set up recurring schedules (daily/weekly) for automated workflows.

This is the simplest approach — no server or config changes needed.

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

This runs every weekday at 9:00 AM in the `myapp` project using Claude.

Common schedules:

| Expression | Meaning |
|-----------|---------|
| `0 9 * * *` | Daily at 9:00 AM |
| `0 9 * * 1-5` | Weekdays at 9:00 AM |
| `*/30 * * * *` | Every 30 minutes |
| `0 */4 * * *` | Every 4 hours |

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
