#!/bin/bash
# kaizen-stop-nudge.sh
# Hook: Stop
# Purpose: Nudge Claude to run /kaizen once at session end — but ONLY on
#          sessions that actually earned a process-learning capture:
#          substantive (>= KAIZEN_MIN_EDITS file edits) OR friction
#          (a tool error / guardrail block occurred). Advisory only.
# Tier: 1 (Nudge) — does not force; Claude can still stop.
# Installed by: Nathan wires the Stop entry into .claude/hooks.json
#               (this script is authored by Claude; hooks.json is
#               Edit/Write-protected, so registration is a propose-to-Nathan
#               step). Until wired, /kaizen self-invokes — no functional gap.
#
# Skip conditions (any → allow stop, no nudge):
#   - stop_hook_active (we already nudged once this cycle — never loop)
#   - Untether / cron session (UNTETHER_SESSION set) — single-message
#     integrity in Telegram's output model; /kaizen self-invokes there
#   - /kaizen already ran this session (transcript contains "## /kaizen")
#   - session not substantive and no friction
#   - transcript missing / unreadable (fail OPEN — never block on our own error)
#
# Claude Code only — other tools do not support Claude Code hooks.

set -euo pipefail

KAIZEN_MIN_EDITS="${KAIZEN_MIN_EDITS:-3}"

INPUT=$(cat)

# jq is required to parse the hook payload; if absent, fail open.
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }

# 1. Never loop: if we already blocked once this cycle, let Claude stop.
STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null || echo false)
[ "$STOP_ACTIVE" = "true" ] && { echo '{}'; exit 0; }

# 2. Skip Untether / cron sessions (single-message integrity; /kaizen self-invokes there).
[ -n "${UNTETHER_SESSION:-}" ] && { echo '{}'; exit 0; }

# 3. Locate the session transcript; fail open if we can't read it.
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || echo "")
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"
[ -n "$TRANSCRIPT" ] && [ -r "$TRANSCRIPT" ] || { echo '{}'; exit 0; }

# 4. Already captured this session? "## /kaizen" is the header /kaizen always
#    prints (even for 0 captures). Grep for it literally. (Our own reason text
#    below deliberately avoids that literal so it can't self-trigger.)
if grep -qF '## /kaizen' "$TRANSCRIPT" 2>/dev/null; then
  echo '{}'; exit 0
fi

# 5. Substantive? Count Edit/Write/MultiEdit/NotebookEdit tool_use blocks.
EDIT_COUNT=$({ grep -oE '"name"[[:space:]]*:[[:space:]]*"(Edit|Write|MultiEdit|NotebookEdit)"' "$TRANSCRIPT" 2>/dev/null || true; } | wc -l | tr -d ' ')
EDIT_COUNT="${EDIT_COUNT:-0}"

# 6. Friction? A tool error, a permission deny, or a guardrail BLOCK.
FRICTION=false
if grep -qE '"is_error"[[:space:]]*:[[:space:]]*true|"permissionDecision"[[:space:]]*:[[:space:]]*"deny"|BLOCKED:' "$TRANSCRIPT" 2>/dev/null; then
  FRICTION=true
fi

# 7. Decide: nudge only if the session earned a capture.
if [ "$EDIT_COUNT" -ge "$KAIZEN_MIN_EDITS" ] || [ "$FRICTION" = true ]; then
  REASON=$(printf '%s' \
"Before ending: this was a substantive/friction session (edits: ${EDIT_COUNT}, friction: ${FRICTION}). "\
"Consider running the /kaizen command to capture 0-3 evidence-linked process learnings on the [kaizen] "\
"collector issue. Capturing nothing is a valid outcome — do not manufacture noise. This is advisory only; "\
"you may stop without it. (See docs/kaizen/README.md and .claude/rules/kaizen.md.)")
  jq -n --arg reason "$REASON" '{decision:"block", reason:$reason}'
  exit 0
fi

# Not substantive, no friction → nothing to capture.
echo '{}'
exit 0
