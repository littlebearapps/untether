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

`auth` defaults to `bearer`, and every mode except `none` needs a `secret`. Choose one:

| Mode | Header | Use case |
|------|--------|----------|
| `bearer` | `Authorization: Bearer <token>` | Simple shared secret |
| `hmac-sha256` | `X-Hub-Signature-256` | GitHub webhooks |
| `hmac-sha1` | `X-Hub-Signature` | Legacy GitHub webhooks |
| `none` | (none) | Local testing only — since v0.35.4 **refused on non-loopback hosts** unless `[triggers] allow_unauthenticated_webhooks = true` ([#382](https://github.com/littlebearapps/untether/issues/382)) |

### Prompt templating

Use `{{field.path}}` to substitute values from the webhook JSON payload:

- **Nested paths**: `{{event.data.title}}`
- **List indices**: `{{items.0}}`
- **Missing fields**: render as empty strings (no error)

All webhook prompts are automatically prefixed with an untrusted-payload marker so the agent treats the content with appropriate caution.

### Test a webhook locally

The `github-push` webhook above uses HMAC and an `event_filter`, so a test request has to sign its body and send the event header (with no `X-GitHub-Event: push` header the server answers `200 filtered` and starts nothing):

```bash
BODY='{"ref": "refs/heads/main", "pusher": {"name": "alice"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'whsec_your_github_secret' | sed 's/^.* //')"
curl -X POST http://127.0.0.1:9876/hooks/github \
  -H "X-Hub-Signature-256: $SIG" \
  -H "X-GitHub-Event: push" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

For a `bearer` webhook, send `-H "Authorization: Bearer <secret>"` instead. A `202 Accepted` response means the run was dispatched.

!!! untether "Untether"
    ⚡ Trigger: webhook:github-push

    working · claude · 4s · step 1

Crons announce themselves the same way, as `⏰ Scheduled: cron:<id>`. The run's progress and final answer reply to that message. If Telegram can't be reached, the announcement is retried after 5 s and 30 s before the trigger is given up ([#758](https://github.com/littlebearapps/untether/issues/758)).

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

The parser supports numbers, `*`, ranges (`1-5`), lists (`1,15`) and steps on `*` or a range (`*/15`, `9-17/2`). It doesn't accept month or day names (`MON`, `JAN`), and a step on a single number (`5/15`) is not expanded. If both day-of-month and day-of-week are restricted, a day must match **both** (standard cron matches either). Schedules aren't validated when the config loads, so check a new one with `/ping` or the `/config` → ⏰ Triggers page, which describe it in words.

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
    prompt_template = "Open issues:\n{{issues}}\n\nReview and propose labels."

    [triggers.crons.fetch]
    type = "http_get"
    url = "https://api.github.com/repos/myorg/myapp/issues?state=open"
    headers = { "Authorization" = "Bearer ghp_your_token_here" }  # literal value — no {{env.NAME}} lookup
    parse_as = "json"
    store_as = "issues"
    ```

`prompt_template` belongs to the cron itself, so it must come before the `[triggers.crons.fetch]` header. Anything after the header is read as a fetch option, and an unknown key there fails the trigger config.

Header values are used literally — `{{…}}` placeholders in `url`, `headers` and `body` render as empty strings and there is no `{{env.NAME}}` lookup — so keep `untether.toml`'s permissions tight (`chmod 600`).

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
    file_path = "~/data/incoming/batch-{{batch_id}}.json"  # {{…}} fields come from the payload
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

Action types: `agent_run` (default), `file_write`, `http_forward`, `notify_only`. Template fields come only from the webhook payload; there are no built-in variables such as a date. A `file_write` path, like a cron `file_read` fetch, is checked at any depth against a fixed deny list (`.git/**`, `.env`, `.envrc`, `**/*.pem`, `**/.ssh/**`), so a templated path can't land in `.git/hooks` or `~/.ssh` ([#831](https://github.com/littlebearapps/untether/issues/831)). This list is separate from `[transports.telegram.files] deny_globs`. See the
[triggers reference](../reference/triggers/triggers.md#non-agent-actions) for details.

## Chat routing

Each webhook and cron can specify where the Telegram notification appears:

- Set `chat_id` to post in a specific chat
- If omitted, uses the default chat from `[transports.telegram]` — even when `project` is set. A project's bound chat is **not** used, so add `chat_id` to post in the project's chat. Untether logs `trigger.cron.chat_fallback` / `trigger.webhook.chat_fallback` once when a project-only trigger will post to a different chat than the project's ([#894](https://github.com/littlebearapps/untether/issues/894))
- Set `project` to run in a specific project's working directory
- Set `engine` to pick the engine. Without it, a trigger with a `project` runs on that project's `default_engine` (since v0.36.0 — it used to fall back to the global default, [#862](https://github.com/littlebearapps/untether/issues/862)), and one without a project uses the global `default_engine`

## Server configuration

=== "toml"

    ```toml
    [triggers.server]
    host = "127.0.0.1"     # bind address (use reverse proxy for internet)
    port = 9876            # listen port
    rate_limit = 60        # max requests per minute
    max_body_bytes = 1048576  # 1 MB max payload
    ```

The server includes a health endpoint at `GET /health` for uptime monitoring. While the master pause toggle is active (see [Pause and resume all triggers](#pause-and-resume-all-triggers)) it returns `{"status": "paused", "paused": true}` so external monitors can distinguish "paused but up" from "down".

## Pause and resume all triggers

Untether ships a master pause toggle ([#294](https://github.com/littlebearapps/untether/issues/294)) that gates **both** crons and webhooks at once — useful when deploying, debugging, or muting overnight without editing config:

* **`/config` home page** shows a one-button toggle row at the bottom whenever triggers are configured.
* **`/config` → `⏰ Triggers`** opens a dedicated page with state, per-chat counts, and a Pause/Resume button. It also lists per-chat crons and webhooks with last-fired times.
* While paused: cron loop skips ticks, webhooks return `503 triggers paused` with `Retry-After: 60`, `/health` returns `paused: true`, and `/ping` shows `⏸ triggers paused: … (suspended)`.
* `run_once` crons are not consumed during the pause and fire on the next matching tick after resume.

Pause is **in-memory only** — restart auto-resumes (the safe default). For a durable shutoff, set `[triggers] enabled = false` in `untether.toml` instead.

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
    Server settings (`host`, `port`, `rate_limit`, `max_body_bytes`) still require a
    restart, and so does turning `enabled` from off to on. Turning `enabled` off applies
    straight away (crons stop and webhook paths return 404). See the [Triggers reference — Hot-reload](../reference/triggers/triggers.md#hot-reload)
    for the full list.

## One-shot crons with `run_once`

Set `run_once = true` on a cron to fire once then auto-disable:

```toml
[[triggers.crons]]
id = "deploy-check"
schedule = "0 15 * * *"
prompt = "Check today's deployment status"
run_once = true
```

After the cron fires, the `triggers.cron.run_once_completed` log line confirms the removal, and the startup message counts it separately from scheduled crons ([#809](https://github.com/littlebearapps/untether/issues/809)). Fired state is persisted to `run_once_fired.json` (sibling of `untether.toml`), so the cron is skipped across config reloads and process restarts — the TOML entry is kept for history but won't refire. To re-enable a one-shot, change its `id` or remove both the TOML entry and its record in `run_once_fired.json`.

A one-shot only counts as fired once its run is dispatched, or once its fetch step fails with `on_failure = "abort"` (the failure notice is the one fire). If the `⏰ Scheduled:` message can't be posted (a wrong `chat_id`, or a Telegram or network outage), nothing runs and the cron stays pending: Untether retries it once a minute for up to 15 minutes, then gives up and logs `triggers.cron.run_once_lost` at error level ([#893](https://github.com/littlebearapps/untether/issues/893)). The pending state is saved to `run_once_pending.json` next to `untether.toml`, so a restart during that window carries on retrying on the first minute after startup, and a restart after the window closed gives the cron up straight away. A pause during the window still gets one retry after you resume before the cron is given up.

## Autonomous crons in plan-mode chats (Claude)

A cron normally inherits the chat's permission mode, then the engine default: `[engines.claude] permission_mode`, or, if that's unset, Claude Code's non-interactive `-p` path, with no Telegram approvals or plan mode. Nobody can approve anything when a cron fires, so an unattended Claude run denies anything that would wait for a tap ([#835](https://github.com/littlebearapps/untether/issues/835)) — a cron that inherits `plan` ends with a plan instead of doing the work. **Give every Claude cron that should act unattended an explicit `permission_mode`.** Set `permission_mode = "auto"` on the cron to override:

```toml
[[triggers.crons]]
id = "overnight-review"
schedule = "0 6 * * *"
chat_id = -1001234567890
engine = "claude"
prompt = "Review overnight PRs and reply with a summary."
permission_mode = "auto"
```

!!! warning "`auto` changed meaning in v0.36.0"
    Before v0.36.0, `permission_mode = "auto"` meant plan mode with the plan gate auto-approved. It now selects Claude Code's own classifier-gated auto mode, which has no plan phase. Existing crons keep running but behave differently — set `"plan-auto"` to restore the previous behaviour. Untether logs one warning at startup, and again if a config reload changes the list, naming every engine setting and cron that uses `"auto"`.

!!! warning "Unattended runs deny instead of waiting"
    Cron and webhook runs never wait on an approval button nobody can tap ([#835](https://github.com/littlebearapps/untether/issues/835)). In `default` / `manual` / `acceptEdits` (what `/planmode off` sets) any tool the mode would ask about is denied; in `plan` the plan approval and the file-changing tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash`) are denied; in `auto`, `dontAsk` and `bypassPermissions` only the requests the CLI still asks about (an `ask` rule, a hook's `ask`, a tool that needs a person, auto mode falling back after repeated blocks) are denied; with diff preview on, an edit awaiting its diff approval is denied. Questions are always denied. Claude is told to carry on without the denied action or stop and report; the final lists the denials (`🔒 unattended (cron:<id>) · denied …`) and each logs `permission.unattended_deny`. A reply to the run's message continues in an attended session with normal buttons. `/at` runs keep their buttons. For hands-off crons set `permission_mode` to `plan-auto`, `auto` or `bypassPermissions` (or pre-approve the tools). Startup logs `trigger.unattended_approval_risk` for crons set to an asking mode, and the startup message lists them.

!!! note "Webhooks have no `permission_mode`"
    A webhook run always uses its chat's `/planmode`, then the engine default — so a webhook into a `plan` (or `/planmode off`) chat has its approvals denied the same way. Set the chat's mode with `/planmode plan-auto` or `/planmode auto` if the webhook should act on its own; per-webhook overrides are tracked in [#332](https://github.com/littlebearapps/untether/issues/332). Webhooks aren't listed in the startup message.

Precedence (Claude only): cron `permission_mode` > per-chat `/planmode` > engine config default. Every run whose cron value differs from the chat's own setting logs `trigger.cron.permission_mode_override` for staging observability. Valid values: `default` (alias `manual`), `plan`, `plan-auto`, `auto`, `acceptEdits`, `dontAsk`, `bypassPermissions`. On a Codex cron, `permission_mode = "safe"` runs that cron in Codex's read-only sandbox; any other value logs `codex.permission_mode.unknown` once and runs full auto ([#830](https://github.com/littlebearapps/untether/issues/830)). On an Antigravity cron the valid values are `workspace`, `full`, `ask` and `plan` (the last two are refused at run time until a later 0.36.1 release); a cron runs with Full access only when it sets `permission_mode = "full"` itself, and a webhook run never does. A scheduled Antigravity run is also held when the project's agy hooks, plugins or MCP config changed since someone last ran Antigravity there from the chat; send a message in the chat to release it. Gemini (deprecated) passes it through as `--approval-mode`; OpenCode, Pi and AMP ignore the field — full coverage is tracked in [#332](https://github.com/littlebearapps/untether/issues/332). See [Schedule tasks — Autonomous crons](schedule-tasks.md#autonomous-crons) for the everyday framing.

## Per-cron model and effort

`model` and `reasoning` on a `[[triggers.crons]]` entry pick the model and effort (reasoning) for that scheduled run only, winning over the chat's `/model` and reasoning setting; unset inherits as before ([#743](https://github.com/littlebearapps/untether/issues/743)):

```toml
[[triggers.crons]]
id = "nightly-triage"
schedule = "0 6 * * *"
engine = "claude"
model = "sonnet"
reasoning = "low"
permission_mode = "auto"
prompt = "Triage new issues."
```

Set `engine` alongside `model` so the name matches the engine. `reasoning` is checked against the engine's levels at config load when `engine` is set (Claude `low`…`max`, Codex `low`…`xhigh`; OpenCode/Pi don't take it) — a bad value fails the trigger config the way a bad `permission_mode` does (on reload the previous triggers stay active and `config.reload.triggers_failed` is logged). A reply to the cron's message runs on the chat's model. Logs: `trigger.cron.model_override`, `trigger.cron.reasoning_override`. Webhooks have neither field yet — per-webhook overrides are tracked in [#332](https://github.com/littlebearapps/untether/issues/332). See [Schedule tasks — Pick a model per cron](schedule-tasks.md#pick-a-model-per-cron).

## Delayed runs with `/at`

For ad-hoc one-shot delays, use the `/at` command directly in Telegram — no TOML edit required:

```
/at 30m Check the build status
/at 2h Review open PRs
/at 90s Run the test suite
```

Duration supports `Ns` / `Nm` / `Nh` with a 60s minimum and 24h maximum. Pending delays are cancelled via `/cancel` and lost on restart. Per-chat cap of 20 pending delays. The run uses the engine and project a plain message in that chat or topic would use at the moment you schedule it ([#950](https://github.com/littlebearapps/untether/issues/950)); see [Schedule tasks](schedule-tasks.md#one-shot-delays-with-at).

## Discovering configured triggers

Once triggers are configured, `/ping` in the targeted chat shows a summary:

```
🏓 pong — up 2d 4h 12m 3s
⏰ triggers: 1 cron (daily-review, 9:00 AM Mon–Fri (Melbourne))
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
