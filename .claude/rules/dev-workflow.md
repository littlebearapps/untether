---
applies_to: "src/untether/**"
---

# Dev/Production Workflow Rules

## Two instances

| | Staging | Dev |
|---|---|---|
| **Service** | `untether.service` | `untether-dev.service` |
| **Bot** | `@hetz_lba1_bot` | `@untether_dev_bot` |
| **Source** | PyPI wheel or TestPyPI rc | Local editable (`/home/nathan/untether/src/`) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |

## Rules

### NEVER restart staging to test local changes

`systemctl --user restart untether` does NOT pick up local code changes — the staging bot runs a PyPI/TestPyPI wheel. Restarting it during development is always wrong and risks disrupting live chat routes.

The ONLY time to restart staging is after `scripts/staging.sh install` (TestPyPI rc) or `pipx upgrade untether` (PyPI release).

### ALWAYS use the dev service for testing

```bash
systemctl --user restart untether-dev
journalctl --user -u untether-dev -f
```

Test changes via `@untether_dev_bot` in Telegram. The dev service runs the local editable source and picks up code changes on restart.

### Staging upgrade path

```bash
# Install a release candidate from TestPyPI:
scripts/staging.sh install X.Y.ZrcN
systemctl --user restart untether

# Or install a final release from PyPI:
scripts/staging.sh reset    # or: pipx upgrade untether
systemctl --user restart untether
```

### Branch model

- **Feature branches** (`feature/*`, `fix/*`) — PR to `dev`
- **`dev` branch** — integration branch, auto-publishes to TestPyPI on merge
- **`master` branch** — release branch, always matches latest PyPI version
- Feature → `dev` → `master` (never feature → master directly)

### Testing before merge

1. Edit code in `src/`
2. `uv run pytest && uv run ruff check src/`
3. `systemctl --user restart untether-dev`
4. Test via `@untether_dev_bot` — follow `docs/reference/integration-testing.md`
5. When satisfied: commit, push feature branch, create PR to `dev`

### Integration testing before release (MANDATORY)

Before ANY version bump (patch, minor, or major), run the structured integration test suite against `@untether_dev_bot`. See `docs/reference/integration-testing.md` for the full playbook.

| Release type | Required tiers | Time |
|---|---|---|
| **Patch** | Tier 7 (smoke) + Tier 1 (affected engine + Claude) + relevant Tier 6 | ~30 min |
| **Minor** | Tier 7 + Tier 1 (all engines) + Tier 2 (Claude) + relevant Tier 3-4 + Tier 6 + upgrade path | ~75 min |
| **Major** | ALL tiers (1-7), ALL engines, full upgrade path | ~120 min |

**NEVER skip integration testing. NEVER test against staging (`@hetz_lba1_bot`).**

All integration test tiers are fully automatable by Claude Code via Telegram MCP tools (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`, `reply_to_message`, `send_voice`, `send_file`) and the Bash tool (for `journalctl` log inspection, `kill -TERM` SIGTERM tests, FD/zombie checks). After testing, check dev bot logs for warnings/errors and create GitHub issues for any Untether bugs found. See `docs/reference/integration-testing.md` for chat IDs, workflow, and test details.

## Staging workflow

See `docs/reference/dev-instance.md` for the full staging workflow. Quick reference:

```bash
# Enter staging (after dev testing)
scripts/staging.sh install X.Y.ZrcN
systemctl --user restart untether

# Fix bugs during staging
scripts/staging.sh install X.Y.ZrcN+1
systemctl --user restart untether

# Promote to release
scripts/staging.sh reset
systemctl --user restart untether

# Rollback from staging
scripts/staging.sh rollback
systemctl --user restart untether
```

## After changes

```bash
# Restart dev to pick up changes
systemctl --user restart untether-dev

# Never this (staging restart for testing):
# systemctl --user restart untether  ← WRONG during development
```
