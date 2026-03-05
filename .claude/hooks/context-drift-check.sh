#!/bin/bash
# context-drift-check.sh
# Hook: PostToolUse (Bash, matching git commit)
# Purpose: Detect stale AI context files after commits
# Installed by: /context-guard install
#
# Claude Code only — OpenCode, Codex CLI, Cursor, and other tools
# do not support Claude Code hooks.

set -euo pipefail

# Read hook input from stdin
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

# Only process successful git commit commands
[ "$TOOL_NAME" != "Bash" ] && echo '{}' && exit 0
[[ "$COMMAND" != *"git commit"* ]] && echo '{}' && exit 0

# Resolve project directory
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
cd "$PROJECT_DIR" || { echo '{}'; exit 0; }

# Must be inside a git repository
git rev-parse --is-inside-work-tree &>/dev/null || { echo '{}'; exit 0; }

# Throttle: skip if checked less than 1 hour ago
THROTTLE_FILE=".git/.context-guard-last-check"
if [ -f "$THROTTLE_FILE" ]; then
  LAST_CHECK=$(cat "$THROTTLE_FILE" 2>/dev/null || echo "0")
  NOW=$(date +%s)
  ELAPSED=$((NOW - LAST_CHECK))
  [ "$ELAPSED" -lt 3600 ] && echo '{}' && exit 0
fi

# Context files to check
CONTEXT_FILES=("CLAUDE.md" "AGENTS.md" "GEMINI.md" ".cursorrules"
               ".github/copilot-instructions.md" ".windsurfrules" ".clinerules")

STALE=()
BROKEN_PATHS=()

for CTX in "${CONTEXT_FILES[@]}"; do
  [ ! -f "$CTX" ] && continue

  # Last commit that touched this context file
  CTX_COMMIT_TIME=$(git log -1 --format=%ct -- "$CTX" 2>/dev/null || echo "0")

  # Last commit that touched source files (excluding docs)
  SRC_COMMIT_TIME=$(git log -1 --format=%ct -- \
    '*.ts' '*.js' '*.py' '*.go' '*.rs' '*.json' '*.toml' '*.yaml' '*.yml' \
    ':!*.md' ':!CHANGELOG.md' ':!README.md' ':!docs/*' 2>/dev/null || echo "0")

  if [ "$SRC_COMMIT_TIME" -gt "$CTX_COMMIT_TIME" ] 2>/dev/null; then
    CTX_HASH=$(git log -1 --format=%H -- "$CTX" 2>/dev/null || echo "HEAD")
    COMMITS_BEHIND=$(git rev-list --count "$CTX_HASH"..HEAD -- \
      '*.ts' '*.js' '*.py' '*.go' '*.rs' '*.json' '*.toml' '*.yaml' '*.yml' \
      ':!*.md' 2>/dev/null || echo "?")
    STALE+=("$CTX: $COMMITS_BEHIND source commits since last update")
  fi

  # Quick broken-path check: extract backtick-quoted file references
  while IFS= read -r REF_PATH; do
    if [ -n "$REF_PATH" ] && [ ! -e "$REF_PATH" ]; then
      BROKEN_PATHS+=("$CTX references \`$REF_PATH\` (not found)")
    fi
  done < <(grep -oE '`[a-zA-Z][a-zA-Z0-9._/-]+\.(ts|js|py|go|rs|md|json|toml|yaml|yml|sh)`' "$CTX" 2>/dev/null \
    | tr -d '`' | sort -u | head -20)
done

# Update throttle timestamp
date +%s > "$THROTTLE_FILE" 2>/dev/null

# Build output
ISSUES=()
for S in "${STALE[@]}"; do ISSUES+=("  - $S"); done
for B in "${BROKEN_PATHS[@]}"; do ISSUES+=("  - $B"); done

if [ ${#ISSUES[@]} -gt 0 ]; then
  # Build multiline message
  MSG="AI CONTEXT DRIFT DETECTED:"
  for I in "${ISSUES[@]}"; do
    MSG="$MSG\n$I"
  done
  MSG="$MSG\nRun /ai-context audit to check, or /ai-context to regenerate."

  # Escape for JSON
  MSG_JSON=$(printf '%s' "$MSG" | sed 's/"/\\"/g')

  cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "$MSG_JSON"
  }
}
EOF
else
  echo '{}'
fi
