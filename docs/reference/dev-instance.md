# Dev Instance

Untether runs two isolated instances on lba-1: **staging** (PyPI/TestPyPI release) and **dev** (local editable source). They use separate Telegram bots, separate configs, and separate state — zero crosstalk.

## How it works

| | Staging | Dev |
|---|---|---|
| **Systemd service** | `untether.service` | `untether-dev.service` |
| **Binary** | `~/.local/bin/untether` (pipx, PyPI wheel) | `/home/nathan/untether/.venv/bin/untether` (editable) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |
| **State files** | `~/.untether/*.json` | `~/.untether-dev/*.json` |
| **Lock file** | `~/.untether/untether.toml.lock` | `~/.untether-dev/untether.toml.lock` |
| **Telegram bot** | `@hetz_lba1_bot` | `@untether_dev_bot` |
| **Source** | PyPI release or TestPyPI rc | Whatever's in `/home/nathan/untether/src/` |

The `UNTETHER_CONFIG_PATH` env var (set in the dev systemd unit) is what directs the dev instance to its own config directory. State and lock files derive their paths from the config file location automatically.

## Why no separate repo or branch?

The dev instance doesn't need its own branch or repo. The separation is at the **runtime** level, not the source level:

- **Staging** runs a PyPI/TestPyPI wheel — changing local source has zero effect on it
- **Dev** runs the local editable install — any code change takes effect on `systemctl --user restart untether-dev`
- You develop on whatever branch you like (master, feature branches, etc.)
- The `~/.untether-dev/` config directory is local infrastructure, not versioned in git

## Quick reference

```bash
# --- Dev instance ---
systemctl --user restart untether-dev     # Pick up code changes
systemctl --user stop untether-dev
journalctl --user -u untether-dev -f      # Tail dev logs

# --- Staging instance ---
systemctl --user restart untether         # Restart (same wheel version)
journalctl --user -u untether -f          # Tail staging logs

# --- Staging: install rc from TestPyPI ---
scripts/staging.sh install X.Y.ZrcN
systemctl --user restart untether

# --- Staging: upgrade after PyPI release ---
scripts/staging.sh reset       # or: pipx upgrade untether
systemctl --user restart untether

# --- Check both ---
systemctl --user status untether untether-dev

# --- Versions ---
/home/nathan/.local/bin/untether --version          # Staging (PyPI/TestPyPI)
/home/nathan/untether/.venv/bin/untether --version   # Dev (local)
```

## Dev workflow

1. Edit code in `/home/nathan/untether/src/`
2. `systemctl --user restart untether-dev`
3. Test via `@untether_dev_bot` in Telegram
4. Run tests: `uv run pytest`
5. When satisfied: commit, push, enter staging

## Staging workflow

After dev testing passes, release candidates go through a staging phase on `@hetz_lba1_bot` before publishing to PyPI. This catches bugs through real-world dogfooding with all chat routes.

```
Dev (local editable)     Staging (TestPyPI rc)           Release (PyPI)
@untether_dev_bot        @hetz_lba1_bot                  (staging bot)

Fix bugs, test locally   Bump to 0.35.0rc1               Bump to 0.35.0
Integration tests        Push master → TestPyPI          Changelog + tag v0.35.0
                         staging.sh install 0.35.0rc1     release.yml → PyPI
                         Dogfood ~1 week                  staging.sh reset → restart
                         Issue watcher catches bugs
                         Fix → 0.35.0rc2 if needed
```

### Enter staging

1. Bump version in `pyproject.toml` to `X.Y.Zrc1` (no changelog entry needed)
2. Run `uv lock` to sync lockfile
3. Commit: `chore: staging X.Y.Zrc1`
4. Push to `master` — CI auto-publishes to TestPyPI
5. Wait for CI to pass
6. Install on staging bot:
   ```bash
   scripts/staging.sh install X.Y.Zrc1
   systemctl --user restart untether
   scripts/healthcheck.sh --version X.Y.Zrc1
   ```

### Fix bugs during staging

1. Fix on a branch, merge to master
2. Bump to `X.Y.Zrc2`, run `uv lock`
3. Commit: `chore: staging X.Y.Zrc2`
4. Push → CI publishes to TestPyPI
5. `scripts/staging.sh install X.Y.Zrc2 && systemctl --user restart untether`

