---
name: claude-stream-json
description: >
  Claude Code CLI stream-json protocol as consumed by Untether.
  Covers JSONL event types, content blocks, control channel protocol,
  permission modes, auto-approve, progressive cooldown, and ExitPlanMode handling.
  This is the CONSUMER side — Untether spawns Claude Code as a subprocess.
triggers:
  - working on Claude runner code
  - modifying control channel handling
  - changing permission request logic
  - working on auto-approve or cooldown
  - debugging Claude Code event streams
  - modifying ExitPlanMode or plan mode handling
---

# Claude Code stream-json Protocol (Consumer)

Untether spawns Claude Code CLI as a subprocess and consumes its JSONL output. This skill covers the protocol from Untether's perspective.

## Key files

| File | Purpose |
|------|---------|
| `src/untether/runners/claude.py` | `ClaudeRunner` — subprocess management, PTY, control channel, event translation |
| `src/untether/schemas/claude.py` | msgspec structs for Claude JSONL events |
| `src/untether/runners/tool_actions.py` | `tool_kind_and_title()` — tool name to ActionKind mapping |
| `docs/reference/runners/claude/runner.md` | Full runner specification |
| `docs/reference/runners/claude/stream-json-cheatsheet.md` | JSONL event shapes with examples |
| `docs/reference/runners/claude/untether-events.md` | Claude JSONL to Untether event mapping |

## CLI invocation

### Non-interactive mode (`-p`)

```bash
claude -p --output-format stream-json --input-format stream-json --verbose -- <prompt>
```

- `-p` / `--print`: non-interactive, prompt as positional arg after `--`
- `--verbose`: required for full stream-json output
- `--input-format stream-json`: enables JSON input on stdin
- Prompt passed after `--` to protect prompts starting with `-`

### Interactive permission mode

```bash
claude --output-format stream-json --input-format stream-json --verbose \
  --permission-mode plan --permission-prompt-tool stdio
```

- **No `-p` flag** — prompt sent via stdin as JSON user message
- `--permission-prompt-tool stdio`: enables bidirectional control channel
- `--permission-mode plan|tool`: determines what needs approval

### Common flags

- `--resume <session_id>`: resume a previous session
- `--model <name>`: model override (sonnet, opus, haiku)
- `--allowedTools "<rules>"`: auto-approve specific tools

## JSONL event types

One JSON object per line on stdout. Required field: `type`.

### `system` (init)

```json
{"type":"system","subtype":"init","session_id":"...","cwd":"/repo","model":"sonnet",
 "permissionMode":"auto","tools":["Bash","Read","Write"],"mcp_servers":[...]}
```

- Emitted once at stream start
- `session_id`: opaque string (do NOT assume UUID format)
- Untether emits `StartedEvent` here

### `assistant` / `user` messages

```json
{"type":"assistant","session_id":"...","message":{"id":"msg_1","role":"assistant",
 "content":[...],"usage":{...}}}
```

Content blocks in `message.content[]`:

| Block type | Fields | Untether mapping |
|-----------|--------|-----------------|
| `text` | `text` | Stored as fallback answer; no action emitted |
| `tool_use` | `id`, `name`, `input` | `ActionEvent(phase="started")` |
| `tool_result` | `tool_use_id`, `content`, `is_error` | `ActionEvent(phase="completed")` |
| `thinking` | `thinking` | Optional note action or ignored |

### `result`

```json
{"type":"result","subtype":"success","session_id":"...","is_error":false,
 "result":"Done.","total_cost_usd":0.01,"usage":{...},
 "duration_ms":12345,"duration_api_ms":12000,"num_turns":2}
```

- `is_error`: authoritative error indicator
- `result`: final answer string
- Untether emits exactly one `CompletedEvent` here
- Lines after `result` are dropped

Fields NOT in Untether's `StreamResultMessage` schema (silently ignored by msgspec):
- `error`, `permission_denials`, `modelUsage`

## Tool name to ActionKind mapping

| Tool name | ActionKind | Title source |
|-----------|-----------|-------------|
| `Bash` | `command` | `input.command` |
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | `file_change` | `input.file_path` or `input.path` |
| `Read` | `tool` | `Read <path>` |
| `Glob`, `Grep` | `tool` | pattern from input |
| `WebSearch` | `web_search` | `input.query` |
| `WebFetch` | `web_search` | URL from input |
| `TodoWrite`, `TodoRead` | `note` | "update todos" |
| `AskUserQuestion` | `note` | "ask user" |
| `Task`, `Agent` | `tool` | tool name |
| `KillShell` | `command` | tool name |
| (other) | `tool` | tool name |

