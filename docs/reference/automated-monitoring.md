# Automated monitoring sessions

Quick-ref for Claude Code sessions that run unattended on lba-1 to observe Untether (and eventually related projects), categorise signals, and file GitHub issues for later triage. These are **observe-and-file** workflows — never fix-and-deploy.

## Shared pattern

Every automated monitoring session follows the same shape:

1. **Trigger** — user kicks off a session from CLI/tmux on lba-1, or a scheduled cron fires it.
2. **Skill encodes methodology** — `.claude/skills/<session-name>/SKILL.md` holds the workflow, guardrails, category taxonomy, dedup logic, and issue-filing templates. Version-controlled. Evolves walk-by-walk via a built-in self-review.
3. **State persists on disk** — `~/.untether-<session-name>/<session-id>/session.json` carries dedup signatures and filed-issue numbers across ticks. Cold-start per tick is fine because state is authoritative.
4. **Scheduler is a Claude Code primitive** — native `/loop` (self-paced via `ScheduleWakeup`) inside a tmux session. No custom shell loops. No Untether cron involvement.
5. **Guardrail is three-layer** — tool allow-list in skill frontmatter, Bash command allow/deny in skill body, invariant reminder repeated at the top of every tick prompt.
6. **Output is GitHub issues** — labelled `auto:<session-name>`. Dedupe across all `auto:*` labels before filing. Wrap-up writes one summary issue per session.
7. **Archive** — on wrap-up, state dir moves from `~/.untether-<session-name>/<id>/` to `~/.untether-<session-name>/archive/<id>/` for post-mortem.

## Why this shape

Alternatives considered, reasons rejected:

| Alternative | Why not |
|---|---|
| Pure bash script (no skill) | Can't reason about judgment-required categorisation — UX/inefficiency needs an LLM. |
| Untether cron trigger | Adds runtime dependency on Untether state + TOML hot-reload. Fine for v2 if Telegram mid-walk pings become worth it; overkill for v1. |
| Claude Code fixed-interval `/loop 15m` | Forever-running, no clean end-time termination. Self-paced `/loop` solves both. |
| Context-accumulating loop | Tokens balloon across 6+ ticks; false-positive carryover biases later categorisation. Cold-start + `state.json` is cleaner for dedup-heavy tasks. |
| Let the skill fix what it finds | Mixes observation with remediation. Drift toward yolo-fixes erodes trust. Strict observe-only with explicit guardrails is the design choice. |

## Current sessions

| Session | Skill path | Trigger | Typical duration | Label |
|---|---|---|---|---|
| [Walking mode](../../.claude/skills/walking-mode-monitor/SKILL.md) — dog-walk QA of Untether staging | `.claude/skills/walking-mode-monitor/` | User-initiated via `/loop` before leaving for a walk | 60–120 min | `auto:walking-mode` |

(Empty slots for future additions: daily security audit, weekly integration smoke, nightly cost digest, release-day tests.)

## How to invoke (walking-mode example)

```bash
ssh lba-1
cd ~/untether
tmux new -s walk \
  'claude /loop "start walking-mode for 90 min. Invoke skill walking-mode-monitor."'
# Detach: Ctrl-b d. Reattach: tmux attach -t walk. Force stop: tmux kill-session -t walk.
```

Inside the skill, `ScheduleWakeup(delaySeconds=900, ...)` fires the next tick. On the final tick (within 15 min of `end_epoch`), the skill skips `ScheduleWakeup` — the `/loop` exits naturally.

## How to add a new session

1. **Pick a name** — `<noun>-<verb>` (e.g. `daily-security-audit`, `release-smoke-check`).
2. **Create the skill** at `.claude/skills/<name>/SKILL.md` following the walking-mode template:
   - YAML frontmatter with `name`, `description`, `triggers`, `allowed_tools: [Read, Glob, Grep, Bash, WebFetch, ScheduleWakeup]`, `disallowed_tools: [Edit, Write, NotebookEdit, Task]`.
   - **INVARIANT** block at top — copy from walking-mode and adjust deny-list for the specific workflow.
   - **Bootstrap** — state-dir creation on first invocation.
   - **Per-tick workflow** — data collection → analyse → dedup → file → update state → self-schedule.
   - **Wrap-up** — summary issue + self-review + archive.
   - **Flag-only mode** — for calibration; first 2–3 runs only file high-confidence categories.
   - **Bash allow/deny** — explicit per-session list; start from walking-mode's as a base.
   - **Self-check** — pre-exit verification checklist.
3. **Create the label** — `gh label create 'auto:<name>' --color <hex> --description '...'`.
4. **Add to the table above** in this doc.
5. **Dry-run first** — invoke with a `... dry-run ...` prompt; skill should print prepared issue bodies without calling `gh issue create`. Sanity-check categorisation and issue quality.
6. **Short calibration session** (15 min) before a real run.
7. **After 3 real sessions**, retro: review all `auto:<name>` issues closed as `wontfix`/`duplicate` and tune the skill.

