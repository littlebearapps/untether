# Inline settings menu

Adjust Untether's behaviour without editing config files or restarting — tap buttons right in Telegram. The `/config` command opens an interactive settings menu with inline keyboard buttons, similar to BotFather's settings style. Navigate sub-pages, toggle settings, and return to the overview, all within a single message that edits in place.

## Open the menu

Send `/config` in any chat:

```
/config
```

The home page shows current values for all settings:

```
Settings

Plan mode: default
Ask mode: default
Verbose: default
Engine: claude (global)
Model: default
Trigger: all

[ Plan mode ] [ Ask mode ]
[  Verbose  ] [  Model   ]
[  Engine   ] [ Trigger  ]
```

!!! note "Gemini CLI"
    When the engine is Gemini CLI, the home page shows **Approval mode** (read-only / full access) instead of Plan mode, Ask mode, and Diff preview.

## Navigate sub-pages

Tap any button to open that setting's page. Each sub-page shows:

- A description of the setting
- The current value
- Buttons to change the value (active option marked with a checkmark)
- A **Clear override** button to revert to the default
- A **Back** button to return to the home page

## Toggle behaviour

When you tap a setting button:

1. **Confirmation toast** — a brief popup appears confirming the change (e.g. "Plan mode: off", "Verbose: on"). This uses the same toast mechanism as Claude Code approval buttons.
2. **Auto-return** — the menu automatically navigates back to the home page, showing the updated value across all settings. No need to tap "Back" manually.

### Engine-aware visibility

Some settings are engine-specific and only appear when relevant:

- **Plan mode** — available for Claude Code. Hidden for other engines; the sub-page shows a "not available" message with a Back button.
- **Approval mode** — only available for Gemini CLI. Toggle between "read-only" (default, write tools blocked) and "full access" (all tools approved). This replaces the Plan mode button on the home page when the engine is Gemini.
- **Ask mode** — only available for Claude Code. When enabled, Claude Code can ask interactive questions with option buttons instead of guessing. Hidden for other engines.
- **Reasoning** — only available for engines that support reasoning levels (Claude Code and Codex). Hidden for OpenCode, Pi, and others.
- **Model** — always visible. Shows the current model override and lets you clear it. To set a model, use `/model set <name>`.

When you switch engines via the Engine sub-page, the home page automatically shows or hides the relevant settings.

## Available settings

| Setting | Options | Persisted |
|---------|---------|-----------|
| Plan mode | off, on, auto | Yes (chat prefs) |
| Approval mode | read-only, full access | Yes (chat prefs) |
| Ask mode | off, on | Yes (chat prefs) |
| Verbose | off, on | No (in-memory, resets on restart) |
| Diff preview | off, on | Yes (chat prefs) |
| Engine | any configured engine | Yes (chat prefs) |
| Model | view + clear (set via `/model set`) | Yes (chat prefs) |
| Reasoning | minimal, low, medium, high, xhigh | Yes (chat prefs) |
| Cost & usage | API cost on/off, subscription usage on/off | Yes (chat prefs) |
| Trigger | all, mentions | Yes (chat prefs) |

Approval mode appears instead of Plan mode when the engine is Gemini CLI.

### Cost & Usage page

The Cost & Usage sub-page (added in v0.31.0) merges the previous separate API cost and subscription usage toggles into a unified page. Toggle whether completed messages show:

- **API cost** — per-run cost in the message footer (requires engine cost reporting)
- **Subscription usage** — 5h/weekly subscription usage in the footer (Claude Code only)

For historical cost data across sessions, use the [`/stats`](../reference/commands-and-directives.md) command.

## Callbacks vs commands

- **Text command** (`/config`): sends a new message with the menu.
- **Button tap**: edits the existing message in place — no message spam.

All button interactions use early callback answering for instant feedback.

## Related

- [Plan mode](plan-mode.md) — detailed plan mode documentation
- [Verbose progress](verbose-progress.md) — verbose mode details and global config
- [Switch engines](switch-engines.md) — engine selection
- [Group chat](group-chat.md) — trigger mode in groups
