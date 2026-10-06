---
description: Release-prep for a stable version — confirm dev is green + ahead of master, bump pyproject.toml to X.Y.Z + uv lock, collapse the rc CHANGELOG sections into one dated release entry, run validate_release.py clean, final FAQ pass, confirm the attestation marker exists, then open ONE dev→master PR and STOP. `--merge` (only after Nathan explicitly approves the release) merges it through the ask-gated guard (→ auto-tag → PyPI) and verifies the publish. Never tags or creates releases by hand.
argument-hint: "<X.Y.Z> (prepare + open dev→master PR) | <X.Y.Z> --merge (after Nathan's go) | <X.Y.Z> --dry-run | --rc-summary | [--help]"
disable-model-invocation: true
allowed-tools: Read Glob Grep Edit Write Skill ToolSearch Bash(git status:*) Bash(git branch:*) Bash(git rev-parse:*) Bash(git symbolic-ref:*) Bash(git log:*) Bash(git diff:*) Bash(git fetch:*) Bash(git checkout:*) Bash(git add:*) Bash(git commit:*) Bash(git push:*) Bash(gh pr create:*) Bash(gh pr view:*) Bash(gh pr list:*) Bash(gh pr checks:*) Bash(gh run list:*) Bash(gh run view:*) Bash(gh issue list:*) Bash(gh issue view:*) Bash(uv run pytest:*) Bash(uv run ruff:*) Bash(uv lock:*) Bash(python3 scripts/validate_release.py:*) Bash(grep:*) Bash(rg:*) Bash(jq:*) Bash(date:*) Bash(wc:*) Bash(head:*) Bash(tail:*) Bash(ls:*) Bash(cat:*)
---

