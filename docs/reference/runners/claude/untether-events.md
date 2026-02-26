# Claude Code -> Untether event mapping (spec)

This document describes how the Claude Code runner translates Claude CLI JSONL events into Untether events.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/claude.py` and the translation logic is in `src/untether/runners/claude.py`. When in doubt, refer to the code.

The goal is to make a Claude runner feel identical to the Codex runner from the bridge/renderer point of view while preserving Untether invariants (stable action ids, per-session serialization, single completed event).

---

## 1. Input stream contract (Claude CLI)

Claude Code CLI emits **one JSON object per line** (JSONL) when invoked with
`--output-format stream-json`.

Non-interactive invocation:

```
claude -p --output-format stream-json --input-format stream-json --verbose -- <query>
```

Permission mode invocation (bidirectional control channel):

```
claude --output-format stream-json --input-format stream-json --verbose --permission-mode plan --permission-prompt-tool stdio
```

Notes:
- `--verbose` is required for `stream-json` output (CLI may otherwise drop events).
- `--input-format stream-json` enables JSON input on stdin.
- In `-p` mode, the prompt is passed as a positional argument after `--`.
- In permission mode, the prompt is sent via stdin as a JSON user message (no `-p`).
- Resuming uses `--resume <session_id>`.
- `-- <query>` safely passes prompts that start with `-`.

---

## 2. Resume tokens and resume lines

- Engine id: `claude`
- Canonical resume line (embedded in chat):

```
`claude --resume <session_id>`
```

Runner must implement its own regex because the resume format is
`claude --resume <session_id>`. Suggested regex:

```
(?im)^\s*`?claude\s+(?:--resume|-r)\s+(?P<token>[^`\s]+)`?\s*$
```

**Note:** Claude session IDs should be treated as opaque strings.

Resume rules:
- If a resume token is provided to `run()`, the runner MUST verify that any
  `session_id` observed in the stream matches it.
- If the stream yields a different `session_id`, emit a fatal error and end the run.

---

## 3. Session lifecycle + serialization

Untether requires **serialization per session id**:

- For new runs (`resume=None`), do **not** acquire a lock until a `session_id`
  is observed (usually the first `system.init` event).
- Once the session id is known, acquire a lock for `claude:<session_id>` and hold
  it until the run completes.
- For resumed runs, acquire the lock immediately on entry.

This matches the Codex runner behavior in `untether/runners/codex.py`.

---

## 4. Event translation (Claude JSONL -> Untether)

### 4.1 Top-level `system` events

Claude emits a system init event early in the stream:

```
{"type":"system","subtype":"init","session_id":"...", ...}
```

**Mapping:**
- Emit a Untether `started` event as soon as `session_id` is known.
- Populate `meta` from `system.init` fields: `cwd`, `model`, `tools`, `permissionMode`, `output_style`. The `model` and `permissionMode` fields are used by the bridge to render the `🏷` footer line on final messages.
- Assume only one `system.init` per run; if more appear, ignore the subsequent
  ones to avoid re-locking.
- Optional: emit a `note` action summarizing tools/MCP servers (debug-only).

### 4.2 `assistant` / `user` message events

Claude messages include a `message` object with a `content[]` array. Each content
block can represent text, tool usage, or tool results.

For each content block:

#### A) `type = "tool_use"`
**Mapping:** emit `action` with `phase="started"`.

- `action.id` = `content.id`
- `action.kind` = map from tool name (see section 5)
- `title`:
  - if kind=`command`: use `input.command` if present
  - else: tool name or derived label
- `detail` should include:
  - `tool_name`, `tool_input`, `message_id`, `parent_tool_use_id` (if provided)

#### B) `type = "tool_result"`
**Mapping:** emit `action` with `phase="completed"`.

- `action.id` = `content.tool_use_id`
- `ok`:
  - if `content.is_error` exists and is true -> `ok=False`
  - else `ok=True`
- `detail` should include:
  - `tool_use_id`, `content` (raw), `message_id`

The runner SHOULD keep a small in-memory map from `tool_use_id -> tool_name`
(learned from `tool_use`) so the completed action title can match the started
action title.

