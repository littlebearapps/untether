# Subscription usage tracking

Keep tabs on your Claude Code subscription from anywhere — Untether surfaces usage directly in [Telegram](https://telegram.org). This guide covers checking usage on demand and enabling automatic usage footers after every run.

In an Antigravity chat, `/usage` shows agy's own quota groups and AI credits — see [Cost budgets → Antigravity](cost-budgets.md#antigravity). The footer switch below also works for Antigravity, where the line shows what your last `/usage` fetched (up to 15 minutes old) — see [Use Antigravity CLI → Usage and quota](antigravity.md#usage-and-quota). In a Codex chat, `/usage` shows your ChatGPT plan quota from Codex itself, and the footer switch works there too — see [Codex](#codex) below. In OpenCode and other chats, `/usage` shows the token totals of the chat's last session instead — see [Cost budgets → Other engines](cost-budgets.md#other-engines).

<!-- TODO: capture screenshot: antigravity-quota-footer — the end of an Antigravity final answer with Subscription usage on: the footer and the `⚡ Gemini Models 5h: 12% | wk: 3%` quota line under it -->
<!-- <img src="../assets/screenshots/antigravity-quota-footer.jpg" alt="Antigravity answer footer with the quota line built from the last /usage" width="360" loading="lazy" /> -->

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

With the footer off, Untether still adds a warning line once the 5-hour window passes 70% (`⚡5h: 75% (1h 14m) | 7d: 30%`), turning to `⚠️` at 90% and `🛑 5h limit hit — resets in …` at 100%. Since v0.36.1 those figures come from the rate-limit snapshot Claude Code itself sends during the run, so Untether doesn't read your Claude login or call Anthropic's usage endpoint unless the footer is on or you send `/usage` ([#1150](https://github.com/littlebearapps/untether/issues/1150)). A run that gets no snapshot (Claude Code sends one only when a figure changes, and none for API-key logins) shows no warning.

!!! tip "Limit notes during a run"
    Separately from the footer, a running Claude session shows a one-off `⚠️ 5h limit 85% used — resets 17:30` note when Claude Code reports the window is nearly used, and `⏳ Rate limited until …` if a request is actually refused. Since v0.36.0 routine usage snapshots no longer show a false `Rate limited` wait ([#790](https://github.com/littlebearapps/untether/issues/790)). See [Troubleshooting → Rate-limit and API-retry notes](troubleshooting.md#rate-limit-and-api-retry-notes-claude).

## Codex

Since v0.36.2, `/usage` in a Codex chat asks Codex for your ChatGPT plan quota. Untether starts `codex app-server` for a moment and reads `account/rateLimits` — it costs no tokens, works on both Codex transports, and the answer is reused for 60 seconds ([#966](https://github.com/littlebearapps/untether/issues/966)).

!!! untether "Untether"
    📊 **Codex usage** · Pro plan<br>
    7d: █░░░░░░░░░ 8% · resets in 2d 4h<br>
    Credits: 62,500 · Rate-limit resets available: 3<br>
    *From Codex's own account/rateLimits (no quota spent).*<br>
    <br>
    **Last session in this chat**<br>
    Session `019dc356…` · 3 runs<br>
    **Session total:** 168k in (142k cached) · 1.6k out

- Every limit Codex reports gets its own line (your plan may have a 5-hour window, a weekly one or both). When Codex itself says it is blocking new turns, `/usage` starts with `🛑 Codex is blocking new turns until the limit resets`.
- **Plan quota vs credits.** The bars are your plan's included usage; *Credits* is any extra credit balance on the account. *Rate-limit resets available* is how many free resets you hold. Untether only shows the count; use a reset from Codex itself.
- **No dollars.** On ChatGPT plans Codex reports no dollar cost per session, so Codex has no `💰` line and `[cost_budget]` limits don't trip for it.
- **Not signed in, or an API key.** `/usage` says `Codex isn't signed in on this host. Run /auth codex, then try again.` when Codex is logged out, and that there's no ChatGPT plan quota to show when Codex runs on an API key, Amazon Bedrock or Google Cloud.
- **Footer.** With the footer switch on (or a window past 70%), a Codex final shows `⚡ 7d: 8%`, `⚠️` from 90%, and `🛑 Codex usage limit reached — resets in …` when Codex blocks turns. The footer only shows what Untether already read: from `/usage`, from Codex's own updates during an app-server run, or from a background read on the first Codex run (then at most hourly). It never makes a final wait. When a run hits the limit, the error hint also says when the limit resets.
- `/usage debug` adds the cache age, the last error, the Codex version and the login type.

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

The `/usage` command, and the footer when you turn it on, read your Claude Code OAuth credentials to fetch live data from the Anthropic API. Nothing else does: with the footer off, a run's near-limit warning uses the data Claude Code streams instead. If you see **"No Claude Code credentials found"**, sign in to Claude Code on that machine (run `claude` and follow the login prompt).

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