You are handling `/pr-main`. `/pr-main` prepares a **stable release** and opens
the `dev`→`master` PR — then **STOPS**. The release itself (merging that PR →
`auto-tag-on-master.yml` → `release.yml` → PyPI) happens only when Nathan
explicitly approves it: then `/pr-main X.Y.Z --merge` merges and verifies the
publish ([#917](https://github.com/littlebearapps/untether/issues/917)).

User input: `$ARGUMENTS`

## Untether adaptations (read first)

Load `.claude/rules/workflow-commands.md` (routing + cross-cutting rules) and
`.claude/rules/release-discipline.md` (semver, changelog format, FAQ, staging vs
release). Key points:

- **Authority boundary (mirrors `release-guard.sh`).** `gh pr create
  --base master` is **allowed**. Merging the `dev`→`master` PR is allowed **only**
  after Nathan explicitly approves *this* release in the conversation ("merge it",
  "release X.Y.Z"), and only as `gh pr merge <n> --squash --admin`. The guard
  then checks the head is `dev` and CI is green, and **asks Nathan to confirm** in
  the permission prompt. A finished `/pr-main X.Y.Z`, a green CI or an earlier
  approval of a different version is NOT approval. `git push master`, `git tag
  v*`, `gh release create` and `--delete-branch` stay **forbidden** — the release
  pipeline tags and publishes. Never work around a block.
- **The version-bump commit lands on `dev`** (pushing to `dev` is allowed;
  `master` is not). The PR is `dev`→`master`.
- **`fleet-rollout.sh X.Y.Z`** runs only after PyPI has the version, and only when
  Nathan asks (it's a separate step from the merge). The attestation marker gates it.
- **Confirm-gated + idempotent.** Surface the PR title + body and wait for a tap;
  a re-invoked `/pr-main` must not double-open the release PR (check for an
  existing open `dev`→`master` PR first).
- **Untether-mode.** State the confirmation in text and STOP for a reply. Keep
  the report brief.

## Sub-commands

| Form | Action |
|---|---|
| `/pr-main X.Y.Z` | Prepare the stable release + open the `dev`→`master` PR, then STOP |
| `/pr-main X.Y.Z --merge` | Only after Nathan's explicit go: merge the open release PR (ask-gated) + verify the PyPI publish (M-8) |
| `/pr-main X.Y.Z --dry-run` | Prepare + validate + print the would-be PR body; open nothing |
| `/pr-main --rc-summary` | Finalise the `(unreleased)` CHANGELOG section (M-3) + validate only; no bump, no PR |
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

- The rc line accumulates under ONE `## vX.Y.Z (unreleased)` heading (rcs carry no
  sections of their own). Date it — `## vX.Y.Z (YYYY-MM-DD)` — drop the maintainer
  `<!-- Status … -->` comment, keep `### breaking` first (each with its
  **Migration:** line), then `fixes/changes/docs/tests`; every entry keeps its
  `[#N](…)` issue link and duplicate rc-era entries are merged.
- If the line was renumbered mid-cycle (the 0.35.5rc1–rc20 line ships as v0.36.0,
  #947), confirm the heading, `pyproject.toml`, the milestone and the attestation
  marker all use the new version, and keep the one-line note naming the rc range it
  went through.
- Run `python3 scripts/validate_release.py` until clean (section exists, ISO
  date, issue links present, allowed subsection headings).

### M-3b. Write the release announcement (required — CI blocks the PR without it)

Every stable release posts a plain-English **Discussions → Announcements** post
([#1008](https://github.com/littlebearapps/untether/issues/1008)). Write
`.github/release-announcements/vX.Y.Z.md` from the collapsed CHANGELOG section,
following `.github/release-announcements/TEMPLATE.md`:

- Audience = everyday users and would-be contributors. Benefits first, plain
  English, Australian spelling, no internal jargon, no rc numbers.
- `## TL;DR` + `## ⬆️ Upgrade` always; at least one of `## ✨ What's new for you` /
  `## 🛠️ Fixes you'll notice` / `## 🧹 Under the hood`; **`## ⚠️ Heads up`
  whenever the section has `### breaking` or a deprecation** (what changed, who's
  affected, what to do — deprecated engines: still shipped? supported? removal?).
- No Links section — `render` appends changelog / release / PyPI / help / Discussions links.
- `python3 scripts/release_announcement.py check X.Y.Z` until clean (also run by
  `validate_release.py` and by `release.yml` before anything is built), then paste
  `render X.Y.Z` into the PR body under `## Announcement` for Nathan to review.
- `release.yml`'s `announce` job posts it after PyPI + the GitHub Release
  (idempotent; links the discussion from the release). If that job fails, re-run
  it — never post by hand with different text.

### M-4. FAQ final pass

Per `.claude/rules/help-faq.md`, scan the collapsed changelog against
`docs/faq/faq.md`; update any user-visible surface answer that the release
changes. (Edit/Write allowed; never `rm`/`mv`/`>` it — `help-faq-protect.sh` blocks that.)

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

## Announcement
<output of `python3 scripts/release_announcement.py render X.Y.Z` — posted to
Discussions → Announcements by release.yml after PyPI>

## Release note
Merging this PR IS the release → auto-tag vX.Y.Z → release.yml → PyPI.
Merged by Nathan, or by Claude via `/pr-main X.Y.Z --merge` once Nathan approves.
Fleet rollout (scripts/fleet-rollout.sh X.Y.Z) follows once PyPI has the version.
```

Surface it, wait for a tap, then `gh pr create --base master`. **STOP** — print
"release PR open — say the word to merge and publish". In `--dry-run`, print the
body and open nothing.

### M-7. Report + hand-off

Brief report: version prepared, changelog collapsed + validated, FAQ pass,
marker status, PR URL (or "dry-run"), and the explicit next step: **Nathan
approves the release** (or merges it himself) → auto-tag → PyPI →
`scripts/fleet-rollout.sh X.Y.Z`. `/pr-main X.Y.Z` is done at the open PR.

### M-8. `--merge` — merge + verify (only after Nathan's explicit go)

1. **Approval check.** Quote Nathan's message approving *this* version. None → STOP
   and ask; never infer approval.
2. **Pre-flight.** The open PR is `dev`→`master`, title/version = `X.Y.Z`, `gh pr
   checks <n>` all green, the attestation marker exists. Anything off → STOP and
   report.
3. **Merge.** `gh pr merge <n> --squash --admin` (no `--delete-branch`). The guard
   asks Nathan to confirm — if he declines, or the call is denied, STOP; never
   retry another way.
4. **Verify the publish** (including the `announce` job's Discussions link). `gh run list --workflow auto-tag-on-master.yml` then
   `--workflow release.yml` until both succeed; the `vX.Y.Z` tag and GitHub release
   exist; PyPI serves it (`curl -s https://pypi.org/pypi/untether/json | jq -r
   .info.version`). If auto-tag succeeded but `release.yml` never ran (the #376
   cascade), `gh workflow run release.yml --ref vX.Y.Z` — the guard asks again.
5. **Report** the merge SHA, tag, release run and PyPI version, then offer
   `scripts/fleet-rollout.sh X.Y.Z` (run it only when Nathan says so).

## Anti-patterns

- Never merge the release PR without Nathan's explicit approval of this version,
  and never fight a guard block or a declined prompt.
- Never `git tag`, `gh release create` or push to `master` — the pipeline does it.
- Never run `fleet-rollout.sh` before PyPI has the version or without Nathan's go.
- Never open a master PR from a non-`dev` branch.
- Never bump to a stable version while rc integration tests are unattested (flag
  the missing marker; recommend `/qa` QA-4).
- Never `--no-verify`; never `git add -A`.

`--help` prints the sub-command table, then stops.

End of /pr-main. The everyday delivery command is `/pr-dev`; validation is `/qa`.
