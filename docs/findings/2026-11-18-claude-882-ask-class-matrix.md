---
question: >-
  On Claude Code CLI 2.1.327, which decision_reason_type and flags does each
  ask-class can_use_tool request carry in the autonomous modes (plan, auto,
  bypassPermissions, dontAsk), does routine work reach stage 6 at all, and can
  --permission-prompts none replace #835's unattended deny?
date: 2026-11-18
sources:
  - https://code.claude.com/docs/en/agent-sdk/permissions (retrieved 2026-11-18)
  - https://code.claude.com/docs/en/permission-modes (retrieved 2026-11-18)
  - https://code.claude.com/docs/en/permissions (retrieved 2026-11-18)
  - https://code.claude.com/docs/en/headless (retrieved 2026-11-18)
  - "binary: ~/.local/share/claude/versions/2.1.327 (can_use_tool request schema describe texts)"
  - "probe r362-882 on lba-1, 2026-11-18: 21 real runs on the subscription login + zero-token fake-API rows (scratch harness, not committed)"
confidence: high (reason types and flags per row, routine-noise rows); med (classifier escalation counters, fake-API rows only)
---

# #882 ask-class permission matrix (CLI 2.1.327)

**Date:** 2026-11-18 · **CLI:** Claude Code 2.1.327 · **Host:** lba-1 · **Issue:**
[#882](https://github.com/littlebearapps/untether/issues/882). Builds on
`2026-08-13-claude-permission-modes.md` (six-stage pipeline) and
`2026-09-30-claude-sdk-control-permissions-context.md` (§Q3a, #835 step-0 A1/A2).
The scratch harness (`fakeapi.py`, `driver.py`, MCP server) wasn't committed; the method bullets
below are the recipe, re-runnable on any CLI minor that touches permissions. Untether's design
from these rows: `docs/reference/runners/claude/runner.md` → "Ask-class requests in autonomous modes".

- **CLI**: Claude Code 2.1.327, lba-1.
- **argv**: Untether's `_build_args` control-channel argv, no `-p`: `--output-format stream-json --input-format stream-json --verbose --model=<m> --allowedTools Bash,Read,Edit,Write [bypass flag] --permission-mode <m> --permission-prompt-tool stdio`.
  - Probe-only flags: `--strict-mcp-config --setting-sources project,local --tools <list> --max-turns <n> --debug-file [--mcp-config]`.
  - Driver sends `initialize` (hooks null), then the user message, and denies every `can_use_tool` unless the row says otherwise.
- **Real runs**: 21 on the subscription, clean env (HOME, PATH, LANG, TERM, USER only). sonnet for plan/auto rows (auto stays available; plan gets classifier review), haiku for bypass/dontAsk. ≈0.62M tokens, ≈95 % cached.
- **Fake rows**: dummy key, temp HOME, fake API on 127.0.0.1:18882, refuser on :18883 (log empty), `CLAUDE_CODE_AUTO_MODE_SERVER=0` except S1, model opus.
- **Deviation**: rules/hooks in `.claude/settings.local.json` (the release guard blocks `settings.json` paths); the CLI reports source `localSettings`. A6's target became `.claude/settings.local.json`.
- **Classifier review judged by**: real rows `tool_result_meta[].permission_decision.reason_type == "classifier"` (server-side; `permissionDecisionMs` 300–2800 ms vs 10–30 ms for rule/mode); fake rows the fake API's classifier log (2.1.327: two stages, `xml_s1` severity then block) plus `non_execution_kind`.

## Scenario table

`—` = key absent. "n/a" = no `can_use_tool`. R = real, F = fake.

| id | mode | run | call | can_use_tool / reason | classifier_approvable | requires_user_interaction | suppress_always_allow_rule | agent_id | side effect | auto | classifier | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 | plan | R sonnet | Write code.txt | yes: `mode` | — | — | — | — | absent | y | n | VALID (2nd try) |
| P2 | plan | R | Write the plan file | no (`rule`) | n/a | n/a | n/a | n/a | created | y | n | VALID |
| P3 | plan | R | Bash `touch p3.txt` | no | n/a | n/a | n/a | n/a | **created** | y | y (accept) | VALID (2nd try) |
| P3-1st | plan | R | ExitPlanMode | yes: None | — | true | — | — | n/a | y | n | incidental |
| P4 | plan | R | Glob, Grep, Read, Agent, WebFetch | no | n/a | n/a | n/a | n/a | n/a | y | y ×2 | VALID |
| P5 | plan | R | Read /etc/hostname | no (`rule`, Untether allowlist) | n/a | n/a | n/a | n/a | n/a | y | n | VALID |
| P6 | plan | R | background Agent → subagent Write | yes: `mode` | — | — | — | set | absent | y | y | VALID |
| P7 | plan | R | MCP probe_read / probe_write | read: no; write: yes `mode` | — | — | — | — | read only | y | y | VALID |
| P8 | plan | R | 5 research Bash | no (classifier) | n/a | n/a | n/a | n/a | n/a | y | y ×5 | VALID |
| B1 | bypass | R haiku | Bash touch + ask rule | yes: `rule` (+ `decision_reason_rule_source`) | — | — | — | — | absent | n/a | n | VALID |
| B2 | bypass | F | `rm -rf .` | yes: `safetyCheck` | false | — | true | — | intact | n/a | n | VALID |
| B3 | bypass | F | `rm -rf "$UNSET"/x` | no (ran; harmless) | n/a | n/a | n/a | n/a | n/a | n/a | n | VALID |
| B3v | bypass | F | `${UNSET}/x`; `$UNSET/`; `$(echo …)`; `~` | first no; others `safetyCheck` | false | — | true | — | intact | n/a | n | VALID |
| B4 | bypass | R | Write, Edit, Bash echo, outside Read, WebFetch | no | n/a | n/a | n/a | n/a | written | n/a | n | VALID |
| B5 | bypass | R | Write + hook `ask` | yes: `hook` | — | — | — | — | absent | n/a | n | VALID |
| A1 | auto | R sonnet | Glob, Grep, Read, Write | no | n/a | n/a | n/a | n/a | written | y | n | VALID |
| A2a | auto | F | 5 blocked rm | 1–2 CLI deny; 3–5 `classifier` | — | — | — | — | intact | y | y | VALID |
| A2b | auto | F | garbage verdict | same | — | — | — | — | intact | y | y | VALID |
| A2c | auto | F | host allows 1st escalation | f4, f5 still escalate (host Approve doesn't reset counter) | — | — | — | — | f3 removed | y | y | VALID |
| A2d | auto | F | b,a,b,b,b | escalates only at f5 | — | — | — | — | — | y | y | VALID |
| A2e | auto | F | 33 actions b,b,a | one escalation at f29 (20 total) | — | — | — | — | intact | y | y | VALID |
| A2f | auto | F | escalation denied, then routine | no further requests | — | — | — | — | written | y | y | VALID |
| A3 | auto | R | Bash touch + ask rule | yes: `rule` | — | — | — | — | absent | y | n | VALID |
| A4 | auto | F | `rm -rf .` | no: CLI denies (safetyCheck) | n/a | n/a | n/a | n/a | intact | y | n | VALID |
| A5 | auto | R | Read /etc/hostname | no (`rule`); no workingDir | n/a | n/a | n/a | n/a | n/a | y | n | VALID |
| A6 | auto | R | Write .claude/settings.local.json | no (classifier) | n/a | n/a | n/a | n/a | created | y | y | VALID |
| A7 | auto | R | 4 research Bash + Edit | no | n/a | n/a | n/a | n/a | edited | y | y ×4 | VALID |
| D1 | dontAsk | R haiku | Bash touch + ask rule | no: CLI denies | n/a | n/a | n/a | n/a | absent | n/a | n | VALID |
| D1b | dontAsk | F | `rm -rf .` | no: CLI denies | n/a | n/a | n/a | n/a | intact | n/a | n | VALID |
| S1 | auto | F (server review on) | 4 rm | no: all "Classifier unavailable"; no fallback | n/a | n/a | n/a | n/a | intact | y | n | VALID |
| U1 | bypass | F | MCP requiresUserInteraction ×2 | yes: no reason type; `mcp_server` | — | **true** | true | — | allow → 1 call; deny → none | n/a | n | VALID |
| EP | plan | F | ExitPlanMode | yes: None | — | true | — | — | allow → mode default | n/a | n | VALID |
| N1 | plan + none | R | ExitPlanMode, Write | **ExitPlanMode missing from tools**; Write → `asyncAgent` deny | n/a | n/a | n/a | n/a | absent | y | n | VALID |
| N2 | bypass + none | R | Bash + rule; Write | Bash `asyncAgent` deny; Write allowed | n/a | n/a | n/a | n/a | n2.txt | n/a | n | VALID |
| N4 | auto + none | F | 4 blocks, routine | 3–4 `asyncAgent` deny; routine OK | n/a | n/a | n/a | n/a | written | y | y | VALID |
| U1n | bypass + none | F | requiresUserInteraction tool | `asyncAgent` deny; no call | n/a | n/a | n/a | n/a | none | n/a | n | VALID |
| N5 | plan + none | F | AskUserQuestion, ExitPlanMode | **both missing from tools** | n/a | n/a | n/a | n/a | absent | n/a | n | VALID |

Never observed: `default_to_no`, `matched_ask_rule`, `workingDir`, `other`, `sandboxOverride`. Absent flags are missing keys, not `null`.

## What this means

- **D4 noise**: no routine call reached the host in plan (P4, P5, P8), auto (A1, A5–A7) or bypass (B4). Plan sends only `mode` edits + ExitPlanMode; auto only `rule`/`classifier`; bypass only rule/hook/safetyCheck/person-only. Supports D4 = patch, conditional on R362-882e.
- **Step 7 rules**: `_PLAN_ROUTINE_REASONS` ships empty; keep the None-reason edit gate fail-closed; ask rules key on `decision_reason_type == "rule"` (keep the `matched_ask_rule` check too); safetyCheck and classifier get a normal card; person-only = `requires_user_interaction: true` and the CLI executes on allow, so D3 stands. **ExitPlanMode also carries `requires_user_interaction: true`: exclude ExitPlanMode/AskUserQuestion before reading the flag** (add a test with that exact shape).
- **Schema**: decode the four flags; optionally `decision_reason_rule_source`, `decision_reason_hook_event`, `blocked_path`, `mcp_server`. Ignore `server_policy_ask_only` (`@internal`).
- **`--permission-prompts none` can't replace #835**: it removes ExitPlanMode and AskUserQuestion from the tool set, breaking unattended plan/plan-auto. Possible later for deny-all modes only.
- **D5 (outside #882)**: with auto available, the CLI drops `--allowedTools Bash` in plan/auto and the plan classifier allowed a `touch` during planning.
- Other: S1 has no local classifier fallback with a non-Anthropic base URL; dangerous-rm check misses `"$VAR"/x` and `${VAR}/x`; `tool_result_meta.permission_decision` is a native per-call signal.
