# Webhooks and cron

Untether can start agent runs automatically from external events (webhooks) or on a schedule (cron). Both use the same engine pipeline as Telegram messages, so project routing, progress streaming, and cost tracking all work normally.

## Enable triggers

Triggers are off by default. Enable them in your config:

=== "untether config"

    ```sh
    untether config set triggers.enabled true
    ```

=== "toml"

    ```toml
    [triggers]
    enabled = true
    ```

When enabled, Untether starts a webhook server on `127.0.0.1:9876` and a cron tick loop.

## Set up a webhook

Webhooks accept HTTP POST requests and turn them into agent runs. Example: trigger a code review when GitHub sends a push event.

=== "toml"

    ```toml
    [[triggers.webhooks]]
    id = "github-push"
    path = "/hooks/github"
    auth = "hmac-sha256"
    secret = "whsec_your_github_secret"
    event_filter = "push"
    project = "myapp"
    engine = "claude"
    prompt_template = """
    Review push to {{ref}} by {{pusher.name}}.
    Repository: {{repository.full_name}}

    Check for bugs, security issues, and style problems.
    """
    ```

### How it works

1. GitHub sends a POST to `http://your-server:9876/hooks/github`
2. Untether verifies the HMAC signature against your secret
3. The `event_filter` checks the `X-GitHub-Event` header — only `push` events proceed
4. `{{ref}}` and `{{pusher.name}}` are substituted from the JSON payload
5. The rendered prompt is sent to Claude Code in the `myapp` project
6. A notification appears in your Telegram chat, and the run streams progress as usual

<!-- TODO: capture screenshot -->
<!-- <img src="../assets/screenshots/webhook-notification.jpg" alt="Webhook-triggered run with rendered prompt and agent progress" width="360" loading="lazy" /> -->

### Authentication

Every webhook requires explicit auth. Choose one:

| Mode | Header | Use case |
|------|--------|----------|
| `bearer` | `Authorization: Bearer <token>` | Simple shared secret |
| `hmac-sha256` | `X-Hub-Signature-256` | GitHub webhooks |
| `hmac-sha1` | `X-Hub-Signature` | Legacy GitHub webhooks |
| `none` | (none) | Local testing only |

### Prompt templating

Use `{{field.path}}` to substitute values from the webhook JSON payload:

- **Nested paths**: `{{event.data.title}}`
- **List indices**: `{{items.0}}`
- **Missing fields**: render as empty strings (no error)

All webhook prompts are automatically prefixed with an untrusted-payload marker so the agent treats the content with appropriate caution.

### Test a webhook locally

```bash
curl -X POST http://127.0.0.1:9876/hooks/github \
  -H "Authorization: Bearer my-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"ref": "refs/heads/main", "pusher": {"name": "alice"}}'
```

A `202 Accepted` response means the run was dispatched.

!!! untether "Untether"
    🔔 **webhook** · github-push

    Review push to refs/heads/main by alice

    working · claude · 4s · step 1

## Set up a cron schedule

Cron triggers fire on a schedule using standard 5-field cron syntax.

=== "toml"

    ```toml
    [[triggers.crons]]
    id = "daily-review"
    schedule = "0 9 * * 1-5"
    project = "myapp"
    engine = "claude"
    prompt = "Review open PRs and summarise their status."
    ```

This runs every weekday at 9:00 AM in the server's local time (usually UTC).

### Timezone

By default, cron schedules use the server's system time. Set `timezone` to
evaluate in a specific timezone:

=== "toml"

    ```toml
    [[triggers.crons]]
    id = "morning-review"
    schedule = "0 8 * * 1-5"
    timezone = "Australia/Melbourne"
    project = "myapp"
    engine = "claude"
    prompt = "Review overnight changes."
    ```

This fires at 8:00 AM Melbourne time (AEST/AEDT), adjusting automatically for
daylight saving. Use [IANA timezone names](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).

Set `default_timezone` in `[triggers]` to apply to all crons without repeating it:

```toml
[triggers]
enabled = true
default_timezone = "Australia/Melbourne"
```

