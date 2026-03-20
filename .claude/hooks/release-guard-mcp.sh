#!/bin/bash
# release-guard-mcp.sh — PreToolUse hook for GitHub MCP write tools
# Always blocks merge_pull_request.
# Blocks push_files/create_or_update_file/delete_file targeting master/main.
# Feature branches are allowed.
# DO NOT MODIFY — protected by release-guard-protect.sh

set -euo pipefail

INPUT=$(cat)

# ── merge_pull_request — allow dev, block master/main ────────────

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)
if [ "$TOOL_NAME" = "mcp__github__merge_pull_request" ]; then
  PR_NUM=$(echo "$INPUT" | jq -r '.tool_input.pullNumber // .tool_input.pull_number // ""' 2>/dev/null)
  if [ -n "$PR_NUM" ] && [ "$PR_NUM" != "null" ]; then
    PR_BASE=$(gh pr view "$PR_NUM" --repo littlebearapps/untether --json baseRefName -q .baseRefName 2>/dev/null || echo "unknown")
    if [ "$PR_BASE" = "dev" ]; then
      echo '{}'
      exit 0
    fi
  fi
  echo '{"decision":"block","reason":"🛑 RELEASE GUARD: PR merging to master/main via GitHub MCP is blocked.\n\nOnly merges to dev are allowed via Claude Code. Master merges must be done manually by Nathan."}'
  exit 0
fi

# Fallback: detect merge by input fields (block if not already handled above)
if echo "$INPUT" | jq -e '.tool_input.pull_number // .tool_input.merge_method' > /dev/null 2>&1; then
  echo '{"decision":"block","reason":"🛑 RELEASE GUARD: PR merging via GitHub MCP is blocked.\n\nUse gh pr merge <number> for dev-targeting PRs, or merge manually in GitHub UI."}'
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
