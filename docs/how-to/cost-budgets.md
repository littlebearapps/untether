# Cost budgets

Running agents remotely means they can rack up costs while you're not watching. Untether tracks API costs per run and per day, with configurable budget limits and warning thresholds to keep spending visible — even when you're away from the screen.

## Configure budgets

=== "untether config"

    ```sh
    untether config set cost_budget.enabled true
    untether config set cost_budget.max_cost_per_run 2.00
    untether config set cost_budget.max_cost_per_day 10.00
    ```

=== "toml"

    ```toml
    [cost_budget]
    enabled = true
    max_cost_per_run = 2.00
    max_cost_per_day = 10.00
    ```

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable cost tracking and budget enforcement |
| `max_cost_per_run` | (none) | Maximum cost for a single run (USD) |
| `max_cost_per_day` | (none) | Maximum total cost per day (USD) |
| `warn_at_pct` | `70` | Show a warning when this percentage of the budget is reached |
| `auto_cancel` | `false` | **Stop at limit**: refuse new runs once the daily budget is reached, and end a session after the reply that passes the per-run budget (see [Stop at limit](#stop-at-limit)) |
| `warn_run_above_usd` | (unset = $20) | Flag any single run that costs more than this, even with `enabled = false`; `0` turns it off |
| `notify_run_outlier` | `true` | Post the one-line chat notice for an outlier run (the log line is written either way) |

## Per-chat overrides

You can toggle budgets on or off per chat without editing the config file. Open `/config` → **💰 Cost & usage** and use the toggle buttons:

- **Budget** — turn budget tracking on or off for this chat
- **Stop at limit** — the per-chat override of `auto_cancel` (see [Stop at limit](#stop-at-limit))

These override the global `[cost_budget]` settings for the specific chat. Clear the override to revert to the global setting. See [Inline settings](inline-settings.md) for the full `/config` menu reference.

!!! warning "Loop mode and budgets"
    If you turn on Loop mode in `/config → 🔁 Loop mode`, autonomous loop fires count toward the same daily and per-run budget caps as manual runs. There is no separate per-loop budget. With **Stop at limit** on, a loop fire after the daily budget is reached is refused like any other run; without it, a budget only alerts you about loop spend. Without it, the runaway caps in `[loop]` (`max_iterations`, `max_total_duration_hours`, `expiry_days`) are what actually bound a loop. **Set a budget before turning on Loop mode** so you hear about its spend. See [Schedule tasks → Loop mode](schedule-tasks.md#loop-mode) for the full picture. ([#289](https://github.com/littlebearapps/untether/issues/289))

## How it works

After each run completes, Untether checks the reported cost against your budgets:

1. **Per-run check**: if the run cost reaches `max_cost_per_run`, you get an alert
2. **Daily check**: if the cumulative daily cost reaches `max_cost_per_day`, you get an alert
3. **Warning threshold**: at `warn_at_pct` (default 70%) of either budget, you get an early warning

Only one alert is shown per run, and the per-run check comes first: a run that trips the per-run warning or limit doesn't also report the daily one.

!!! note "Claude costs are per run"
    Claude reports a running total for the whole session, including earlier runs you resumed. Since v0.35.5 Untether subtracts what the session had already cost, so budgets, `/stats` and the footer see only this run's spend. A turn that Claude runs on its own after a background task counts as its own small run ([#778](https://github.com/littlebearapps/untether/issues/778)).

!!! note "Token-only engines"
    Engines that don't report USD costs (Codex, and OpenCode on its free tier) show token counts in the footer instead, marked `🔢` (e.g. `🔢12.3k/400`, input/output); `💰` means the footer carries a cost ([#417](https://github.com/littlebearapps/untether/issues/417)). Codex reports a running total for the whole thread, so since v0.35.5 Untether shows each run's own share; a figure that is still the whole thread (for example the first `/continue` of a thread started outside Untether) is labelled `· thread total` ([#419](https://github.com/littlebearapps/untether/issues/419)). Pi currently shows no cost or token footer: its usage block isn't in a shape the footer reads. Gemini CLI and AMP (both deprecated) surface `total_cost_usd` when their CLI reports one. Budget alerts apply only to the USD-reporting path.

### Expensive single runs

Even with no budget configured, a single run that costs more than `warn_run_above_usd` ($20 unless you set it) adds one line to its final message, whatever your footer settings ([#702](https://github.com/littlebearapps/untether/issues/702)):

```
💸 This run cost $24.30 (over the $20.00 alert)
```

It also logs `cost.run_outlier` with the run's shape (turns, cost per turn, duration and token counts), so you can tell one long task from a session whose context has grown expensive ([#717](https://github.com/littlebearapps/untether/issues/717)). The line is skipped when a budget alert already covered the run. Separately, if spend is neither shown (`[footer] show_api_cost = false`) nor bounded (no `[cost_budget]`), Untether logs one `config.cost_visibility_gap` warning per start ([#658](https://github.com/littlebearapps/untether/issues/658)).

### Alert levels

| Alert | Icon | Meaning |
|-------|------|---------|
| Warning | ⚠️ | Cost is approaching the budget threshold |
| Exceeded | 🛑 | Cost has exceeded the budget |

Because the check runs when a result arrives, an alert on its own doesn't stop anything: turn on [Stop at limit](#stop-at-limit) for that. Claude's automatic retry after a stalled stream is skipped either way when the failed run already hit a per-run or daily limit.

With the cost footer on (`[footer] show_api_cost = true`, the default), the alert is a suffix on the `💰` line, ` ⚠️ 73%` or ` 🛑 budget`. With the footer off, it's a line of its own:

!!! untether "Untether"
    ⚠️ Run cost $1.45 is 73% of per-run budget $2.00

<img src="../assets/screenshots/cost-warning-alert.jpg" alt="Cost warning alert showing budget threshold exceeded" width="360" loading="lazy" />

### Stop at limit

Set `auto_cancel = true` (or turn on **Stop at limit** in `/config` → **💰 Cost & usage**) to make the limits act, not just alert ([#896](https://github.com/littlebearapps/untether/issues/896)). Budgets must be enabled for the chat. Claude reports cost only when a reply finishes, so Untether acts at the two points it safely can; it never interrupts a reply in progress.

- **Daily budget reached:** new runs are refused until midnight: prompts, `/continue`, follow-ups into a live session, `/at`, loop fires, crons and webhooks. In a chat you get a message with a **Run anyway** button, which starts the refused run once (only in that chat):

    !!! untether "Untether"
        🛑 Daily budget reached ($10.20 of $10.00). New runs are paused until midnight.

    Crons, webhooks and loop fires are skipped with a one-line reply to their own announcement instead (no button), for example `🛑 Daily budget reached ($10.20 of $10.00). Skipped cron:daily-review; new runs are paused until midnight.` Both are logged as `cost_budget.run_blocked`.
- **Per-run budget passed:** when a reply takes the run's total past `max_cost_per_run`, that reply is delivered with a `🛑 Stopped: run cost $2.30 passed the per-run budget $2.00` line, then the session is closed, so no background wake-ups or follow-ups add to it. The run's total counts every turn of a live session, not only the last one. Background tasks still running are stopped and named. The same happens, with `🛑 Stopped: today's cost … reached the daily budget …`, when a reply takes the day's total to `max_cost_per_day`. Logged as `cost_budget.run_stopped`.

Your next message after a per-run stop starts a new run with a fresh per-run total. A session stopped by the daily budget stays paused until midnight unless you tap **Run anyway**.

### Daily reset

The daily cost counter resets at midnight in the host's local time zone (the server clock, not yours), and each new day starts from zero. Since v0.35.5 the day's total is saved to `daily_cost.json` next to `untether.toml` after every run, so restarts, upgrades and daily reboots no longer reset it; a file left over from an earlier day is ignored ([#898](https://github.com/littlebearapps/untether/issues/898)).

## Check current usage

Use the `/usage` command in Telegram to see your Claude Code subscription usage:

```
/usage
```

This shows:

- **5h window**: usage percentage and time until reset
- **Weekly**: 7-day usage percentage and time until reset
- **Per-model breakdown**: Sonnet and Opus usage (if applicable)
- **Extra**: overage credits used, when extra usage is turned on for your account

The `/usage` command reads your Claude Code OAuth credentials to fetch live data from the Anthropic API. If you see "No Claude Code credentials found", sign in to Claude Code on that machine (run `claude` and follow the login prompt).

!!! untether "Untether"
    📊 Claude Code Usage

    5h window: ████░░░░░░ 42% (resets in 2h 6m)<br>
    Weekly:    ███░░░░░░░ 28% (resets in 5d 2h)<br>
    Sonnet:    ████░░░░░░ 38%<br>
    Opus:      ░░░░░░░░░░ 4%

### Other engines

Codex, OpenCode and the other engines don't report subscription quota, so in their chats `/usage` shows the token totals of the chat's last session of that engine instead: the session total, the last run, the run count, and the last run's cost when the engine reports one. Codex reports a running total for the whole thread, so Untether records each run's difference — the footer (`🔢12.3k/400`) shows *this run's* tokens, and `/usage` shows the thread total. A token-only footer uses `🔢`; `💰` means the footer carries a cost.

!!! untether "Untether"
    📊 **codex** · last session in this chat<br>
    Session `019dc356…` · 3 runs<br>
    **Session total:** 168k in (142k cached) · 1.6k out (reasoning 900)<br>
    **Last run:** 12k in · 400 out<br>
    Quota and plan limits are not available for codex — its exec mode doesn't report them. Transcript: /export

The history behind `/usage` is kept in memory, so straight after a restart it says there is no completed run yet; send a prompt first.

<img src="../assets/screenshots/usage-command.jpg" alt="/usage command output showing 5h window, weekly usage, and per-model breakdown" width="360" loading="lazy" />

## Subscription usage footer

Untether can show subscription usage in the footer of completed messages. This is configured in the `[footer]` section:

=== "toml"

    ```toml
    [footer]
    show_subscription_usage = true
    ```

When enabled, completed messages show a line like:

```
⚡ 5h: 45% | 7d: 30%
```

This tells you how much of your 5-hour and 7-day rate limits you've used. A window's reset time is added once it passes 50%, for example `⚡ 5h: 72% (1h 14m) | 7d: 30%`. See [Subscription usage](subscription-usage.md) for details.

## Historical statistics

For historical run data beyond the current session, use the `/stats` command:

```
/stats
```

This shows per-engine session statistics (runs, actions, duration) across today, this week, and all time. Pass an engine name to filter (e.g. `/stats claude`). Data is persisted in the config directory and auto-pruned after 90 days.

## Related

- [Configuration](../reference/config.md) — full config reference for budget settings
- [Commands & directives](../reference/commands-and-directives.md) — `/stats` and `/usage` command reference
- [Troubleshooting](troubleshooting.md) — credential issues with `/usage`
