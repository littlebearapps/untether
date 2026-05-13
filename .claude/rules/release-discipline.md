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
6. **FAQ touch-up check (`docs/faq/faq.md`)** — scan the new CHANGELOG entries against the help-centre FAQ. If any entry changes engine support, auth/billing model, privacy/data flow, approval semantics, cost budgets, voice transcription config, install/update/uninstall paths, or any other user-facing surface answered by the FAQ, update `docs/faq/faq.md` in the same release branch. The file is gate-protected — Bash `rm`/`mv`/`>` are blocked by `help-faq-protect.sh`, but Edit/Write are encouraged. See [`help-faq.md`](./help-faq.md) for the full update cadence and shape rules. Tracking issue: [#477](https://github.com/littlebearapps/untether/issues/477).

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

**ALWAYS use `@untether_dev_bot`** (dev service) for initial integration testing. NEVER use `@hetz_lba1_bot` (staging) for dev testing — use `@untether_dev_bot` first. Stage rc versions on `@hetz_lba1_bot` only after dev integration tests pass.

Integration tests are automated via Telegram MCP tools (`send_message`, `get_history`, `list_inline_buttons`, `press_inline_button`, `reply_to_message`). Claude Code sends test prompts to the 6 `ut-dev:` engine chats, reads back responses, and verifies expected behaviour. See `docs/reference/integration-testing.md` for chat IDs, workflow, and test details.

### Pre-rollout integration test attestation

Once integration tests pass for a given version, **write the attestation marker** so the fleet-rollout script can proceed:

```bash
scripts/run-integration-tests.sh ${VERSION} --manual \
  --tiers "tier7,tier1-claude,tier1-codex" \
  --notes "U1-U8 all pass on @untether_dev_bot; tier 6 stress ok"
```

This writes `~/.untether-dev/integration-test-pass-${VERSION}.json` with timestamp, tester, tier list, and notes. `scripts/fleet-rollout.sh ${VERSION}` REQUIRES this marker to exist — it refuses to roll the rc/stable to nsd, channelo, or mac without it. The only way around the gate is `--skip-test-gate`, which prints a loud warning and is not recommended for any change that touches production hosts.

**The marker is per-version, not per-host.** One pass on `@untether_dev_bot` is enough to gate the fleet rollout because the dev bot exercises the same code paths every host runs. Re-test if the version number changes (e.g. rc14 → rc15 each get their own marker).

**Markers are durable.** Delete them manually if you want to invalidate a rollout (e.g. discovered a regression post-test): `rm ~/.untether-dev/integration-test-pass-${VERSION}.json` then the rollout script will refuse to run.

## Staging / rc versions

Pre-release versions (`X.Y.ZrcN`) are used for staging on `@hetz_lba1_bot` before final release:

- rc versions live on the `dev` branch — merged via PR from feature branches
- rc versions do **NOT** require changelog entries — `validate_release.py` skips them
- rc versions are **NOT** tagged (`auto-tag-on-master.yml` skips pre-releases)
- Commit message convention: `chore: staging X.Y.ZrcN`
- Only stable releases (`X.Y.Z`) get tagged and changelog entries on `master`
- **Single-gate release flow**: `dev` push → TestPyPI (auto); `master` push of a stable version → `auto-tag-on-master.yml` creates `vX.Y.Z` → `release.yml` publishes to PyPI via OIDC → GitHub Release. The master PR review is the only manual approval — no PyPI environment gate, no manual tag step.
- See `docs/reference/dev-instance.md` for the full staging workflow.

## Fleet rollout (rc and stable)

Untether ships from one repo to **four hosts**: lba-1 staging, nsd VPS, channelo VPS, and Nathan's Mac. As of 2026-05-13, all four hosts are rolled in parallel after integration tests pass (no separate dogfood window — the integration tests are the quality gate).

```bash
scripts/run-integration-tests.sh 0.35.3rc14 --manual    # write attestation marker
scripts/fleet-rollout.sh 0.35.3rc14                     # parallel upgrade across 4 hosts
scripts/fleet-rollout.sh 0.35.3rc14 --dry-run           # preview without executing
scripts/fleet-rollout.sh 0.35.3rc14 --only mac          # roll one host
scripts/fleet-rollback.sh 0.35.2 --only mac             # revert one host to known-good
```

**Order of operations:**

1. Push rc to dev → CI publishes to TestPyPI in ~3 min
2. Run integration tests via `@untether_dev_bot` (Tier 7 + Tier 1 minimum)
3. Write attestation marker (`scripts/run-integration-tests.sh ${VERSION} --manual`)
4. Run fleet rollout (`scripts/fleet-rollout.sh ${VERSION}`) — all 4 hosts in parallel
5. Verify each host's bot responds to `/ping` via Telegram

**Partial failure handling:** if one host fails (network glitch, SSH timeout, etc.), the script reports the failure but does NOT roll back successful hosts. Operator decides whether to roll forward (rerun) or roll back the failed host (`fleet-rollback.sh <prev> --only <host>`).

**Rc supersede:** if rc14 is already deployed and rc15 is ready, just run `fleet-rollout.sh 0.35.3rc15` — the script detects the supersede and proceeds. `--force-downgrade` is required for older-than-current versions.

**Strategic plan:** [`docs/plans/2026-05-13-fleet-monitoring-and-upgrades.md`](../../docs/plans/2026-05-13-fleet-monitoring-and-upgrades.md) (Phase 4). See also `.claude/rules/dev-workflow.md` for dev/staging separation rules that still apply per-host.

## Audit-filed issues (release triage)

Two automated systems file GitHub issues into this repo. Recognise them by label:

- **`auto:error-report`** — the always-on `untether-issue-watcher` daemon (local on lba-1) creates these from production error log patterns
- **`auto:monitor-audit`** — the user-invoked `/monitor` command files these from a fixed-window audit of staging logs + chat behaviour; covers bugs (severity-tagged) and enhancements

Before tagging a release, scan both:

```bash
# Open audit findings against the current milestone, ranked by severity
gh issue list --repo littlebearapps/untether \
  --label auto:monitor-audit --state open \
  --milestone v0.35.3 --json number,title,labels,milestone

# Just the release-blockers
gh issue list --repo littlebearapps/untether \
  --label "severity:critical" --state open
```

`severity:critical` and `severity:major` should be resolved before tag; `severity:minor` and `severity:trivial` can defer to the next patch if scope-pressured. Enhancements (`enhancement` label) are routed to `next_patch`/`next_minor`/`Future` milestones by the auditor and don't block release.

## Changelog format

- Sections: `### fixes`, `### changes`, `### breaking`, `### docs`, `### tests`
- Each entry: `- description [#N](https://github.com/littlebearapps/untether/issues/N)`
- Sub-bullets for implementation details (no issue link needed on sub-bullets)

## Automated validation

`scripts/validate_release.py` runs in CI on PRs that bump the version. It checks:
- Changelog section exists for the new version
- Date is valid ISO format
- All entries have issue links `[#N]`
- Subsection headings are from the allowed set

Run locally: `python3 scripts/validate_release.py`

## After changes

```bash
# Verify changelog format
grep -E '## v[0-9]' CHANGELOG.md | head -5
grep -E '#[0-9]+' CHANGELOG.md | head -10

# Full automated validation
python3 scripts/validate_release.py
```
