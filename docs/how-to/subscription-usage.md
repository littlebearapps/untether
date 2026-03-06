# Subscription usage tracking

Keep tabs on your Claude Code subscription from anywhere — Untether surfaces usage directly in [Telegram](https://telegram.org). This guide covers checking usage on demand and enabling automatic usage footers after every run.

## Check usage with /usage

Send `/usage` in any chat to see a full breakdown of your Claude Code subscription usage:

!!! untether "Untether"
    **5h window**: 45% used (resets in 2h 15m)
    ████████░░░░░░░░░░░░ 45%

    **7d window**: 30% used (resets in 4d 3h)

    **Sonnet (7d)**: 25% used
    **Opus (7d)**: 5% used

    **Extra credits**: $0.00

The breakdown includes:

| Section | What it shows |
|---------|--------------|
| **5h window** | Percentage used in the current 5-hour rate limit window, time until reset, and a progress bar |
| **7d window** | Percentage used in the 7-day rolling window, time until reset |
| **Sonnet (7d)** | Sonnet-specific 7-day usage |
| **Opus (7d)** | Opus-specific 7-day usage |
| **Extra credits** | Any overage credits consumed (if applicable) |

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
5h: 45% (2h 15m) | 7d: 30% (4d 3h)
```

This tells you how much of your 5-hour and 7-day rate limits you've used, and when they reset — all without leaving the chat.

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

## Claude Code credentials

The `/usage` command reads your Claude Code OAuth credentials to fetch live data from the Anthropic API. If you see **"No Claude credentials found"**, run `claude login` in your terminal to authenticate.

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
