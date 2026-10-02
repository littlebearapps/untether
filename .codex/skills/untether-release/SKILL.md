---
name: untether-release
description: Prepare an Untether release. Use when asked to bump the version (rc or stable), update the changelog/spec/readme, or prepare the dev→master release PR. Agents never tag, push to master or publish — Nathan's merge of the dev→master PR is the release gate.
---

# Untether Release

## Overview

Untether ships from one repo: feature branches → PR → `dev` (CI publishes rc
versions to TestPyPI) → PR `dev`→`master` (Nathan merges → `auto-tag-on-master.yml`
creates `vX.Y.Z` → `release.yml` publishes to PyPI). `release.yml` checks that
the tag matches `pyproject.toml`; `src/untether/__init__.py` reads `__version__`
from the installed package metadata, so there is nothing to bump there.

**Agents MUST NOT** push to `master`, merge PRs that target `master`, create
`v*` tags, or run `gh release`. Full rules: `CLAUDE.md` → "Release guard" and
`.claude/rules/release-discipline.md`.

## Workflow

### 1) Choose version + date

- rc: `X.Y.ZrcN` on `dev` (commit `chore: staging X.Y.ZrcN`). rc versions get
  no CHANGELOG section of their own and are never tagged.
- stable: `X.Y.Z` with a release date (YYYY-MM-DD).

### 2) Update changelog (stable)

Add a top section to `CHANGELOG.md`: `## vX.Y.Z (YYYY-MM-DD)`, with subsections
from `fixes`, `changes`, `breaking`, `docs`, `tests`. Every entry links its
issue: `[#N](https://github.com/littlebearapps/untether/issues/N)`. Put
user-facing changes first.

### 3) Bump versions

- `pyproject.toml`: `project.version = "<version>"`
- `uv.lock`: run `uv lock` so the root package version matches.

### 4) Update spec + docs

- `docs/reference/specification.md` header: `# Untether Specification vX.Y.Z [YYYY-MM-DD]`
  (`[unreleased]` while the version is still in rc).
- `README.md` / `docs/faq/faq.md` if the release changes a user-facing surface
  (see `.claude/rules/help-faq.md`).

### 5) Run checks

- `just check` (ruff format check, ruff, ty, pytest)
- `uv lock --check`
- `python3 scripts/validate_release.py`

### 6) Integration tests + attestation

Run the tiers required by `docs/reference/integration-testing.md` against
`@untether_dev_bot`, then `scripts/run-integration-tests.sh <version> --manual`
to write the attestation marker that `scripts/fleet-rollout.sh` requires.

### 7) Hand off

Open the PR to `dev` (rc) or the `dev`→`master` PR (stable) and stop. Tagging,
PyPI publishing and the GitHub Release happen automatically after Nathan merges.
