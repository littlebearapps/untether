---
description: 8-step deep debugging for Untether — general sweep across fleet logs + open issues, OR targeted investigation of named GitHub issues with hypothesis + verification spec. Project-aware, fleet-aware.
argument-hint: "[sweep [hours=24]] | [issue_number]... | [--issue <n>] | [--help]"
disable-model-invocation: true
allowed-tools: Bash(journalctl:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(gh issue comment:*) Bash(gh issue edit:*) Bash(gh search:*) Bash(gh api:*) Bash(gh label list:*) Bash(gh pr list:*) Bash(gh pr view:*) Bash(ssh:*) Bash(systemctl --user list-timers:*) Bash(systemctl --user status:*) Bash(systemctl --user is-active:*) Bash(pipx list:*) Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(git blame:*) Bash(ls:*) Bash(cat:*) Bash(grep:*) Bash(jq:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(sort:*) Bash(uniq:*) Bash(awk:*) Bash(sed:*) Bash(uv run pytest:*) Bash(uv run ruff:*) Read Glob Grep Skill ToolSearch
---

You are handling the user's `/debug` request. /debug is Untether's 8-step deep
debugging protocol. It runs in one of two modes — **general sweep** (ranked
triage report across fleet logs + open issues, no auto-filing) or **targeted**
(full 8-step rigor per named GitHub issue, posted as an issue comment with a
verification spec).

User input: `$ARGUMENTS`

This command is project-scoped to Untether. It is **not** the generic
`/debug <issue>` shipped in the user's global `~/.claude/commands/debug.md`;
project commands take precedence when invoked from inside `/home/nathan/untether`.

## Companion files (canonical references)

| File | Step covered | Purpose |
|---|---|---|
| `.claude/commands/debug/README.md` | n/a | How the bundle fits together |
| `.claude/commands/debug/step-classify.md` | Step 1 | 15 Untether issue classes + diagnostic hints |
| `.claude/commands/debug/step-evidence.md` | Step 2 | Data-source catalogue (journalctl, structlog, fleet SSH, MCP, state files) |
| `.claude/commands/debug/step-research.md` | Step 3 | Docs, closed issues, upstream engine repos, library docs |
| `.claude/commands/debug/systemic-patterns.md` | Step 4 | 15-20 known Untether patterns + memory-aware exceptions |
| `.claude/commands/debug/step-fix.md` | Step 7 | Implementation checklist (tests, lint, format, changelog, branch model) |
| `.claude/commands/debug/step-verify.md` | Step 8 | Post-fix verification (dev restart, integration tests, attestation, fleet rollout) |
| `.claude/commands/debug/output-template.md` | output | Debug-report-comment and triage-report templates |
| `~/.claude/commands/monitor/severity-rubric.md` | (reused) | Severity buckets and label routing — single source of truth |
| `~/.claude/commands/monitor/signal-categories.md` | (reused) | Bug/enhancement signal taxonomy |

Read the relevant companion file BEFORE executing each step. Do not duplicate
their content here — they are the canonical source.

## Sub-commands

| Form | Action |
|---|---|
| `/debug` (no args) | General sweep, default window = 24 hours |
| `/debug sweep [hours]` | General sweep, custom window |
| `/debug <issue_number> [more numbers...]` | Targeted: full 8-step per issue |
| `/debug --issue <n>` | Targeted: single issue (explicit form) |
| `/debug --help` | Print usage block |

## Parsing

The first whitespace-delimited token decides the sub-command:

1. **empty or `sweep`** → general sweep mode. If `sweep` is followed by a
   positive integer, that's the window in hours; otherwise default to 24.
2. **`--issue <n>`** → targeted, single issue.
3. **`--help`** → print usage and stop.
4. **A bare integer (e.g. `547`)** → targeted; treat as issue number. Multiple
   integers run sequentially, one debug report per issue.
5. **Anything else** → print usage and stop.

If `--help` or arguments are unparseable, print:

```
/debug                        — general sweep, last 24h across fleet
/debug sweep 12               — general sweep, last 12h
/debug 547                    — targeted: full 8-step on issue #547
/debug 547 553 555            — targeted: three issues, sequentially
/debug --issue 547            — equivalent explicit form

Companion files:
  cat .claude/commands/debug/README.md
  cat .claude/commands/debug/systemic-patterns.md
  cat ~/.claude/commands/monitor/severity-rubric.md
```

Then stop.

---

## Sub-command: sweep

Produce a ranked triage report — does **not** file new issues. The
`untether-issue-watcher` daemon and `/monitor` already auto-file from these
log sources; this mode synthesises a human-readable triage view.

### S-1. Gather scope

1. Read `.claude/commands/debug/systemic-patterns.md` in full before scoring
   anything — many "errors" are by-design and must not be escalated.
2. Run, in parallel:
   ```bash
   gh issue list --repo littlebearapps/untether --label "auto:error-report" --state open --json number,title,labels,createdAt,updatedAt
   gh issue list --repo littlebearapps/untether --label "auto:monitor-audit" --state open --json number,title,labels,createdAt,updatedAt
   gh issue list --repo littlebearapps/untether --label "severity:critical" --state open --json number,title,labels,createdAt,updatedAt
   gh issue list --repo littlebearapps/untether --label "severity:major" --state open --json number,title,labels,createdAt,updatedAt
   ```
3. De-duplicate by issue number (an issue may carry multiple labels).

### S-2. Pull fleet error events

For window = `$HOURS` (default 24), in parallel:

**Local (lba-1) — five services:**
```bash
for unit in untether untether-dev untether-demo untether-dev-hf untether-dev-ws; do
  systemctl --user is-active "$unit.service" >/dev/null 2>&1 || continue
  journalctl --user -u "$unit" --since "${HOURS}h ago" --output=cat \
    | grep -E 'level=(error|warning)|"level":\s*"(error|warning)"' \
    > "/tmp/debug-sweep-${unit}-$$.log" || true
done
```

**Remote — four hosts (nsd, channelo, sl, mac):**
```bash
for host in nsd channelo mac; do
  ssh "$host" "journalctl --user -u untether --since '${HOURS}h ago' --output=cat \
    | grep -E 'level=(error|warning)|\"level\":\\s*\"(error|warning)\"'" \
    > "/tmp/debug-sweep-${host}-$$.log" 2>/dev/null || true
done
```

Prefix each line with `[host:unit]` when aggregating. See
`.claude/commands/debug/step-evidence.md` for the full set of structlog
event signatures worth grepping.

### S-3. Group + score

For each open issue from S-1:
1. Look up its referenced files / event sigs in CHANGELOG.md and the issue body.
2. Count matching lines in the aggregated log buffer over the window.
3. Cross-reference against `systemic-patterns.md` — flag if it matches a
   "by-design" exception (don't escalate).
4. Score: severity (from labels) × recency-weighted count × cross-host
   prevalence. Cross-host signals rank higher than single-host.

For each error-event signature in the buffer **not** matched to an open
issue: classify per `step-classify.md`, note it as a candidate for filing
(but do **not** file — flag for the user instead).

### S-4. Emit triage report

Use the `## Triage Report` template in `.claude/commands/debug/output-template.md`.
Print to stdout. The table columns are:

`# | severity | class | systemic_pattern | open_issue | host(s) | count_${HOURS}h | one_liner`

Cap at 25 rows. Below the table, list any unclaimed signatures (max 10) under
"Candidates without an open issue" with the user's recommended next move
(usually: "run `/debug` in targeted mode on the closest matching issue, or
manually file with `gh issue create`").

### S-5. Stop

Do not file issues. Do not comment on issues. The triage report is the entire
deliverable.

---

## Sub-command: targeted (issue numbers passed)

For each issue number in sequence, run the full 8-step protocol. Each issue
produces one Debug Report comment ready to post.

### T-0. Pre-flight per issue

```bash
gh issue view <N> --repo littlebearapps/untether --json number,title,body,labels,assignees,milestone,createdAt,comments
```

Capture the title, body, current labels, and last 5 comments. If the issue
is closed, ask the user whether to re-open or just produce a retrospective
report.

### Step 1 — Classify

Read `.claude/commands/debug/step-classify.md`. Pick the single best class
from the 15-class table. Note any secondary classes if behaviour crosses
boundaries (e.g. a control-channel issue triggered by a config-hot-reload).

### Step 2 — Gather evidence (MCP- and journalctl-first)

Read `.claude/commands/debug/step-evidence.md` and run every relevant data
source listed for the classified type. Specifically:

- **journalctl** for the affected service (lba-1) and across the fleet
  (nsd, channelo, sl, mac) if the issue is fleet-wide. Filter by event signature
  (e.g. `event=subprocess.liveness_stall`) and time window.
- **structlog field extraction** — pull `engine`, `session_id`, `chat_id`,
  `proc_returncode`, `peak_idle`, `tool_name` etc. from matching lines.
- **state files** — `active_progress.json`, `last_update_id.json`,
  `seen.json`, attestation marker files.
- **Telegram chat history** for the 6 dev engine chats if the issue is
  chat-side (verify via `mcp__telegram__get_history`).
- **git log + CHANGELOG** for regressions correlated with recent rc/release
  bumps.

Quote 2-5 verbatim log lines (timestamps + event names + key fields) in the
Debug Report. Truncate at 200 chars per line.

### Step 3 — Research

Read `.claude/commands/debug/step-research.md`. Look up:
- Untether docs in `docs/reference/runners/<engine>/`, `.claude/skills/`,
  `.claude/rules/`.
- Closed issues in the last 90 days: `gh issue list --state closed --search "closed:>=$(date -u -d '90 days ago' +%Y-%m-%d)" --json number,title,closedAt`.
- Upstream engine repo issues if the bug looks like an engine quirk.
- Python ecosystem libraries (anyio, msgspec, structlog) for known issues.

### Step 4 — Cross-reference systemic patterns

Read `.claude/commands/debug/systemic-patterns.md`. Walk the pattern list
top to bottom. For each match:
- If the pattern has a canonical issue, **comment on that issue** with the
  new evidence rather than creating a new one.
- If the pattern is flagged "by-design" (e.g. cron + plan-mode stalls), say
  so clearly in the Debug Report and stop — no fix needed.
- If the pattern is "regression of previously-fixed" — flag as a regression
  with explicit reference to the prior fix commit.

### Step 5 — Form hypothesis

State it clearly in the Debug Report:

```
Hypothesis: <root cause stated as cause→effect>
Supporting evidence: <log lines, file references, doc citations>
Ruled out: <alternative causes investigated and dismissed>
Prior confidence: <0–100%>      ← recorded BEFORE testing
Pre-mortem (if <80%): <if the fix fails in 48h, the most likely reason is...>
```

Untether-specific hypothesis classes commonly missed (read in full):
- PTY master_fd leak across run boundaries (`control-channel.md`)
- callback_data > 64 bytes (Telegram silent drop)
- Restart-required config key edited mid-run, silently warned
- Signal-death loop with auto-continue (rc=143/137 should suppress retry)
- Plan-mode cooldown bypass via rapid-fire ExitPlanMode
- MCP catalog staleness (#365 — detect, optionally refresh)
- Hot-reload race during an active run (TelegramBridgeConfig field copy)
- Outbox deny-glob false-positive (legitimate file matched a deny pattern)
- Ephemeral cleanup missing in `finally` (registry leak)
- Tool-active stall threshold mismatch (MCP 15 min vs local tool 10 min)
- Auto-continue retry exhaustion masking a different root cause
- `_clear_background_handle` racing watchdog read (`#374`, `#333`, `#507` redux)

### Step 6 — Verify hypothesis

Before proposing a fix:

1. Reproduce in dev — write a stub-subprocess test per
   `.claude/rules/testing-conventions.md` (fake CLI script that emits the
   problem JSONL) or live-repro via `@untether_dev_bot`.
2. Confirm doc support — cite specific runner spec / rule / changelog line.
3. Search closed issues for prior fixes: `gh search issues "<key phrase>"
   --repo littlebearapps/untether --state closed`.
4. Cross-check on staging (`@hetz_lba1_bot`) only after dev reproduces — never
   debug directly on staging.
5. **Draft the Verification Spec block** (see template in
   `.claude/commands/debug/output-template.md` — adapted from Scout's 12-hint
   vocabulary but trimmed to Untether sources: `journalctl:event=<sig>`,
   `gh_issues:label=<lbl>`, `pytest:tests/test_<X>.py::test_<Y>`,
   `attestation:integration-test-pass-${VERSION}.json`,
   `telegram_chat:<chat_id>`, `proc_diag:<field>`).

### Step 7 — Fix with verification criteria

Read `.claude/commands/debug/step-fix.md` for the full checklist. Summary:
1. Minimal code change in a `fix/<issue-N>-<slug>` branch.
2. `uv run pytest tests/test_<area>.py -x` (targeted) → `uv run pytest` (full).
3. `uv run ruff check src/` + `uv run ruff format src/ tests/`.
4. CHANGELOG entry under the correct rc/version section with `[#N](https://github.com/littlebearapps/untether/issues/N)` link.
5. Push feature branch. PR to `dev` (never to `master`).
6. **Apply `needs-verification` label**; comment the Debug Report + Verification Spec on the issue; do **not** close.

Hard rules from `.claude/rules/dev-workflow.md` and `release-discipline.md`:
- NEVER push to master. NEVER tag. NEVER merge PRs to master. NEVER `--no-verify`.
- NEVER restart `untether.service` to test code changes — use `untether-dev`.
- If you find yourself needing to restart untether mid-debug to apply a code
  change, you're testing on the wrong service. Stop, check
  `.claude/rules/dev-workflow.md`, route to `untether-dev`.

### Step 8 — Post-fix health check

Read `.claude/commands/debug/step-verify.md`. Summary:
1. Restart `untether-dev` (NEVER `untether` staging).
2. Tail journalctl for the targeted event signature; confirm absence.
3. Run integration tests at the tier required by `release-discipline.md` for
   the change scope (patch/minor/major).
4. Write attestation marker: `scripts/run-integration-tests.sh ${VERSION} --manual ...`.
5. If this is part of an rc release, run `scripts/fleet-rollout.sh ${VERSION}`
   only after Nathan merges the PR.
6. Re-run Step 4 grep on the fresh dev logs to ensure no other systemic
   pattern regressed.

### T-X. Emit Debug Report

Use the `## Debug Report` template in
`.claude/commands/debug/output-template.md`. The user should be able to copy
the comment block straight into `gh issue comment <N>`. Do **not** post the
comment yourself unless the user has explicitly approved this run for
posting (default: print only, do not post).

---

## Hard rules (apply to both modes)

- **Read companion files first.** They are the canonical source for each
  step's detail. Do not rederive them from training data.
- **Memory-aware.** Consult MEMORY.md and the linked feedback memories before
  escalating any signal. Some "errors" are by-design — see `systemic-patterns.md`
  for the curated exceptions list.
- **Never auto-file in sweep mode.** Print recommendations only. The watcher
  daemon and `/monitor` cover auto-filing.
- **Never auto-close in targeted mode.** Apply `needs-verification`; the close
  is a future automation step.
- **Never push to master.** Never merge PRs to master. Never tag. Hooks block
  these — do not attempt workarounds.
- **Never restart staging to test changes.** Use `untether-dev.service`.
- **Fleet awareness.** Probe all four hosts (lba-1 local + nsd + channelo + mac
  via SSH) in sweep mode by default. In targeted mode, probe only the hosts
  relevant to the issue. If you can't SSH to a host, log it as a partial scope
  and continue — never silently drop a host.
- **Stay in the dev branch model.** Feature branch → PR to `dev`. Never feature
  → master directly. Squash-merge to `dev` is allowed; merging to master is
  Nathan's only.

End of /debug command file. See companion files for step detail.
