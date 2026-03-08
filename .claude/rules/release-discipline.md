# Release & Issue Tracking Discipline

## When fixing bugs

1. Create a GitHub issue FIRST (or alongside the fix) with: description, impact, affected files
2. Label it `bug` and reference the issue number in the commit message and CHANGELOG
3. After merging the fix, close the issue with a comment referencing the PR/commit

## When bumping versions

1. Update `pyproject.toml` version
2. Add a CHANGELOG.md section: `## vX.Y.Z (YYYY-MM-DD)`
3. Every changelog entry must link to a GitHub issue: `[#N](https://github.com/littlebearapps/untether/issues/N)`
4. Run `uv lock` to sync the lockfile
5. **Run integration tests against `@untether_dev_bot`** — see below and `docs/reference/integration-testing.md`

## Semantic versioning

- **Patch**: bug fixes, schema updates, dependency bumps
- **Minor**: new features, new commands, new engines, config additions
- **Major**: breaking changes to config, runner protocol, or public API

## MANDATORY integration testing before release

**Every version bump MUST include integration testing via `@untether_dev_bot`.** This is not optional. See `docs/reference/integration-testing.md` for the full playbook.

| Release type | Required integration test tiers | Time |
|---|---|---|
| **Patch** | Tier 7 (command smoke) + Tier 1 (affected engine + Claude) + relevant Tier 6 (stress) | ~30 min |
| **Minor** | Tier 7 + Tier 1 (all 6 engines) + Tier 2 (Claude interactive) + Tier 3 (transport, if changed) + Tier 4 (overrides, if changed) + Tier 6 + upgrade path | ~75 min |
| **Major** | ALL tiers (1-7), ALL engines, full upgrade path testing | ~120 min |

**NEVER skip integration testing.** Unit tests alone are insufficient — production bugs consistently slip through areas only exercisable via live Telegram interaction.

**ALWAYS use `@untether_dev_bot`** (dev service) for integration testing. NEVER test against `@hetz_lba1_bot` (production).

## Changelog format

- Sections: `### fixes`, `### changes`, `### breaking`, `### docs`, `### tests`
- Each entry: `- description [#N](https://github.com/littlebearapps/untether/issues/N)`
- Sub-bullets for implementation details (no issue link needed on sub-bullets)

## After changes

```bash
# Verify changelog format
grep -E '## v[0-9]' CHANGELOG.md | head -5
grep -E '#[0-9]+' CHANGELOG.md | head -10
```
