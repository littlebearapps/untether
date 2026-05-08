# Inline settings menu

Adjust Untether's behaviour without editing config files or restarting — tap buttons right in Telegram. The `/config` command opens an interactive settings menu with inline keyboard buttons, similar to BotFather's settings style. Navigate sub-pages, toggle settings, and return to the overview, all within a single message that edits in place.

## Open the menu

Send `/config` in any chat:

```
/config
```

The home page shows current values for all settings, with buttons arranged in pairs (max 2 per row) for comfortable mobile tap targets:

```
🐕 Untether settings

Agent controls (Claude Code)
Plan mode: on  · approve actions
Ask mode: on  · interactive questions
Diff preview: off  · buttons only

Verbose: off
Cost & usage: cost on, sub off
Resume line: on
Engine: claude (global)
Model: default
Listen: all

[📋 Plan mode]     [❓ Ask mode]
[📝 Diff preview]  [🔍 Verbose]
[💰 Cost & usage]  [↩️ Resume line]
[📡 Listen]        [⚙️ Engine & model]
[🧠 Reasoning]     [ℹ️ About]

📖 Help guides · 🐛 Report a bug
```

<!-- TODO: capture screenshot: config-menu-v035 — /config home page with 2-column toggle layout -->

!!! note "Engine-specific controls"
    The home page adapts to the current engine. **Claude Code** shows Plan mode, Ask mode, and Diff preview under "Agent controls". **Codex CLI** shows **Approval policy** (full auto / safe). **Gemini CLI** shows **Approval mode** (read-only / edit files / full access). Engines without interactive controls (OpenCode, Pi, Amp) skip the agent controls section entirely.

## Navigate sub-pages

Tap any button to open that setting's page. Each sub-page shows:

- A description of the setting
- The current effective value (resolved from override or default — never shows a bare "default" label)
- Buttons to change the value
- A **Clear override** button to revert to the global/engine default
- A **← Back** button to return to the home page

## Toggle behaviour

Most settings use a **two-button selection** pattern: `[On] [Off] [Clear]` with a ✓ on the active option. Tap either button to set the value. Tapping **Clear** removes the per-chat override and falls back to the global setting.

When you tap a setting button:

1. **Confirmation toast** — a brief popup appears confirming the change (e.g. "Plan mode: off", "Verbose: on"). This uses the same toast mechanism as Claude Code approval buttons.
2. **Auto-return** — the menu automatically navigates back to the home page, showing the updated value across all settings. No need to tap "Back" manually.

### Multi-state settings

Some settings have more than two states and use a different layout:

- **Plan mode** — three options (off / on / auto) shown as separate buttons in a 2+1 split: `[Off] [On]` on the first row, `[Auto] [Clear override]` on the second
- **Approval mode** (Gemini) — three options (read-only / edit files / full access)
- **Effort** (Claude Code) — low / medium / high / xhigh / max
- **Reasoning** (Codex) — minimal / low / medium / high / xhigh

The active option is marked with a ✓ prefix. Tap a different option to switch.

### Engine-aware visibility

Settings are engine-specific and only appear when relevant:

- **Plan mode** — Claude Code only. Codex and Gemini have their own pre-run policies instead.
- **Approval policy** — Codex CLI only. Toggle between "full auto" (default, all tools approved) and "safe" (untrusted tools blocked via `--ask-for-approval untrusted`). This is a pre-run policy — not interactive mid-run approval.
- **Approval mode** — Gemini CLI only. Toggle between "read-only" (default, write tools blocked), "edit files" (file reads/writes OK, shell commands blocked via `--approval-mode auto_edit`), and "full access" (all tools approved via `--approval-mode yolo`). This is a pre-run policy.
- **Ask mode** and **Diff preview** — Claude Code only. Hidden for other engines.
- **Reasoning** — Claude Code and Codex only. Hidden for OpenCode, Pi, Gemini, and Amp.
- **Engine & model** — always visible. Engine and model are merged into a single page. Shows the current engine and model override; to set a model, use `/model set <name>`.

When you switch engines via the Engine & model page, the home page automatically shows or hides the relevant controls.

## Available settings

| Setting | Options | Persisted |
|---------|---------|-----------|
| Plan mode | off, on, auto | Yes (chat prefs) |
| Approval policy | full auto, safe | Yes (chat prefs) |
| Approval mode | read-only, edit files, full access | Yes (chat prefs) |
| Ask mode | off, on | Yes (chat prefs) |
| Verbose | off, on | Yes (chat prefs) |
| Diff preview | off, on | Yes (chat prefs) |
| Engine & model | any configured engine + model | Yes (chat prefs) |
| Effort / Reasoning | Claude: low, medium, high, xhigh, max; Codex: minimal, low, medium, high, xhigh | Yes (chat prefs) |
| Cost & usage | API cost, subscription usage, budget, auto-cancel | Yes (chat prefs) |
| Resume line | off, on | Yes (chat prefs) |
| Listen | all, mentions | Yes (chat prefs) |
| Budget enabled | off, on | Yes (chat prefs) |
| Budget auto-cancel | off, on | Yes (chat prefs) |

