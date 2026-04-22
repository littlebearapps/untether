---
name: walking-mode-monitor
description: >
  Read Untether staging bot logs and Claude Code session logs during a
  user-initiated monitoring window (typical: dog walks, 60–120 min).
  Identify bugs, errors, blocked tool calls, UX/UI friction,
  inefficiencies, and upstream issues. Dedupe against session state and
  existing GitHub issues. File new issues with label `auto:walking-mode`
  and full context. Self-pace via ScheduleWakeup between ticks; call
  wrap-up on the final tick. MONITORING AND ISSUE-FILING ONLY — never
  fixes, edits source, deploys, restarts, or runs any git-mutating
  command. Invoked via `claude /loop` on lba-1 inside a tmux session.
triggers:
  - user says "walking-mode" with a duration (e.g. "start walking-mode for 90 min")
  - user says "start walking mode"
  - invoked via `claude /loop "walking-mode ..."`
  - dry-run test of the skill ("walking-mode dry-run")
allowed_tools:
  - Read
  - Glob
  - Grep
  - Bash
  - WebFetch
  - ScheduleWakeup
disallowed_tools:
  - Edit
  - Write
  - NotebookEdit
  - Task
---

# Walking-mode monitor

## INVARIANT — read this every invocation

**This is a MONITORING SESSION. You do NOT:**

- Edit, Write, or create any source file.
- Run `git push`, `git commit`, `git merge`, `git rebase`, `git reset`, `git clean`, `git rm`, or any mutating git command.
- Run `gh pr merge`, `gh pr create`, `gh pr close`, `gh release *`.
- Run `systemctl restart|stop|start|kill|enable|disable`, `launchctl *`.
- Run `pipx`, `uv tool`, `npm`, `pip`, `cargo`, or any package installer.
- Run `sudo` or anything requiring elevation.
- Attempt to fix, refactor, improve, or "just quickly clean up" anything you observe.

**Your ONLY actions:**

