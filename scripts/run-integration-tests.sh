#!/usr/bin/env bash
# Integration-test attestation writer for Untether.
#
# Writes ~/.untether-dev/integration-test-pass-${VERSION}.json after a
# successful Telegram MCP integration test run against @untether_dev_bot.
# The attestation marker is the precondition for scripts/fleet-rollout.sh —
# without it, fleet-rollout refuses to upgrade any production host.
#
# Two modes:
#   --manual (default): write the marker assuming the tests already passed
#                       (Claude Code or Nathan ran them via Telegram MCP and
#                       confirmed success).
#   --auto:             reserved for future automation; exits 1 with a notice.
#                       Auto-orchestrating the MCP test runs from a shell
#                       script isn't feasible today because the Telegram MCP
#                       tools are only available inside Claude Code.
#
# Usage:
#   scripts/run-integration-tests.sh 0.35.3rc14 --manual
#   scripts/run-integration-tests.sh 0.35.3 --manual --tiers "tier7,tier1-claude"
#   scripts/run-integration-tests.sh 0.35.3rc14 --manual --notes "U1-U8 all pass"
#
# After this script writes the marker, you can run:
#   scripts/fleet-rollout.sh ${VERSION}
#
# Strategic plan: docs/plans/2026-05-13-fleet-monitoring-and-upgrades.md (Phase 4)
# Rule: .claude/rules/release-discipline.md (Pre-rollout integration test attestation)

set -euo pipefail

ATTESTATION_DIR="${HOME}/.untether-dev"
VERSION=""
MODE="manual"
TIERS=""
NOTES=""
TESTER="${UT_INTEGRATION_TESTER:-${USER}@$(hostname)}"

usage() {
    cat <<EOF
Usage: run-integration-tests.sh VERSION [--manual|--auto] [--tiers LIST] [--notes TEXT]

Write the per-version integration-test attestation marker that
scripts/fleet-rollout.sh requires as a precondition.

Required:
  VERSION              e.g. 0.35.3rc14, 0.35.3

Options:
  --manual             (default) Write the marker. You attest that the tests
                       were run successfully via Telegram MCP against
                       @untether_dev_bot.
  --auto               Reserved for future — exit 1 with a notice.
  --tiers LIST         Comma-separated tier list run (e.g. "tier7,tier1-claude")
  --notes TEXT         Free-form notes about the test run

The marker is written to:
  ${ATTESTATION_DIR}/integration-test-pass-VERSION.json
EOF
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage 0 ;;
        --manual) MODE="manual"; shift ;;
        --auto) MODE="auto"; shift ;;
        --tiers) TIERS="${2:?--tiers requires a value}"; shift 2 ;;
        --notes) NOTES="${2:?--notes requires a value}"; shift 2 ;;
        -*) echo "Unknown option: $1" >&2; usage 1 ;;
        *)
            if [[ -z "$VERSION" ]]; then VERSION="$1"; else echo "Unexpected: $1" >&2; usage 1; fi
            shift
            ;;
    esac
done

[[ -n "$VERSION" ]] || { echo "VERSION argument is required." >&2; usage 1; }

if [[ "$MODE" == "auto" ]]; then
    cat <<EOF >&2
ERROR: --auto mode is not yet implemented.

The Telegram MCP tools (send_message, get_history, list_inline_buttons,
press_inline_button, reply_to_message, send_voice, send_file) live inside
Claude Code, not in a standalone shell. Auto-orchestration would need
either (a) a Claude Code agent invoked from this script, or (b) a separate
MCP-aware test runner. Neither exists today.

For now, run the tests interactively via Claude Code or your Telegram
client, then call this script with --manual to write the attestation.

See docs/reference/integration-testing.md for the manual playbook.
EOF
    exit 1
fi

mkdir -p "$ATTESTATION_DIR"
MARKER="${ATTESTATION_DIR}/integration-test-pass-${VERSION}.json"

# Default tier list mirrors the release-discipline rule's per-bump-severity
# requirements. Patch -> tier7+tier1-affected; minor -> wider; major -> all.
# We don't know the bump severity from this script, so default empty and
# leave it to the user via --tiers.
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

python3 - <<EOF
import json
marker = "${MARKER}"
data = {
    "version": "${VERSION}",
    "tester": "${TESTER}",
    "attested_at": "${TIMESTAMP}",
    "mode": "${MODE}",
    "tiers": "${TIERS}".split(",") if "${TIERS}" else [],
    "notes": """${NOTES}""",
    "dev_bot": "@untether_dev_bot",
    "playbook": "docs/reference/integration-testing.md",
}
with open(marker, "w") as f:
    json.dump(data, f, indent=2)
print(f"Wrote attestation marker: {marker}")
EOF

cat <<EOF

The fleet rollout gate is now satisfied for version ${VERSION}.

Next steps:
  scripts/fleet-rollout.sh ${VERSION}              # roll to all 4 hosts in parallel
  scripts/fleet-rollout.sh ${VERSION} --dry-run    # preview without executing
  scripts/fleet-rollout.sh ${VERSION} --only mac   # roll a single host

The marker is consumed by scripts/fleet-rollout.sh on every invocation.
It is NOT automatically cleaned up — you can delete it manually after the
rollout if you want, but leaving it in place lets you re-run rollouts at
will (the gate only checks existence, not freshness).
EOF
