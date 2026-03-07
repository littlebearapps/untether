# Agent preamble

Untether injects a context preamble at the start of every agent prompt, telling the engine it's running via Telegram and requesting structured end-of-task summaries. This works across all engines (Claude Code, Codex, OpenCode, Pi, Gemini CLI, Amp).

## What the default preamble does

The built-in preamble tells the agent:

1. **Context** — it's running via Untether on Telegram, and the user is on a mobile device
2. **Visibility constraints** — only final assistant text is visible; tool calls, thinking blocks, and terminal output are invisible to the user
3. **Summary format** — every response that completes work should end with a structured summary including "Completed", "Next Steps", and "Decisions Needed" sections

This means agents naturally produce mobile-friendly summaries instead of expecting the user to read terminal output or file diffs.

## Disable the preamble

If you don't want Untether to inject any preamble:

=== "toml"

    ```toml
    [preamble]
    enabled = false
    ```

## Customise the preamble

Replace the default text with your own:

=== "toml"

    ```toml
    [preamble]
    enabled = true
    text = "You are running via Telegram. Keep responses concise and use bullet points."
    ```

Set `text` to your custom string. When `text` is `null` (the default), Untether uses the built-in preamble. Setting `text` to an empty string (`""`) effectively disables the preamble while keeping the `enabled` flag on.

## Configuration reference

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `enabled` | bool | `true` | Inject preamble into prompts |
| `text` | string or null | `null` | Custom preamble text; `null` uses the built-in default |

## Ask mode interaction

When ask mode is enabled (via `/config`), Untether appends a line to the preamble encouraging the agent to use `AskUserQuestion` with structured options. When ask mode is disabled, it appends a line discouraging interactive questions so the agent proceeds with defaults instead.

## Related

- [Configuration reference](../reference/config.md) — full `[preamble]` config
- [Inline settings](inline-settings.md) — `/config` toggles including ask mode
