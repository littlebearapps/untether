---
description: Untether's validation stage — answer "is this validated enough for its risk?" Classify the target + risk, pick the lightest sufficient depth (QA-0..QA-5), run local checks, and (opt-in) orchestrate the integration-test tiers against the allowlisted @untether_dev_bot. Files capped findings, routes repair to /debug→/fix, writes the attestation marker on green. Defaults to plan/dry-run. Never fixes, merges, tags, releases, or rolls the fleet.
argument-hint: "<target> (dry-run tier plan) | <target> --run (drive dev bot) | --level QA-N | --retest #NN | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Skill ToolSearch Bash(uv run pytest:*) Bash(uv run ruff:*) Bash(python3 scripts/validate_release.py:*) Bash(scripts/run-integration-tests.sh:*) Bash(journalctl:*) Bash(systemctl --user is-active:*) Bash(systemctl --user status:*) Bash(ps:*) Bash(pgrep:*) Bash(kill -TERM:*) Bash(git log:*) Bash(git diff:*) Bash(git rev-parse:*) Bash(git status:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh issue create:*) Bash(gh issue comment:*) Bash(gh issue edit:*) Bash(gh label list:*) Bash(gh pr view:*) Bash(gh pr list:*) Bash(grep:*) Bash(rg:*) Bash(jq:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(sort:*) Bash(uniq:*) Bash(awk:*) Bash(sed:*) Bash(ls:*) Bash(cat:*) mcp__telegram__send_message mcp__telegram__get_history mcp__telegram__get_messages mcp__telegram__list_inline_buttons mcp__telegram__press_inline_button mcp__telegram__reply_to_message mcp__telegram__send_voice mcp__telegram__send_file mcp__telegram__get_me
---

You are handling `/qa`. `/qa` is Untether's **validation** stage — it answers one
question: *"is this target validated enough for its risk?"* It classifies the
target, picks the **lightest sufficient depth**, runs the checks, and — only when
asked — drives the integration-test tiers against the **allowlisted dev bot**. It
is the VALIDATE boundary between PREPARE (`/plan`→`/implement` or `/fix`) and
RELEASE (`/pr-dev` / `/pr-main`).

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + the 7 cross-cutting rules).
`docs/reference/integration-testing.md` is the **design-of-record** — `/qa`
*implements* its tier definitions, chat IDs, and MCP tool list; it never
re-litigates them. Key points for `/qa`:

- **Authority: observe + exercise, never change.** Read repo/logs/diffs, run
  safe local tests, drive the **bounded, allowlisted** dev bot, file **capped**
  findings, write the attestation marker. **Never** fix code, merge, tag,
  release, roll the fleet, or mutate a protected file. Repair routes to
  `/debug`→`/fix`.
- **Dev/staging separation.** Drive `@untether_dev_bot` (`untether-dev.service`)
  **only**. Never `@hetz_lba1_bot` (staging), never a fleet/production target.
  Never `systemctl restart` from inside this session.
- **Confirm-gated + idempotent.** Findings are confirm-gated (surface the drafted
  `gh issue create`, de-dupe open **and** closed first); a re-invoked `/qa` must
  not double-file or double-write the marker.
- **Redaction.** Scrub tokens/keys/env/chat-content/fleet identifiers from any
  log/response evidence before it lands in a finding (see
  `.claude/commands/debug/step-evidence.md`).
- **Untether-mode.** `AskUserQuestion`/`ExitPlanMode` return empty — the `--run`
  confirmation is stated in text and STOPS for a reply. Keep the report brief.

## The live-bot guardrails (non-negotiable)

A slash command cannot enforce capability limits in code, so these are hard
rules the command obeys:

1. **Default to plan/dry-run.** `/qa <target>` prints the tier matrix **and the
   exact `send_message`/`press_inline_button` script it *would* run**, then
   STOPS. Live drive requires `--run` (or explicit operator confirmation in text
   under Untether-mode).
2. **Allowlist only.** Drive `@untether_dev_bot` and the 6 documented engine chat
   IDs **only** (`docs/reference/integration-testing.md` → Test chats). Prove the
   target is the dev bot first (`mcp__telegram__get_me` / bot ID `8678330610`);
   **fail closed** if it can't be proven. Never staging, never fleet-wide.
3. **Bounded.** Cap messages-per-run and per-chat pacing, cap retries, cap total
   runtime; tag **every emitted message** with a unique test-run ID
   (`qa-<UTC-stamp>-<n>`); capture command/response/timestamp/cleanup as evidence.
4. **No authority escalation.** `/qa` may observe + exercise the allowlisted test
   bot; it may never merge, tag, release, roll the fleet, or mutate protected
   files.

## Adaptive levels (pick the lightest sufficient depth)