Per-cron `timezone` overrides the global default. See the
[triggers reference](../reference/triggers/triggers.md#timezone-support) for details.

### Cron syntax

```
┌─── minute (0-59)
│ ┌─── hour (0-23)
│ │ ┌─── day of month (1-31)
│ │ │ ┌─── month (1-12)
│ │ │ │ ┌─── day of week (0-7, Sun=0 or 7)
* * * * *
```

Common patterns:

| Expression | Meaning |
|-----------|---------|
| `0 9 * * *` | Daily at 9:00 AM |
| `0 9 * * 1-5` | Weekdays at 9:00 AM |
| `*/15 * * * *` | Every 15 minutes |
| `0 */2 * * *` | Every 2 hours |
| `0 9,17 * * *` | At 9:00 AM and 5:00 PM |

### Data-fetch crons

Crons can pull data from external sources before rendering the prompt:

=== "toml"

    ```toml
    [[triggers.crons]]
    id = "daily-issue-triage"
    schedule = "0 9 * * 1-5"
    engine = "claude"
    project = "my-app"

    [triggers.crons.fetch]
    type = "http_get"
    url = "https://api.github.com/repos/myorg/myapp/issues?state=open"
    headers = { "Authorization" = "Bearer {{env.GITHUB_TOKEN}}" }
    parse_as = "json"
    store_as = "issues"

    prompt_template = "Open issues:\n{{issues}}\n\nReview and propose labels."
    ```

The fetch step runs before prompt rendering. Fetched data is injected into `prompt_template` via the `store_as` variable name. If the fetch fails, the default behaviour (`on_failure = "abort"`) sends a failure notification to Telegram and skips the agent run.

Fetch types: `http_get`, `http_post`, `file_read`. See the
[triggers reference](../reference/triggers/triggers.md#data-fetch-crons) for all options.

## Non-agent webhook actions

Webhooks can perform lightweight actions without spawning an agent:

=== "toml"

    ```toml
    # Archive webhook payloads to disk
    [[triggers.webhooks]]
    id = "data-ingest"
    path = "/hooks/ingest"
    auth = "bearer"
    secret = "whsec_..."
    action = "file_write"
    file_path = "~/data/incoming/batch-{{date}}.json"
    notify_on_success = true

    # Send a Telegram notification
    [[triggers.webhooks]]
    id = "stock-alert"
    path = "/hooks/stock"
    auth = "bearer"
    secret = "whsec_..."
    action = "notify_only"
    message_template = "📈 {{ticker}} hit {{price}}"
    ```

Action types: `agent_run` (default), `file_write`, `http_forward`, `notify_only`. See the
[triggers reference](../reference/triggers/triggers.md#non-agent-actions) for details.

## Chat routing

Each webhook and cron can specify where the Telegram notification appears:

- Set `chat_id` to post in a specific chat
- If omitted, uses the default chat from `[transports.telegram]`
- Set `project` to run in a specific project's working directory

## Server configuration

=== "toml"

    ```toml
    [triggers.server]
    host = "127.0.0.1"     # bind address (use reverse proxy for internet)
    port = 9876            # listen port
    rate_limit = 60        # max requests per minute
    max_body_bytes = 1048576  # 1 MB max payload
    ```

The server includes a health endpoint at `GET /health` for uptime monitoring.

## Hot-reload configuration

When `watch_config = true` is set in your top-level config, you can add, remove, or modify
webhooks and crons by editing `untether.toml` — changes are applied automatically without
restarting Untether. Active runs are not interrupted.

For example, to add a new cron, just edit the TOML and save:

```toml
[[triggers.crons]]
id = "new-task"
schedule = "0 14 * * 1-5"
prompt = "Check the deployment status"
timezone = "Australia/Melbourne"
```

The new cron will start firing on the next minute tick. Similarly, new webhooks become
accessible immediately, and removed webhooks start returning 404.

!!! note
    Server settings (`host`, `port`, `rate_limit`) and the `enabled` toggle still
    require a restart. See the [Triggers reference — Hot-reload](../reference/triggers/triggers.md#hot-reload)
    for the full list.

## One-shot crons with `run_once`

Set `run_once = true` on a cron to fire once then auto-disable. The cron stays in the TOML but is skipped until the next reload or restart:

```toml
[[triggers.crons]]
id = "deploy-check"
schedule = "0 15 * * *"
prompt = "Check today's deployment status"
run_once = true
```

After the cron fires, the `triggers.cron.run_once_completed` log line confirms the removal. To re-enable, save the TOML again (triggers a reload) or restart the service.

## Delayed runs with `/at`

For ad-hoc one-shot delays, use the `/at` command directly in Telegram — no TOML edit required:

```
/at 30m Check the build status
/at 2h Review open PRs
/at 90s Run the test suite
```

Duration supports `Ns` / `Nm` / `Nh` with a 60s minimum and 24h maximum. Pending delays are cancelled via `/cancel` and lost on restart. Per-chat cap of 20 pending delays.

## Discovering configured triggers

Once triggers are configured, `/ping` in the targeted chat shows a summary:

```
🏓 pong — up 2d 4h 12m 3s
⏰ triggers: 1 cron (daily-review, 9:00 AM daily (Melbourne))
```

Runs initiated by a trigger show their provenance in the meta footer:

```
🏷 opus 4.6 · plan · ⏰ cron:daily-review
```

See the [Triggers reference — Trigger visibility](../reference/triggers/triggers.md#trigger-visibility) for details.

## Security notes

- The server binds to localhost by default. Use a reverse proxy (nginx, Caddy) with TLS to expose it to the internet.
- All secret comparisons use timing-safe comparison.
- Rate limiting prevents abuse (token-bucket, per-webhook and global).
- Webhook prompts are prefixed with an untrusted-payload marker.

## Related

- [Triggers reference](../reference/triggers/triggers.md) — full configuration reference with all options
- [Schedule tasks](schedule-tasks.md) — native Telegram scheduling (no server needed)
