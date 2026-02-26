Below is a concrete implementation spec for the **Anthropic Claude Code (“claude” CLI / Agent SDK runtime)** runner shipped in Untether (v0.3.0).

---

## Scope

### Goal

Provide the **`claude`** engine backend so Untether can:

* Run Claude Code non-interactively via the **Agent SDK CLI** (`claude -p`). ([Claude Code][1])
* Run Claude Code interactively via permission mode (`--permission-mode plan --permission-prompt-tool stdio`) with a bidirectional control channel.
* Stream progress in Telegram by parsing **`--output-format stream-json --input-format stream-json --verbose`** (newline-delimited JSON). ([Claude Code][1])
* Support resumable sessions via **`--resume <session_id>`** (Untether emits a canonical resume line the user can reply with). ([Claude Code][1])

---

## UX and behavior

### Engine selection

* Default: `untether` (auto-router uses `default_engine` from config)
* Override: `untether claude`

Untether runs in auto-router mode by default; `untether claude` or `/claude` selects
Claude for new threads.

### Resume UX (canonical line)

Untether appends a **single backticked** resume line at the end of the message, like:

```text
`claude --resume 8b2d2b30-...`
```

Rationale:

* Claude Code supports resuming a specific conversation by session ID with `--resume`. ([Claude Code][1])
* The CLI reference also documents `--resume/-r` as the resume mechanism.

Untether should parse either:

* `claude --resume <id>`
* `claude -r <id>` (short form from docs)

**Note:** Claude session IDs should be treated as **opaque strings**. Do not assume UUID format.

### Permissions

Untether supports two modes:

**Non-interactive (`-p` mode):** Claude Code can require tool approvals but Untether cannot answer interactive prompts. Users must preconfigure permissions via `--allowedTools` or Claude Code settings. ([Claude Code][2])

**Interactive (permission mode):** When `permission_mode` is set (e.g. `plan` or `auto`), Untether uses `--permission-mode <mode> --permission-prompt-tool stdio` to establish a bidirectional control channel over stdin/stdout. Claude emits `control_request` events for tool approvals and plan mode exits; Untether responds with `control_response` (approve/deny with optional `denial_message`). This uses a PTY (`pty.openpty()`) to prevent stdin deadlock.

Key control channel features:
* Session registries (`_SESSION_STDIN`, `_REQUEST_TO_SESSION`) for concurrent session support
* Auto-approve for routine tools (Grep, Glob, Read, Bash, etc.)
* `ExitPlanMode` requests shown as Telegram inline buttons (Approve / Deny / Pause & Outline Plan) in `plan` mode
* `ExitPlanMode` requests silently auto-approved in `auto` mode (no buttons shown)
* Progressive cooldown on rapid ExitPlanMode retries (30s → 60s → 90s → 120s) — only applies in `plan` mode

**Safety note:** `-p/--print` skips the workspace trust dialog; only use this flag in trusted directories.

---

## Config additions

Untether config lives at `~/.untether/untether.toml`.

Add a new optional `[claude]` section.

Recommended v1 schema:

=== "untether config"

    ```sh
    untether config set default_engine "claude"
    untether config set claude.model "claude-sonnet-4-5-20250929"
    untether config set claude.allowed_tools '["Bash", "Read", "Edit", "Write"]'
    untether config set claude.dangerously_skip_permissions false
    untether config set claude.use_api_billing false
    ```

=== "toml"

    ```toml
    # ~/.untether/untether.toml

    default_engine = "claude"

    [claude]
    model = "claude-sonnet-4-5-20250929" # optional (Claude Code supports model override in settings too)
    permission_mode = "auto"             # optional: "plan", "auto", or "acceptEdits"
    allowed_tools = ["Bash", "Read", "Edit", "Write"] # optional but strongly recommended for automation
    dangerously_skip_permissions = false # optional (high risk; prefer sandbox use only)
    use_api_billing = false             # optional (keep ANTHROPIC_API_KEY for API billing)
    ```

Notes:

* `--allowedTools` exists specifically to auto-approve tools in programmatic runs. ([Claude Code][1])
* Claude Code tools (Bash/Edit/Write/WebSearch/etc.) and whether permission is required are documented. ([Claude Code][2])
* If `allowed_tools` is omitted, Untether defaults to `["Bash", "Read", "Edit", "Write"]`.
* Untether reads `model`, `permission_mode`, `allowed_tools`, `dangerously_skip_permissions`, and `use_api_billing` from `[claude]`.
* `permission_mode = "auto"` uses `--permission-mode plan` on the CLI but auto-approves ExitPlanMode requests without showing Telegram buttons. Can also be set per chat via `/planmode auto`.
* By default Untether strips `ANTHROPIC_API_KEY` from the subprocess environment so Claude uses subscription billing. Set `use_api_billing = true` to keep the key.

