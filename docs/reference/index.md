# Reference

Reference docs are **authoritative and exact**. Use these when you need stable facts, schemas, and contracts.

If you’re trying to achieve a goal (“enable topics”, “fetch a file”), use **[How-to](../how-to/index.md)**.  
If you’re trying to understand the *why*, use **[Explanation](../explanation/index.md)**.

## Most-used reference pages

- [Commands & directives](commands-and-directives.md)
  - Message prefixes like `/<engine-id>`, `/<project-alias>`, and `@branch`
  - In-chat commands like `/cancel`, `/new`, `/ctx`, `/file …`, `/topic …`
- [Configuration](config.md)
  - `untether.toml` options and defaults
  - Telegram transport options (sessions, topics, files, voice transcription)
- [Workflow modes](modes.md)
  - Assistant, workspace, and handoff — what each mode configures and when to use it
- [Environment variables](env-vars.md)
  - Variables Untether reads, and the engine-subprocess environment allowlist
- [Error reference](errors.md)
  - Engine error patterns and the recovery hint Untether shows for each

## Normative behaviour

- [Specification](specification.md)  
  The normative (“MUST/SHOULD/MAY”) contract for:
  - resume tokens + resume lines
  - event model
  - progress/final message semantics
  - per-thread serialisation rules

## Plugins and extension contracts

- [Plugin API](plugin-api.md)  
  The **only** supported import surface for plugins: `untether.api`
- [Context resolution](context-resolution.md)  
  How Untether resolves project + worktree context from directives, replies, and chat ids.

## Transport reference

- [Telegram transport](transports/telegram.md)
  Rate limits, outbox behaviour, retries, message editing rules.

## Trigger reference

- [Triggers](triggers/triggers.md)
  Webhook and cron trigger system: config, auth, templating, routing.

## Runner reference

These are “engine adapter” implementation details: JSONL formats, mapping rules, and emitted events.

- [Runners overview](runners/index.md)
- Claude Code:
  - [runner.md](runners/claude/runner.md)
  - [stream-json-cheatsheet.md](runners/claude/stream-json-cheatsheet.md)
  - [untether-events.md](runners/claude/untether-events.md)
- Codex:
  - [exec-json-cheatsheet.md](runners/codex/exec-json-cheatsheet.md)
  - [untether-events.md](runners/codex/untether-events.md)
- OpenCode:
  - [runner.md](runners/opencode/runner.md)
  - [stream-json-cheatsheet.md](runners/opencode/stream-json-cheatsheet.md)
  - [untether-events.md](runners/opencode/untether-events.md)
- Pi:
  - [runner.md](runners/pi/runner.md)
  - [stream-json-cheatsheet.md](runners/pi/stream-json-cheatsheet.md)
  - [untether-events.md](runners/pi/untether-events.md)
- Gemini CLI (deprecated — removal targeted for 0.36.0):
  - [runner.md](runners/gemini/runner.md)
  - [stream-json-cheatsheet.md](runners/gemini/stream-json-cheatsheet.md)
  - [untether-events.md](runners/gemini/untether-events.md)
- AMP (deprecated — removal targeted for 0.36.0):
  - [runner.md](runners/amp/runner.md)
  - [stream-json-cheatsheet.md](runners/amp/stream-json-cheatsheet.md)
  - [untether-events.md](runners/amp/untether-events.md)

## Quick lookup

- [Glossary](glossary.md)
  Definitions for key terms: engine, runner, directive, resume token, worktree, permission mode, and more.

## For LLM agents

If you’re an LLM agent contributing to Untether, start here:

- [Agent entrypoint](agents/index.md)
- [Repo map](agents/repo-map.md)
- [Invariants](agents/invariants.md) (runner contract, resume handling, “don’t break this” rules)
- [Feature catalogue](feature-catalog.md) and [test catalogue](test-catalog.md) (which file owns a feature; what each test file covers)
- [Dev instance](dev-instance.md) and [integration testing](integration-testing.md) (testing against the dev bot before a release)
