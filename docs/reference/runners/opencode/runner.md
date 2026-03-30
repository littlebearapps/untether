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
    untether config set opencode.model "claude-sonnet"
    ```

=== "toml"

    ```toml
    [opencode]
    model = "claude-sonnet"  # optional
    ```

## Usage

```bash
untether opencode
```

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
| `step_finish` | End of a step (reason: "stop" or "tool-calls" when present) |
| `error` | Error event |

See [stream-json-cheatsheet.md](./stream-json-cheatsheet.md) for detailed event format documentation.

## Known Limitations

### No auto-compaction

OpenCode does not support automatic context compaction. Unlike Pi (which emits `AutoCompactionStart`/`AutoCompactionEnd` events to trim context) and Claude Code (which manages its context window internally), OpenCode sessions accumulate unbounded context across turns.

**Impact:** Long sessions with many prompts will progressively slow down as the full conversation history is sent to the model on every turn. A session that starts at 72k tokens can grow past 77k+ after just 4-5 prompts.

**Workaround:** Start a fresh session with `/new` when response times degrade noticeably.

If OpenCode adds compaction events in the future, Untether will need schema and runner updates following the Pi compaction pattern.

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