---

## Code changes (by file)

### 1) New file: `src/untether/runners/claude.py`

#### Backend export

Expose a module-level `BACKEND = EngineBackend(...)` (from `untether.backends`).
Untether auto-discovers runners by importing `untether.runners.*` and looking for
`BACKEND`.

`BACKEND` should provide:

* Engine id: `"claude"`
* `install_cmd`:
  * Install command for `claude` (used by onboarding when missing on PATH).
  * Error message should include official install options and “run `claude` once to authenticate”.

    * Install methods include install scripts, Homebrew, and npm. ([Claude Code][4])
    * Agent SDK / CLI can use Claude Code authentication from running `claude`, or API key auth. ([Claude][5])

* `build_runner()` should parse `[claude]` config and instantiate `ClaudeRunner`.

#### Runner implementation

Implement a new `Runner`:

#### Public API

* `engine: EngineId = "claude"`
* `format_resume(token) -> str`: returns `` `claude --resume {token}` ``
* `extract_resume(text) -> ResumeToken | None`: parse last match of `--resume/-r`
* `is_resume_line(line) -> bool`: matches the above patterns
* `run(prompt, resume)` async generator of `UntetherEvent`

#### Subprocess invocation

Core invocation (non-interactive):

* `claude -p --output-format stream-json --input-format stream-json --verbose` ([Claude Code][1])
  * `--verbose` overrides config and is required for full stream-json output.
  * `--input-format stream-json` enables JSON input on stdin.

Core invocation (permission mode):

* `claude --output-format stream-json --input-format stream-json --verbose --permission-mode <mode> --permission-prompt-tool stdio`
  * No `-p` flag — prompt is sent via stdin as a JSON user message.
  * `--permission-prompt-tool stdio` enables the bidirectional control channel.

Resume:

* add `--resume <session_id>` if resuming. ([Claude Code][1])

Model:

* add `--model <name>` if configured. ([Claude Code][1])

Permissions:

* add `--allowedTools "<rules>"` if configured. ([Claude Code][1])
* add `--dangerously-skip-permissions` only if explicitly enabled (high risk; document clearly).

Prompt passing:

* Pass the prompt as the final positional argument after `--` (CLI expects `prompt` as an argument). This also protects prompts that begin with `-`. ([Claude Code][1])

Other flags:

* Claude exposes more CLI flags, but Untether does not surface them in config.

#### Stream parsing

In stream-json mode, Claude emits newline-delimited JSON objects. ([Claude Code][1])

Per the official Agent SDK TypeScript reference, message types include:

* `system` with `subtype: 'init'` and fields like `session_id`, `cwd`, `tools`, `model`, `permissionMode`, `output_style`. ([Claude Code][3])
* `assistant` / `user` messages with Anthropic SDK message objects. ([Claude Code][3])
* final `result` message with:

  * `subtype: 'success'` or `'error'`,
  * `is_error`, `result` (string on success),
  * `usage`, `total_cost_usd`,
  * `duration_ms`, `duration_api_ms`, `num_turns`,
  * `structured_output` (optional). ([Claude Code][3])

  Note: upstream Claude CLI may also emit `error`, `permission_denials`, and
  `modelUsage` fields, but these are **not captured** by Untether's
  `StreamResultMessage` schema (msgspec silently ignores unknown fields).

Untether should:

* Parse each line as JSON; on decode error emit a warning ActionEvent (like CodexRunner does) and continue.
* Prefer stdout for JSON; log stderr separately (do not merge).
* Treat unknown top-level fields (e.g., `parent_tool_use_id`) as optional metadata and ignore them unless needed.

#### Mapping to Untether events

**StartedEvent**

* Emit upon first `system/init` message:

  * `resume = ResumeToken(engine="claude", value=session_id)`
    (treat `session_id` as opaque; do not validate as UUID)
  * `title = model` (or user-specified config title; default `"claude"`)
  * `meta` should include `cwd`, `model`, `tools`, `permissionMode`, `output_style` for debugging. `model` and `permissionMode` are used for the `🏷` footer line on final messages.

