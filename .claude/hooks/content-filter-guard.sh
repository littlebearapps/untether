#!/bin/bash
# content-filter-guard.sh
# Hook: PreToolUse (Write)
# Purpose: Prevent content filter errors by intercepting Write operations
#          on files known to trigger Claude Code's API content filter (HTTP 400).
#          HIGH-risk files are blocked with a fetch-from-URL suggestion.
#          MEDIUM-risk files pass through with a chunked-writing advisory.
# Installed by: /context-guard install
#
# Claude Code only — OpenCode, Codex CLI, Cursor, and other tools
# do not support Claude Code hooks.

set -euo pipefail

# Read hook input from stdin
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

# Only process Write operations
[ "$TOOL_NAME" != "Write" ] && echo '{}' && exit 0
[ -z "$FILE_PATH" ] && echo '{}' && exit 0

# Extract just the filename for matching
FILENAME=$(basename "$FILE_PATH")

# HIGH-risk files: BLOCK the write
case "$FILENAME" in
  CODE_OF_CONDUCT.md|CODE_OF_CONDUCT.MD)
    cat << 'EOF'
{
  "decision": "block",
  "reason": "CODE_OF_CONDUCT.md is HIGH risk for content filter errors (HTTP 400). Fetch from the canonical URL instead:\n\ncurl -sL \"https://www.contributor-covenant.org/version/3/0/code_of_conduct/code_of_conduct.md\" -o CODE_OF_CONDUCT.md\n\nThen use Edit to replace [INSERT CONTACT METHOD] with the project's contact details."
}
EOF
    exit 1
    ;;
  LICENSE|LICENSE.md|LICENSE.txt|LICENCE|LICENCE.md|LICENCE.txt)
    cat << 'EOF'
{
  "decision": "block",
  "reason": "LICENSE is HIGH risk for content filter errors (HTTP 400). Fetch from SPDX instead:\n\ncurl -sL \"https://raw.githubusercontent.com/spdx/license-list-data/main/text/MIT.txt\" -o LICENSE\n\nReplace MIT with the appropriate SPDX identifier. Then use Edit to fill in [year] and [fullname]."
}
EOF
    exit 1
    ;;
  SECURITY.md|SECURITY.MD)
    cat << 'EOF'
{
  "decision": "block",
  "reason": "SECURITY.md is MEDIUM-HIGH risk for content filter errors (HTTP 400). Fetch a template first:\n\ncurl -sL \"https://raw.githubusercontent.com/github/.github/main/SECURITY.md\" -o SECURITY.md\n\nNote: This fetches GitHub's own security policy. Use Edit to replace all GitHub-specific references with the project's details, including reporting method, response timeline, and supported versions."
}
EOF
    exit 1
    ;;
esac

# MEDIUM-risk files: ALLOW but advise
case "$FILENAME" in
  CHANGELOG.md|CHANGELOG.MD)
    cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "CONTENT FILTER ADVISORY: CHANGELOG.md is MEDIUM risk. Keep this write under 15 lines of template-like content. For larger changelogs, write in chunks of 5-10 entries and use Edit to append subsequent sections."
  }
}
EOF
    exit 0
    ;;
  CONTRIBUTING.md|CONTRIBUTING.MD)
    cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "CONTENT FILTER ADVISORY: CONTRIBUTING.md is LOW-MEDIUM risk. Start with project-specific content (development setup, actual commands) before generic sections (commit conventions, code review). Keep each write under 15 lines of template-like content."
  }
}
EOF
    exit 0
    ;;
esac

# All other files: pass through
echo '{}'