#### C) `type = "text"`
**Mapping:**
- Default: do **not** emit an action (avoid duplicate rendering).
- Store the latest assistant text as a fallback final answer if `result.result`
  is empty or missing.

#### D) `type = "thinking"` or other unknown types
**Mapping:** optional `note` action (phase completed) with title derived from
content; otherwise ignore.

### 4.3 `result` events

The terminal event looks like:

```
{"type":"result","subtype":"success", ...}
```

**Mapping:** emit a single Untether `completed` event:

- `ok = !event.is_error`
- `answer = event.result` (fallback to last assistant text if empty)
- `error = event.error` (if present)
- `resume = ResumeToken(engine="claude", value=event.session_id)`
- `usage = event.usage` (pass through)
- Emit exactly one `completed` event; ignore any trailing JSON lines afterward.
  No idle-timeout completion is used.

#### Permission denials

> **Not yet implemented.** The upstream Claude CLI may include
> `result.permission_denials` with blocked tool calls, but Untether's
> `StreamResultMessage` schema does not capture this field and the runner does
> not emit warning actions for denials. This is a candidate for future work.

### 4.4 Error handling / malformed lines

- If a JSONL line is invalid JSON: emit a warning action and continue.
- If the subprocess exits non-zero or the stream ends without a `result` event:
  emit `completed` with `ok=False` and `error` explaining the failure.
- Emit **exactly one** `completed` event per run.

---

## 5. Tool name -> ActionKind mapping heuristics

Claude tool names can evolve. The runner SHOULD map based on tool name and input
shape. Suggested rules:

| Tool name pattern | ActionKind | Title logic |
| --- | --- | --- |
| `Bash`, `Shell` | `command` | `input.command` |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `file_change` | `input.path` |
| `Read` | `tool` | `Read <path>` |
| `WebSearch` | `web_search` | `input.query` |
| (default) | `tool` | tool name |

For `file_change`, emit `detail.changes = [{"path": <path>, "kind": "update"}]`.
If input indicates creation (ex: `create: true`), use `kind: "add"`.

If a tool name is unknown, map to `tool` and include the full input in `detail`.

---

## 6. Usage mapping

Untether `completed.usage` should mirror the Claude `result.usage` object
without transformation. Optionally include `modelUsage` inside `usage` or
`detail` if downstream consumers want it (currently unused by renderers).

---

## 7. Implementation checklist (v0.3.0)

Claude runner implementation summary (no Untether domain model changes):

1. [x] Create `untether/runners/claude.py` implementing `Runner` and (custom)
   resume parsing.
2. [x] Define `BACKEND` in `untether/runners/claude.py`:
   - `install_cmd`: install command for the `claude` binary
   - `build_runner`: read `[claude]` config + construct runner
3. [x] Add new docs (this file + `stream-json-cheatsheet.md`).
4. [x] Add fixtures in `tests/fixtures/` (see below).
5. [x] Add unit tests mirroring `tests/test_codex_*` but for Claude translation
   and resume parsing (recommended, not required for initial handoff).

---

## 8. Suggested Untether config keys

A minimal TOML config for Claude:

=== "untether config"

    ```sh
    untether config set claude.model "sonnet"
    untether config set claude.allowed_tools '["Bash", "Read", "Edit", "Write", "WebSearch"]'
    untether config set claude.dangerously_skip_permissions false
    untether config set claude.use_api_billing false
    ```

=== "toml"

    ```toml
    [claude]
    # model: opus | sonnet | haiku
    model = "sonnet"

    allowed_tools = ["Bash", "Read", "Edit", "Write", "WebSearch"]
    dangerously_skip_permissions = false
    use_api_billing = false
    ```

Untether only maps these keys to Claude CLI flags; other options should be configured in Claude Code settings.
If `allowed_tools` is omitted, Untether defaults to `["Bash", "Read", "Edit", "Write"]`.
When `use_api_billing` is false (default), Untether strips `ANTHROPIC_API_KEY` from the Claude subprocess environment to prefer subscription billing.
