# Session statistics

See how much work your agents have done while you've been away. The `/stats` command shows per-engine session statistics — run counts, action totals, and duration — across today, this week, and all time.

## View statistics

Send `/stats` in any chat:

```
/stats
```

Example output:

```
📊 Session Stats — Today

claude: 5 runs, 42 actions, 12m 30s, last 2h ago (1 triggered, 4 manual)
codex: 3 runs, 18 actions, 4m 15s, last 45m ago (0 triggered, 3 manual)

Total: 8 runs, 60 actions, 16m 45s (1 triggered, 7 manual)
```

## Filter by engine

Pass an engine name to see stats for just that engine:

```
/stats claude
```

## Change the time period

Specify a period after the engine name (or on its own):

```
/stats today         # today only (default)
/stats week          # the last 7 days, today included
/stats all           # everything in stats.json
/stats claude week   # claude, this week
```

## Check auth status

Use `/stats auth` to see authentication status for all installed engines:

```
/stats auth
```

Example output:

```
🔑 Auth Status

claude: ✅ <auth method>
codex: ✅ logged in using chatgpt
opencode: ✅ 2 provider(s)
pi: ❌ no credentials
```

This checks each engine's credential files or auth status commands without starting a run. Claude shows the `authMethod` its `claude auth status` reports; an engine whose status can't be read shows `❓ status unavailable`. On `/stats`, each engine line ends with a `(N triggered, M manual)` breakdown when either count is non-zero (cron, webhook and `/at` runs count as triggered).

## How data is collected

Untether automatically records statistics after each run completes:

- **Run count** — incremented for every completed run
- **Action count** — total tool calls / actions across all runs
- **Duration** — cumulative wall-clock time of the runs (stored in milliseconds)
- **Last run timestamp** — when the engine last completed a run

Data is stored in `stats.json` in the Untether config directory (`~/.untether/` by default), one bucket per engine per day. Day buckets older than 90 days are folded into a single per-engine `archive` total at startup and on the first run of each new day, so the file stays small. `/stats all` still covers everything recorded, while `today` and `week` only read the recent day buckets ([#897](https://github.com/littlebearapps/untether/issues/897)).

## Background-task telemetry

v0.35.2 adds per-session tracking for Claude Code's long-running primitives. Counts and outstanding deadlines are recorded for: `Monitor`, `Bash` (`run_in_background`), `Agent` (`run_in_background`), `ScheduleWakeup`, and `RemoteTrigger`. These feed the stall detector, the background-tasks message and `/ping` (`⏳ background: N tasks running`), so background work isn't mistaken for a wedged session. ([#347](https://github.com/littlebearapps/untether/issues/347)) Since v0.36.0 Claude Code's own task events drive this, and running background tasks are shown live in the progress and status messages — see [Verbose progress → Background tasks](verbose-progress.md#background-tasks-claude).

## Cost footer accuracy

Run and session cost footers were tightened in v0.35.2. Claude usage queries use a 60 s TTL cache (stale-while-error), Gemini pulls from `stats.total_cost_usd`, and AMP now reports `total_cost_usd`. Zero-turn sessions render `$0.00` instead of a blank. ([#316](https://github.com/littlebearapps/untether/issues/316))

Since v0.36.0 a resumed session's figures are per run. Claude reports a running cost total for the whole session, and Codex and Antigravity a running token total for the whole thread, so Untether records the difference from the previous run; footers and budgets no longer count a session's earlier runs again ([#778](https://github.com/littlebearapps/untether/issues/778), [#419](https://github.com/littlebearapps/untether/issues/419)). A turn Claude runs by itself after a background task counts as its own small run. In Codex and OpenCode chats, `/usage` shows the token totals of the chat's last session — see [Cost budgets → Other engines](cost-budgets.md#other-engines).

## Related

- [Cost budgets](cost-budgets.md) — per-run and daily cost limits
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
