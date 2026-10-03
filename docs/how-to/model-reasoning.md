# Model and reasoning overrides

Untether lets you override which model the agent uses and its reasoning level, per chat or per engine — all from [Telegram](https://telegram.org), without editing config files or restarting.

## Check current model

Send `/model` to see the override for the current engine and where it comes from:

```
/model
```

!!! untether "Untether"
    engine: claude (chat default)

    model: **sonnet** (chat default)

    defaults: topic: none, chat: sonnet

    available engines: codex, claude, opencode, pi

`model: **default** (no override)` means no chat or topic override is set, so the engine uses its own default (see the resolution order below).

## Set a model override

Use `/model set` to override the model for the current engine:

```
/model set sonnet
```

To target a specific engine, include the engine name:

```
/model set claude opus
```

The override applies to the current chat (or topic, if you're in a forum thread).

!!! note "OpenCode: use provider/model format"
    OpenCode requires the `provider/model` format for model overrides (e.g. `openai/gpt-5.5`, `anthropic/claude-sonnet-5-5`). Using just the model name will fail. Example: `/model set opencode openai/gpt-5.5`.

## Clear model override

Remove the override to revert to the default:

```
/model clear
```

To clear the override for a specific engine:

```
/model clear claude
```

## Set reasoning level

Some engines support reasoning levels that control how much thinking the model does before responding. Use `/reasoning set`:

```
/reasoning set high
```

Valid levels depend on the engine:

- **Claude Code**: `low`, `medium`, `high`, `xhigh`, `max` (passed as `--effort`)
- **Codex CLI**: `low`, `medium`, `high`, `xhigh`

`minimal` was removed for Codex in 0.35.5: no current Codex model supports it, and it fails alongside Codex's default web search. A saved `minimal` is ignored with a one-line note on each run, and the run uses the engine default until you pick another level.

Other engines (OpenCode, Pi, Gemini, Amp) ignore this setting.

## Per-engine reasoning

Target a specific engine with the engine name:

```
/reasoning set claude high
```

## Clear reasoning

Remove the reasoning override:

```
/reasoning clear
```

Or for a specific engine:

```
/reasoning clear claude
```

## View full resolution

Use `/agent` to see how all configuration layers resolve for the current scope:

```
/agent
```

The resolution order for model and reasoning is (highest priority first):

1. **Cron override** — a `[[triggers.crons]]` entry's own `model` / `reasoning`, for that scheduled run only ([#743](https://github.com/littlebearapps/untether/issues/743); see [Schedule tasks](schedule-tasks.md#pick-a-model-per-cron))
2. **Topic override** — set via `/model set` or `/reasoning set` in a forum topic
3. **Chat default** — set via `/model set` or `/reasoning set` in a private or group chat
4. **Engine config** — `model` under `[engines.claude]`, `[engines.opencode]` or `[engines.pi]` in `untether.toml` (Codex takes its model from its own `~/.codex/config.toml` or `profile`; there is no `untether.toml` default for reasoning)
5. **CLI default** — whatever the engine CLI picks on its own

There is no per-project model setting; projects only pick a `default_engine`.

!!! tip "Quick check"
    `/agent` shows the effective engine, model, and reasoning for the current context, including which layer each setting comes from.

## Admin-only in groups

In group chats, model and reasoning changes require **admin** or **creator** status. This prevents non-admin members from switching to expensive models or changing settings that affect everyone in the group.

## Related

- [Switch engines](switch-engines.md) — change which engine handles messages
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Configuration](../reference/config.md) — config reference for model and reasoning settings
