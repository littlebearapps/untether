# Plan: Interactive AskUserQuestion Support in Untether

## Context

Untether's Claude Code control channel already supports Approve/Deny buttons for tool permissions (ExitPlanMode, Bash, etc.). AskUserQuestion uses the same `can_use_tool` control request protocol, but needs the user to **answer questions** (pick from options or type free text) rather than just approve/deny.

AskUserQuestion can present 1-4 questions per invocation, each with 2-4 options plus implicit "Other" for free text. The response mechanism is `updatedInput.answers` -- a dict mapping question text to selected option label.

## Protocol (verified from source)

- AskUserQuestion arrives as `control_request` / `can_use_tool` / `tool_name: "AskUserQuestion"`
- Input: `{questions: [{question: str, header: str, options: [{label, description}], multiSelect: bool}]}`
- Response: `{behavior: "allow", updatedInput: {questions: [...], answers: {"Which DB?": "PostgreSQL"}}}`
- The `updatedInput.answers` dict is what Claude sees as the tool result

## Scope

**In scope (Phase 1):** Single-select questions, sequential multi-question, "Other" free text
**Deferred:** multiSelect (toggle buttons with Done confirmation)

## Implementation

### 1. New state in `claude.py`

Add alongside existing registries (`_ACTIVE_RUNNERS`, `_SESSION_STDIN`, etc.):

```python
@dataclass
class PendingQuestionState:
    request_id: str
    questions: list[dict[str, Any]]   # from tool_input["questions"]
    current_index: int = 0            # which question is active
    answers: dict[str, str] = field(default_factory=dict)  # question -> answer

_PENDING_QUESTIONS: dict[str, PendingQuestionState] = {}   # request_id -> state
_PENDING_TEXT_ANSWERS: dict[int, str] = {}                  # chat_id -> request_id (for "Other")
```

Add `build_question_keyboard()` helper:
- Builds `inline_keyboard` buttons for a single question's options
- callback_data format: `claude_control:ans:Q:IDX:REQUEST_ID` (max 59 bytes, fits 64-byte limit)
- "Other..." button: `claude_control:other:Q:REQUEST_ID` (59 bytes)
- Layout: one button per row for clarity

### 2. Detect AskUserQuestion in `translate_claude_event`

**File: `claude.py`**, in the `StreamControlRequest` match arm (~line 370)

Branch on `tool_name == "AskUserQuestion"` before the generic Approve/Deny path:
- Parse `questions` from tool input
- Create `PendingQuestionState`, store in `_PENDING_QUESTIONS`
- Call `build_question_keyboard()` for question[0]
- Set action title to the question text (e.g. `"Question: Which database should we use?"`)
- Return `ActionEvent(kind="warning")` with question-specific `inline_keyboard` in detail

The existing `TelegramPresenter.render_progress()` in `bridge.py` already finds the first non-completed action with `inline_keyboard` and merges it with Cancel -- no changes needed there.

### 3. New public API: `send_claude_question_answer()`

**File: `claude.py`**

Parallel to existing `send_claude_control_response()`:
- Takes `request_id` and `answers: dict[str, str]`
- Copies original tool input from `_REQUEST_TO_INPUT`, sets `answers` key
- Overwrites `_REQUEST_TO_INPUT[request_id]` with updated input
- Calls existing `write_control_response(request_id, approved=True)` (which already reads `_REQUEST_TO_INPUT` for `updatedInput`)
- Cleans up all registries (`_REQUEST_TO_SESSION`, `_REQUEST_TO_INPUT`, `_PENDING_QUESTIONS`, `_HANDLED_REQUESTS`)

### 4. Extend `ClaudeControlCommand` callback handler

**File: `claude_control.py`**

Currently parses `approve:REQUEST_ID` / `deny:REQUEST_ID`. Add two new sub-commands:

**`ans:Q:IDX:REQUEST_ID`** -- option selected:
1. Look up `_PENDING_QUESTIONS[request_id]`
2. Resolve option label from `questions[Q].options[IDX].label`
3. Record `answers[question_text] = label`
4. If more questions: advance `current_index`, edit progress message with next question's keyboard (direct `bot.edit_message_text` -- safe because Claude is blocked)
5. If all answered: call `send_claude_question_answer(request_id, answers)`

