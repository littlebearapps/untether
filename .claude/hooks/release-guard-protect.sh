#!/bin/bash
# release-guard-protect.sh — PreToolUse hook for Edit and Write tools
# Prevents modification of release guard infrastructure files.
# Registered in .claude/settings.json (#915).
# DO NOT MODIFY — this hook protects itself and the release guard.

set -euo pipefail
# Fail closed: an internal error exits 2, which blocks the call (exit 1 would let it through).
trap 'echo "release-guard-protect.sh: internal error at line $LINENO — blocked to be safe" >&2; exit 2' ERR

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
[ -z "$FILE_PATH" ] && echo '{}' && exit 0
# Normalise `.`, `..`, `//` and symlinks so .claude/./settings.json can't slip past the patterns below.
FILE_PATH=$(realpath -m -- "$FILE_PATH" 2>/dev/null || echo "$FILE_PATH")

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
  */help-faq-protect.sh)
    deny "🛑 HELP-FAQ PROTECTION: This hook script is protected.\n\nThe FAQ-protect hook can only be edited manually by Nathan to prevent silent removal of docs/faq/index.md (issue #477).\nProtected: .claude/hooks/help-faq-protect.sh"
    ;;
  "$HOME/.claude/settings.json")
    : # user-level settings — only the disableAllHooks check below applies
    ;;
  */.claude/settings.json | .claude/settings.json | */.claude/hooks.json | .claude/hooks.json)
    deny "🛑 RELEASE GUARD: .claude/settings.json is protected.\n\nIt registers the release guard hooks (#915). Hook configuration must be edited manually by Nathan. Personal settings (permissions, plugins) belong in .claude/settings.local.json."
    ;;
esac

# Any Claude settings file: disableAllHooks switches every hook off, guards included.
case "$FILE_PATH" in
  */settings.json | */settings.local.json | settings.json | settings.local.json)
    NEW_TEXT=$(echo "$INPUT" | jq -r '[.tool_input.content, .tool_input.new_string, (.tool_input.edits[]?.new_string)] | map(select(. != null)) | join("\n")' 2>/dev/null || echo "")
    if printf '%s' "$NEW_TEXT" | grep -qF 'disableAllHooks'; then
      deny "🛑 RELEASE GUARD: disableAllHooks would switch off the release guard (#915). Only Nathan sets it."
    fi
    ;;
esac

echo '{}'