### Promote to release

1. Bump to `X.Y.Z` in `pyproject.toml`
2. Add full changelog entry covering all changes since last stable release
3. Run `uv lock`, commit, tag `vX.Y.Z`, push with tags
4. `release.yml` publishes to PyPI
5. `scripts/staging.sh reset && systemctl --user restart untether`

### Rollback from staging

If a staging rc is too broken:

```bash
scripts/staging.sh rollback
systemctl --user restart untether
```

This reinstalls the last stable PyPI version.

### Conventions

- **rc versions are NOT git-tagged** — avoids triggering `release.yml`
- **No changelog for rc** — changelog is written once for the final release
- **Commit message**: `chore: staging X.Y.ZrcN`
- **Issue watcher** works identically during staging (monitors the same staging service)
- **`validate_release.py`** skips changelog validation for pre-release versions

## Config files

**Dev config** (`~/.untether-dev/untether.toml`): Minimal config with the dev bot token and test chat routes. Edit directly — not version-controlled.

**Dev systemd unit** (`~/.config/systemd/user/untether-dev.service`): Sets `UNTETHER_CONFIG_PATH` and points `ExecStart` at the local `.venv`. Run `systemctl --user daemon-reload` after editing.

## Test project directories

Six test workspaces live under `test-projects/` in the repo (gitignored, not version-controlled):

| Directory | Engine | Dev config route |
|-----------|--------|-----------------|
| `test-projects/test-claude/` | Claude Code | `[projects.claude-test]` |
| `test-projects/test-codex/` | Codex | `[projects.codex-test]` |
| `test-projects/test-opencode/` | OpenCode | `[projects.opencode-test]` |
| `test-projects/test-pi/` | Pi | `[projects.pi-test]` |
| `test-projects/test-gemini/` | Gemini CLI | `[projects.gemini-test]` |
| `test-projects/test-amp/` | AMP | `[projects.amp-test]` |

Each has a `CLAUDE.md` and `.claude/settings.json`. They're throwaway workspaces — agents run here during dev testing so untether source isn't accidentally modified.

### Telegram groups

Each test project has a dedicated Telegram group (all in the `ut-dev` folder):

| Group | Chat ID | Engine |
|-------|---------|--------|
| ut-dev: claude | `-5284581592` | Claude Code |
| ut-dev: codex | `-4929463515` | Codex |
| ut-dev: opencode | `-5200822877` | OpenCode |
| ut-dev: pi | `-5156256333` | Pi |
| ut-dev: gemini | `-5207762142` | Gemini CLI |
| ut-dev: amp | `-5230875989` | AMP |

Main dev chat (private): `8351408485` (direct messages to `@untether_dev_bot`)

### Adding more routes

To add another test route:
1. Create a Telegram group and add `@untether_dev_bot`
2. Get the chat_id from dev logs: `journalctl --user -u untether-dev -f`
3. Add a `[projects.name]` section to `~/.untether-dev/untether.toml`
4. Create a workspace directory under `test-projects/`
5. Restart dev: `systemctl --user restart untether-dev`

## Systemd service configuration

An example service file lives at `contrib/untether.service`. Two settings are
critical for graceful shutdown:

```ini
KillMode=mixed          # SIGTERM main process first, then SIGKILL remaining cgroup
TimeoutStopSec=150      # Give the 120s drain timeout room to complete
```

`KillMode=mixed` sends SIGTERM only to the main Untether process first, allowing
the drain mechanism to gracefully finish active runs. After the main process
exits, systemd sends SIGKILL to all remaining processes in the cgroup — cleaning
up orphaned MCP servers, containers, or other long-lived children instantly.

Other modes have drawbacks:

- `process` — SIGTERM main only, but orphaned children (MCP servers, Podman containers) survive across restarts, accumulating memory
- `control-group` — SIGTERM **all** processes simultaneously, bypassing the drain mechanism entirely and killing active engine sessions (rc=143); long-lived children with restart policies can cause a 150s restart delay

Without `TimeoutStopSec=150`, systemd's default 90s timeout may kill
the process before the 120s drain finishes.

To apply:

```bash
cp contrib/untether.service ~/.config/systemd/user/untether.service
systemctl --user daemon-reload
systemctl --user restart untether
```

The same settings should be applied to `untether-dev.service`.
