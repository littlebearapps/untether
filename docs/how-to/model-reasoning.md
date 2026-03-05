# Model and reasoning overrides

Untether lets you override which model the agent uses and its reasoning level, per chat or per engine — all from [Telegram](https://telegram.org), without editing config files or restarting.

## Check current model

Send `/model` to see what model is active and where the setting comes from:

```
/model
```

!!! untether "Untether"
    **Model:** claude-opus-4-6
    **Source:** global default

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

Valid levels are `low`, `medium`, and `high` — availability depends on the engine and model.

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

The resolution order is (highest priority first):

1. **Topic override** — set via `/model set` in a forum topic
2. **Chat default** — set via `/model set` in a private or group chat
3. **Project default** — configured in `projects.<alias>.default_model`
4. **Global default** — configured at the top level of your config

!!! tip "Quick check"
    `/agent` shows the effective engine, model, and reasoning for the current context, including which layer each setting comes from.

## Admin-only in groups

In group chats, model and reasoning changes require **admin** or **creator** status. This prevents non-admin members from switching to expensive models or changing settings that affect everyone in the group.

## Related

- [Switch engines](switch-engines.md) — change which engine handles messages
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Configuration](../reference/config.md) — config reference for model and reasoning settings
