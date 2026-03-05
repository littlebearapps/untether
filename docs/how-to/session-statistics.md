# Session statistics

See how much work your agents have done while you've been away. The `/stats` command shows per-engine session statistics — run counts, action totals, and duration — across today, this week, and all time.

## View statistics

Send `/stats` in any chat:

```
/stats
```

Example output:

```
Session Stats — Today

claude: 5 runs, 42 actions, 12m 30s, last 2h ago
codex: 3 runs, 18 actions, 4m 15s, last 45m ago

Total: 8 runs, 60 actions, 16m 45s
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
/stats week          # this week
/stats all           # all time (up to 90 days)
/stats claude week   # claude, this week
```

## Check auth status

Use `/stats auth` to see authentication status for all installed engines:

```
/stats auth
```

Example output:

```
Auth Status

claude: logged in (oauth)
codex: logged in using chatgpt
opencode: 2 provider(s)
pi: 1 provider(s)
```

This checks each engine's credential files or auth status commands without starting a run.

## How data is collected

Untether automatically records statistics after each run completes:

- **Run count** — incremented for every completed run
- **Action count** — total tool calls / actions across all runs
- **Duration** — cumulative engine execution time (in milliseconds)
- **Last run timestamp** — when the engine last completed a run

Data is stored in `stats.json` in the Untether config directory (`~/.untether/` by default). Records older than 90 days are automatically pruned on startup.

## Related

- [Cost budgets](cost-budgets.md) — per-run and daily cost limits
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
