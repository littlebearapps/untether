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
Trigger: all

[ Plan mode ] [ Verbose ]
[  Engine   ] [ Trigger ]
```

## Navigate sub-pages

Tap any button to open that setting's page. Each sub-page shows:

- A description of the setting
- The current value
- Buttons to change the value (active option marked with a checkmark)
- A **Clear override** button to revert to the default
- A **Back** button to return to the home page

## Available settings

| Setting | Options | Persisted |
|---------|---------|-----------|
| Plan mode | off, on, auto | Yes (chat prefs) |
| Verbose | off, on | No (in-memory, resets on restart) |
| Engine | any configured engine | Yes (chat prefs) |
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
