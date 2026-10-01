# OpenCode Runner

This runner integrates with the [OpenCode CLI](https://github.com/sst/opencode).
Shipped in Untether v0.5.0.

## Installation

```bash
npm i -g opencode-ai@latest
```

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

### No auto-compaction

OpenCode does not support automatic context compaction. Unlike Pi (which emits `AutoCompactionStart`/`AutoCompactionEnd` events to trim context) and Claude Code (which manages its context window internally), OpenCode sessions accumulate unbounded context across turns.

**Impact:** Long sessions with many prompts will progressively slow down as the full conversation history is sent to the model on every turn. A session that starts at 72k tokens can grow past 77k+ after just 4-5 prompts.

**Workaround:** Start a fresh session with `/new` when response times degrade noticeably.

If OpenCode adds compaction events in the future, Untether will need schema and runner updates following the Pi compaction pattern.

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
- [Env for Codex and OpenCode](../../env-vars.md#env-codex-opencode) — what OpenCode and its MCP servers inherit, and the `set -u` wrapper pattern
