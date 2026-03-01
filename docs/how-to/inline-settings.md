# Inline settings menu

The `/config` command opens an interactive settings menu with inline keyboard buttons — similar to BotFather's settings style. Tap buttons to navigate sub-pages, toggle settings, and return to the overview, all within a single message that edits in place.

## Open the menu

Send `/config` in any chat:

```
/config
```

The home page shows current values for all settings:

```
Settings

Plan mode: default
Verbose: default
Engine: claude (global)
Model: default
Trigger: all

[ Plan mode ] [ Verbose ]
[  Engine   ] [  Model  ]
[  Trigger  ]
```

## Navigate sub-pages

Tap any button to open that setting's page. Each sub-page shows:

- A description of the setting
- The current value
- Buttons to change the value (active option marked with a checkmark)
- A **Clear override** button to revert to the default
- A **Back** button to return to the home page

## Toggle behaviour

When you tap a setting button:

1. **Confirmation toast** — a brief popup appears confirming the change (e.g. "Plan mode: off", "Verbose: on"). This uses the same toast mechanism as Claude approval buttons.
2. **Auto-return** — the menu automatically navigates back to the home page, showing the updated value across all settings. No need to tap "Back" manually.

### Engine-aware visibility

Some settings are engine-specific and only appear when relevant:

- **Plan mode** — only available for Claude Code. Hidden for other engines; the sub-page shows "Only available for Claude Code" with a Back button.
- **Reasoning** — only available for engines that support reasoning levels (currently Codex). Hidden for Claude, OpenCode, and Pi.
- **Model** — always visible. Shows the current model override and lets you clear it. To set a model, use `/model set <name>`.

When you switch engines via the Engine sub-page, the home page automatically shows or hides the relevant settings.

## Available settings

| Setting | Options | Persisted |
|---------|---------|-----------|
| Plan mode | off, on, auto | Yes (chat prefs) |
| Verbose | off, on | No (in-memory, resets on restart) |
| Engine | any configured engine | Yes (chat prefs) |
| Model | view + clear (set via `/model set`) | Yes (chat prefs) |
| Reasoning | minimal, low, medium, high, xhigh | Yes (chat prefs) |
| Trigger | all, mentions | Yes (chat prefs) |

## Callbacks vs commands

- **Text command** (`/config`): sends a new message with the menu.
- **Button tap**: edits the existing message in place — no message spam.

All button interactions use early callback answering for instant feedback.

## Related

- [Plan mode](plan-mode.md) — detailed plan mode documentation
- [Verbose progress](verbose-progress.md) — verbose mode details and global config
- [Switch engines](switch-engines.md) — engine selection
- [Group chat](group-chat.md) — trigger mode in groups
