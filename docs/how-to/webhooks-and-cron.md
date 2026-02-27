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
5. The rendered prompt is sent to Claude in the `myapp` project
6. A notification appears in your Telegram chat, and the run streams progress as usual

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

This runs every weekday at 9:00 AM.

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

## Security notes

- The server binds to localhost by default. Use a reverse proxy (nginx, Caddy) with TLS to expose it to the internet.
- All secret comparisons use timing-safe comparison.
- Rate limiting prevents abuse (token-bucket, per-webhook and global).
- Webhook prompts are prefixed with an untrusted-payload marker.

## Related

- [Triggers reference](../reference/triggers/triggers.md) — full configuration reference with all options
- [Schedule tasks](schedule-tasks.md) — native Telegram scheduling (no server needed)
