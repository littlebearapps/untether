# #380 — Auto-approve scope review for `ControlRewindFilesRequest` and `ControlMcpMessageRequest`

**Audit date:** 2026-04-27
**Author:** Claude (Untether agent, supervised by @npschram)
**Issue:** [#380](https://github.com/littlebearapps/untether/issues/380)
**Cross-ref:** [Audit 2026-04-20 §ASI02](./agent-orchestration-security-audit-2026-04-20.md), `[security] priority: high`

## Scope

`src/untether/runners/claude.py` auto-approves five non-tool control_request subtypes
without surfacing them to the Telegram user:

```python
_AUTO_APPROVE_TYPES = (
    ControlInitializeRequest,    # protocol housekeeping
    ControlHookCallbackRequest,  # hook plumbing
    ControlMcpMessageRequest,    # ← reviewed here
    ControlRewindFilesRequest,   # ← reviewed here
    ControlInterruptRequest,     # cancel
)
```

The 2026-04-20 audit flagged the two MCP-/rewind-related types as worth a deeper
look because:

- `ControlRewindFilesRequest` could in principle undo state that drove a prior
  denial decision.
- `ControlMcpMessageRequest` could carry tainted payloads from a compromised
  MCP server.

This memo documents the audit findings and the regression locks added to keep
the audit honest.

## Methodology

1. Read the message-shape definitions in `src/untether/schemas/claude.py:154-174`.
2. Trace every call site in `src/untether/runners/claude.py` that handles each
   subtype.
3. Cross-reference Untether's session-level approval state
   (`_PLAN_EXIT_APPROVED`, `_DISCUSS_APPROVED`, `_HANDLED_REQUESTS`) to confirm
   nothing in the auto-approve path mutates those registries.
4. Confirm Claude Code's upstream invocation surface for each subtype.

## Findings

### `ControlMcpMessageRequest` — auto-approve **safe**

**Shape:** `{server_name: str, message: Any}` (subtype `"mcp_message"`).

**Behaviour at the auto-approve path:**

- Untether stores the request_id in `state.auto_approve_queue` and the raw
  payload in `_REQUEST_TO_INPUT[request_id]`.
- The payload is **never inspected, executed, parsed, or rendered** by Untether.
  The drain task (`_drain_auto_approve`) only reads the request_id; the payload
  is opaque storage so that an `updated_input` round-trip would be possible if
  the protocol ever requires it (it currently doesn't for this subtype).
- The drain emits a `control_response{approved: true}` over the stdin PTY back
  to Claude Code.

**Threat model considered:**

A compromised MCP server could craft `message` to contain prompt-injection
content. That payload would flow through Claude Code to the model. Routing
this control_request through Telegram approval would NOT block the payload —
the payload is already in flight to Claude Code by the time we see the
control_request, and Claude Code is the path of record for delivering MCP
messages to the model regardless of our acknowledgement.

The risk of compromised MCP servers is the inherent threat model of any MCP
server, not specific to auto-approve. The mitigation lives upstream (in
Claude Code's MCP hardening work, e.g. `system.init` connection-status
filtering and #365 catalog refresh) — not on Untether's approval channel.

**Verdict:** auto-approve is correct.

### `ControlRewindFilesRequest` — auto-approve **safe**

**Shape:** `{user_message_id: str}` (subtype `"rewind_files"`).

**Behaviour at the auto-approve path:** identical pass-through pattern as
mcp_message — request_id queued, payload opaque, response written verbatim.

**Threat model considered:**

The intuitive concern is "rewind could undo state that drove a prior denial."
Specifically: a prior turn might have included a denial that prevented a write;
rewind to a checkpoint before that denial could let the model re-attempt and
succeed.

Three things mitigate this in practice:

1. **Rewind is user-initiated.** Upstream Claude Code 2.1.x exposes rewind via
   the `/rewind` slash command (or programmatic equivalent). The model cannot
   autonomously trigger it. Untether currently has no UI that issues `/rewind`,
   so this control_request only fires when the user types `/rewind` themselves
   in a chat. The user has already consented.
2. **Approval state does not live in the file system.** Untether's per-session
   approval state — `_PLAN_EXIT_APPROVED`, `_DISCUSS_APPROVED`, denial counts,
   discuss cooldowns — lives in Untether-owned module-level dicts on the
   parent process. `rewind_files` operates on Claude Code's internal file
   checkpoints; it does not touch Untether registries.
3. **A subsequent write would still pass through the standard tool gate.**
   Even if rewind reset the file state, the next write tool call would emit
   a fresh `ControlCanUseToolRequest`, which goes through Untether's normal
   approval flow (with diff_preview when configured). The user would see the
   write and have a chance to deny again.

**Verdict:** auto-approve is correct **as long as rewind remains
user-initiated upstream**. If a future Claude Code release allows the model
to trigger rewind autonomously, this audit must be revisited and rewind moved
to `_TOOLS_REQUIRING_APPROVAL`.

## Documentation + regression locks

- **Inline comment** added to `src/untether/runners/claude.py` near
  `_AUTO_APPROVE_TYPES` documenting both subtypes' invariants and the
  re-audit trigger (upstream semantic change to either subtype).
- **Three regression-lock tests** added to
  `tests/test_claude_control.py::TestAutoApproveSafetyInvariant`:
  - `test_mcp_message_payload_not_inspected` — asserts the auto-approve path
    does not stringify, iterate, or otherwise interact with the `message`
    payload (defence against drift toward inspecting payloads here, which
    would mean the trust model has shifted).
  - `test_rewind_files_request_does_not_clear_plan_approval` — asserts that
    handling a `rewind_files` request leaves `_PLAN_EXIT_APPROVED` and
    `_DISCUSS_APPROVED` untouched. Prevents a future change from
    accidentally coupling rewind to per-session approval state.
  - `test_auto_approve_emits_no_telegram_events` — asserts all five
    auto-approve subtypes emit `[]`, the invariant that justifies skipping
    the Telegram-side gate.

## Recommendations

1. **No code change beyond comment + tests.** The current auto-approve list is
   correct under the present trust model.
2. **Re-audit trigger.** Subscribe to upstream Claude Code release notes for
   any semantic change to either subtype. Specifically watch for:
   - `mcp_message` gaining the ability to carry executable instructions
     interpreted by Claude Code itself (e.g. local CLI side effects from MCP
     server messages).
   - `rewind_files` becoming model-callable (e.g. via a new `Rewind` tool or
     a model-initiated subtype).
   The inline comment in `runners/claude.py` and this memo together form the
   audit trail; the regression tests fail loudly if the auto-approve path
   starts behaving differently.
3. **Follow-up scope.** A broader audit of Claude Code's parent-initiated
   control_request surface (currently only `mcp_status` for #365) is out of
   scope for #380 but would be useful for v0.36.x.

## References

- `src/untether/runners/claude.py` — auto-approve gate (around the
  `_AUTO_APPROVE_TYPES` definition; line numbers shift with edits — see the
  inline comment for the canonical rationale).
- `src/untether/schemas/claude.py:154-174` — control_request type
  definitions.
- `tests/test_claude_control.py::TestAutoApproveSafetyInvariant` — regression
  locks.
- `.claude/rules/control-channel.md` — control-channel architecture rules
  (invariant maintained: PTY lifecycle, session registries, response
  routing).
- [Claude Code SDK docs](https://github.com/anthropics/claude-agent-sdk-python)
  — wire format and subtype semantics.
