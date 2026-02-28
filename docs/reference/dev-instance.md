# Dev Instance

Untether runs two isolated instances on lba-1: **production** (PyPI release) and **dev** (local editable source). They use separate Telegram bots, separate configs, and separate state — zero crosstalk.

## How it works

| | Production | Dev |
|---|---|---|
| **Systemd service** | `untether.service` | `untether-dev.service` |
| **Binary** | `~/.local/bin/untether` (pipx, PyPI wheel) | `/home/nathan/untether/.venv/bin/untether` (editable) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |
| **State files** | `~/.untether/*.json` | `~/.untether-dev/*.json` |
| **Lock file** | `~/.untether/untether.toml.lock` | `~/.untether-dev/untether.toml.lock` |
| **Telegram bot** | `@hetz_lba1_bot` | `@untether_dev_bot` |
| **Source** | Frozen PyPI release | Whatever's in `/home/nathan/untether/src/` |

The `UNTETHER_CONFIG_PATH` env var (set in the dev systemd unit) is what directs the dev instance to its own config directory. State and lock files derive their paths from the config file location automatically.

## Why no separate repo or branch?

The dev instance doesn't need its own branch or repo. The separation is at the **runtime** level, not the source level:

- **Production** runs a frozen PyPI wheel — changing local source has zero effect on it
- **Dev** runs the local editable install — any code change takes effect on `systemctl --user restart untether-dev`
- You develop on whatever branch you like (master, feature branches, etc.)
- The `~/.untether-dev/` config directory is local infrastructure, not versioned in git

## Quick reference

```bash
# --- Dev instance ---
systemctl --user restart untether-dev     # Pick up code changes
systemctl --user stop untether-dev
journalctl --user -u untether-dev -f      # Tail dev logs

# --- Production instance ---
systemctl --user restart untether         # Restart (same PyPI version)
journalctl --user -u untether -f          # Tail prod logs

# --- Upgrade production after a PyPI release ---
pipx upgrade untether
systemctl --user restart untether

# --- Check both ---
systemctl --user status untether untether-dev

# --- Versions ---
/home/nathan/.local/bin/untether --version          # Production (PyPI)
/home/nathan/untether/.venv/bin/untether --version   # Dev (local)
```

## Dev workflow

1. Edit code in `/home/nathan/untether/src/`
2. `systemctl --user restart untether-dev`
3. Test via `@untether_dev_bot` in Telegram
4. Run tests: `uv run pytest`
5. When satisfied: commit, push, release to PyPI
6. Upgrade production: `pipx upgrade untether && systemctl --user restart untether`

## Config files

**Dev config** (`~/.untether-dev/untether.toml`): Minimal config with the dev bot token and test chat routes. Edit directly — not version-controlled.

**Dev systemd unit** (`~/.config/systemd/user/untether-dev.service`): Sets `UNTETHER_CONFIG_PATH` and points `ExecStart` at the local `.venv`. Run `systemctl --user daemon-reload` after editing.

## Test project directories

Four test workspaces live under `test-projects/` in the repo (gitignored, not version-controlled):

| Directory | Engine | Dev config route |
|-----------|--------|-----------------|
| `test-projects/test-claude/` | Claude Code | `[projects.claude-test]` |
| `test-projects/test-codex/` | Codex | `[projects.codex-test]` |
| `test-projects/test-opencode/` | OpenCode | `[projects.opencode-test]` |
| `test-projects/test-pi/` | Pi | `[projects.pi-test]` |

Each has a `CLAUDE.md` and `.claude/settings.json`. They're throwaway workspaces — agents run here during dev testing so untether source isn't accidentally modified.

### Telegram groups

Each test project has a dedicated Telegram group (all in the `ut-dev` folder):

| Group | Chat ID | Engine |
|-------|---------|--------|
| ut-dev: claude | `-5284581592` | Claude Code |
| ut-dev: codex | `-4929463515` | Codex |
| ut-dev: opencode | `-5200822877` | OpenCode |
| ut-dev: pi | `-5156256333` | Pi |

Main dev chat (private): `8351408485` (direct messages to `@untether_dev_bot`)

### Adding more routes

To add another test route:
1. Create a Telegram group and add `@untether_dev_bot`
2. Get the chat_id from dev logs: `journalctl --user -u untether-dev -f`
3. Add a `[projects.name]` section to `~/.untether-dev/untether.toml`
4. Create a workspace directory under `test-projects/`
5. Restart dev: `systemctl --user restart untether-dev`
