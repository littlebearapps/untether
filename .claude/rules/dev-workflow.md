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

Before ANY version bump, run integration tests against `@untether_dev_bot`. See `docs/reference/integration-testing.md` for the full playbook and `.claude/rules/release-discipline.md` for tier requirements per release type. **NEVER skip integration testing. NEVER test against staging (`@hetz_lba1_bot`).**

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