Mapping implemented in `src/untether/runners/tool_actions.py`.

## Control channel protocol

When using `--permission-prompt-tool stdio`, Claude Code sends control requests as JSONL on stdout and expects responses on stdin.

### Control request (stdout)

```json
{"type":"assistant","session_id":"...","message":{"content":[
  {"type":"tool_use","id":"toolu_ctrl_1","name":"PermissionPromptTool",
   "input":{"type":"control_request","request_id":"req_1",
            "tool_name":"Bash","tool_input":{"command":"rm -rf /"}}}
]}}
```

### Control response (stdin)

```json
{"type":"control_response","request_id":"req_1","approved":true}
```

Or with denial:
```json
{"type":"control_response","request_id":"req_1","approved":false,
 "denial_message":"Not allowed — explain your plan first."}
```

### ControlInitializeRequest

Sent at session start; auto-approved immediately (no user prompt):
```json
{"type":"control_response","request_id":"req_init","approved":true}
```

## PTY for stdin

ClaudeRunner uses `pty.openpty()` instead of `subprocess.PIPE` for stdin:
- Prevents deadlock when keeping stdin open for control responses
- Master FD held by the runner; slave FD passed to subprocess
- `tty.setraw(master_fd)` for raw byte passthrough
- Stdin refs captured locally at spawn time (not on `self`)

## Session registries (concurrent sessions)

```python
_SESSION_STDIN: dict[str, anyio.abc.ByteSendStream]   # session_id -> stdin pipe
_REQUEST_TO_SESSION: dict[str, str]                    # request_id -> session_id
```

- Registered in `_iter_jsonl_events` when session_id is first seen
- Control responses routed via `_REQUEST_TO_SESSION` lookup
- Cleaned up when run completes

## Auto-approve logic

Non-interactive tools are auto-approved without user prompt:

```python
AUTO_APPROVE_TOOLS = {"Grep", "Glob", "Read", "LS", "Bash", "BashOutput",
                      "TodoWrite", "TodoRead", "WebSearch", "WebFetch", ...}
```

- `ControlInitializeRequest`: always auto-approved
- Tool requests where `tool_name in AUTO_APPROVE_TOOLS`: auto-approved silently
- `ExitPlanMode`: always shown to user as inline buttons

## ExitPlanMode handling

When Claude requests `ExitPlanMode`:
1. Inline keyboard shown: **Approve** / **Deny** / **Pause & Outline Plan**
2. "Pause & Outline Plan" sends a deny with a detailed message asking Claude to write a step-by-step plan
3. Progressive cooldown on rapid retries: 30s, 60s, 90s, 120s (capped)

### Progressive cooldown

```python
# In ClaudeRunner
_discuss_deny_count: int = 0          # escalates per click
_discuss_last_at: float = 0.0         # timestamp of last discuss/auto-deny
_DISCUSS_BASE_COOLDOWN_S = 30         # base cooldown
_DISCUSS_MAX_COOLDOWN_S = 120         # cap
```

- After "Pause & Outline Plan", auto-deny rapid ExitPlanMode retries within cooldown window
- Cooldown: `min(base * count, max)` seconds
- Deny count preserved across expiry (keeps escalating)
- Resets on explicit Approve or Deny

## Early callback answering

Telegram buttons show a spinner until `answerCallbackQuery`. The Claude control callback handler sets `answer_early = True` to clear the spinner immediately with a toast ("Approved", "Denied", "Outlining plan...").

## `write_control_response` helper

```python
async def write_control_response(
    session_id: str,
    request_id: str,
    approved: bool,
    deny_message: str | None = None,
) -> None:
```

Looks up stdin in `_SESSION_STDIN[session_id]`, writes JSON response, handles cleanup.

## Config keys (`[claude]` section in untether.toml)

```toml
[claude]
model = "sonnet"
allowed_tools = ["Bash", "Read", "Edit", "Write"]
dangerously_skip_permissions = false
use_api_billing = false
permission_mode = "plan"  # set via /planmode command or ChatPrefsStore
```

- `use_api_billing = false` (default): strips `ANTHROPIC_API_KEY` from subprocess env
- `permission_mode`: overridable per-chat via `/planmode` command
