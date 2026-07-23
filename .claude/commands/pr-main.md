---
description: Release-prep for a stable version — confirm dev is green + ahead of master, bump pyproject.toml to X.Y.Z + uv lock, collapse the rc CHANGELOG sections into one dated release entry, run validate_release.py clean, final FAQ pass, confirm the attestation marker exists, then open ONE dev→master PR and STOP. Merging that PR is Nathan's single release gate (→ auto-tag → PyPI). Never merges, tags, releases, or rolls the fleet.
argument-hint: "<X.Y.Z> (prepare + open dev→master PR) | <X.Y.Z> --dry-run | --rc-summary | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Edit Write Skill ToolSearch Bash(git status:*) Bash(git branch:*) Bash(git rev-parse:*) Bash(git symbolic-ref:*) Bash(git log:*) Bash(git diff:*) Bash(git fetch:*) Bash(git checkout:*) Bash(git add:*) Bash(git commit:*) Bash(git push:*) Bash(gh pr create:*) Bash(gh pr view:*) Bash(gh pr list:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(uv run pytest:*) Bash(uv run ruff:*) Bash(uv lock:*) Bash(python3 scripts/validate_release.py:*) Bash(grep:*) Bash(rg:*) Bash(jq:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(ls:*) Bash(cat:*)
---

You are handling `/pr-main`. `/pr-main` prepares a **stable release** and opens
the `dev`→`master` PR — then **STOPS**. Merging that PR is Nathan's single release
gate (→ `auto-tag-on-master.yml` → `release.yml` → PyPI). `/pr-main` does
everything Claude is *allowed* to do and hard-stops at the operator boundary.

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules) and
`.claude/rules/release-discipline.md` (semver, changelog format, FAQ, staging vs
release). Key points:

- **Authority boundary (verified against `release-guard.sh`).** `gh pr create
  --base master` is **allowed** (creating a PR is not a push/merge/tag/release).
  `gh pr merge` to master, `git push master`, `git tag v*`, and `gh release
  create` are **blocked**. So `/pr-main` may OPEN the release PR but may **never**
  merge/tag/release it — and it must not try (don't fight the guard).
- **The version-bump commit lands on `dev`** (pushing to `dev` is allowed;
  `master` is not). The PR is `dev`→`master`.
- **`fleet-rollout.sh` is the operator's step** — `/pr-main` never runs it. The
  attestation marker is surfaced as advisory context in the PR body only.
- **Confirm-gated + idempotent.** Surface the PR title + body and wait for a tap;
  a re-invoked `/pr-main` must not double-open the release PR (check for an
  existing open `dev`→`master` PR first).
- **Untether-mode.** State the confirmation in text and STOP for a reply. Keep
  the report brief.

## Sub-commands

| Form | Action |
|---|---|
| `/pr-main X.Y.Z` | Prepare the stable release + open the `dev`→`master` PR, then STOP |
| `/pr-main X.Y.Z --dry-run` | Prepare + validate + print the would-be PR body; open nothing |
| `/pr-main --rc-summary` | Collapse the rc CHANGELOG sections + validate only; no bump, no PR |
| `/pr-main --help` | Usage, then stop |

## Flow

### M-1. Confirm the release is cuttable

- `git fetch` and confirm **`dev` is green + ahead of `master`** (CI on `dev` is
  the last TestPyPI publish).
- Confirm the intended stable `X.Y.Z` is decided and is a proper stable version
  (no `rc`/`a`/`b`/`dev` suffix — those are skipped by `auto-tag-on-master.yml`).
- If an open `dev`→`master` PR already exists for this version → STOP (idempotent;
  don't double-open).

### M-2. Bump + lock (on `dev`)

- Edit `pyproject.toml` version to the stable `X.Y.Z` (drop any rc suffix).
- `uv lock` to sync the lockfile.
- Commit to `dev` with `chore: release X.Y.Z` (stage explicit paths).

### M-3. Finalise the CHANGELOG

- Collapse the accumulated `rc` entries into ONE `## vX.Y.Z (YYYY-MM-DD)` section
  with `### fixes/changes/breaking/docs/tests` subsections; every entry keeps its
  `[#N](…)` issue link.
- Run `python3 scripts/validate_release.py` until clean (section exists, ISO
  date, issue links present, allowed subsection headings).

### M-4. FAQ final pass

Per `.claude/rules/help-faq.md`, scan the collapsed changelog against
`docs/faq/faq.md`; update any user-visible surface answer that the release
changes. (Edit/Write allowed; the file is gate-protected against `rm`/`mv`/`>`.)

### M-5. Confirm the attestation marker (advisory)

Check that `/qa` wrote the marker for this version:

```bash
ls -la ~/.untether-dev/integration-test-pass-X.Y.Z.json 2>/dev/null && \
  cat ~/.untether-dev/integration-test-pass-X.Y.Z.json | jq .
```

Surface the marker (SHA + tiers + timestamp) in the PR body as advisory context.
`fleet-rollout.sh` verifies the marker against the artifact — that's the
operator's gate, not `/pr-main`'s. If the marker is missing, say so and recommend
`/qa` QA-4 before merge (don't block — but flag it loudly).

### M-6. Open ONE `dev`→`master` PR, then STOP

Push `dev`, draft the release PR body:

```
Release vX.Y.Z

## Changelog
<the collapsed ## vX.Y.Z section>

## Version
pyproject.toml → X.Y.Z · uv.lock synced

## Tests / attestation
- validate_release.py — clean
- attestation: integration-test-pass-X.Y.Z.json (head_sha=…, tiers=…, <ts>)

## Release note
Merging this PR IS the release approval → auto-tag vX.Y.Z → release.yml → PyPI.
Fleet rollout (scripts/fleet-rollout.sh X.Y.Z) is the operator's follow-up step.
```

Surface it, wait for a tap, then `gh pr create --base master`. **STOP** — print
"ready for Nathan to merge (the single release gate)". In `--dry-run`, print the
body and open nothing.

### M-7. Report + hand-off

Brief report: version prepared, changelog collapsed + validated, FAQ pass,
marker status, PR URL (or "dry-run"), and the explicit next step: **Nathan merges
the PR** → auto-tag → PyPI → `scripts/fleet-rollout.sh X.Y.Z` (operator, LOOPS
A3). `/pr-main` is done at the open PR.

## Anti-patterns

- Never attempt the master merge / `git tag` / `gh release create` (blocked —
  don't fight the guard).
- Never run `fleet-rollout.sh` (operator).
- Never open a master PR from a non-`dev` branch.
- Never bump to a stable version while rc integration tests are unattested (flag
  the missing marker; recommend `/qa` QA-4).
- Never `--no-verify`; never `git add -A`.

`--help` prints the sub-command table, then stops.

End of /pr-main. The everyday delivery command is `/pr-dev`; validation is `/qa`.
