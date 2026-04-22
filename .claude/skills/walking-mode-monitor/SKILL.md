---
name: walking-mode-monitor
description: >
  Read Untether staging bot logs and Claude Code session logs, identify
  bugs/errors/UX-friction/inefficiencies/blocked-tool-calls/upstream-issues,
  dedupe against session state and existing GitHub issues, file new
  issues with label auto:walking-mode and full context. MONITORING AND
  ISSUE-FILING ONLY — never fixes, edits source, deploys, restarts, or
  runs any git-mutating command. Typically invoked by
  ~/.local/bin/walking-mode-monitor.sh inside a tmux session on lba-1
  during dog walks; can also be invoked manually for dry-runs.
triggers:
  - walking-mode tick prompt from walking-mode-monitor.sh
  - walking-mode END wrap-up prompt
  - walking-mode dry-run invocation for skill validation
allowed_tools:
  - Read
  - Glob
  - Grep
  - Bash
  - WebFetch
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
- Run `systemctl restart|stop|start|kill|enable|disable`, `launchctl *`, or otherwise touch services.
- Run `pipx`, `uv tool`, `npm`, `pip`, `cargo`, or any package installer.
- Run `sudo` or anything requiring elevation.
- Attempt to fix, refactor, improve, or "just quickly clean up" anything you observe.

**Your ONLY actions:**

1. Read logs (`journalctl`, `~/.untether/untether.log`, `~/.claude/projects/*/*.jsonl`, `~/.untether/stats.json`).
2. Analyse + categorise what you see.
3. Dedupe against state and existing GH issues.
4. File **new** GitHub issues with `gh issue create`, or **comment** on existing issues with new evidence via `gh issue comment`.
5. Update the session state file atomically.
6. Print one terse summary line to stdout.

If you encounter a bug that looks fixable in 30 seconds — **do not fix it**. File the issue and move on. A dedicated debugging session handles fixes.

## Invocation modes

You'll be invoked with one of three prompt shapes. Detect mode by keywords in the starter prompt:

| Prompt contains | Mode | Behaviour |
|---|---|---|
| `"Walking-mode tick <N>"` | Tick | Full per-tick workflow (steps 1–8 below) |
| `"Walking-mode END"` | Wrap-up | Final pass: summary issue + self-review + archive |
| `"Walking-mode dry-run"` | Dry-run | Full workflow, but print issue bodies to stdout instead of calling `gh issue create` |

The starter prompt also carries `state_dir` and `end_epoch`. Always parse those before reading anything else.

## Per-tick workflow

### 1. Load state

```bash
STATE_FILE="$STATE_DIR/session.json"
[ -f "$STATE_FILE" ] || { echo "ERROR: state file missing at $STATE_FILE"; exit 1; }
SESSION_ID=$(jq -r .session_id "$STATE_FILE")
TICK_COUNT=$(jq -r .tick_count "$STATE_FILE")
LAST_TICK_AT=$(jq -r '.last_tick_completed_at // .start_epoch | tonumber' "$STATE_FILE")
END_EPOCH=$(jq -r .end_epoch "$STATE_FILE")
NOW=$(date +%s)
```

Bail early with a printed `time-bounded exit` line if `NOW >= END_EPOCH`.

### 2. Read log sources — delta-only

All paths are **staging**. Do not confuse with `untether-dev`.

```bash
SINCE_ISO=$(date -u -d "@$LAST_TICK_AT" +%Y-%m-%dT%H:%M:%SZ)

# Staging systemd service
journalctl --user -u untether --since "$SINCE_ISO" --output=json --no-pager > "$STATE_DIR/tick-$TICK_COUNT.journal.json" 2>/dev/null || true

# Structured logs — filter by timestamp
awk -v since="$SINCE_ISO" '$1 >= since' ~/.untether/untether.log > "$STATE_DIR/tick-$TICK_COUNT.log"
tail -n 500 ~/.untether/untether.err > "$STATE_DIR/tick-$TICK_COUNT.err"

# Recent Claude Code session JSONL (only those modified in the window)
find ~/.claude/projects -name "*.jsonl" -newer "$STATE_DIR/session.json" -type f 2>/dev/null | head -20
```

If any file's delta is >5 MB, **summarise by sampling** (head + tail + random interior) rather than reading full content. Token budget over value.

### 3. Analyse — categorise each observation