**Action events (progress)**
The core useful progress comes from tool usage.

Claude Code tools list is documented (Bash/Edit/Write/WebSearch/WebFetch/TodoWrite/Task/etc.). ([Claude Code][2])

Strategy:

* When you see an **assistant message** with a content block `type: "tool_use"`:

  * Emit `ActionEvent(phase="started")` with:

    * `action.id = tool_use.id`
    * `action.kind` based on tool name (complete mapping):

      * `Bash` → `command`
      * `Edit`/`Write`/`NotebookEdit` → `file_change` (best-effort path extraction)
      * `Read` → `tool`
      * `Glob`/`Grep` → `tool`
      * `WebSearch`/`WebFetch` → `web_search`
      * `TodoWrite`/`TodoRead` → `note`
      * `AskUserQuestion` → `note`
      * `Task`/`Agent` → `tool`
      * `KillShell` → `command`
      * otherwise → `tool`
    * `action.title`:

      * Bash: use `input.command` if present
      * Read/Write/Edit/NotebookEdit: use file path (best-effort; field may be `file_path` or `path`)
      * Glob/Grep: use pattern
      * WebSearch: use query
      * WebFetch: use URL
      * TodoWrite/TodoRead: short summary (e.g., “update todos”)
      * AskUserQuestion: short summary (e.g., “ask user”)
      * otherwise: tool name
    * `detail` includes a compacted copy of input (or a safe summary).

* When you see a **user message** with a content block `type: "tool_result"`:

  * Emit `ActionEvent(phase="completed")` for `tool_use_id`
  * `ok = not is_error`
  * `content` may be a string or an array of content blocks; normalize to a string for summaries
  * `detail` includes a small summary (char count / first line / “(truncated)”)

This mirrors CodexRunner’s “started → completed” item tracking and renders well in existing `UntetherProgressRenderer`.

**CompletedEvent**

* Emit on `result` message:

  * `ok = (is_error == false)` (treat `is_error` as authoritative; `subtype` is informational)
  * `answer = result` on success; on error, a concise message using `errors` and/or denials
  * `usage` attach:

    * `total_cost_usd`, `usage`, `modelUsage`, `duration_ms`, `duration_api_ms`, `num_turns` ([Claude Code][3])
  * Always include `resume` (same session_id).
* Emit exactly one completed event per run. After emitting it, ignore any
  trailing JSON lines (do not emit a second completion).
* We do not use an idle-timeout completion; completion is driven by Claude’s
  `result` event or process exit handling.

**Permission denials**
Because result includes `permission_denials`, optionally emit warning ActionEvent(s) *before* CompletedEvent (CompletedEvent must be final):

* kind: `warning`
* title: “permission denied: <tool_name>”
  This preserves the “warnings before started/completed” ordering principle Untether already tests for CodexRunner.

#### Session serialization / locks

Must match Untether runner contract:

* Lock key: `claude:<session_id>` (string) in a `WeakValueDictionary` of `anyio.Lock`.
* When resuming:

  * acquire lock before spawning subprocess.
* When starting a new session:

  * you don’t know session_id until `system/init`, so:

    * spawn process,
    * wait until the **first** `system/init`,
    * acquire lock for that session id **before** yielding StartedEvent,
    * then continue yielding.

This mirrors CodexRunner’s correct behavior and ensures “new run + resume run” serialize once the session is known.
Assumption: Claude emits a single `system/init` per run. If multiple `init`
events arrive, ignore the subsequent ones (do not attempt to re-lock).

#### Cancellation / termination

Reuse the existing subprocess lifecycle pattern (like `CodexRunner.manage_subprocess`):

* Kill the process group on cancellation
* Drain stderr concurrently (log-only)
* Ensure locks release in `finally`

## Documentation updates

### README

Add a “Claude Code engine” section that covers:

* Installation (install script / brew / npm). ([Claude Code][4])
* Authentication:

  * run `claude` once and follow prompts, or use API key auth (Agent SDK docs mention `ANTHROPIC_API_KEY`). ([Claude][5])
* Non-interactive permission caveat + how to configure:

  * settings allow/deny rules,
  * or `--allowedTools` / `[claude].allowed_tools`. ([Claude Code][2])
* Resume format: `` `claude --resume <id>` ``.

### `docs/developing.md`

Extend “Adding a Runner” with:

* “ClaudeRunner parses Agent SDK stream-json output”
* Mention key message types and the init/result messages.

---

## Test plan

