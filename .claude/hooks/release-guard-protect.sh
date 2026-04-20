#!/bin/bash
# release-guard-protect.sh — PreToolUse hook for Edit and Write tools
# Prevents modification of release guard infrastructure files.
# DO NOT MODIFY — this hook protects itself and the release guard.

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
[ -z "$FILE_PATH" ] && echo '{}' && exit 0

# Helper: emit the current Claude Code PreToolUse deny shape (2026+).
# Legacy {"decision":"block",...} is silently ignored. See:
# https://code.claude.com/docs/en/hooks
deny() {
  jq -n --arg r "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
  exit 0
}

case "$FILE_PATH" in
  */release-guard.sh | */release-guard-protect.sh | */release-guard-mcp.sh)
    deny "🛑 RELEASE GUARD: This file is protected.\n\nRelease guard hooks can only be edited manually by Nathan.\nProtected: .claude/hooks/release-guard*.sh"
    ;;
  */.claude/hooks.json)
    deny "🛑 RELEASE GUARD: .claude/hooks.json is protected.\n\nHook configuration must be edited manually by Nathan to prevent removal of release guard hooks."
    ;;
esac

echo '{}'
