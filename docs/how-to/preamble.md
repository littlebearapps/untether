# Agent preamble

Untether injects a context preamble at the start of every agent prompt, telling the engine it's running via Telegram and requesting structured end-of-task summaries. This works across all engines (Claude Code, Codex, OpenCode, Pi, Antigravity CLI, Amp).

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

## Hook awareness

Claude Code plugins can install hooks that fire at session end (Stop hooks). In a terminal, these are harmless — the user can scroll up. But in Telegram, only the final assistant message is visible. If a Stop hook causes Claude to spend its final response addressing hook concerns, the user's actual content is lost.

Untether addresses this in two ways:

1. **Preamble guidance** — the default preamble includes: "If hooks fire at session end, your final response MUST still contain the user's requested content. Hook concerns are secondary."

2. **`UNTETHER_SESSION` env var** — the Claude runner sets `UNTETHER_SESSION=1` in the subprocess environment. Well-behaved Claude Code plugins check for this variable and skip blocking hooks in Telegram sessions. For example, [PitchDocs](https://github.com/littlebearapps/lba-plugins) context-guard detects `$UNTETHER_SESSION` and allows the session to stop without blocking.

!!! tip "For plugin authors"
    If your Claude Code plugin includes Stop hooks, add an early exit for Untether sessions:

    ```bash
    # Skip blocking in Untether sessions — Stop hook blocks displace
    # user-requested content in the Telegram final message.
    [ -n "${UNTETHER_SESSION:-}" ] && echo '{}' && exit 0
    ```

    This is not a security bypass — it simply prevents your hook from interfering with Telegram's single-message output model. See the [interference audit](../audits/pitchdocs-context-guard-interference.md) for a detailed case study.

## Related

- [Configuration reference](../reference/config.md) — full `[preamble]` config
- [Inline settings](inline-settings.md) — `/config` toggles including ask mode
- [Environment variables](../reference/env-vars.md) — `UNTETHER_SESSION` and other env vars