Mirror the existing `CodexRunner` tests patterns.

### New tests: `tests/test_claude_runner.py`

1. **Contract & locking**

* `test_run_serializes_same_session` (stub `run_impl` like Codex tests)
* `test_run_allows_parallel_new_sessions`
* `test_run_serializes_new_session_after_session_is_known`:

  * Provide a fake `claude` executable in tmp_path that:

    * prints system/init with session_id,
    * then waits on a file gate,
    * a second invocation with `--resume` writes a marker file and exits,
    * assert the resume invocation doesn’t run until gate opens.

2. **Resume parsing**

* `format_resume` returns `claude --resume <id>`
* `extract_resume` handles both `--resume` and `-r`

3. **Translation / event ordering**

* Fake `claude` outputs:

  * system/init
  * assistant tool_use (Bash)
  * user tool_result
  * result success with `result: "ok"`
* Assert Untether yields:

  * StartedEvent
  * ActionEvent started
  * ActionEvent completed
  * CompletedEvent(ok=True, answer="ok")

4. **Failure modes**

* `result` subtype error with `errors: [...]`:

  * CompletedEvent(ok=False)
* permission_denials exist:

  * warning ActionEvent(s) emitted before CompletedEvent

5. **Cancellation**

* Stub `claude` that sleeps; ensure cancellation kills it (pattern already used for codex subprocess cancellation tests).

---

## Implementation checklist (v0.3.0)

* [x] Export `BACKEND = EngineBackend(...)` from `src/untether/runners/claude.py`.
* [x] Add `src/untether/runners/claude.py` implementing the `Runner` protocol.
* [x] Add tests + stub executable fixtures.
* [x] Update README and developing docs.
* [ ] Run full test suite before release.

---

If you want, I can also propose the exact **event-to-action mapping table** (tool → kind/title/detail rules) you should start with, based on Claude Code’s documented tool list (Bash/Edit/Write/WebSearch/etc.). ([Claude Code][2])

---

## Interactive enhancements (v0.4.0+)

### AskUserQuestion support

When Claude calls `AskUserQuestion`, the control request is intercepted and shown in Telegram. The question text is extracted from the tool input (supports both `{"question": "..."}` and `{"questions": [{"question": "..."}]}` formats).

Flow:
1. Claude emits `control_request` with `tool_name: "AskUserQuestion"`
2. Runner registers in `_PENDING_ASK_REQUESTS[request_id] = question_text`
3. Telegram shows the question with Approve/Deny buttons
4. User replies with text → `telegram/loop.py` intercepts via `get_pending_ask_request()`
5. `answer_ask_question()` sends `control_response(approved=False, denial_message="The user answered...")` — the answer is in the denial message so Claude reads it and continues

### Diff preview in tool approvals

When a tool requiring approval (Edit/Write/Bash) goes through the control request path, `_format_diff_preview()` generates a compact preview:
- **Edit**: shows removed (`-`) and added (`+`) lines (up to 4 each, truncated to 60 chars)
- **Write**: shows first 8 lines of new content
- **Bash**: shows the command prefixed with `$`

The preview is appended to the `warning_text` in the progress message. Only applies to tools that go through `ControlRequest` (not auto-approved tools).

### Cost tracking and budget

`runner_bridge.py` calls `_check_cost_budget()` after each `CompletedEvent` to compare run cost against configured budgets (`[cost_budget]` in `untether.toml`). Budget alerts are shown in the progress footer.

`cost_tracker.py` provides:
- `CostBudget` — per-run and daily budget thresholds with configurable warning percentage
- `CostAlert` — alert levels: info, warning, critical, exceeded
- `record_run_cost()` / `get_daily_cost()` — daily accumulation with midnight reset

### Session export

`commands/export.py` records session events during runs via `record_session_event()` and `record_session_usage()`. Up to 20 sessions are retained. `/export` outputs markdown; `/export json` outputs structured JSON.

[1]: https://code.claude.com/docs/en/headless "Run Claude Code programmatically - Claude Code Docs"
[2]: https://code.claude.com/docs/en/settings "Claude Code settings - Claude Code Docs"
[3]: https://code.claude.com/docs/en/sdk/sdk-typescript "Agent SDK reference - TypeScript - Claude Docs"
[4]: https://code.claude.com/docs/en/quickstart "Quickstart - Claude Code Docs"
[5]: https://platform.claude.com/docs/en/agent-sdk/quickstart "Quickstart - Claude Docs"
