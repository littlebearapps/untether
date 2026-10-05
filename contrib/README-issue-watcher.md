# untether-issue-watcher — deployment & sync contract

The `untether-issue-watcher` daemon tails Untether logs and auto-files GitHub
issues (label `auto:error-report`) for tracked structlog events. The **script
itself lives out-of-repo** at `~/.local/bin/untether-issue-watcher` on every
fleet host (lba-1, nsd, channelo, sl, mac); this repo ships only the service
templates:

| File | Platform |
|---|---|
| `untether-issue-watcher.service` | Linux (systemd user unit) |
| `com.littlebearapps.untether-issue-watcher.plist` | macOS (launchd agent) |

## Sync contract with `/monitor` (#639)

The `/monitor` command's shared config (`~/.config/monitor/_shared.toml` on the
invoking host) has a `[signals.already_tracked]` list of patterns the monitor
**skips on the assumption this daemon files them**. Signatures present in that
list but absent from the watcher's `BUG_EVENTS`/`WARNING_EVENTS` sets are
invisible to BOTH systems — exactly the gap that let the `tool_progress`
schema break (#637) go unfiled for 35+ minutes.

**Whenever you edit the watcher's event sets, reconcile the monitor list (and
vice versa):**

1. Every `already_tracked` pattern (except pure noise-excludes such as
   `catalog.refresh_sent`) must appear in the watcher's `BUG_EVENTS` or
   `WARNING_EVENTS`.
2. Deliberate ownership exceptions must be commented in BOTH files. Current
   exception: `progress_edits.stall_detected` is owned by `/monitor` (most
   stall warnings are by-design approval waits, #526/#527 — auto-filing them
   would violate the by-design rule), so it is absent from the watcher AND
   absent from `already_tracked`.
3. Quick drift check (run on any host):

   ```bash
   # Patterns the monitor skips
   python3 - <<'EOF'
   import tomllib, pathlib, re
   cfg = tomllib.loads((pathlib.Path.home() / ".config/monitor/_shared.toml").read_text())
   skip = set(cfg["signals"]["already_tracked"]["patterns"]) - {"catalog.refresh_sent"}
   src = (pathlib.Path.home() / ".local/bin/untether-issue-watcher").read_text()
   missing = {p for p in skip if f'"{p}"' not in src}
   print("DRIFT:", missing) if missing else print("in sync")
   EOF
   ```

## Candidate signatures (not yet in the watcher)

Documented here so the next watcher edit picks them up; add each to the
out-of-repo script's `WARNING_EVENTS` **and** reconcile `already_tracked`
per the contract above.

| Signature | Level | Since | Why |
|---|---|---|---|
| `claude.live_session.close_grace_expired` | WARN | 0.35.5rc12 ([#791](https://github.com/littlebearapps/untether/issues/791)) | A live session didn't exit within 15 s of stdin EOF and had to be signalled (SIGINT, then SIGTERM). Carries a proc snapshot (state, wchan, CPU, children). A clean idle close is no longer quarantined, so this line is the only trace of a slow-exiting CLI; recurring hits point at an upstream exit-path hang worth filing. |
| `control_request.unanswerable` | WARN | 0.35.5rc15 ([#684](https://github.com/littlebearapps/untether/issues/684)) | A Claude control request has been pending past `tool_timeout` with no answerable surface (`reasons`: `no_keyboard`, `no_session_writer`). Detect-only, once per request. Every hit is a UX or registry bug (swallowed keyboard, stale `da:` entry, a background agent's request with no turn to render it). Carries `kind`, `live_idle`, `holds_live_session`. Kill switch `[watchdog] detect_unanswerable_control_requests`. |
| `final.early_delivery_timeout` | WARN | 0.36.0rc2 ([#928](https://github.com/littlebearapps/untether/issues/928)) | A live session's early final took longer than the 60 s bound to hand to Telegram. The answer counts as delivered once handed over, so it is no longer resent at close, but the bound firing means Telegram was badly delayed for that run. Was silent before rc2; any hit is worth a look. |

## msgspec dedup signature (#639)

For `jsonl.msgspec.invalid` events the dedup signature includes the invalid
value + JSONPath parsed from the error message (e.g. `'tool_progress'@$.type`),
so each **novel** schema gap files its own issue instead of collapsing into the
first one ever seen. Other events keep the coarse
`event_name:error_type:engine` signature.

## Deploying watcher script changes

```bash
# From lba-1 (script already updated locally):
for host in nsd channelo sl mac; do
  scp ~/.local/bin/untether-issue-watcher $host:.local/bin/untether-issue-watcher
done
# Restart daemons
systemctl --user restart untether-issue-watcher          # lba-1
for host in nsd channelo sl; do
  ssh $host 'systemctl --user restart untether-issue-watcher'
done
ssh mac 'launchctl kickstart -k gui/$(id -u)/com.littlebearapps.untether-issue-watcher'
```
