# Workflow Commands — routing + cross-cutting rules

The thin always-on slice for Untether's agentic loop commands (`/debug`, `/fix`,
`/plan`, `/implement`, `/qa`, `/pr-dev`, `/pr-main`, `/kaizen`, `/kaizen-review`,
`/handover`). Every one of those command files cites this rule in its header. It
does two jobs: **route** work to the right command, and load the **cross-cutting
rules** every workflow command must obey.

This rule sequences and guards. It never re-describes how to code, and never
re-quotes the 8-step protocol — that lives in the `.claude/commands/debug/`
bundle. See `docs/LOOPS.md` for the loop registry and
`docs/plans/agentic-loops-and-commands/README.md` for the full design.

## Routing — which command for which work shape

| Work shape | Command |
|---|---|
| bug / regression / incident — **understand** it | `/debug` |
| bug / regression / incident — **ship the fix** | `/fix` (or `/debug` targeted for a one-off ≤3-file fix) |
| net-new capability — **scope** it | `/plan` |
| net-new capability — **build** an approved phase | `/implement` |
| **validate** a target enough for its risk | `/qa` |
| finalise a branch → **PR to `dev`** (→ TestPyPI) | `/pr-dev` |
| prepare a **stable release** → open `dev`→`master` PR | `/pr-main` (stops at Nathan's merge) |
| capture a **process learning** | `/kaizen` |
| **pausing** mid-work | `/handover` |

When a command discovers it's the wrong tool (a "bug" that's really net-new →
`/plan`; an "idea" that's really a defect → `/fix`), STOP and route rather than
pushing on. Record the redirect in the run summary.

## Cross-cutting rules (every workflow command obeys these)

1. **Untether-mode aware.** When run via Telegram, `AskUserQuestion` /
   `ExitPlanMode` return empty — never block on them. State assumptions in text
   and STOP for a reply. Final summaries stay brief (≈500–1500 chars, 3–7
   bullets); never re-paste a full plan body (see
   `feedback_telegram_summary_brevity`).

2. **Release-guard obedience.** Never `git push`/merge to `master`/`main`, never
   `git tag`, never `gh release create`. Every PR targets **`dev`**.
   `gh pr merge <n> --squash` is allowed **only** when base = `dev`. Local hooks
   are defense-in-depth; the real authorization boundary is the GitHub branch
   ruleset + CODEOWNERS. Never edit `hooks.json` or the guard scripts
   (self-protected). See `.claude/rules/release-discipline.md`.

3. **Dev/staging separation.** Never restart `untether.service` (staging) to test
   code — always `untether-dev.service`. Respect hot-reload: never
   `systemctl restart` from inside an active session (the 120s drain drops the
   final message — see `feedback_agent_self_restart_pattern`). See
   `.claude/rules/dev-workflow.md`.

4. **Reuse, don't duplicate.** Defer to the `.claude/commands/debug/` bundle, the
   8 rules under `.claude/rules/`, and the superpowers skills (via the Skill
   tool). A command sequences + guards; it never re-describes how to code or
   re-quotes the 8-step protocol.

5. **Confirm-gated external writes.** Surface a drafted `gh issue create` / PR
   body / issue comment and wait for a tap — never a silent create under
   auto-mode. De-dupe against open **and** closed issues first.

6. **Idempotent under retries.** Telegram duplicate updates / reconnects can
   invoke a command twice — a command's external effects (issue create, PR open,
   marker write, collector comment) must be safe to re-run: de-dupe,
   check-before-create, resolve collectors by exact title with `--limit 200`.

7. **Redaction.** Evidence gathering (journalctl, structlog, fleet SSH, state
   files, Telegram history) can surface bot tokens, chat content, env values, and
   fleet identifiers — scrub before posting to an issue/PR/Telegram. See the
   redaction pass + output-size bound in
   `.claude/commands/debug/step-evidence.md`.

## Authority ladder (at a glance)

| Command | May write code? | May open PR? | May merge? | May release? |
|---|---|---|---|---|
| `/debug` | ≤3-file minimal fix (targeted) | no | no | no |
| `/fix` | yes (in-scope) | to `dev` | no | no |
| `/plan` | no (docs only) | no | no | no |
| `/implement` | yes (approved phase) | no | no | no |
| `/qa` | no | no | no | no |
| `/pr-dev` | no | to `dev` | to `dev` only (`--merge` + confirm) | no |
| `/pr-main` | version bump + changelog + lock | opens `dev`→`master` PR | no | no |
| `/kaizen` | no (one comment) | no | no | no |
| `/kaizen-review` | no (propose-only) | no | no | no |
| `/handover` | handover doc only | no | no | no |

The one action reserved for Nathan across the whole suite: **merging the
`dev`→`master` PR** (→ auto-tag → PyPI → `fleet-rollout.sh`).
