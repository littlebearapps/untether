---
question: Can Untether's Claude engine natively mirror all Claude Code permission modes — in name and in function — and what upstream primitives are available to do it?
date: 2026-08-13
sources:
  - https://code.claude.com/docs/en/permission-modes
  - https://code.claude.com/docs/en/agent-sdk/permissions
  - https://code.claude.com/docs/en/agent-sdk/user-input
  - https://code.claude.com/docs/en/permissions
  - https://code.claude.com/docs/en/hooks
  - "local probe: Claude Code CLI 2.1.229 on lba-1"
confidence: high
---

## Answer

**Yes — behaviourally, on the tested CLI version.** Every primitive needed to mirror all seven modes
in name *and* behaviour exists and was verified working on **CLI 2.1.229**. The gaps are entirely in
Untether's own layer and are removable without a protocol change.

Deliberately *not* claiming "nothing upstream is in the way": the whole stage-6 path rests on
`--permission-prompt-tool`, which is no longer documented anywhere (see below, and #750). This is a
working integration on a tested version, **not a durable supported contract**. Any claim of parity
should be version-qualified.

Claude Code evaluates permissions in a fixed six-stage order:

```
1 Hooks → 2 Deny rules → 3 Ask rules → 4 Permission mode → 5 Allow rules → 6 canUseTool
```

`--permission-prompt-tool stdio` — the flag Untether passes on every spawn — **is stage 6**.
Anything resolved at stages 1–5 never reaches Untether. That single fact explains the whole
observed behaviour: `plan` and `auto` resolve at stage 4 (above the allow rules) and therefore
work correctly under Untether, while `default` / `manual` / `acceptEdits` fall through to stage 5,
where Untether's own `--allowedTools "Bash,Read,Edit,Write"` approves the four tools that matter
before Untether is ever consulted.

## Evidence

### Mode acceptance — CLI 2.1.229, spawn-probed 2026-08-13

All seven values accepted (`rc=0`); an invalid value exits 1 with commander's
`Allowed choices are …` usage error. `default` is accepted **but is not in the advertised choices
list** (`acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`). rc8's
`CLAUDE_CLI_PERMISSION_MODES` is therefore correct; `tests/test_claude_permission_modes.py` passes
46/46 against 2.1.229.

### Gate probe — which tool calls actually reach the stage-6 parent

Harness replicated Untether's exact argv (stream-json I/O, `initialize` handshake,
`--permission-prompt-tool stdio`), asked Claude to run `touch /tmp/ut_probe_marker.txt`, and
**denied** anything reaching the parent. Probe script:
`/tmp/…/scratchpad/probe_perm.py` (throwaway).

| # | Config | `can_use_tool` reached parent | Tool ran despite deny |
|---|---|---|---|
| A | `--permission-mode default`, no `--allowedTools` | 1 × `Bash` | no |
| B | `default` + Untether's `--allowedTools Bash,Read,Edit,Write` | **0** | **yes** |
| C | B + `--settings '{"permissions":{"ask":["Bash"]}}'` | 1 × `Bash` | no |
| D | `bypassPermissions` + same ask rule | 1 × `Bash` | no |
| E | `acceptEdits`, no allowlist, out-of-scope path | 1 × `Bash` | no |
| F | `dontAsk` + allowlist | 0 | yes |

**A vs B is the finding** — the only difference is Untether's own allowlist, and it is the
difference between "manual mode works" and "manual mode is bypass".
**C and D** establish that `ask` rules override allow rules and fire even under
`bypassPermissions`, and that `--settings` accepts a JSON string (so rules can be injected per-run
without touching user config files).
**E** confirms `acceptEdits` only auto-approves filesystem commands for **in-scope** paths.
**F** confirms `dontAsk` depends on the allowlist to be usable at all.

### Plan mode vs the allowlist — probed separately 2026-08-13

The question that decides whether `plan` needs the allowlist dropped alongside `default`/`manual`:

| # | Config | Attempted | Reached parent | Executed |
|---|---|---|---|---|
| G | `plan` + `--allowedTools Bash,Read,Edit,Write` | `Write /tmp/ut_plan_probe.txt` | `ExitPlanMode` only | **no** (file absent) |
| H | `plan` + allowlist | `Read /etc/hostname` | 0 | yes |
| I | `plan`, **no** allowlist | `Read /etc/hostname` | 1 × `Read` | — (denied by probe) |

**G: plan mode is not leaky under Untether's argv.** The allow rule did **not** let the write
through — the file was never created, and Claude pivoted to `ExitPlanMode` instead. Note the
mechanism differs from the SDK docs' wording ("plan routes file-edit and shell-write tools to your
`canUseTool` callback regardless of allow rules"): with Untether's shape the write was blocked
**internally**, never surfacing as a `can_use_tool`. Same safety outcome, different path — worth
knowing before writing a test that asserts a `Write` approval appears in plan mode. It will not.

**H vs I: dropping the allowlist in plan mode is pure overhead.** Every `Read` would become a
stage-6 round-trip that Untether's handler auto-approves anyway. Hence the rc9 design keeps
`--allowedTools` for `plan` / `plan-auto` and drops it only for `default` / `manual`.

### Parent-initiated `set_permission_mode` — verified working

Session started in `plan`; `system.init` reported `permissionMode: "plan"`. Parent sent
`{"subtype":"set_permission_mode","mode":"acceptEdits"}` and the CLI replied
`{"subtype":"success","response":{"mode":"acceptEdits"}}`. Unlike #365's catalog refresh, this
request returns a meaningful payload worth parsing. `system.init` carrying `permissionMode` also
means Untether can read back the *effective* mode rather than assuming its request took effect.

### `--permission-prompt-tool` is undocumented

Zero occurrences across `permissions.md`, `hooks.md`, `headless.md`, `settings.md`, and
`claude --help` on 2.1.229. Still functional (verified). Anthropic's documented equivalent is the
Agent SDK's `canUseTool` callback; Untether is using an SDK-internal flag directly.

### Upstream primitives available but unused by Untether

| Primitive | Status | What it enables |
|---|---|---|
| `ask` rules via `--settings '<json>'` | verified (C, D) | Force specific tools to prompt regardless of mode, even under `bypassPermissions` |
| `set_permission_mode` control_request | verified | Mode switching mid-session; protocol-honest plan transitions |
| `permission_suggestions` on `can_use_tool` | already in `schemas/claude.py`, unused | An "Always allow" button that persists a rule |
| `PreToolUse` hook | documented | Stage-1 gate that runs on *every* tool call, even under bypass |
| `permissionDecision: "defer"` | documented, `-p` mode only | Pause at a tool call, exit the process, resume later — would decouple long Telegram approval waits from the stall watchdog |

## Implications for Untether

Consumed by the `docs/plans/v0.35.5-rc9/` pack (`/plan`, 2026-08-13) and by:

- **#749** — the mode-aware allowlist + mode-aware approval gate. Cites probe rows A/B as the
  reproduction and C/D as the `ask`-rule option.
- **#747** — UI vocabulary. Must land with #749; exposing `manual` before the gate is fixed would
  ship a button whose label is actively wrong.
- **#750** — the undocumented-flag risk. Cites the zero-occurrence table.
- **#370** — `set_permission_mode` research gate is cleared; sequence *after* #749.
- **#383** — the post-plan-approval bypass is the narrower half; the baseline is already open.

Corrects one stale claim in `docs/plans/mode-switching-design-v2.md` (2026-04-11): auto mode is no
longer Team/Enterprise-only. As of 2026-08-13 it is available on **all plans**, requires
Opus 4.6+ / Sonnet 4.6+ / Fable 5, and becomes the **default mode for new Pro/Max/Team sessions on
2026-08-14** — the day after this finding was written.