| Level | Target shape | Checks |
|---|---|---|
| **QA-0** | docs / command-docs | link/path/consistency checks (mirror the context hooks' concerns) — no code run |
| **QA-1** | small code/config | `uv run pytest tests/test_<area>.py` + import/smoke + `ruff check` |
| **QA-2** | feature/module exercisable safely | full `uv run pytest` + coverage + local dry-run + structlog inspection |
| **QA-3** | multi-component / transport / lifecycle / hot-reload / watchdog | **integration tiers via Telegram MCP** + `journalctl` signature review + `ps`/FD/zombie checks |
| **QA-4** | release-gating (patch/minor/major) | the full tier matrix per `.claude/rules/release-discipline.md` + **write the attestation marker** |
| **QA-5** | retest after `/fix` | re-run the failed tier + **signature-absence before/after** |

`--level QA-N` forces a level; otherwise classify the target and choose. When in
doubt, go one level up for risk-bearing surfaces (runner/schema/telegram/watchdog),
one down for docs/trivials.

## Reuse map (do not duplicate)

- `docs/reference/integration-testing.md` — the tier definitions (U1–U10, C1–C7,
  T1–T10, B/S stress), the 6 engine chat IDs, and the "Changed area → required
  tiers" table. **Implement it; do not restate the tiers here.**
- Telegram MCP: `send_message`, `get_history`/`get_messages`,
  `list_inline_buttons`, `press_inline_button`, `reply_to_message`, `send_voice`,
  `send_file`. Bash: `journalctl`, `kill -TERM` (SIGTERM tiers, dev only),
  `ps`/`pgrep` (FD/zombie checks).
- `.claude/rules/release-discipline.md` — the per-release-type tier requirements
  (patch/minor/major) and the attestation-gate contract.
- `.claude/commands/debug/step-evidence.md` (redaction + evidence catalogue) and
  `step-verify.md` (the `needs-verification` hand-off shape).

## Flow

### Q-1. Classify target + risk

Identify the target (a branch/diff, a module, a version, a specific `/fix`
hand-off). Map it to a level via the table above. For code targets, use the
integration-testing "Changed area" table to pick the required tiers. State the
chosen level + tiers.

### Q-2. Run the local checks (always safe)

Run the level's local checks first — these never touch the bot:

```bash
uv run pytest tests/test_<area>.py -x      # QA-1+
uv run pytest                              # QA-2+ (80% coverage gate)
uv run ruff check src/ && uv run ruff format --check src/ tests/
python3 scripts/validate_release.py        # QA-4 (if a version is bumped)
```

Inspect `journalctl --user -u untether-dev` for the target's signatures; for
QA-3+ add `ps`/FD/zombie checks. Redact before quoting anything.

### Q-3. Plan the tier drive (dry-run — the default)

For QA-3+, **print the tier matrix and the exact MCP script** you *would* run:
each `send_message`/`press_inline_button`/`reply_to_message` call, its target
chat ID, the run-ID tag, the expected response, and the cleanup. Then **STOP**.
This is the default output of `/qa <target>` — it touches nothing.

### Q-4. Drive the tiers (only with `--run` + confirmation)

Under `--run` (and, under Untether-mode, an explicit text confirmation):

- Re-assert the allowlist (Q-guardrail 2) — `get_me`, prove dev bot, fail closed
  otherwise.
- Execute the planned script within the bounds (guardrail 3): tag every message
  `qa-<stamp>-<n>`, pace per-chat, cap volume/retries/runtime.
- After each tier, read back via `get_history`, verify expected content, and
  capture command/response/timestamp as evidence.
- Post-drive: `journalctl … | grep -E "WARNING|ERROR"` + zombie/FD sweep.

### Q-5. File capped findings (confirm-gated)

Collate results into **≤5 findings per run**, de-duped against open **and** closed
issues. Each finding uses real labels (`bug`/`enhancement`/`severity:*`/
`priority: *`/`engine:*`). Distinguish Untether bugs from upstream engine quirks
(the latter are noted, not filed). Surface each drafted `gh issue create` and wait
for a tap. Route repair to `/debug`→`/fix`.

### Q-6. Attestation marker (QA-4, on green only — SHA-bound)

On a green release-gating tier run, write the marker via the script (never by
hand), binding the exact commit SHA:

```bash
HEAD_SHA=$(git rev-parse HEAD)
scripts/run-integration-tests.sh <VERSION> --manual \
  --tiers "<the tiers actually run>" \
  --head-sha "${HEAD_SHA}" \
  --notes "green on @untether_dev_bot; run-id qa-<stamp>"
```

The marker binds commit SHA + dev-bot identity + suite/tier set + outcome +
actor + timestamp — `run-integration-tests.sh` records `head_sha` + `dev_bot_id`
as first-class fields (#674), and `fleet-rollout.sh` surfaces them at the gate.
**Writing the marker never invokes
rollout** — `fleet-rollout.sh` is the operator's step and verifies the marker
matches the artifact. Idempotent: if a green marker for this exact
VERSION+SHA already exists, do not rewrite it.

### Q-7. Report + hand-off

Brief report: the level + tiers run, local-check results, live-drive outcome (or
"dry-run only"), findings filed (with numbers), marker written (or why not), and
the hand-off:

- `/debug`→`/fix` — a finding to investigate/ship.
- `/pr-dev` — target is clean and delivery-ready.
- `/pr-main` — a release-gating tier passed and the marker is written.
- `/research` — provider/current-truth uncertainty surfaced during testing.
- `/handover` — QA paused mid-drive.

## Anti-patterns

- No live drive without `--run` + confirmation; no driving a non-dev-bot target
  (fail closed).
- No unbounded message volume; no untagged test messages.
- No fixing code, merging, tagging, releasing, or running `fleet-rollout.sh`.
- No restarting staging (or dev) to "reset" state mid-run.
- No hand-writing the attestation marker; no marker on a non-green run.
- No uncapped findings dump — ≤5/run, de-duped, real labels.

`--help` prints the level table + guardrails, then stops.

End of /qa. Repair is `/debug`→`/fix`; delivery is `/pr-dev` / `/pr-main`.
