# Step 8 — Post-fix health check

Confirm the fix worked. Confirm no regression. Attest. Roll the fleet only
after the gate is satisfied.

This file expands the parent command's Step 8 summary. The "fix is verified"
bar is high — a passing test suite is necessary but not sufficient.

## 1. Restart `untether-dev` and tail logs

```bash
# NEVER restart untether.service (staging) here. Always untether-dev.
systemctl --user restart untether-dev
journalctl --user -u untether-dev -f
```

In the tail, watch for:
- The targeted event signature (e.g. `subprocess.liveness_stall`) — should
  be absent for sessions exhibiting the same input shape that previously
  triggered it.
- New error signatures — if you fixed one thing and broke another, the new
  signature surfaces here.
- Auto-continue chatter — should be quiet for normal completions; firing
  only when the upstream Claude bug surfaces (`last_event_type=user`).

If the fix is for a chat-side issue (control-channel, telegram-transport),
also re-run the affected interaction via `@untether_dev_bot` and confirm
visually.

## 2. Run integration tests at the right tier

From `docs/reference/integration-testing.md` and
`.claude/rules/release-discipline.md`:

| Change scope | Required tiers | Time |
|---|---|---|
| **Patch** (bug fix) | Tier 7 (command smoke) + Tier 1 (affected engine + Claude) + relevant Tier 6 (stress) | ~30 min |
| **Minor** (new feature) | Tier 7 + Tier 1 (all 6 engines) + Tier 2 (Claude interactive) + Tier 3 (transport if changed) + Tier 4 (overrides if changed) + Tier 6 + upgrade path | ~75 min |
| **Major** (breaking) | ALL tiers (1–7), ALL engines, full upgrade path | ~120 min |

Integration tests are automated via Telegram MCP tools (`send_message`,
`get_history`, `list_inline_buttons`, `press_inline_button`,
`reply_to_message`, `send_voice`, `send_file`) + Bash (`journalctl`,
`kill -TERM`, FD/zombie checks). Chat IDs in
`testing-conventions.md` §Integration testing via Telegram MCP.

Use `@untether_dev_bot`. **NEVER** test on `@hetz_lba1_bot` (staging) until
dev tests pass.

## 3. Write the attestation marker

Once integration tests pass, write the marker file so `fleet-rollout.sh`
will let the rc/release through:

```bash
scripts/run-integration-tests.sh ${VERSION} --manual \
  --tiers "tier7,tier1-claude,tier1-<affected-engine>" \
  --notes "U1-U8 all pass on @untether_dev_bot; tier 6 stress ok"
```

This writes `~/.untether-dev/integration-test-pass-${VERSION}.json` with
timestamp, tester, tier list, and notes. The marker is per-version; rc14 →
rc15 each get their own.

**Markers are durable.** Delete manually if you discover a regression
post-test:

```bash
rm ~/.untether-dev/integration-test-pass-${VERSION}.json
```

After deletion, `fleet-rollout.sh` refuses to run until a fresh marker is
written.

## 4. Fleet rollout (only if this is an rc or stable release)

If the fix has been merged to `dev` and a fresh rc is published to TestPyPI:

```bash
scripts/fleet-rollout.sh ${VERSION}              # parallel upgrade across 5 hosts
scripts/fleet-rollout.sh ${VERSION} --dry-run    # preview
scripts/fleet-rollout.sh ${VERSION} --only mac   # one host
```

The four hosts: lba-1 staging, nsd VPS, channelo VPS, Nathan's Mac.

**Partial failure handling:** if one host fails, the script reports it but
does NOT roll back successful hosts. Operator decides: rerun the failed
host, or roll back to the previous good version via `fleet-rollback.sh
<prev> --only <host>`.

This step is **only** for fixes that ship as part of an rc or stable
release. A fix landing in `dev` for the next rc does not trigger an
immediate rollout — the rc bump does.

## 5. Re-run Step 4 grep on the fresh dev logs

Did the fix introduce regressions in other systemic-pattern surfaces? Read
`systemic-patterns.md` and grep the last hour of `untether-dev` logs for
each pattern's signature:

```bash
journalctl --user -u untether-dev --since "1 hour ago" --output=cat \
  | grep -E '<signatures from systemic-patterns.md>' | head -50
```

Any new occurrences of patterns that weren't present before the fix are
red flags. Investigate before declaring the fix verified.

## 6. Final sanity sweep

```bash
# Full suite still green
uv run pytest

# Lint + format
uv run ruff check src/
uv run ruff format --check src/ tests/

# CHANGELOG validation (if version bumped)
python3 scripts/validate_release.py

# Confirm no settings.json or hooks.json drift
git status .claude/hooks.json .claude/settings.json 2>/dev/null
```

## 7. Note `needs-verification` on the issue

If the issue had `needs-verification` applied in Step 7, leave it. The label
signals that a follow-up automation (when built) or a manual reviewer should
close on PASS. Untether's auto-close cron is a planned enhancement; until
then, `needs-verification` issues are reviewed by Nathan or the next
person walking the open-issue list.

If the verification spec criteria are clearly met now (e.g. the targeted
event has not surfaced in 24h post-fix on `untether-dev`), you may comment
on the issue with a PASS note:

```
## Verification PASS

- Tests pass: tier-X, no regressions in other patterns
- Attestation: ${VERSION} marker written
- Fleet status: ${VERSION} on all 5 hosts as of <ts>
- Signature absence: <event sig> not seen in untether-dev journalctl since <ts>
```

But still leave the issue open with `needs-verification` — closing is
Nathan's decision until the auto-close automation lands.

## What "verified" does NOT mean

- It does NOT mean the test suite is green. The suite passes for many
  changes that subtly regress production behaviour.
- It does NOT mean local repro is fixed. Local repro could pass while a
  fleet host still surfaces the issue due to version skew or config drift.
- It does NOT mean the changelog is correct. CHANGELOG validation happens
  in CI, but format and issue-link correctness are separate concerns.

"Verified" means: tests pass + integration tests pass + attestation marker
exists + the targeted signature is absent in fresh dev logs + no new
systemic-pattern hits + fleet hosts (if rolled) show consistent version
and behaviour.

## After Step 8

The issue is verified. The Debug Report (from `output-template.md`) was
posted as a comment in Step 7. The `needs-verification` label is in place.
You're done with `/debug` on this issue.