1. Read logs (`journalctl`, `~/.untether/untether.log`, `~/.untether/untether.err`, `~/.claude/projects/*/*.jsonl`, `~/.untether/stats.json`).
2. Analyse + categorise what you see.
3. Dedupe against state and existing GH issues.
4. File **new** GitHub issues with `gh issue create`, or **comment** on existing issues with new evidence via `gh issue comment`.
5. Update the session state file atomically.
6. Schedule the next tick via `ScheduleWakeup` (or don't, if the walk is over).
7. Print one terse summary line to stdout.

If you encounter a bug that looks fixable in 30 seconds — **do not fix it**. File the issue and move on. A dedicated debugging session handles fixes.

## Invocation

```bash
# lba-1 (VPS), typical 90-min walk, inside a persistent tmux session:
ssh lba-1
cd ~/untether
tmux new -s walk \
  'claude /loop "start walking-mode for 90 min. Invoke skill walking-mode-monitor."'

# Detach with Ctrl-b d. Reattach: tmux attach -t walk. Force stop: tmux kill-session -t walk.
```

`/loop` is Claude Code's built-in self-pacing loop primitive. This skill calls `ScheduleWakeup(delaySeconds=900, prompt=<next-tick prompt>)` each tick; when the walk is over, it **does not** call `ScheduleWakeup` and the loop exits naturally.

No external shell scheduler is needed.

## Invocation modes

Detect mode by keywords + state:

| Prompt shape + state | Mode | Behaviour |
|---|---|---|
| First invocation (no active state dir) with a duration phrase like "for 90 min" | **Bootstrap** | Create state dir + `session.json` with `end_epoch`; run one tick; call `ScheduleWakeup(900s)`. |
| Subsequent ticks ("walking-mode tick" or similar), state dir exists, `now < end_epoch` | **Tick** | Full per-tick workflow (steps 1–8 below); call `ScheduleWakeup(900s)` at end. |
| State dir exists, `now >= end_epoch` | **Wrap-up** | Final pass: summary issue + self-review + archive; do NOT call ScheduleWakeup. |
| Prompt contains "walking-mode dry-run" | **Dry-run** | Full workflow, but print prepared issue bodies to stdout instead of calling `gh issue create`. Do not call ScheduleWakeup. |
| Prompt contains "walking-mode stop" or user-initiated stop | **Manual stop** | Run wrap-up immediately; do not call ScheduleWakeup. |

## Bootstrap (first invocation only)

If `~/.untether-walking-mode/` has no unfinished session directory (directories other than `archive/`):

1. Refuse to start if there IS an unfinished session — print `ERROR: unfinished session at <path>. Move to archive/ or delete, then retry.` and exit without calling ScheduleWakeup.
2. Parse duration from the prompt. Default 60 min if not specified. Clamp 15–180 min.
3. Generate `SESSION_ID=$(date +%Y%m%d-%H%M%S)`.
4. Create `~/.untether-walking-mode/$SESSION_ID/`.
5. Write `session.json`:
   ```json
   {
     "session_id": "<id>",
     "host": "<hostname>",
     "start_epoch": <now>,
     "end_epoch": <now + duration_min*60>,
     "interval_sec": 900,
     "tick_count": 0,
     "observed_signatures": [],
     "filed_issues": []
   }
   ```
6. Print a banner to stdout:
   ```
   Walking-mode <session_id> — start <start_iso>, end <end_iso>, interval 15m.
   Scope: MONITORING ONLY. No fixes, edits, deploys, restarts.
   ```
7. Proceed to the **Tick** workflow immediately (tick 1).

## Per-tick workflow

### 1. Load state

```bash
STATE_DIR=$(ls -dt ~/.untether-walking-mode/*/ 2>/dev/null | grep -v archive | head -1)
STATE_FILE="$STATE_DIR/session.json"
[ -f "$STATE_FILE" ] || { echo "ERROR: no active session state"; exit 1; }
SESSION_ID=$(jq -r .session_id "$STATE_FILE")
TICK_COUNT=$(jq -r .tick_count "$STATE_FILE")
END_EPOCH=$(jq -r .end_epoch "$STATE_FILE")
LAST_TICK_AT=$(jq -r '.last_tick_completed_at // .start_epoch' "$STATE_FILE")
NOW=$(date +%s)
```

If `NOW >= END_EPOCH`, skip straight to **Wrap-up** (do NOT run a data-collection tick — the walk is over).

### 2. Read log sources — delta-only

All paths below are **staging** (`untether.service` via `journalctl --user -u untether` on lba-1). Do not confuse with `untether-dev`.

```bash
SINCE_ISO=$(date -u -d "@$LAST_TICK_AT" +%Y-%m-%dT%H:%M:%SZ)

# Staging systemd service logs
journalctl --user -u untether --since "$SINCE_ISO" --output=json --no-pager \
  > "$STATE_DIR/tick-$TICK_COUNT.journal.json" 2>/dev/null || true

# Structured untether.log — filter by timestamp
awk -v since="$SINCE_ISO" '$1 >= since' ~/.untether/untether.log \
  > "$STATE_DIR/tick-$TICK_COUNT.log"

# stderr tail
tail -n 500 ~/.untether/untether.err > "$STATE_DIR/tick-$TICK_COUNT.err"

# Claude Code session JSONLs modified this window
find ~/.claude/projects -name "*.jsonl" -newer "$STATE_DIR/session.json" -type f 2>/dev/null | head -20
```

If any file's delta is >5 MB, **summarise by sampling** (head + tail + random interior) rather than reading full content. Token budget over completeness.

### 3. Analyse — categorise each observation

Five categories. Apply in order; the first match wins:

| Category | Signal | Examples |
|---|---|---|
| `bug` | Explicit error/exception; non-zero `rc` with traceback; `structlog level=error` | `handle.worker_failed`, `subprocess.died_without_completion`, `config.read.toml_error` |
| `blocked-tool` | Tool/MCP call that couldn't execute | `mcp: connection refused`, `tool_use denied by permission`, `Bash: command not found`, persistent `catalog.staleness.detected` |
| `inefficiency` | Works but wastes time/tokens/resources | `liveness_stall` warnings without cancel, retry loops, excessive stream idle, hot-reload fires >N times/min |
| `ux-ui` | User-facing artefact that's confusing | Truncated messages missing key info, orphaned ephemeral messages, wrong emoji, missing meta footer, outbox file empty, unresponsive inline buttons |
| `upstream` | Clearly upstream fault | Claude Code CLI traceback without Untether code in stack, model returns garbage, Telegram 5xx, GitHub API outage |

**Note even ambiguous cases.** Rule: if you'd want this triaged later, file it. Better a small amount of extra triage work than a missed signal.

### 4. Dedupe

Two layers of dedup:

**A — Session signatures** (state file):
```
signature = sha1(f"{category}|{normalised_identifier}|{engine}")
```
`normalised_identifier` = first non-timestamp identifier tokens (event name, error type, function name, tool name). Strip timestamps, PIDs, paths containing timestamps, random IDs.

If signature exists in `state.json["observed_signatures"]` → skip filing; optionally comment on the existing issue with new timestamp + 5-line evidence.

**B — Cross-session**: before filing, query open issues:
```bash
gh issue list --label auto:walking-mode --state open --limit 100 --json number,title,body \
  > "$STATE_DIR/open-issues.json"
```

If any existing issue's title or body contains the `normalised_identifier`, comment on it rather than filing a new one.

Also cross-check `auto:error-report` (the `untether-issue-watcher` label) — dedup across both.

### 5. File new issues

For each unmatched observation, call:

```bash
gh issue create \
  --title "[walking-mode] <engine>: <one-line summary>" \
  --label "auto:walking-mode" \
  --label "<category-label>" \
  --label "<engine-label-if-known>" \
  --body "$BODY"
```

Where `<category-label>` maps: `bug→bug`, `blocked-tool→bug`, `inefficiency→enhancement`, `ux-ui→enhancement`, `upstream→bug` (plus an `upstream` text note in the body).

**Issue body template**:

```markdown
> Auto-filed from walking-mode session `{session_id}` (tick {tick_count}). No investigation performed — triage and debug in a dedicated session.

**Category:** {category}
**Engine:** {engine or "unknown"}
**First observed:** {iso_timestamp}
**Source:** {journalctl | untether.log | untether.err | ~/.claude/projects/... | other}

### Summary

{one-paragraph description of what was observed, max 4 lines}

### Log excerpt

```
{top 30 lines of log context, sensitive tokens redacted — see redaction rules below}
```

### Related

- Possibly related open issues: {links from cross-session dedup check, or "none found"}
- Recent relevant commits: {git log --oneline -5 -- src/untether/<likely-area> — only if obvious, else omit}

### Triage notes

- [ ] Reproduce and classify severity.
- [ ] Confirm in-scope vs upstream.
- [ ] Link PR or close as wontfix/dup.
```

**Redaction rules** applied to log excerpts:
- Replace `\b[0-9]{10}:[A-Za-z0-9_-]{20,}\b` (Telegram bot tokens) with `<TELEGRAM_TOKEN_REDACTED>`.
- Replace `\bgsk_[A-Za-z0-9]+\b` (Groq keys) with `<GROQ_KEY_REDACTED>`.
- Replace `\bsk-[A-Za-z0-9]{20,}\b` (generic secret prefixes) with `<SECRET_REDACTED>`.
- Truncate any line longer than 200 chars with `… [truncated]`.

### 6. Update state (atomic)

Temp file + rename:

```bash
jq \
  --argjson new_sigs "$NEW_SIGNATURES_JSON_ARRAY" \
  --argjson new_issues "$NEW_ISSUE_NUMBERS_JSON_ARRAY" \
  --arg last_tick_at "$(date +%s)" \
  --arg last_tick_iso "$(date -Iseconds)" \
  '.tick_count += 1
   | .observed_signatures += $new_sigs
   | .filed_issues += $new_issues
   | .last_tick_completed_at = ($last_tick_at | tonumber)
   | .last_tick_completed_iso = $last_tick_iso' \
  "$STATE_FILE" > "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"
```

### 7. Schedule next tick (or not)

```bash
NOW=$(date +%s)
TIME_REMAINING=$((END_EPOCH - NOW))
```

**Three cases:**

- **`TIME_REMAINING > 1200`** (more than 20 min left): call `ScheduleWakeup(delaySeconds=900, reason="walking-mode tick N+1", prompt="walking-mode tick. Invoke skill walking-mode-monitor.")`. The loop continues.
- **`900 < TIME_REMAINING <= 1200`** (between 15 and 20 min left): call `ScheduleWakeup(delaySeconds=TIME_REMAINING, reason="final tick = wrap-up")` so the last wake coincides with end time. Next invocation will see `NOW >= END_EPOCH` and run wrap-up.
- **`TIME_REMAINING <= 900`** (less than 15 min left): skip ScheduleWakeup; run **Wrap-up** immediately after the state update.

### 8. Print summary line

One line, no decoration:

```
Tick {N} · filed: {new_count} · dup: {dup_count} · bug/blk/inef/ux/up: {a}/{b}/{c}/{d}/{e}
```

Example:
```
Tick 3 · filed: 2 · dup: 5 · bug/blk/inef/ux/up: 1/0/3/1/2
```

## Wrap-up workflow

Triggered when **any** of:

- Starter prompt contains "walking-mode stop" or "walking-mode END" or equivalent.
- `NOW >= END_EPOCH` at step 1.
- Step 7 flow chose the "less than 15 min left" branch.

Steps:

1. Load `state.json` one last time.
2. For each number in `filed_issues`, `gh issue view <n> --json number,title,labels,url` and collect.
3. Group by category; generate a grouped summary.
4. File **one summary issue**:
   ```
   Title: [walking-mode] session {id} — summary ({tick_count} ticks, {new_issues} new, {dup_count} dups)
   Labels: auto:walking-mode, documentation
   Body: grouped summary + links + self-review section (below)
   ```
5. **Skill self-review** — in the summary body, include a bullet list of:
   - Categories that were hard to decide (borderline bug vs inefficiency, etc.).
   - Signatures that almost collided (dedup near-misses).
   - Any category you skipped entirely (may indicate blind spots).
   - Any patterns noticed but not yet captured in the taxonomy.
6. Archive state: `mv "$STATE_DIR" ~/.untether-walking-mode/archive/<session-id>`.
7. Print `Walk {session_id} ended. Ticks: N. New issues: X. Dups: Y. Summary: #<summary_issue>`.
8. **Do NOT call ScheduleWakeup.** The `/loop` exits naturally.

## Dry-run mode

If prompt contains `"walking-mode dry-run"`:

- Execute all of steps 1–4 and 6–7 normally.
- For step 5 (file new issues): do NOT call `gh issue create`. Print the full prepared body + labels to stdout under a marked heading:
  ```
  ====== DRY-RUN: WOULD FILE =====
  Title: ...
  Labels: ...
  Body:
  ...
  ====== END ======
  ```
- Do NOT call `ScheduleWakeup`. Exit after one tick.

Useful for validating categorisation and issue quality before a real walk.

## Flag-only mode (calibration)

For the first 2–3 walks, only `bug` and `blocked-tool` categories should file issues. `inefficiency`, `ux-ui`, and `upstream` should be flagged in the tick summary but NOT trigger `gh issue create`. This avoids spamming the issue tracker while calibrating signal quality.

Toggle via marker files:
- `$STATE_DIR/flag-only-all` (all soft categories flag-only — default for first walks)
- `$STATE_DIR/file-all-categories` (promote all to file-issue — after calibration)

In flag-only mode, still log would-be-filed observations to `$STATE_DIR/tick-$TICK_COUNT.flagged.jsonl` for post-walk review.

## Bash command allow/deny (Layer B)

Before any `Bash` call, mentally check against this list. If unsure, don't run it.

**Allowed prefixes** (broad, read-only):
- `journalctl --user -u untether*` (but NOT `--vacuum-*`, `--rotate`, `--flush`)
- `gh issue create|comment|list|view|search|edit`, `gh label list|view`
- `systemctl status untether*` (status only)
- `grep`, `tail`, `head`, `cat`, `jq`, `ls`, `find`, `stat`, `wc`, `awk`, `sort`, `uniq`, `date`, `echo`, `printf`, `cut`, `sed` (non-destructive only — NO `-i`)
- `mkdir -p` under `~/.untether-walking-mode/` only
- `mv "$STATE_FILE.tmp" "$STATE_FILE"` for atomic state updates
- `mv <state_dir> ~/.untether-walking-mode/archive/<id>` on wrap-up only
- `sha1sum`, `md5sum`

**Denied always** (even if the rest of your reasoning makes it sound fine):
- `git push|merge|commit|rebase|reset|clean|rm|checkout` (anything mutating)
- `gh pr merge|create|close|review|ready`, `gh release *`
- `systemctl restart|stop|start|kill|enable|disable|reload`, `launchctl *`, `service *`
- `rm`, `cp`, `mv` on paths OUTSIDE `~/.untether-walking-mode/` (except the state-update pattern and the wrap-up archive pattern above)
- `sudo` (anything)
- `pipx|uv|npm|pip|cargo|brew install|upgrade|remove|uninstall`
- `curl|wget` with `-o`/`--output` writing outside `/tmp/`
- `chmod`, `chown`
- `sed -i`, `awk -i`, any `--in-place` editing

If a tool call you want to make isn't in the allow-list, **don't run it**. Log what you wanted in the flagged log for human review.

## Error handling

- **Bash call fails**: log inline, continue with other data sources — don't abort the tick.
- **`gh issue create` fails** (rate limit / auth / network): retry once with 3s sleep; on second failure append to `$STATE_DIR/failed-to-file.jsonl` for wrap-up retry.
- **`state.json` corrupt or missing**: do NOT reconstruct. Print `STATE_CORRUPT — aborting tick, manual review needed` and DO NOT call ScheduleWakeup. The loop ends.

## Performance / cost

- Target: 1–3 min per tick at Claude Opus rates (~$0.50–$1 per tick).
- Don't load full `~/.untether/untether.log` each tick — delta only via timestamp filter.
- `gh issue list` once per tick, cached to `$STATE_DIR/open-issues.json`.
- Skill body reloads fresh each cold-start tick — don't panic about re-reading it.

## Self-check before finishing (each tick)

Before you call `ScheduleWakeup` (or exit on wrap-up), mentally verify:

- ✅ Did I modify `state.json` atomically (temp + rename)?
- ✅ Did I call `gh issue create` with `--label auto:walking-mode` on every new issue?
- ✅ Did I redact secrets in log excerpts?
- ✅ Did I avoid all tools in Layer A/B denylist?
- ✅ Did I print exactly one summary line to stdout?
- ✅ For non-final ticks: am I calling `ScheduleWakeup` with the correct delay and a next-tick prompt?
- ✅ For the final tick (wrap-up): am I NOT calling `ScheduleWakeup`?

If any answer is no — fix before exiting. If fixing would require a denied tool, log the omission and exit noisily.

---

**One more time, because it matters: you do not fix, edit, deploy, merge, push, or restart anything in this session. You observe, you file, you ScheduleWakeup (or don't). That is all.**
