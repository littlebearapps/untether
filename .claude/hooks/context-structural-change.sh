#!/bin/bash
# context-structural-change.sh
# Hook: PostToolUse (Write|Edit, matching structural files)
# Purpose: Remind about context file updates after structural changes
# Installed by: /context-guard install
#
# Claude Code only — OpenCode, Codex CLI, Cursor, and other tools
# do not support Claude Code hooks.

set -euo pipefail

# Read hook input from stdin
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

# Only process Write and Edit
[[ "$TOOL_NAME" != "Write" && "$TOOL_NAME" != "Edit" ]] && echo '{}' && exit 0
[ -z "$FILE_PATH" ] && echo '{}' && exit 0

# Resolve project directory
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
cd "$PROJECT_DIR" || { echo '{}'; exit 0; }

# Check if any context files exist (no point reminding if none are tracked)
HAS_CONTEXT=false
for CTX in CLAUDE.md AGENTS.md GEMINI.md .cursorrules .windsurfrules .clinerules; do
  [ -f "$CTX" ] && HAS_CONTEXT=true && break
done
[ "$HAS_CONTEXT" = false ] && echo '{}' && exit 0

# Determine what type of structural change this is
MSG=""
# Extract just the filename/relative portion for matching
# Claude Code may pass absolute or relative paths
REL_PATH="${FILE_PATH##"$PROJECT_DIR"/}"
REL_PATH="${REL_PATH#/}"

case "$REL_PATH" in
  commands/*.md)
    MSG="You modified a command definition. AGENTS.md, CLAUDE.md, and llms.txt may need their command tables updated."
    ;;
  .claude/skills/*/SKILL.md|.agents/skills/*/SKILL.md)
    MSG="You modified a skill. AGENTS.md, CLAUDE.md, and llms.txt may need their skill listings updated."
    ;;
  .claude/agents/*.md|.agents/agents/*.md)
    MSG="You modified an agent definition. AGENTS.md may need updating."
    ;;
  .claude/rules/*.md)
    MSG="You modified a rule. CLAUDE.md and AGENTS.md may need updating if they list rules."
    ;;
  package.json|*/package.json|pyproject.toml|*/pyproject.toml|Cargo.toml|*/Cargo.toml|go.mod|*/go.mod)
    MSG="Project manifest changed. AI context files may reference outdated dependencies or commands."
    ;;
  tsconfig*.json|*/tsconfig*.json|wrangler.toml|*/wrangler.toml|vitest.config*|*/vitest.config*|jest.config*|*/jest.config*|eslint.config*|*/eslint.config*|biome.json|*/biome.json)
    MSG="Build/test/lint configuration changed. AI context files may reference outdated tooling."
    ;;
  *)
    echo '{}'; exit 0
    ;;
esac

if [ -n "$MSG" ]; then
  MSG_JSON=$(printf '%s' "$MSG" | sed 's/"/\\"/g')
  cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "CONTEXT REMINDER: $MSG_JSON Run /ai-context audit to check."
  }
}
EOF
else
  echo '{}'
fi
