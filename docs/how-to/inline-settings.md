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
Trigger: all

[📋 Plan mode]     [❓ Ask mode]
[📝 Diff preview]  [🔍 Verbose]
[💰 Cost & usage]  [↩️ Resume line]
[📡 Trigger]       [⚙️ Engine & model]
[🧠 Reasoning]     [ℹ️ About]

📖 Settings guide · Troubleshooting
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

Most settings use a **single toggle button** pattern: `[✓ Feature: on]` paired with `[Clear]`. Tapping the toggle flips it between on and off. Tapping **Clear** removes the per-chat override and falls back to the global setting.

When you tap a setting button:

1. **Confirmation toast** — a brief popup appears confirming the change (e.g. "Plan mode: off", "Verbose: on"). This uses the same toast mechanism as Claude Code approval buttons.
2. **Auto-return** — the menu automatically navigates back to the home page, showing the updated value across all settings. No need to tap "Back" manually.

### Multi-state settings

Some settings have more than two states and use a different layout:

- **Plan mode** — three options (off / on / auto) shown as separate buttons in a 2+1 split: `[Off] [On]` on the first row, `[Auto] [Clear override]` on the second
- **Approval mode** (Gemini) — three options (read-only / edit files / full access)
- **Reasoning** — engine-specific levels: Claude Code (low / medium / high / max), Codex (minimal / low / medium / high / xhigh)

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
| Reasoning | Claude: low, medium, high, max; Codex: minimal, low, medium, high, xhigh | Yes (chat prefs) |
| Cost & usage | API cost, subscription usage, budget, auto-cancel | Yes (chat prefs) |
| Resume line | off, on | Yes (chat prefs) |
| Trigger | all, mentions | Yes (chat prefs) |
| Budget enabled | off, on | Yes (chat prefs) |
| Budget auto-cancel | off, on | Yes (chat prefs) |

Approval policy appears instead of Plan mode when the engine is Codex CLI. Approval mode appears instead of Plan mode when the engine is Gemini CLI.

### Cost & Usage page

The Cost & Usage sub-page merges cost display and budget controls into a unified page with toggle rows:

- **API cost** — per-run cost in the message footer (requires engine cost reporting)
- **Subscription usage** — 5h/weekly subscription usage in the footer (Claude Code only)
- **Budget enabled** — turn budget tracking on or off for this chat (overrides global `[cost_budget]` setting)
- **Budget auto-cancel** — enable or disable automatic run cancellation when a budget is exceeded

Each toggle uses the `[✓ Feature: on] [Clear]` pattern. Clear removes the per-chat override and falls back to the global config.

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
- [Group chat](group-chat.md) — trigger mode in groups
