---
applies_to: "src/untether/runners/claude.py,src/untether/telegram/commands/claude_control.py"
---

# Control Channel Rules

## PTY lifecycle

ClaudeRunner uses `pty.openpty()` for stdin (not `subprocess.PIPE`):
1. `master_fd, slave_fd = pty.openpty()`
2. `tty.setraw(master_fd)` for raw byte passthrough
3. Slave FD passed to subprocess as stdin
4. Master FD wrapped in `anyio.AsyncFile` for async writes
5. **Always close master FD in `finally`** — FD leaks break subsequent runs

## Session registries

```python
_SESSION_STDIN: dict[str, anyio.abc.ByteSendStream]   # session_id -> stdin
_REQUEST_TO_SESSION: dict[str, str]                    # request_id -> session_id
_DISCUSS_COOLDOWN: dict[str, tuple[float, int]]        # session_id -> (timestamp, deny_count)
_DISCUSS_APPROVED: set[str]                            # sessions with post-outline approval
_PENDING_ASK_REQUESTS: dict[str, str]                  # request_id -> question text
```

- Register on first `system.init` event (when session_id is known)
- Clean up all registries in the `finally` block of `run_impl` (including cooldown and approval state)
- All control responses go through `write_control_response(session_id, request_id, approved, deny_message)`

## Auto-approve

Non-interactive tools are auto-approved without showing buttons:
- List maintained in `_AUTO_APPROVE_TOOLS` set
- `ControlInitializeRequest`: always auto-approved immediately
- Tool requests: check `tool_name in _AUTO_APPROVE_TOOLS`
- `ExitPlanMode`: NEVER auto-approved — always show Telegram buttons
- `AskUserQuestion`: NEVER auto-approved — shown in Telegram for user to reply with text

## AskUserQuestion flow

When Claude calls `AskUserQuestion`:
1. Control request intercepted → registered in `_PENDING_ASK_REQUESTS[request_id]`
2. Question extracted from `input.question` or `input.questions[0].question`
3. Progress message shows `❓ <question text>` with Approve/Deny buttons
4. User replies with text → `telegram/loop.py` intercepts via `get_pending_ask_request()`
5. `answer_ask_question()` sends deny response with user's text as `denial_message`
6. Claude reads the denial message as the answer and continues

## Diff preview

`_format_diff_preview(tool_name, tool_input)` generates compact diffs for approval messages:
- Only for tools going through `ControlRequest` (not auto-approved)
- Edit: `- old` / `+ new` lines (max 4 each, 60 char truncation)
- Write: `+ content` (max 8 lines)
- Bash: `$ command` (max 200 chars)

## Progressive cooldown

After "Pause & Outline Plan" click:
- Base cooldown: 30 seconds
- Escalation: `min(30 * deny_count, 120)` seconds
- Auto-deny rapid `ExitPlanMode` retries within cooldown window
- Deny count preserved across expiry (keeps escalating)
- Resets to 0 on explicit Approve or Deny
- Cooldown and approval state cleaned up on session end

## Post-outline approval

After cooldown auto-deny, synthetic Approve/Deny buttons appear in Telegram:
- User clicks "Approve Plan" → session added to `_DISCUSS_APPROVED`, cooldown cleared
- User clicks "Deny" → cooldown cleared, no auto-approve flag set
- Next `ExitPlanMode` checks `_DISCUSS_APPROVED` → auto-approves if present
- Synthetic callback_data prefix: `da:` (fits 64-byte Telegram limit)
- Handled in `claude_control.py` before the normal approve/deny flow

## Control request/response format

Request (from Claude on stdout):
```json
{"type":"control_request","request_id":"req_1","tool_name":"Bash","tool_input":{...}}
```

Response (to Claude on stdin):
```json
{"type":"control_response","request_id":"req_1","approved":true}
```

Denial with message:
```json
{"type":"control_response","request_id":"req_1","approved":false,"denial_message":"..."}
```

## After changes

```bash
uv run pytest tests/test_claude_control.py tests/test_ask_user_question.py tests/test_diff_preview.py -x
```
