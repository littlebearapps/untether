#!/bin/bash
# release-guard-mcp.sh — PreToolUse hook for GitHub MCP write tools
# Always blocks merge_pull_request.
# Blocks push_files/create_or_update_file/delete_file targeting master/main.
# Feature branches are allowed.
# DO NOT MODIFY — protected by release-guard-protect.sh

set -euo pipefail

INPUT=$(cat)

# ── Always block merge_pull_request ───────────────────────────────

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)
if [ "$TOOL_NAME" = "mcp__github__merge_pull_request" ]; then
  echo '{"decision":"block","reason":"🛑 RELEASE GUARD: PR merging via GitHub MCP is blocked.\n\nPR merging must be done manually by Nathan in the GitHub UI."}'
  exit 0
fi

# Fallback: detect merge by input fields
if echo "$INPUT" | jq -e '.tool_input.pull_number // .tool_input.merge_method' > /dev/null 2>&1; then
  echo '{"decision":"block","reason":"🛑 RELEASE GUARD: PR merging via GitHub MCP is blocked.\n\nPR merging must be done manually by Nathan in the GitHub UI."}'
  exit 0
fi

# ── push_files / create_or_update_file / delete_file — check branch ──

BRANCH=$(echo "$INPUT" | jq -r '.tool_input.branch // ""' 2>/dev/null)

if [ "$BRANCH" = "master" ] || [ "$BRANCH" = "main" ] || [ -z "$BRANCH" ]; then
  DISPLAY="${BRANCH:-default}"
  jq -n --arg reason "🛑 RELEASE GUARD: GitHub MCP write to '${DISPLAY}' branch is blocked.\n\nSpecify a feature branch or 'dev' branch instead of master/main." \
    '{"decision": "block", "reason": $reason}'
  exit 0
fi

# Feature branch — allow
echo '{}'
