#!/bin/bash
# context-guard-stop.sh
# Hook: Stop
# Purpose: Nudge Claude to update AI context files before ending a session
#          when structural files (commands, skills, rules, config) were modified
#          but context docs (CLAUDE.md, AGENTS.md, etc.) were not.
# Tier: 1 (Nudge) — advisory, does not force; Claude can still stop
# Installed by: /contextdocs:context-guard install
#
# Claude Code only — OpenCode, Codex CLI, Cursor, and other tools
# do not support Claude Code hooks.

set -euo pipefail

# Read hook input from stdin
INPUT=$(cat)

# CRITICAL: Prevent infinite loops.
# When stop_hook_active is true, Claude is already continuing due to a previous
# Stop hook block. Allow it to stop this time.
STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null)
[ "$STOP_ACTIVE" = "true" ] && echo '{}' && exit 0

# Skip in Untether sessions — Stop hook blocks displace user-requested
# content in Telegram's single-message output model.
[ -n "${UNTETHER_SESSION:-}" ] && echo '{}' && exit 0

# Resolve project directory
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}"
cd "$PROJECT_DIR" || { echo '{}'; exit 0; }

# Must be inside a git repository
git rev-parse --is-inside-work-tree &>/dev/null || { echo '{}'; exit 0; }

# Check working tree + staged changes for structural file patterns
CHANGED_FILES=$(git status --porcelain 2>/dev/null | awk '{print $NF}')
[ -z "$CHANGED_FILES" ] && echo '{}' && exit 0

# Structural file patterns that warrant context doc updates
HAS_STRUCTURAL=false
while IFS= read -r FILE; do
  case "$FILE" in
    # Skip Context Guard's own infrastructure — not project structural changes
    .claude/hooks/*|.claude/rules/context-quality.md|.claude/settings.json) continue ;;
    commands/*.md) HAS_STRUCTURAL=true; break ;;
    .claude/skills/*/SKILL.md) HAS_STRUCTURAL=true; break ;;
    .agents/skills/*/SKILL.md) HAS_STRUCTURAL=true; break ;;
    .claude/agents/*.md) HAS_STRUCTURAL=true; break ;;
    .agents/agents/*.md) HAS_STRUCTURAL=true; break ;;
    .claude/rules/*.md) HAS_STRUCTURAL=true; break ;;
    package.json) HAS_STRUCTURAL=true; break ;;
    pyproject.toml) HAS_STRUCTURAL=true; break ;;
    Cargo.toml) HAS_STRUCTURAL=true; break ;;
    go.mod) HAS_STRUCTURAL=true; break ;;
    tsconfig*.json) HAS_STRUCTURAL=true; break ;;
    wrangler.toml) HAS_STRUCTURAL=true; break ;;
    vitest.config*) HAS_STRUCTURAL=true; break ;;
    jest.config*) HAS_STRUCTURAL=true; break ;;
    eslint.config*) HAS_STRUCTURAL=true; break ;;
    biome.json) HAS_STRUCTURAL=true; break ;;
    .claude-plugin/plugin.json) HAS_STRUCTURAL=true; break ;;
  esac
done <<< "$CHANGED_FILES"

# Fast exit if no structural files changed (most sessions)
[ "$HAS_STRUCTURAL" = false ] && echo '{}' && exit 0

# Check if any context files were also modified
HAS_CONTEXT=false
while IFS= read -r FILE; do
  case "$FILE" in
    CLAUDE.md|AGENTS.md|GEMINI.md) HAS_CONTEXT=true; break ;;
    .cursorrules|.windsurfrules|.clinerules) HAS_CONTEXT=true; break ;;
    .github/copilot-instructions.md) HAS_CONTEXT=true; break ;;
    llms.txt) HAS_CONTEXT=true; break ;;
  esac
done <<< "$CHANGED_FILES"

# Collect which structural files changed for the agent
STRUCTURAL_LIST=""
while IFS= read -r FILE; do
  case "$FILE" in
    .claude/hooks/*|.claude/rules/context-quality.md|.claude/settings.json) continue ;;
    commands/*.md|.claude/skills/*/SKILL.md|.agents/skills/*/SKILL.md|.claude/agents/*.md|.agents/agents/*.md|.claude/rules/*.md|package.json|pyproject.toml|Cargo.toml|go.mod|tsconfig*.json|wrangler.toml|vitest.config*|jest.config*|eslint.config*|biome.json|.claude-plugin/plugin.json)
      STRUCTURAL_LIST="$STRUCTURAL_LIST\n  - $FILE"
      ;;
  esac
done <<< "$CHANGED_FILES"

# If structural changes exist without context updates, instruct agent launch
if [ "$HAS_CONTEXT" = false ]; then
  cat << EOF
{
  "decision": "block",
  "reason": "CONTEXT DRIFT DETECTED — structural files changed this session without context file updates:\\n$STRUCTURAL_LIST\\n\\nAffected context files likely include: AGENTS.md, CLAUDE.md, llms.txt\\n\\nACTION REQUIRED: Launch the context-updater agent (defined in .claude/agents/context-updater.md) to update these files before ending the session. The agent will apply surgical edits to only the affected sections. If context docs genuinely don't need changes, you can finish — this is advisory only."
}
EOF
  exit 0
fi

echo '{}'
