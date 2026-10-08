# Post-rollout `/ping` verification playbook

`scripts/fleet-rollout.sh` confirms each host's **service** is active/running (and, since
[#745](https://github.com/littlebearapps/untether/issues/745), that it stays up through a
`scripts/fleet-postcheck.sh` stability window with no restart loop), but a running service
is not proof the **bot answers**. The last-mile check is a `/ping` sweep of all five bots —
expecting `🏓 pong` from each.

A shell script can't do this: the Telegram MCP tools (`send_message`, `get_history`,
…) live **inside Claude Code**, not in a standalone shell. So this is a documented,
Claude-driven step run once after a rollout — not a pure-shell postcheck. (Do **not**
build a shell Telegram client for it; the MCP path already exists and the fleet is
small.)

## When to run

Immediately after `scripts/fleet-rollout.sh <version>` reports all hosts `OK`, and
after `scripts/fleet-status.sh` shows every host on the target version. Version-on-disk
+ service-active + `🏓 pong` together mean the rollout truly landed.

## The five bots

| Host | Bot | Primary chat to ping |
|---|---|---|
| lba-1 | `@hetz_lba1_bot` | the lba-1 staging chat |
| nsd | `@hetz_nsd_bot` | nsd supergroup `-1003953881142` |
| channelo | `@hetz_channelo_bot` | owner DM `8351408485` |
| sl | `@hetz_sl_bot` | owner DM `8351408485` (or group `-5115715467`) |
| mac | `@local_mb_bot` | owner DM `8351408485` |

> Chat IDs are the current known routes (mirrored from `~/.config/monitor/untether-*.toml`
> and the per-host memory files). If one has changed, resolve it with the Telegram MCP
> `resolve_username`/`get_chats` first. These are the **production** bots — distinct from
> the `@untether_dev_bot` chats used for pre-release integration testing.

## The sweep (Claude Code, one pass)

For each bot in the table:

1. `send_message(chat_id=<chat>, message="/ping")`
2. Wait ~3–5s, then `get_history(chat_id=<chat>, limit=3)`
3. **PASS** if the newest bot message starts `🏓 pong — up <uptime>` (e.g. `🏓 pong — up 4m 12s`).
   A freshly restarted host shows a short uptime — a long one means the restart didn't land.
   **FAIL** if there's no reply within ~15s, an error, or a stale/oversized response.

   `/ping` (`src/untether/telegram/commands/ping.py`) carries no version or mode footer — use
   `scripts/fleet-status.sh` for versions. It may add up to two extra lines, both normal:

   - a triggers line when crons/webhooks target that chat — `⏰ triggers: 1 cron (<id>, <schedule>)`
     or `⏰ triggers: 2 crons, 1 webhook`; while triggers are paused it reads
     `⏸ triggers paused: … (suspended)`
   - `⏳ background: N tasks running` while a live Claude run in that chat holds background work
     ([#777](https://github.com/littlebearapps/untether/issues/777))

Report a one-line-per-host result, e.g.:

```
/ping sweep — 0.36.0rc1
  lba-1     🏓 pong   ✓
  nsd       🏓 pong   ✓
  channelo  🏓 pong   ✓
  sl        🏓 pong   ✓
  mac       🏓 pong   ✓
```

Any FAIL → investigate that host (`journalctl --user -u untether` / `scripts/fleet-status.sh
--only <host>`) and, if needed, roll it back: `scripts/fleet-rollback.sh <prev> --only <host>`.

## Optional: record ping verification in the state file

`scripts/fleet-rollout.sh` writes `~/.untether-dev/fleet-rollout-state.json`. After a
clean sweep you can annotate it so `fleet-status.sh`/audits can later see the rollout was
bot-verified, not just service-verified:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/".untether-dev/fleet-rollout-state.json"
s = json.loads(p.read_text())
s["ping_verified"] = True            # set False (or omit) if any host failed the sweep
p.write_text(json.dumps(s, indent=2))
PY
```

This is a convention, not enforced machinery — the sweep result in the run log is the
authoritative record.

## Relationship to other tooling

- `scripts/fleet-rollout.sh` — installs + restarts + confirms **service** state.
- `scripts/fleet-status.sh` — one-shot **version + service** view (no bot check).
- **This playbook** — the **bot-answers** last mile, Claude-driven via Telegram MCP.
- `scripts/healthcheck.sh` — deeper single-host post-deploy check (systemd, version, logs, Bot API).