Approval policy appears instead of Plan mode when the engine is Codex CLI. Approval mode appears instead of Plan mode when the engine is Gemini CLI.

### Triggers page {#triggers-page}

When `[triggers]` is enabled and at least one cron or webhook is configured, the home page gains a one-button toggle row at the bottom and a dedicated `📡 Triggers` button that opens the Triggers page (`config:tg`) ([#271](https://github.com/littlebearapps/untether/issues/271) Tier 2 + [#294](https://github.com/littlebearapps/untether/issues/294)).

The Triggers page shows:

* **State and counts** — `running` / `paused`, plus per-chat cron and webhook totals.
* **Master pause/resume toggle** — tap **Pause** to suspend all cron firing and webhook dispatch globally without editing config; tap **Resume** to clear it. While paused, webhooks return `503 triggers paused` (with `Retry-After: 60`), `/health` reports `paused: true`, and `/ping` shows `⏸ triggers paused: … (suspended)`. Pause is in-memory only — restart auto-resumes (the safe default).
* **Per-chat cron list** — each line shows the cron `id`, human-readable schedule via `describe_cron(schedule, timezone)`, project, engine, and last-fired relative time.
* **Per-chat webhook list** — each line shows the webhook `id`, path, auth scheme, project, engine, and last-fired.

Lists are scoped to the current chat (`crons_for_chat()` / `webhooks_for_chat()` with the bridge `default_chat_id` fallback), capped at 10 entries with a `…and N more (see untether.toml)` overflow marker. The pause/resume controls remain visible even when the chat has no triggers configured.

See [Schedule tasks](schedule-tasks.md#pausing-all-triggers) for the pause flow end-to-end.

### Loop mode page {#loop-mode}

When the active engine is Claude Code, the home page gains a `🔁 Loop mode` button that opens the Loop sub-page ([#289](https://github.com/littlebearapps/untether/issues/289)). Loop mode is **off by default** — turning it on enables Untether's observation of Claude's session-scoped scheduling tools (`CronCreate`, `ScheduleWakeup`) so iterations keep firing after the subprocess exits.

The page shows:

* **State** — `Loop mode: on` / `off` for the current chat (per-chat override over the global `[loop] enabled` default).
* **Cost + quota warning** — explicit reminder before turning ON: every loop fire counts against `[cost_budget]`, and the runaway caps in `[loop]` (`max_iterations`, `max_total_duration_hours`, `expiry_days`) are the safety net.
* **💰 Set a budget** — deep-link to the `Cost & Usage` page (`config:cu`) for one-tap budget setup.
* **Toggle row** — `[On] [Off] [Clear]` with ✓ on the active option.

`/cancel` and `/new` both drop pending loop iterations for the current session and write a do-not-resume sentinel so a subsequent `loop_scheduler` resume can't replay them. `/continue` is unaffected (it doesn't trigger loop replay).

Loop mode is **Claude-only** (`LOOP_SUPPORTED_ENGINES = frozenset({"claude"})`); the button is hidden for other engines. See [Schedule tasks → Loop mode](schedule-tasks.md#loop-mode) for the full architecture and cost guidance.

### Cost & Usage page

The Cost & Usage sub-page merges cost display and budget controls into a unified page with toggle rows:

- **API cost** — per-run cost in the message footer (requires engine cost reporting)
- **Subscription usage** — 5h/weekly subscription usage in the footer (Claude Code only)
- **Budget enabled** — turn budget tracking on or off for this chat (overrides global `[cost_budget]` setting)
- **Budget auto-cancel** — enable or disable automatic run cancellation when a budget is exceeded

Each toggle uses the `[✓ Label: on] [Label: off] [Clear]` compact pattern (labels distinguish the four toggles). Clear removes the per-chat override and falls back to the global config.

For historical cost data across sessions, use the [`/stats`](../reference/commands-and-directives.md) command.

## Callbacks vs commands

- **Text command** (`/config`): sends a new message with the menu.
- **Button tap**: edits the existing message in place — no message spam.

All button interactions use early callback answering for instant feedback.

## Related

- [Plan mode](plan-mode.md) — detailed plan mode documentation
- [Interactive approval](interactive-approval.md) — approval buttons and engine-specific policies
- [Cost budgets](cost-budgets.md) — budget configuration and alerts
- [Verbose progress](verbose-progress.md) — verbose mode details and global config
- [Switch engines](switch-engines.md) — engine selection
- [Group chat](group-chat.md) — listen mode in groups
