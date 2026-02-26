---
name: release-coordination
description: >
  Release workflow for Untether — issue audit, version decisions,
  changelog drafting, pre-release validation, tagging, and post-release
  verification. Use when preparing a release or debugging release failures.
triggers:
  - preparing a release or version bump
  - cutting a patch or minor release
  - drafting changelog entries
  - auditing issues before a release
  - debugging a failed release or rollback
  - tagging and publishing to PyPI
---

# Release Coordination

Step-by-step release workflow for Untether. Covers the full lifecycle from issue audit through PyPI publishing and post-release verification.

## Key files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package version (`version = "X.Y.Z"`) |
| `CHANGELOG.md` | Release notes with issue links |
| `uv.lock` | Locked dependency versions |
| `.github/workflows/release.yml` | Tag-triggered PyPI publish (OIDC trusted publishing) |
| `.github/workflows/ci.yml` | PR/push CI (format, lint, ty, pytest, build, lockfile, audit, bandit, docs) |
| `.claude/rules/release-discipline.md` | Auto-loaded rule enforcing issue/changelog discipline |

## Release workflow phases

```
1. Issue audit  →  2. Version decision  →  3. Changelog  →  4. Validate  →  5. Tag & publish
```

All five phases happen in a single branch (typically `master` for patches, `feature/*` for minors). The CI release pipeline triggers on `v*` tags pushed to `master`.

## Phase 1: Issue audit

Find commits since the last release that lack GitHub issues.

```bash
# Find the last release tag
LAST_TAG=$(git describe --tags --abbrev=0)

# List commits since last tag
git log --oneline "$LAST_TAG"..HEAD

# List open issues
direnv exec . gh issue list --state open

# List recently closed issues
direnv exec . gh issue list --state closed --limit 20
```

**For each commit without a corresponding issue:**

1. Create an issue with: title, description, impact, affected files
2. Label it: `bug`, `enhancement`, or `documentation`
3. If already fixed, close immediately with a comment referencing the commit/PR

```bash
direnv exec . gh issue create --title "..." --label "bug" --body "..."
direnv exec . gh issue close N --comment "Fixed in <commit-or-PR>"
```

## Phase 2: Version decision

Analyse commits since the last tag and determine the version bump:

| Bump | When | Examples |
|------|------|---------|
| **Patch** (0.23.x) | Bug fixes only, schema additions for new upstream events, dependency updates | macOS credentials fix, rate_limit_event schema |
| **Minor** (0.x.0) | New features, new commands, new engine support, config additions | `/browse` command, Pi runner, cost tracking |
| **Major** (x.0.0) | Breaking changes to config format, runner protocol, or public API | Remove `untether.bridge`, change TOML schema |

**Decision rule**: If ANY commit is breaking → major. If ANY commit adds features → minor. Otherwise → patch.

## Phase 3: Changelog drafting

### Format

```markdown
## vX.Y.Z (YYYY-MM-DD)

### fixes

- description [#N](https://github.com/littlebearapps/untether/issues/N)
  - implementation detail (no issue link needed on sub-bullets)

### changes

- description [#N](https://github.com/littlebearapps/untether/issues/N)

### breaking

- description [#N](https://github.com/littlebearapps/untether/issues/N)

### docs

- description [#N](https://github.com/littlebearapps/untether/issues/N)

### tests

- description [#N](https://github.com/littlebearapps/untether/issues/N)
```

### Rules

- Every entry links to a GitHub issue: `[#N](...)`
- Sub-bullets for implementation details (no issue link needed)
- Sections appear only when they have entries (omit empty sections)
- Section order: `fixes` → `changes` → `breaking` → `docs` → `tests`
- One changelog section per release — no retroactive edits to prior sections
- Date is the date of the release tag, not the date of the commit

## Phase 4: Pre-release validation

Run all checks before tagging:

```bash
# Tests (all Python versions are tested in CI, but run locally on current)
uv run pytest

# Lint
uv run ruff check src/

# Format check
uv run ruff format --check src/ tests/

# Lockfile sync
uv lock --check

# Verify version matches changelog
python3 -c "
import tomllib, re
with open('pyproject.toml', 'rb') as f:
    v = tomllib.load(f)['project']['version']
with open('CHANGELOG.md') as f:
    first_heading = re.search(r'## v([\d.]+)', f.read()).group(1)
assert v == first_heading, f'Version mismatch: pyproject.toml={v}, CHANGELOG={first_heading}'
print(f'Version {v} matches changelog ✓')
"
```

### Checklist

- [ ] All related GitHub issues exist
- [ ] All issues referenced in CHANGELOG.md with `[#N](...)`
- [ ] `pyproject.toml` version matches changelog heading
- [ ] Tests pass: `uv run pytest`
- [ ] Lint clean: `uv run ruff check src/`
- [ ] Format clean: `uv run ruff format --check src/ tests/`
- [ ] Lockfile synced: `uv lock --check`
- [ ] No uncommitted changes: `git status`

## Phase 5: Tag and publish

```bash
# Commit release changes (version bump, changelog, lockfile)
git add pyproject.toml CHANGELOG.md uv.lock
git commit -m "chore: release vX.Y.Z"

# Tag
git tag vX.Y.Z

# Push commit and tag
git push origin master --tags
```

The `release.yml` workflow triggers automatically on `v*` tags:

1. Validates tag matches `pyproject.toml` version
2. Runs full pytest suite
3. Builds wheel + sdist via `uv build`
4. Publishes to PyPI via trusted publishing (OIDC)
5. Creates a GitHub Release with auto-generated notes

**Do not push the tag until the commit is on `master`.**

## Post-release verification

```bash
# Check CI release workflow
direnv exec . gh run list --workflow=release.yml --limit=3

# Verify PyPI (wait ~60s for index propagation)
pip index versions untether

# Verify GitHub Release exists
direnv exec . gh release view vX.Y.Z

# Install and test locally
pipx install untether==X.Y.Z
untether --version
```

## Rollback procedures

### Failed CI (tag pushed, PyPI publish failed)

```bash
# Delete the tag locally and remotely
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z

# Fix the issue, re-tag
git tag vX.Y.Z
git push origin master --tags
```

### Bad release (already on PyPI)

```bash
# Yank the release (hides from default install, but still accessible by version)
# Requires PyPI API token with yank permissions
pip install twine
twine yank untether X.Y.Z

# Cut a patch release with the fix
# Bump to vX.Y.(Z+1), fix the issue, follow the full release workflow
```

**Never re-upload the same version to PyPI** — PyPI rejects duplicate version numbers even after yanking.

### Revert commit on master

```bash
git revert <commit-sha>
git push origin master
# Then cut a new patch release
```

## Common failure modes

| Failure | Cause | Fix |
|---------|-------|-----|
| `release.yml` fails at version check | Tag doesn't match `pyproject.toml` version | Delete tag, fix version, re-tag |
| `release.yml` fails at pytest | Tests pass locally but fail in CI | Check Python version matrix (3.12/3.13/3.14), platform differences |
| `uv lock --check` fails | `pyproject.toml` changed without running `uv lock` | Run `uv lock` and commit `uv.lock` |
| Changelog missing issue links | Issue not created before release | Create issue retroactively, amend changelog in next release |
| PyPI publish fails with 403 | Trusted publisher not configured for this repo | Check PyPI project settings → Publishing → Trusted Publishers |
| GitHub Release not created | Workflow `release.yml` missing `create_release` step | Check workflow file, ensure `gh release create` runs |

## Untether-specific considerations

- **macOS vs Linux**: Some features are platform-specific (e.g., Keychain credential storage). Test on both when possible.
- **Engine compatibility**: Version bumps may coincide with upstream CLI changes (Claude Code, Codex). Note upstream version requirements in changelog.
- **Schema forward-compatibility**: Use `forbid_unknown_fields=False` on msgspec structs so new upstream JSONL fields don't break existing releases.
- **Entry points**: New engines require `pyproject.toml` entry point registration — `uv lock` must follow.