Five categories. Apply in order; the first match wins:

| Category | Signal | Examples |
|---|---|---|
| `bug` | Explicit error/exception; non-zero `rc` with traceback; `structlog level=error` | `handle.worker_failed`, `subprocess.died_without_completion`, `config.read.toml_error` |
| `blocked-tool` | Tool/MCP call that couldn't execute | `mcp: connection refused`, `tool_use denied by permission`, `Bash: command not found`, `catalog.staleness.detected` with persistent disconnection |
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
`normalised_identifier` = first non-timestamp identifier tokens (event name, error type, function name, tool name). Strip timestamps, PIDs, paths that contain timestamps, random IDs.

If signature exists in `state.json["observed_signatures"]` → skip filing; optionally add to `filed_issues_touched` with a comment:
```bash
gh issue comment "$ISSUE_NUMBER" --body "Observed again at $(date -Iseconds) in session $SESSION_ID, tick $TICK_COUNT. Evidence: <5-line excerpt>"
```

**B — Cross-session**: before filing, query open issues:
```bash
gh issue list --label auto:walking-mode --state open --limit 100 --json number,title,body > "$STATE_DIR/open-issues.json"
```
If any existing issue's title or body contains the `normalised_identifier`, add a comment to it rather than filing a new one.

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

**Issue body template** (use this verbatim, filled in):

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

### 6. Update state

Atomic write — temp file + rename:

```bash
jq \
  --argjson new_sigs "$NEW_SIGNATURES_JSON_ARRAY" \
  --argjson new_issues "$NEW_ISSUE_NUMBERS_JSON_ARRAY" \
  --arg last_tick_at "$(date +%s)" \
  --arg last_tick_iso "$(date -Iseconds)" \
  '.tick_count += 1 | .observed_signatures += $new_sigs | .filed_issues += $new_issues | .last_tick_completed_at = ($last_tick_at | tonumber) | .last_tick_completed_iso = $last_tick_iso' \
  "$STATE_FILE" > "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"
```

### 7. Print summary line

One line, no decoration:

```
Tick {N} · filed: {new_count} · dup: {dup_count} · bug/blk/inef/ux/up: {a}/{b}/{c}/{d}/{e}
```

Example:
```
Tick 3 · filed: 2 · dup: 5 · bug/blk/inef/ux/up: 1/0/3/1/2
```

### 8. Clean up tick artifacts

Keep `$STATE_DIR/tick-<N>.*` files for the duration of the walk (useful for wrap-up debugging). Wrap-up archives everything together.

## Wrap-up workflow

Triggered when the starter prompt contains `"Walking-mode END"`. Steps:

1. Load `state.json` one last time.
2. Fetch all filed issues this session via `gh issue view` for each number in `filed_issues`.
3. Group by category, generate a summary.
4. File one **summary issue**:
   ```
   Title: [walking-mode] session {id} — summary ({tick_count} ticks, {new_issues} new, {dup_count} dups)
   Labels: auto:walking-mode, documentation
   Body: grouped summary + links + self-review section (see below)
   ```
5. **Skill self-review** — in the summary body, include a bullet list of:
   - Categories that were hard to decide (borderline bug vs inefficiency, etc.).
   - Signatures that almost collided (dedup near-misses).
   - Any category you skipped entirely (may indicate blind spots).
   - Any patterns noticed but not yet captured in the taxonomy.
6. Print to stdout: `Walk {session_id} ended. Ticks: N. New issues: X. Dups: Y. Summary: #<summary_issue>`.
7. Do NOT archive state here — the outer shell script (`walking-mode-monitor.sh`) handles the `mv` to `archive/`. Leave state files in place.

## Dry-run mode

If prompt contains `"Walking-mode dry-run"`:

- Execute all of steps 1–4 and 6–7 normally.
- For step 5 (file new issues): do NOT call `gh issue create`. Instead, print the full prepared body + labels to stdout under a clearly-marked heading:
  ```
  ====== DRY-RUN: WOULD FILE =====
  Title: ...
  Labels: ...
  Body:
  ...
  ====== END ======
  ```
- Useful for validating categorisation and issue quality before a real walk.

## Flag-only mode (calibration)

For the first 2–3 walks, only `bug` and `blocked-tool` categories should file issues. `inefficiency`, `ux-ui`, and `upstream` should be flagged in the tick summary but NOT trigger `gh issue create`. This avoids spamming the issue tracker while Nathan calibrates signal quality.

