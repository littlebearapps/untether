---
paths:
  - "scripts/staging.sh"
  - "scripts/fleet-*.sh"
  - "scripts/healthcheck.sh"
  - "contrib/**"
  - "docs/reference/dev-instance.md"
---

# Dev/Staging Workflow Rules

The always-on core (dev vs staging table, "never restart staging to test local code", "never restart from inside an
active session") lives in `CLAUDE.md`. Full workflow: `docs/reference/dev-instance.md`.

## Staging upgrade path (the ONLY times to restart `untether.service`)

```bash
scripts/staging.sh install X.Y.ZrcN && systemctl --user restart untether   # TestPyPI rc
scripts/staging.sh reset && systemctl --user restart untether              # PyPI stable (or: pipx upgrade untether)
scripts/staging.sh rollback && systemctl --user restart untether           # roll back
```

## Fleet (5 hosts: lba-1 staging, nsd, channelo, sl, mac)

The dev/staging rules apply per host; the fleet scripts wrap them.

```bash
scripts/run-integration-tests.sh X.Y.ZrcN --manual    # attestation marker — the rollout gate
scripts/fleet-rollout.sh X.Y.ZrcN [--dry-run|--only <host>]
scripts/fleet-rollback.sh X.Y.Z-prev --only <host>
```

The marker `~/.untether-dev/integration-test-pass-${VERSION}.json` is required; see `.claude/rules/release-discipline.md`
→ "Fleet rollout (rc and stable)".
