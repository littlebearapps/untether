---
applies_to: "src/untether/**"
---

# Dev/Production Workflow Rules

## Two instances

| | Production | Dev |
|---|---|---|
| **Service** | `untether.service` | `untether-dev.service` |
| **Bot** | `@hetz_lba1_bot` | `@untether_dev_bot` |
| **Source** | PyPI wheel (frozen) | Local editable (`/home/nathan/untether/src/`) |
| **Config** | `~/.untether/untether.toml` | `~/.untether-dev/untether.toml` |

## Rules

### NEVER restart production to test local changes

`systemctl --user restart untether` does NOT pick up local code changes — production runs a PyPI wheel. Restarting it during development is always wrong and risks disrupting live chat routes.

The ONLY time to restart production is after `pipx upgrade untether` following a PyPI release.

### ALWAYS use the dev service for testing

```bash
systemctl --user restart untether-dev
journalctl --user -u untether-dev -f
```

Test changes via `@untether_dev_bot` in Telegram. The dev service runs the local editable source and picks up code changes on restart.

### Production upgrade path

```bash
# Only after code is merged and released to PyPI:
pipx upgrade untether
systemctl --user restart untether
```

### Testing before merge

1. Edit code in `src/`
2. `uv run pytest && uv run ruff check src/`
3. `systemctl --user restart untether-dev`
4. Test via `@untether_dev_bot`
5. When satisfied: commit, push, release

## After changes

```bash
# Restart dev to pick up changes
systemctl --user restart untether-dev

# Never this (production restart for testing):
# systemctl --user restart untether  ← WRONG during development
```
