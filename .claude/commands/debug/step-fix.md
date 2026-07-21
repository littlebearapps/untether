# Step 7 — Fix with verification criteria

Implement the minimal fix. Document a verification spec. Apply
`needs-verification`. Do **not** close the issue.

This file expands the parent command's Step 7 summary. Read both — the parent
file is the orchestration; this file is the canonical checklist.

## Hard release-guard rules (read first)

From `.claude/rules/release-discipline.md` and `.claude/rules/dev-workflow.md`:

- **NEVER push to master.** Hooks block; do not work around. `git push origin
  master` is blocked.
- **NEVER create tags.** `git tag v*` is blocked. `auto-tag-on-master.yml`
  creates tags from stable PR merges.
- **NEVER merge PRs to master.** `gh pr merge` to master is blocked.
- **NEVER run `gh release create`.** Release publishing is automated.
- **NEVER use `--no-verify`, `--no-gpg-sign`, or any hook-skip flag.** Hooks
  block guard-script edits too.
- **NEVER restart `untether.service` (staging) to test code changes.** Restart
  `untether-dev.service` instead. Restarting staging during dev is *always*
  wrong (see `dev-workflow.md`).
- **NEVER edit guard scripts or `.claude/hooks.json`.** `release-guard-protect.sh`
  blocks these. Only Nathan changes them outside Claude Code.

The release pipeline is single-gate: `dev` push → TestPyPI; Nathan
squash-merges a stable version PR to `master` → auto-tag → release.yml
publishes to PyPI. The master PR review IS the release approval.

## The 7-step implementation checklist

### 1. Branch off `dev`

```bash
git fetch origin
git checkout dev
git pull --ff-only origin dev
git checkout -b "fix/<issue-N>-<short-slug>"
```

Branch naming: `fix/<N>-<slug>` for bugs, `feature/<slug>` for features.

### 2. Implement the minimal fix

Edit only the files needed. Don't add comments, don't refactor surrounding
code, don't add backwards-compat shims. If the fix is one line, the diff
should be one line.

Follow the area's rule file:
- runner-* changes → `runner-development.md` (3-event contract, session
  locking, signal-death handling, EventFactory).
- telegram-* changes → `telegram-transport.md` (outbox-only writes, 64-byte
  callback, ephemeral cleanup).
- control-channel changes → `control-channel.md` (PTY lifecycle, registry
  cleanup, cooldown).
- runner edits trigger `.claude/hooks/runner-edit-context.sh`; telegram edits
  trigger `telegram-edit-context.sh` — these print contract reminders.

### 3. Run targeted tests

```bash
uv run pytest tests/test_<area>.py -x -v
```

If this is a new test file, mirror an existing one (e.g.
`test_claude_control.py`, `test_telegram_files.py`). See
`.claude/rules/testing-conventions.md` for stub-subprocess + mock-transport
patterns. Coverage threshold is 80%.

### 4. Run full suite + lint + format

```bash
uv run pytest               # 2372 tests, ~30 sec
uv run ruff check src/      # lint
uv run ruff format src/ tests/   # format — CI checks formatting
```

If lint or format fails, fix and re-run. Never push code that doesn't pass
`ruff format --check`.

### 5. Update CHANGELOG

Find the active rc/stable section in `CHANGELOG.md`. If a section for the
current version doesn't exist yet, add one with header `## vX.Y.Z (YYYY-MM-DD)`.
Add an entry under the correct subsection (`### fixes`, `### changes`,
`### breaking`, `### docs`, `### tests`):

```markdown
- description of the fix [#N](https://github.com/littlebearapps/untether/issues/N)
```

Every entry MUST include the issue link in the `[#N](https://...)` form.
`scripts/validate_release.py` enforces this in CI.

Note: rc versions (e.g. `0.35.3rc14`) don't require changelog entries —
`validate_release.py` skips them.

### 6. Commit + push the feature branch

```bash
git add -A    # only files you actually changed; never blanket-add
git commit -m "fix: <one-line description> (#N)"
git push -u origin "fix/<N>-<slug>"
```

Conventional commits: `fix:` for bug fixes, `feat:` for features, `docs:`
for docs, `chore:` for non-functional. Body should be short — the PR
description has the details.

### 7. Open the PR (to `dev`, never to `master`)

```bash
gh pr create --base dev \
  --title "fix: <one-line>" \
  --body "$(cat <<'EOF'
## Summary
- <one to three bullets — what changed and why>

## Issue
Fixes #<N>

## Test plan
- [x] Targeted: uv run pytest tests/test_<area>.py
- [x] Full suite: uv run pytest
- [x] Lint: uv run ruff check src/
- [x] Format: uv run ruff format --check src/ tests/
- [ ] Integration tests on @untether_dev_bot per release-discipline.md tier

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The PR targets `dev`. If you accidentally target `master`,
`release-guard-mcp.sh` blocks the merge.

### 8. Apply `needs-verification` to the issue

```bash
gh issue edit <N> --add-label "needs-verification"
gh issue comment <N> --body "$(cat <<'EOF'
<paste the Debug Report from output-template.md>
EOF
)"
```

Do **not** close the issue. Nathan or a follow-up automation closes after
verification.

## When to use `Co-Authored-By: Claude`

Per global commit conventions: append to commits where Claude wrote the
substantive change. Use the heredoc form:

```bash
git commit -m "$(cat <<'EOF'
fix: <message>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

## Common Untether mistakes to avoid

- **Restarting staging from inside an active session.** The 120s drain
  timeout drops your final response. See `feedback_agent_self_restart_pattern`.
  Use `untether-dev.service` and let hot-reload pick up config changes.
- **Editing `.claude/hooks.json` or guard scripts.** Blocked. Don't try.
- **Skipping the test step.** Pre-commit hook will fail; `--no-verify` is
  blocked. Run tests locally first.
- **Committing files with secrets.** `secret-warning` hook fires on
  `git add`/`commit`. If it warns, fix the file before continuing — never
  bypass.
- **Adding boilerplate to a tiny fix.** Don't add docstrings, don't refactor,
  don't add comments unless they explain a non-obvious why.
- **Forgetting the FAQ touch-up check.** If the fix changes user-visible
  surfaces (engine support, auth, privacy, approval semantics, cost,
  voice, install paths), update `docs/faq/faq.md` in the same branch. See
  `.claude/rules/help-faq.md`.

## After Step 7

Hand off to Step 8 (`step-verify.md`) for post-fix health check + integration
testing + attestation. Step 7 is "the fix is written and a PR is open"; Step
8 is "the fix is proven safe across the fleet".