**`other:Q:REQUEST_ID`** -- free text requested:
1. Set `_PENDING_TEXT_ANSWERS[chat_id] = request_id`
2. Return CommandResult prompting user to type their answer

### 5. Bot client access for multi-question keyboard edits

**Problem:** `CommandContext` doesn't expose the bot client, but we need it to `edit_message_text` when advancing to Q2.

**Solution:** Store a module-level `_BOT_CLIENT` reference in `claude.py`:
- Set it from `run_main_loop` in `loop.py` during startup (one line: `claude._BOT_CLIENT = cfg.bot`)
- Import in `claude_control.py` when needed for multi-question edits
- Graceful fallback: if `_BOT_CLIENT` is None, send all questions' answers at once (less interactive but functional)

### 6. Text message interception for "Other"

**File: `loop.py`**, at the top of `route_message()`

Early check before normal message processing:
```python
pending_req_id = _PENDING_TEXT_ANSWERS.pop(msg.chat_id, None)
if pending_req_id:
    pending = _PENDING_QUESTIONS.get(pending_req_id)
    if pending:
        q = pending.questions[pending.current_index]
        pending.answers[q["question"]] = msg.text.strip()
        pending.current_index += 1
        if pending.current_index < len(pending.questions):
            # edit progress message with next question keyboard
        else:
            await send_claude_question_answer(pending_req_id, pending.answers)
        return
```

### 7. Cleanup

- Add `_PENDING_QUESTIONS` and `_PENDING_TEXT_ANSWERS` to existing cleanup in `process_error_events`, `stream_end_events`, and expired-request loop
- Add to autouse test fixture `_clear_registries`

### 8. Tests

**File: `test_claude_control.py`** (extend existing)

| Test | What it verifies |
|------|-----------------|
| `test_ask_user_question_produces_question_keyboard` | AskUserQuestion control request -> ActionEvent with option buttons (not Approve/Deny) |
| `test_ask_user_question_pending_state_created` | `_PENDING_QUESTIONS` populated with correct structure |
| `test_ask_user_question_single_answer_sends_response` | Answering single-question flow sends control response with `updatedInput.answers` |
| `test_ask_user_question_multi_question_sequential` | 2-question flow: Q1 answer recorded, Q2 would render, final response has both |
| `test_ask_user_question_other_sets_pending_text` | "Other" button populates `_PENDING_TEXT_ANSWERS` |
| `test_ask_user_question_text_interception` | Text message intercepted and used as answer |
| `test_ask_user_question_callback_data_fits_64_bytes` | Verify callback_data for worst case (Q=3, IDX=3, UUID) fits 64 bytes |
| `test_ask_user_question_expired_cleanup` | Expired pending questions cleaned up |

## Files to modify

| File | Changes |
|------|---------|
| `src/untether/runners/claude.py` | `PendingQuestionState`, `_PENDING_QUESTIONS`, `_PENDING_TEXT_ANSWERS`, `_BOT_CLIENT`, `build_question_keyboard()`, `send_claude_question_answer()`, AskUserQuestion branch in `translate_claude_event`, cleanup |
| `src/untether/telegram/commands/claude_control.py` | Handle `ans:` and `other:` callback sub-commands, multi-question edit logic |
| `src/untether/telegram/loop.py` | Set `_BOT_CLIENT` on startup, text interception early-return in `route_message` |
| `tests/test_claude_control.py` | ~8 new tests for question flow |

## Verification

1. `cd /home/nathan/untether-fork && uv run pytest tests/test_claude_control.py -v`
2. `cd /home/nathan/untether-fork && uv run pytest --no-cov -x -q`
3. `systemctl --user restart untether`
4. Via Telegram: send a task that triggers AskUserQuestion (e.g. ask Claude to clarify something), verify question + option buttons appear, tap an option, verify Claude continues with that answer
5. Test "Other": tap Other, type a response, verify it's used
6. Test multi-question: trigger a task that asks 2+ questions, verify sequential flow
