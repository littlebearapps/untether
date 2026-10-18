---
name: antigravity-cli
description: >
  Antigravity CLI (agy) runner protocol for Untether. Covers the argv rules
  (utils/antigravity_argv.py), the four permission modes (Workspace, Plan first,
  Full access, Ask me), Untether's agy-gate plugin (approvals, questions, steer),
  stream-json events and their translation, resume and /continue, errors and
  redaction, and the gotchas. agy runs one process per message (no live sessions).
triggers:
  - working on Antigravity runner code (runners/antigravity.py, utils/antigravity_*.py)
  - working on the agy gate (src/untether/antigravity/)
  - debugging an Antigravity chat, refusal or blocked tool
  - an agy self-update changed flags, events or hook payloads
---

# Antigravity CLI (`agy`) Runner Protocol

Engine id `antigravity` (never `gemini`: different auth, flags and sessions). Supported from v0.36.1 (#558). Minimum
agy 1.3.1 (older is refused pre-spawn); the last probed version is `PROBED_CLI_VERSION` in `runners/antigravity.py`,
pinned by `tests/test_antigravity_cli_schema_drift.py`. agy self-updates about every 15 min, so re-check `agy --help`
and `agy changelog` against the installed version before designing (native-first rule). Depth:
`docs/reference/runners/antigravity/{runner,stream-json-cheatsheet,untether-events}.md`; the native-vs-custom
register is at the end of `runner.md`.

## Key files

| File | Purpose |
|------|---------|
| `src/untether/runners/antigravity.py` | `AntigravityRunner` (overrides `run_impl`), `AntigravityStreamState`, `decide_antigravity_mode`, stderr kill classes |
| `src/untether/schemas/antigravity.py` | msgspec structs for `init` / `step_update` / `result` / `command_result` |
| `src/untether/utils/antigravity_argv.py` | `agy_flag()` + `check_*` allow-lists, `native_compact_prompt()` |
| `src/untether/utils/antigravity_redact.py` | `redact_agy_text()` — every agy string shown or logged at INFO+ |
| `src/untether/utils/antigravity_agent.py` | The no-shell `untether-workspace` agent file; scheduled-run prompt note (#1055) |
| `src/untether/utils/antigravity_plan.py`, `telegram/commands/antigravity_plan.py` | Plan first artifact detection and the `agy_plan:` card (#981) |
| `src/untether/utils/antigravity_scan.py`, `antigravity_state.py` | Planted-config scan (hooks/plugins/agents/MCP) and its persisted state |
| `src/untether/utils/antigravity_continue.py` | `/continue` lookup in agy's `last_conversations.json` (#1039) |
| `src/untether/utils/antigravity_quota.py` | Zero-token `agy -p /usage`, `/credits`, `/effort`, `/config` probes |
| `src/untether/utils/antigravity_context.py` | Seeded context-window table for the estimated `~N% ctx` (#979) |
| `src/untether/antigravity/gate_*.py` | The agy gate plugin: install, precheck, per-run socket, hook, Ask me policy, canary (#980) |
| `src/untether/cli/agy_gate.py` | `untether agy-gate install`, `uninstall`, `status [--project] [--canary]` |

## CLI invocation

```text
agy --input-format stream-json --output-format stream-json --print-timeout 0 [--disable-slash-commands]
    [--conversation=<id>] [--model=<model>] [--effort=<level>]
    (--mode=accept-edits --agent=untether-workspace | --mode=plan --agent=untether-workspace
     | --dangerously-skip-permissions)
```

- **Prompt on stdin** as one line `{"event":"user","message":{"content":"…"}}`, then stdin is closed. Never on argv.
- **Every value is joined to its flag** via `agy_flag(flag, value)`: agy reads a separate dash-leading value as another
  flag (`--model --version` prints the version). A new value-taking flag needs its own `check_*` in `_CHECKS` first. A
  model or conversation id that fails the check is refused pre-spawn (`prespawn_blocked=invalid_argument`).
- `--disable-slash-commands` on every run except Plan first (it would make `--mode=plan` a no-op; a `/`-leading prompt
  is sent as `Task: /…` instead) and a native `/compact` run (#1114).
- A model id that already ends in an effort (`-low`, `-high` …) gets no `--effort`.
- Filtered env: the engine allowlist plus `DBUS_SESSION_BUS_ADDRESS`, `AGY_ADC_AUTH`, `GOOGLE_CLOUD_QUOTA_PROJECT`,
  `GOOGLE_GEMINI_BASE_URL`; `NO_COLOR=1`. Gate env (`UNTETHER_AGY_GATE*`) only on gated runs, never on `-p` probes.
- A project is required: no run in `/`, `$HOME` or the bot's own directory (`prespawn_blocked=no_project`).

## Permission modes

Chosen before the run (`/config` → Permission mode, `/planmode`, or `[antigravity] permission_mode`); headless agy
cannot stop mid-run to ask.

| Mode | Value | Flags | Behaviour |
|------|-------|-------|-----------|
| Workspace (default) | `workspace` | `--mode=accept-edits --agent=untether-workspace` | File edits in the project; no shell/web/MCP/subagent tool. Not a sandbox |
| Plan first | `plan` | `--mode=plan --agent=untether-workspace` | agy writes a plan to `~/.gemini/antigravity-cli/brain/<cid>/` and stops; Approve resumes in Workspace |
| Full access | `full` | `--dangerously-skip-permissions` | Every tool approved |
| Ask me | `ask` | `--dangerously-skip-permissions`, default agent, no `--mode` | Only once the gate is ready; the gate is the sole check (safe list runs, the rest gets an `ia:` card) |

- **`decide_antigravity_mode()` is the only source of the bypass flag**; `build_args` reads its `ModeDecision`. Ask
  me without a ready gate refuses (`gate_missing`, `gate_not_ready`, `gate_self_protect`, `gate_foreign_config`),
  never downgrades. Unknown stored values fail closed to Workspace.
- Precedence: chat/topic (or a cron's own `permission_mode`) → `[antigravity] permission_mode` → Workspace.
  Crons/webhooks never inherit Full access or Ask me from the chat or toml.
- Workspace checks agy's own `toolPermission` twice (zero-token `-p /config`, cached 1 h, and `init.permission_mode`);
  anything but `request-review` / `strict` refuses. One check may be missing, never both (#1044).
- **A conversation keeps the agent it started with** (agy ignores `--agent` on resume and `init` doesn't say). After
  switching to or from Full access / Ask me, the user must send `/new`; Workspace ↔ Plan first and Ask me ↔ Full
  access need none. Untether keeps no per-conversation record; only wording tells the user.
- If any `.agents/` agy loads has `agents`/`plugins` entries, or the user-level agent folder isn't Untether's alone,
  `--agent` is dropped and the run uses agy's default agent (blocked shell ends the turn).

## Untether's agy gate

Opt-in agy plugin (`untether agy-gate install`, via `agy plugin validate` + `agy plugin install`; lands in
`~/.gemini/config/plugins/untether-gate/`). Three hooks — `PreToolUse` (questions, Ask me approvals), `PreInvocation`
(steer via `injectSteps`, liveness), `Stop` (`fullyIdle: false` → background-hold note) — run
`python -I -m untether.antigravity.gate_hook <Event>` only when `UNTETHER_AGY_GATE` is set, so the user's own agy
sessions never start Python.

- Per run: precheck (plugin bytes match this version's templates, not disabled, `agy -p /hooks` lists it; cached 1 h),
  then a 0600 Unix socket under `$XDG_RUNTIME_DIR/untether/<inst8>/`, a 32-byte token, and peer checks
  (`SO_PEERCRED`, descendant of the spawned agy, exact hook cmdline).
- `PreToolUse` fails **closed** (no socket / bad reply / timeout → `deny`); never prints `ask`/`force_ask`.
  `PreInvocation` and `Stop` fail open.
- In Workspace, Plan first and Full access the gate only carries questions and steer; agy's own checks decide tools.
  A failed precheck just turns questions and steer off there.
- Questions: agy's `ask_question` is answered by `deny` + a reason carrying the user's answer. Its step arrives as
  `step_type: unknown` and isn't shown; the card comes from the gate.
- Ask me liveness: a model step with no `PreInvocation` before it, or a tool step with no `PreToolUse` within 3 s,
  stops the run (Ask me only).
- Unattended runs: every tool off the safe list is denied at once (`permission.unattended_deny engine=antigravity`).

## Stream events → Untether

Every line has `event`: `init`, `step_update`, `result`, `command_result`. There is **no `error` event**.

| agy | Untether |
|-----|----------|
| `init` with `conversation_id` | `StartedEvent`, resume `agy --conversation <id>`; meta model/effort/permissionMode |
| `init` id ≠ requested `--conversation` | agy stopped; "conversation no longer exists" (agy silently starts a new one otherwise) |
| `step_update` `tool` ACTIVE / DONE / ERROR | `ActionEvent` started / completed (`ERROR` or `tool_info.error` → `ok=False`) |
| `tool` DONE with no output for a blockable tool | Held; becomes `⚠️ Blocked: …` if `result.denied_actions` names its family |
| `invoke_subagent` / `subagent` | `ActionEvent` kind `subagent` + `sub:` background rows |
| `agent_response` | Text collected as fallback answer; DONE `usage` feeds `~N% ctx` (only when `--model` was passed) |
| `user_input`, `system_message`, `checkpoint`, `unknown` | No event (steer receipt row, background map, `/compact` confirmation) |
| `result` `SUCCESS` | `CompletedEvent(ok=True)`; answer `result.response` or collected text |
| `result` `ERROR` + `interrupted` / other / other status | `CompletedEvent(ok=False)` (resumable / redacted text / `ended with status …`) |
| `command_result` | Ignored in runs (only the `-p` probes read it) |

- A blocked tool looks like a finished one: step DONE, no output, no error; only `result.denied_actions` says so, and
  agy 1.3.x ends the turn at the first one. A hook refusal is different: step `ERROR` with `tool_info.error`.
- `result.usage`, `num_turns` and `duration_seconds` are **conversation totals**; the footer sums the run's own
  `agent_response` usage. `num_turns` is not passed on, so agy isn't a turn-count engine for resume auto-clear.
- `init` may be missing (early failure → only a `result` with empty `conversation_id`).

## Resume and `/continue`

- Resume line `` `agy --conversation <id>` ``; passed as `--conversation=<id>`. Unknown id: agy warns on stderr and
  starts a new conversation, so Untether stops it and clears the saved session (`_RESUME_FAILURE_RE`:
  `antigravity conversation not found`).
- `/continue` never uses agy's `--continue` (it also matches parent/child dirs): it reads
  `~/.gemini/antigravity-cli/cache/last_conversations.json` for the exact realpath of the run directory, refuses an id
  listed under two paths, and never clears the chat's saved session on failure.

## Errors and redaction

- stderr stop classes (first 4 KiB, case-insensitive): sign-in required, account verify/ToS block, quota reached,
  credits low, unknown conversation → SIGTERM then SIGKILL after 3 s, one final message, session kept (except unknown
  conversation).
- Exit codes: 0 ok (incl. blocked tool), 1 error result, 2 bad flag (`flags provided but not defined` →
  `antigravity.argv.rejected`), 3 `AGY_ERROR` (parsed defensively, never captured live).
- Every agy string that reaches Telegram or an INFO+ log passes `redact_agy_text()` (sign-in URLs carry one-time
  secrets); raw stderr only at DEBUG.

## Gotchas

- agy holds its answer until background commands and timers end (up to 30 min); stall warnings become a
  `background_waiting` notice, and a `/cancel` during the hold loses the answer (`result.response` empty).
- Every running `run_command` counts as possible background work after 5 s (the stream carries no
  `WaitMsBeforeAsync`); subagent completions are attributed FIFO unless a timer's output names the sender.
- Daemon commands outlive the run; Untether stops only child processes it saw agy start.
- No live/warm sessions (#977 left only the `live_sessions.py` seam); steer is a steer-only backend through the gate.
- agy hooks, plugins, agents and MCP servers run in every mode without a tool call; the planted-config scan warns on
  first sight, holds crons on change, and re-checks after each run (tripwire, not sandbox).
- Never read agy state files beyond the recorded exceptions (`last_conversations.json`, the Plan first artifact +
  sidecar); transcripts and task files are off-limits (native-first rule 3).

## Tests

`uv run pytest tests/test_antigravity_*.py -x`. Fixtures: `tests/fixtures/antigravity/*.jsonl` (+ `.script`), fake CLI
`tests/fake_clis/fake_agy.py`, gate helpers `tests/antigravity_gate_support.py`. The drift suite runs only where `agy`
is on PATH; `UNTETHER_AGY_DRIFT=1` adds the gated (network, zero-token) tier — run it on lba-1 for each rc.
