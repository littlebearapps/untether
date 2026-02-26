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

## Semantic versioning

- **Patch**: bug fixes, schema updates, dependency bumps
- **Minor**: new features, new commands, new engines, config additions
- **Major**: breaking changes to config, runner protocol, or public API

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
