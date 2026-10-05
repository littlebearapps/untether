# OpenCode Runner

This runner integrates with the [OpenCode CLI](https://github.com/anomalyco/opencode).
Shipped in Untether v0.5.0.

## Installation

```bash
npm i -g opencode-ai@1
```

### Supported versions

Untether supports the **OpenCode 1.x CLI** (npm package `opencode-ai`;
tested on 1.14–1.18). **OpenCode 2.x** (the separate npm package
`@opencode/cli`, which also installs a binary called `opencode`) is **not
supported yet**: its `run` command sends prompts to a shared per-user
background service unless `--standalone` is passed, which would put the agent
outside Untether's process control (environment, working directory,
`/cancel`, stall watchdog), and its `run --format json` output is unverified.

Before each run, Untether checks `opencode --version` (once per installed
binary; an upgrade re-checks). On 2.x it refuses the run with
`🛑 OpenCode 2.x.y isn't supported yet…` and spawns nothing; the chat's saved
session is kept. To go back to 1.x:

```bash
npm uninstall -g @opencode/cli && npm install -g opencode-ai@1
```

If the version can't be read (the probe fails or prints something unexpected),
the run goes ahead and Untether logs `opencode.version.unknown`.

## Configuration

Add to your `untether.toml`:

=== "untether config"

    ```sh
    untether config set opencode.model "anthropic/claude-sonnet-5-5"
    ```

=== "toml"

    ```toml
    [opencode]
    model = "anthropic/claude-sonnet-5-5"  # optional; passed as --model
    ```

Model IDs **must** use OpenCode's `provider/model` format (e.g. `openai/gpt-5.5`,
`anthropic/claude-sonnet-5-5`); a bare model name fails. The same applies to a
`/config` or `/model` override. When `[opencode] model` is unset, Untether reads
the top-level `"model"` from `~/.config/opencode/opencode.json` and passes that
instead (it also labels the `🏷` footer).

`[opencode]` has no `extra_args` or permission-mode setting: OpenCode runs
non-interactively, and approvals, plan mode, AskUserQuestion, live sessions and
steer are Claude Code-only.

## Permissions in `opencode run`

`opencode run` has no way to ask you anything, so it decides for you:

- **`ask` permissions are auto-rejected, not auto-approved.** A tool call that
  your OpenCode permission rules resolve to `ask` fails, and the model sees the
  rejection. Only `opencode run --dangerously-skip-permissions` approves them,
  and Untether never passes that flag.
- **The `question`, `plan_enter` and `plan_exit` tools are denied** in the
  sessions `run` creates, so OpenCode never asks questions or switches plan
  mode under Untether.
- OpenCode's default policy allows almost everything, so `ask` mostly comes up
  for rules you configured yourself. The built-in exceptions are access outside
  the project directory, `.env` reads and, on v1, `doom_loop`.

If a tool fails under Untether but works in the OpenCode TUI, look for an `ask`
rule in your OpenCode config: change it to `allow` for the tools you want
OpenCode to run unattended.

## Usage

```bash
untether opencode
```

## Invocation

```text
opencode run --format json [--session <ses_id> | --continue] [--model <provider/model>] -- <prompt>
```

The prompt goes after `--`, so a prompt starting with `-` is never read as a flag.
`--continue` is used for `/continue`; a reply to a message with a resume line uses
`--session`.

## Resume Format

Resume line format: `` `opencode --session ses_XXX` ``

The runner recognizes both `--session` and `-s` flags (with or without `run`).

Note: The resume line is meant to reopen the interactive TUI session. `opencode run` is headless and requires a message or command, so it is not the canonical resume command.

## JSON Event Format

OpenCode outputs JSON events with the following types:

| Event Type | Description |
|------------|-------------|
| `step_start` | Beginning of a processing step |
| `tool_use` | Tool invocation with input/output |
| `text` | Text output from the model |
| `step_finish` | End of a step (reason: "stop" or "tool-calls" when present; carries cost and tokens) |
| `error` | Error event |

Any other `type` is reported as an `opencode emitted unsupported event: <type>` warning rather than dropped silently.

See [stream-json-cheatsheet.md](./stream-json-cheatsheet.md) for detailed event format documentation.

## Known Limitations

### Compaction isn't shown

OpenCode compacts long sessions itself: v1 auto-compacts (the `compaction.auto` setting) and emits `session.compacted` on its server event bus, and v2 has [checkpoint compaction](https://opencode.ai/v2/docs/compaction). But `opencode run --format json` only forwards `step_start`, `tool_use`, `text`, `step_finish` and `error`, so Untether never sees a compaction. There is no `🗜️` row for OpenCode as there is for Claude Code and Pi.

**Impact:** a long session still grows turn by turn until OpenCode compacts it, and each turn resends the history, so responses can slow down (a session that starts at 72k tokens can pass 77k after 4–5 prompts). Per-run token counts drop after a compaction with no explanation in the chat.

**Workaround:** start a fresh session with `/new` when response times degrade noticeably.

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
- [Env for Codex and OpenCode](../../env-vars.md#env-codex-opencode) — what OpenCode and its MCP servers inherit, and the `set -u` wrapper pattern