## Invariant rules for any session

Hard constraints that hold across all automated monitoring sessions. If a new session violates any of these, rework the design before shipping:

- **Never mutates anything.** Reads logs and filesystem state; writes only to `~/.untether-<name>/<id>/` and `gh issue create|comment`. Never edits source, never deploys, never pushes, never merges, never restarts services.
- **Cold-start per tick.** State lives on disk in JSON; the skill reloads it fresh each wake. No ambient context between ticks.
- **Dedup is authoritative.** Session `state.json` + `gh issue list -l auto:<name>,auto:error-report` cross-check before every filing. No duplicate issues.
- **Secrets redacted in every log excerpt.** Apply the redaction regex set before any `gh issue create` call: Telegram bot tokens, Groq/OpenAI/generic-SK keys, anything looking like a signed URL. Over-redact rather than under-redact.
- **Three-layer guardrail is non-negotiable.** Tool allow-list in frontmatter + Bash allow/deny in body + INVARIANT block at top. All three. Not two.
- **One summary issue per session.** Wrap-up runs even on manual stop (if practical). Self-review bullet list feeds next iteration of the skill.
- **Cost-aware.** Per-tick Opus cost is $0.50–$1. Daily budget cap via `[budget] daily_cap_usd` applies. Abort a walk early rather than blow the budget.

## Interaction with other tooling

- **`untether-issue-watcher`** — local script at `~/.local/bin/untether-issue-watcher` (not in repo) files GH issues for deterministic error events with label `auto:error-report`. Automated-monitoring sessions are **complementary**: they handle judgment-required signals the watcher can't. Skill dedup queries both labels.
- **GitHub release guard** — blocks `git push`, `gh pr merge`, `gh release create` on feature/main branches. Automated monitoring only creates issues, never triggers.
- **Claude Code auto-memory** — automated-monitoring skills explicitly do NOT write to `~/.claude/projects/*/memory/`. Session state goes to `~/.untether-<name>/<id>/` only. Keeps future Claude context unpolluted by walk-specific observations.
- **Telegram MCP** — flickers. Skill uses `gh` CLI via Bash, not MCP — reliable.

## Cost & scale notes

- **Per session**: 90-min walking-mode × 6 ticks + 1 wrap-up at Claude Opus ≈ $1–$5 total. Tick timeout is not enforced by a bash wrapper (native `/loop`); if the harness doesn't cap individual ticks, a single runaway tick could exceed budget. **Mitigation**: the skill's self-check aborts ticks that take suspiciously long (>5 min of analysis with no tool calls).
- **Per day**: running multiple sessions (e.g. morning walk + evening review) is fine; daily budget cap applies.
- **GitHub API rate limit**: 5000 req/hr authenticated. Even a heavy walk files <100 issues. Not a constraint.
- **Skill token size**: cold-start reloads the skill body each tick. Keep SKILL.md under ~10 KB to avoid per-tick token overhead. Walking-mode's is ~8 KB — fine.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "ERROR: unfinished session at ..." on start | Previous session's wrap-up failed or was interrupted | Inspect the state dir, then `mv` it to `~/.untether-<name>/archive/` manually |
| Ticks fire but no issues are filed | Skill is in flag-only mode (marker file present) | Remove `$STATE_DIR/flag-only-all`, add `$STATE_DIR/file-all-categories` once calibration is done |
| `ScheduleWakeup` doesn't fire next tick | Likely not running in `/loop` mode, or running wrapped by Untether (#289) | Must be direct `claude /loop ...` CLI invocation on lba-1 (not via Untether) |
| Issue flood | Dedup signatures too narrow, or flag-only was prematurely disabled | Pause the session (`tmux kill-session`), review recent `auto:<name>` issues, tighten the `normalised_identifier` logic in the skill, close spam issues as `duplicate` |
| Session silently ends after 1 tick | Skill hit an error before calling `ScheduleWakeup` | Check tmux output, state dir for partial files, `~/.claude/projects/<slug>/*.jsonl` for last session's trace |

## Further reading

- [`.claude/skills/walking-mode-monitor/SKILL.md`](../../.claude/skills/walking-mode-monitor/SKILL.md) — canonical implementation; template for new sessions.
- [`.claude/rules/testing-conventions.md`](../../.claude/rules/testing-conventions.md) — coverage + integration-test rules (informs what a session should flag as "missing coverage").
- Claude Code `/loop` skill — built-in Claude Code primitive. `omit interval` for self-paced mode.
- `docs/reference/triggers/triggers.md` — Untether cron triggers (alternative scheduler if you ever want Telegram mid-session pings).
