# Step 2 — Gather evidence

This is the data-source catalogue for Untether debugging. Run the relevant
sources for the classified issue type (Step 1). Don't skip — each surfaces
different context.

Replace `${HOURS}` with the window (default 24 for sweep, sized to fit the
issue's reported window in targeted mode).

## 2a. journalctl (lba-1 — five services)

Untether runs five services on lba-1:

| Service | Bot | Source |
|---|---|---|
| `untether.service` | `@hetz_lba1_bot` (staging) | PyPI / TestPyPI wheel |
| `untether-dev.service` | `@untether_dev_bot` (dev) | Local editable `/home/nathan/untether/src/` |
| `untether-demo.service` | demo bot | Local editable, demo config |
| `untether-dev-hf.service` | handoff dev | Local editable, handoff config |
| `untether-dev-ws.service` | workspace dev | Local editable, workspace config |

```bash
# Per-service error/warning lines
for unit in untether untether-dev untether-demo untether-dev-hf untether-dev-ws; do
  systemctl --user is-active "$unit.service" >/dev/null 2>&1 || continue
  echo "=== $unit ==="
  journalctl --user -u "$unit" --since "${HOURS}h ago" --output=cat \
    | grep -E 'level=(error|warning)|"level":\s*"(error|warning)"' | head -200
done
```

For targeted mode, restrict to the affected service. Most issues live in
`untether-dev` (development) or `untether` (staging-PyPI); the demo/handoff/ws
services rarely surface novel bugs.

## 2b. Fleet — four remote hosts (nsd, channelo, sl, mac)

The fleet runs one `untether.service` per host (the PyPI wheel). SSH from
lba-1 via the tailnet:

```bash
for host in nsd channelo mac; do
  echo "=== $host ==="
  ssh "$host" "journalctl --user -u untether --since '${HOURS}h ago' --output=cat \
    | grep -E 'level=(error|warning)|\"level\":\\s*\"(error|warning)\"'" 2>/dev/null \
    | head -200 || echo "(host unreachable)"
done
```

If a host is unreachable, **never silently drop it** — log "partial scope: <host>
unreachable" in the report. The `mac` host uses OpenSSH only (Tailscale ACL
forbids `tag:server → user-owned`); ensure `ssh mac` works from lba-1 before
debugging.

## 2c. structlog JSON event signatures

Untether emits structlog JSON in journalctl. Key event names worth grepping:

| Event signature | What it means | Class hint |
|---|---|---|
| `event=session.summary` | Completion event with cost/turns/peak_idle/tool counts | runner-subprocess |
| `event=runner.completed` | Runner finished a run (success or fail) | runner-subprocess |
| `event=handle.incoming` | New turn started for a chat | runner-subprocess |
| `event=handle.worker_failed` | Run handler crashed | runner-subprocess |
| `event=handle.runner_failed` | Runner errored before completion | runner-subprocess |
| `event=subprocess.create.failed` | Couldn't spawn the engine CLI | runner-subprocess |
| `event=subprocess.died_without_completion` | Process exited but no `CompletedEvent` | runner-subprocess |
| `event=subprocess.liveness_stall` | No output for `tool_timeout` seconds | stall-liveness-watchdog |
| `event=subprocess.liveness_kill` | Watchdog killed silent-but-alive subprocess | stall-liveness-watchdog |
| `event=stall_warning` / `stall_detected` | Progressive stall warning fired | stall-liveness-watchdog |
| `event=auto_continue` | Runner auto-resumed after silent exit | auto-continue |
| `event=control_request` / `control_response` | Approval flow events | control-channel |
| `event=control_response.write_failed` | Couldn't write response to stdin (FD leak?) | control-channel |
| `event=ask_question.extraction_failed` | AskUserQuestion parsing failed | control-channel |
| `event=callback.parse_failed` | Inline-keyboard callback couldn't be parsed | telegram-transport |
| `event=cost_budget` | Cost alert (per-run or daily) | cost-budget |
| `event=file_transfer.denied` | Deny-glob blocked an outbox file | outbox-delivery |
| `event=outbox.*` | Outbox scan / send / cleanup events | outbox-delivery |
| `event=voice_transcription` | Voice message processing | telegram-transport |
| `event=config.reload` | Hot-reload fired | config-hot-reload |
| `event=config.read.toml_error` | TOML parse error on reload | config-hot-reload |
| `event=trigger.fired` | Cron or webhook started a run | trigger-cron-webhook |
| `event=catalog_staleness.detected` | MCP catalog drift detected (#365) | runner-subprocess (claude only) |
| `event=catalog.refresh_sent` / `catalog.refresh_failed` | MCP catalog refresh attempt (#365) | runner-subprocess (claude only) |
| `event=session.registry.cleanup_failed` | Session-registry cleanup error | session-resume-lock |
| `event=subprocess.create.failed` | Couldn't spawn engine | runner-subprocess |
| `event=browse.path_escape_attempted` | `/browse` path-traversal block | telegram-transport |
| `event=auth.timeout` | Engine auth timeout | runner-subprocess |
| `event=model.override.failed` / `reasoning.override.failed` | Per-chat override apply failed | config-hot-reload |
| `event=cancel.requested` | User-initiated cancel | at-scheduler-cancel-restart |

Quote 2-5 matching lines verbatim in the Debug Report. Truncate at 200 chars.

## 2d. structlog field extraction

For any matching line, pull these fields when present (JSON or `key=value`):
`engine`, `session_id`, `chat_id`, `thread_id`, `tool_name`, `proc_returncode`,
`peak_idle`, `last_event_type`, `request_id`, `event_count`, `version`,
`host`, `permission_mode`, `model`.

`peak_idle` excessive + `last_event_type=user` is the auto-continue trigger
signature. `last_event_type=result` with `proc_returncode!=0` is a hard error.

## 2e. State files

| Path | What's in it |
|---|---|
| `~/.untether/active_progress.json` | Active progress messages (staging) |
| `~/.untether-dev/active_progress.json` | Active progress messages (dev) |
| `~/.untether/last_update_id.json` | Telegram update_id watermark (staging) |
| `~/.untether-dev/last_update_id.json` | Telegram update_id watermark (dev) |
| `~/.local/state/untether-issue-watcher/seen.json` | Watcher dedup signatures (lba-1 + per-host) |
| `~/.untether-dev/integration-test-pass-${VERSION}.json` | Integration test attestation marker — fleet-rollout gate |
| `<project>/.untether-outbox/` | Agent-initiated file delivery staging dir (per-project) |

```bash
ls -la ~/.untether*/active_progress.json ~/.untether*/last_update_id.json
cat ~/.local/state/untether-issue-watcher/seen.json | jq '. | keys | length'  # how many signatures tracked
ls ~/.untether-dev/integration-test-pass-*.json
```

## 2f. Telegram chat history (dev engine chats)

For chat-side issues, pull last N messages from the relevant dev engine chat
via the Telegram MCP. Chat IDs from `.claude/rules/testing-conventions.md`:

| Engine | Chat ID |
|---|---|
| Claude Code | `5284581592` |
| Codex CLI | `4929463515` |
| OpenCode | `5200822877` |
| Pi | `5156256333` |
| Gemini CLI | `5207762142` |
| AMP CLI | `5230875989` |

```
mcp__telegram__get_history(chat_id=<id>, limit=50)
```

Look for: missing buttons, wrong button order, plan-mode loops, ExitPlanMode
double-prompt, final message truncation, missing footer, voice transcription
errors, AskUserQuestion stuck flows.

## 2g. systemd state

```bash
# Active services and last failure timestamps
systemctl --user list-units 'untether*' --all
systemctl --user is-active untether.service untether-dev.service
systemctl --user list-timers --all
journalctl --user -u untether-issue-watcher --since "${HOURS}h ago" --output=cat | tail -50
```

## 2h. Per-host PyPI version

```bash
pipx list --short | grep untether                       # local lba-1 staging
for host in nsd channelo mac; do
  echo -n "$host: "
  ssh "$host" "pipx list --short 2>/dev/null | grep untether" 2>/dev/null || echo "(unreachable)"
done
```

Version mismatch across hosts during/after a rollout is a fleet-rollout class
issue.

## 2i. Git regression hunt

```bash
# Recent changes to the suspected area
git log --since="$(date -u -d '7 days ago' +%Y-%m-%d)" --oneline -- src/untether/<area>/

# Diff vs last stable tag (latest PyPI release, not rc)
LAST_STABLE=$(git tag --list 'v*' | grep -vE 'rc|alpha|beta' | sort -V | tail -1)
git log --oneline "${LAST_STABLE}..HEAD" -- src/untether/<area>/

# Blame for a specific suspected line
git blame -L <start>,<end> src/untether/<file>.py
```

## 2j. CHANGELOG sweep

```bash
# Find changelog entries mentioning the area
grep -n -E "<area>|#<issue>" CHANGELOG.md | head -20

# Check the active rc/release header range
head -100 CHANGELOG.md
```

## 2k. GitHub issue history

```bash
# Open issues mentioning this signature
gh issue list --repo littlebearapps/untether --search "<keyword>" --state all --limit 20 --json number,title,state,labels,closedAt

# Closed-in-last-90d (regression check)
SINCE=$(date -u -d '90 days ago' +%Y-%m-%d)
gh issue list --repo littlebearapps/untether --search "closed:>=${SINCE}" --state closed --json number,title,closedAt
```

## 2l. Pytest reproduction

For most Untether bugs, the fastest hypothesis falsifier is a stub-subprocess
test. Existing patterns to mirror live in `tests/test_*_runner.py`,
`test_claude_control.py`, `test_exec_bridge.py`. Run targeted first:

```bash
uv run pytest tests/test_<area>.py -x -v
```

Then full suite once the targeted test passes:

```bash
uv run pytest
```

## 2m. Process diagnostics (live or recent)

For stall investigations, check `/proc` snapshots captured by `utils/proc_diag.py`:

```bash
# In journalctl, look for proc_diag JSON blobs
journalctl --user -u untether-dev --since "${HOURS}h ago" --output=cat \
  | grep -A 1 'proc_diag' | head -40
```

Key fields: `cpu_active`, `rss_mb`, `tcp_open`, `fds`, `children`,
`tool_name`. `cpu_active=None` means the diag couldn't read /proc — usually
permissions or zombie state.

## What NOT to gather

- **Source code dumps.** Read targeted files (Read tool, line ranges). Never
  cat whole modules.
- **Full journalctl spans.** Always pipe to `grep` first; an unfiltered
  span can be hundreds of MB.
- **Bot tokens, API keys.** Never include credentials in the Debug Report.
  Sanitise log lines if a token appears.

## Redaction pass (MANDATORY before any external output)

Evidence gathered here (journalctl, structlog, fleet SSH, state files, Telegram
history) routinely contains secrets and private content. **Before pasting any
evidence into a GitHub issue/PR comment, a Telegram message, or a report that
leaves this session, scrub it.** Redaction is not optional and not best-effort —
if you cannot confidently scrub a line, drop it and describe it instead.

Scrub these classes (replace the value with `‹redacted:token›`, `‹redacted:key›`,
etc. — keep enough shape to be useful, never the value):

| Class | What it looks like | Action |
|---|---|---|
| Telegram bot token | `\d{8,10}:[A-Za-z0-9_-]{35}` | Replace whole token → `‹redacted:bot-token›` |
| API keys | `gsk_…` (Groq), `sk-ant-…` (Anthropic), `sk-…` (OpenAI), `AIza…` (Google), `xoxb-…` (Slack), bearer/HMAC secrets | Replace → `‹redacted:key›` |
| Env / credential values | anything sourced from `.envrc`, `printenv`, `bws`, `BWS_ACCESS_TOKEN`, `*_API_KEY`, `*_TOKEN`, `*_SECRET` | Replace value → `‹redacted:env›` |
| Private chat content | verbatim user/agent message bodies from `mcp__telegram__get_history` or `chat_id` payloads | **Summarise, never quote** beyond the minimum needed to show the bug; never paste a user's message text into a public issue |
| Fleet/network identifiers | tailnet FQDNs (`*.tail129742.ts.net`), tailnet/public IPs, Tailscale service-token values, `NOTIFY_SOCKET` paths | Replace → `‹redacted:host›` (the friendly aliases `nsd`/`channelo`/`sl`/`mac`/`lba-1` are fine to keep — they carry no secret) |
| Absolute home paths in others' contexts | `/home/<other-user>/…` | Keep `/home/nathan/…` (public in this repo); redact any other operator's paths |

A quick self-check before posting: grep your drafted evidence for `token`,
`key`, `secret`, `gsk_`, `sk-`, `Bearer `, `:AA` (Telegram token infix), and
`ts.net`. If any hit is a live value, it must be redacted.

## Output-size bound (MANDATORY)

Evidence dumps must stay compact — an issue comment or Telegram message that
pastes hundreds of raw log lines is unreadable and risks leaking un-reviewed
secrets. Bounds:

- **Per line:** truncate at 200 chars (already applied when quoting signatures).
- **Per evidence block:** ≤ 15 quoted lines. If a source has more, quote the 2–5
  most diagnostic lines and summarise the rest as
  `… (+N more matching lines over ${HOURS}h; counts: <sig>×N …)`.
- **Per report:** the total pasted evidence across all sources should stay under
  ~2 KB. Prefer counts, signatures, and field extractions over raw dumps —
  the point is the *diagnostic shape*, not the transcript.
- Aggregation temp files (`/tmp/debug-sweep-*.log`) are working scratch only —
  never attach them to an issue; extract, redact, bound, then discard.

## Paused mid-investigation?

If the investigation is genuinely blocked or handed off before a conclusion,
capture resumable stop-state rather than losing context — see `/handover`
(Loop L9; state-derived, not memory-derived). Routine completion needs no
handover.