Toggle via presence of a file `$STATE_DIR/flag-only-all` (treat all soft categories as flag-only) or the default: soft categories are flag-only unless `$STATE_DIR/file-all-categories` exists.

In flag-only mode, still log the would-be-filed observations to `$STATE_DIR/tick-$TICK_COUNT.flagged.jsonl` for post-walk review.

## Bash command allow/deny (Layer B)

Before any `Bash` call, mentally check against this list. If unsure, don't run it.

**Allowed prefixes** (broad, read-only):
- `journalctl --user -u untether*` (but NOT `--vacuum-*`, `--rotate`, `--flush`)
- `gh issue create|comment|list|view|search|edit`, `gh label list|view`
- `systemctl status untether*` (status only)
- `grep`, `tail`, `head`, `cat`, `jq`, `ls`, `find`, `stat`, `wc`, `awk`, `sort`, `uniq`, `date`, `echo`, `printf`, `cut`, `sed` (for non-destructive filtering — NOT `-i`)
- `mkdir -p` under `~/.untether-walking-mode/` only
- `mv "$STATE_FILE.tmp" "$STATE_FILE"` for atomic state updates (this one specific pattern)
- `sha1sum`, `md5sum` (for signature computation)

**Denied always** (even if the rest of your reasoning makes it sound fine):
- `git push|merge|commit|rebase|reset|clean|rm|checkout` (anything mutating)
- `gh pr merge|create|close|review|ready`, `gh release *`
- `systemctl restart|stop|start|kill|enable|disable|reload`, `launchctl *`, `service *`
- `rm`, `cp`, `mv` on paths OUTSIDE `~/.untether-walking-mode/` (except the `mv $STATE_FILE.tmp $STATE_FILE` pattern above)
- `sudo` (anything)
- `pipx|uv|npm|pip|cargo|brew install|upgrade|remove|uninstall`
- `curl|wget` with `-o`/`--output` writing outside `/tmp/`
- `chmod`, `chown` (not needed for monitoring)
- `sed -i`, `awk -i`, any `--in-place` editing

If a tool call you want to make isn't in the allow-list, **don't run it**. Log what you wanted to do as a note in the flagged log for later human review.

## Error handling

If a Bash call fails:
- Log the failure inline in the tick output.
- Continue with other data sources — don't abort the tick.
- The `timeout 600 claude -p` wrapper in the outer script catches hung processes.

If `gh issue create` fails (rate limit, auth, network):
- Retry once with a 3-second sleep.
- On second failure: append the prepared body to `$STATE_DIR/failed-to-file.jsonl` for wrap-up retry.

If `state.json` is corrupt or missing:
- Do NOT attempt to reconstruct it automatically.
- Print "STATE_CORRUPT — aborting tick, manual review needed" and exit.
- Outer script will continue the loop; next tick will see the same error until Nathan intervenes.

## Performance / cost

- Target: 1–3 min per tick, <$1/tick at Claude Opus rates.
- Per-tick cost cap: the outer `timeout 600 claude ...` kills runaway ticks; any token spend already incurred is unavoidable but bounded.
- Don't load the full `~/.untether/untether.log` each tick — delta only via timestamp filter.
- Don't `gh issue list` more than once per tick; cache the result in `$STATE_DIR/open-issues.json`.
- Don't re-read the skill file between ticks (each cold-start re-reads it anyway).

## What to return

At end of tick/wrap-up/dry-run, emit one final stdout line so the outer shell script can grep/parse it. No progress-printing beyond the one-line summary from step 7 (for tick) or step 6 (for wrap-up).

## Self-check before finishing

Before you `exit`, mentally verify:

- ✅ Did I modify `state.json` atomically (temp + rename)?
- ✅ Did I call `gh issue create` with `--label auto:walking-mode` on every new issue?
- ✅ Did I redact secrets in log excerpts?
- ✅ Did I avoid all tools in Layer A/B denylist?
- ✅ Did I print exactly one summary line to stdout?

If any answer is no — **do not exit**, fix it first, unless fixing would itself require a denied tool. In that case, log the omission in stdout and exit with a noisy note.

---

**One more time, because it matters: you do not fix, edit, deploy, merge, push, or restart anything in this session. You observe and you file. That is all.**
