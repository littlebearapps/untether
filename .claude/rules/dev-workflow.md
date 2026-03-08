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
4. Test via `@untether_dev_bot` — follow `docs/reference/integration-testing.md`
5. When satisfied: commit, push, release

### Integration testing before release (MANDATORY)

Before ANY version bump (patch, minor, or major), run the structured integration test suite against `@untether_dev_bot`. See `docs/reference/integration-testing.md` for the full playbook.

| Release type | Required tiers | Time |
|---|---|---|
| **Patch** | Tier 7 (smoke) + Tier 1 (affected engine + Claude) + relevant Tier 6 | ~30 min |
| **Minor** | Tier 7 + Tier 1 (all engines) + Tier 2 (Claude) + relevant Tier 3-4 + Tier 6 + upgrade path | ~75 min |
| **Major** | ALL tiers (1-7), ALL engines, full upgrade path | ~120 min |

**NEVER skip integration testing. NEVER test against production (`@hetz_lba1_bot`).**

All integration test tiers are fully automatable by Claude Code via Telegram MCP tools (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`, `reply_to_message`, `send_voice`, `send_file`) and the Bash tool (for `journalctl` log inspection, `kill -TERM` SIGTERM tests, FD/zombie checks). After testing, check dev bot logs for warnings/errors and create GitHub issues for any Untether bugs found. See `docs/reference/integration-testing.md` for chat IDs, workflow, and test details.

## After changes

```bash
# Restart dev to pick up changes
systemctl --user restart untether-dev

# Never this (production restart for testing):
# systemctl --user restart untether  ← WRONG during development
```
