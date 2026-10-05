# Subscription usage tracking

Keep tabs on your Claude Code subscription from anywhere — Untether surfaces usage directly in [Telegram](https://telegram.org). This guide covers checking usage on demand and enabling automatic usage footers after every run.

In Codex, OpenCode and other non-Claude chats, `/usage` shows the token totals of the chat's last session instead — see [Cost budgets → Other engines](cost-budgets.md#other-engines).

## Check usage with /usage

Send `/usage` in any chat to see a full breakdown of your Claude Code subscription usage:

!!! untether "Untether"
    📊 Claude Code Usage

    5h window: ████░░░░░░ 45% (resets in 2h 15m)<br>
    Weekly:    ███░░░░░░░ 30% (resets in 4d 3h)<br>
    Sonnet:    ██░░░░░░░░ 25%<br>
    Opus:      █░░░░░░░░░ 5%<br>
    Extra:     $0.00 used

The breakdown includes:

| Line | What it shows |
|---------|--------------|
| **5h window** | Percentage used in the current 5-hour rate limit window, a progress bar, and time until reset |
| **Weekly** | Percentage used in the 7-day rolling window, and time until reset |
| **Sonnet** | Sonnet-specific 7-day usage |
| **Opus** | Opus-specific 7-day usage |
| **Extra** | Overage credits used, shown only when extra usage is turned on for your account |

## Enable footer usage line

To show a compact usage summary after every completed Claude Code run, enable the subscription usage footer:

=== "untether config"

    ```sh
    untether config set footer.show_subscription_usage true
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [footer]
    show_subscription_usage = true
    ```

When enabled, completed messages include a line like:

```
⚡ 5h: 45% | 7d: 30%
```

This tells you how much of your 5-hour and 7-day rate limits you've used — all without leaving the chat. Once a window passes 50%, its reset time is added (`⚡ 5h: 72% (1h 14m) | 7d: 30%`).

With the footer off, Untether still adds a warning line once the 5-hour window passes 70% (`⚡5h: 75% (1h 14m) | 7d: 30%`), turning to `⚠️` at 90% and `🛑 5h limit hit — resets in …` at 100%.

!!! tip "Limit notes during a run"
    Separately from the footer, a running Claude session shows a one-off `⚠️ 5h limit 85% used — resets 17:30` note when Claude Code reports the window is nearly used, and `⏳ Rate limited until …` if a request is actually refused. Since v0.36.0 routine usage snapshots no longer show a false `Rate limited` wait ([#790](https://github.com/littlebearapps/untether/issues/790)). See [Troubleshooting → Rate-limit and API-retry notes](troubleshooting.md#rate-limit-and-api-retry-notes-claude).

## Combine with API cost

By default, Untether shows API token and cost information in the footer (`show_api_cost = true`). You can show both API cost and subscription usage together:

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [footer]
    show_api_cost = true
    show_subscription_usage = true
    ```

Or disable API cost to show only subscription usage:

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [footer]
    show_api_cost = false
    show_subscription_usage = true
    ```

## Debug page (`/usage debug`)

When the subscription usage footer goes silent, run `/usage debug` to see a one-message diagnostic block ([#410](https://github.com/littlebearapps/untether/issues/410)) without grepping `journalctl`:

!!! untether "Untether"
    📊 Claude Code Usage<br>
    …<br>
    🔧 **debug**<br>
    • cache: last success 2026-05-04T11:07:32+00:00 (42s ago, fresh)<br>
    • last error: none<br>
    • OAuth token: expires 2026-05-15T08:00:00+00:00 (in 261h 3m)<br>
    • schema mismatches this process: 0

The block shows:

| Field | What it tells you |
|---|---|
| **cache** | UTC timestamp, age and freshness label (`fresh` within 60 s, otherwise `stale`) for the last successful Anthropic API call. |
| **last error** | Class name and truncated message of the most recent failure (or `none`). |
| **OAuth token** | UTC expiry time and hours/minutes until expiry for the Claude Code OAuth token. Reads "expired" if the token has lapsed. |
| **schema mismatches** | Count of `claude_usage.schema_mismatch` warnings since Untether started — increments whenever Anthropic ships a usage-payload shape change. Stays at `0` on a healthy host. |

Use this when subscription usage stops appearing in the footer or returns stale numbers — the four fields point at the most likely root causes (auth lapsed, API shape changed, transient HTTP failure, or simply nothing fresh has been fetched yet).

## Claude Code credentials

The `/usage` command reads your Claude Code OAuth credentials to fetch live data from the Anthropic API. If you see **"No Claude Code credentials found"**, sign in to Claude Code on that machine (run `claude` and follow the login prompt).

Credential storage varies by platform:

| Platform | Storage | Path |
|----------|---------|------|
| Linux | Plain-text file | `~/.claude/.credentials.json` |
| macOS | macOS Keychain | Entry: `Claude Code-credentials` |

Untether checks both locations automatically. If `/usage` still fails after logging in, verify that the Claude Code CLI is working by running `claude` directly.

## Related

- [Cost budgets](cost-budgets.md) — set per-run and daily cost limits
- [Configuration](../reference/config.md) — full config reference for footer settings
- [Troubleshooting](troubleshooting.md) — credential issues with `/usage`
