#!/bin/bash
# release-guard.sh — PreToolUse hook for Bash tool
# Blocks pushes to master/main, tag creation, releases, and PR merging.
# Feature branch pushes are ALLOWED.
# DO NOT MODIFY — protected by release-guard-protect.sh

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$COMMAND" ] && echo '{}' && exit 0

BLOCKED=false
REASON=""

# ── git push — block only if targeting master/main ────────────────

if echo "$COMMAND" | grep -qPi '\bgit\b.*\bpush\b' && \
   ! echo "$COMMAND" | grep -qPi '\bgit\s+stash\b'; then

  # Broad operations that could affect master or trigger releases
  if echo "$COMMAND" | grep -qPi '\bpush\b.*--(all|mirror|tags|follow-tags)'; then
    BLOCKED=true
    REASON="git push with --all/--mirror/--tags/--follow-tags is blocked."
  fi

  # Explicitly mentions master/main as push target
  if echo "$COMMAND" | grep -qPi '\bpush\b.*\b(master|main)\b'; then
    BLOCKED=true
    REASON="git push to master/main is blocked."
  fi

  # Refspec targeting master/main (e.g. HEAD:master, feature:refs/heads/main)
  if echo "$COMMAND" | grep -qP ':(refs/heads/)?(master|main)\b'; then
    BLOCKED=true
    REASON="git push with refspec targeting master/main is blocked."
  fi

  # No explicit branch target — check if current branch is master/main
  if [ "$BLOCKED" = false ]; then
    PUSH_ARGS=$(echo "$COMMAND" | grep -oP '(?i)\bpush\b\K[^;&|]*' | head -1)
    PUSH_NOFLAG=$(echo "$PUSH_ARGS" | sed -E 's/(^|\s)--?[a-zA-Z][a-zA-Z0-9_-]*//g' | xargs)
    PUSH_BRANCH=$(echo "$PUSH_NOFLAG" | awk '{print $2}')

    if [ -z "$PUSH_BRANCH" ] || [ "$PUSH_BRANCH" = "HEAD" ]; then
      CURRENT=$(git branch --show-current 2>/dev/null || echo "")
      if [ "$CURRENT" = "master" ] || [ "$CURRENT" = "main" ]; then
        BLOCKED=true
        REASON="git push on master/main without explicit feature branch target is blocked. Use: git push -u origin <feature-branch>"
      fi
    fi
  fi
fi

# ── git tag with version arg ─────────────────────────────────────

if echo "$COMMAND" | grep -qPi '\bgit\s+tag\b' && \
   echo "$COMMAND" | grep -qP 'v\d' && \
   ! echo "$COMMAND" | grep -qPi '\bgit\s+tag\s+(-[ldv]\b|--list|--delete|--verify)'; then
  BLOCKED=true
  REASON="git tag creation is blocked. Tags must be created manually by Nathan."
fi

# ── gh release create ────────────────────────────────────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+release\s+create\b'; then
  BLOCKED=true
  REASON="gh release create is blocked. Releases must be created manually by Nathan."
fi

# ── gh pr merge — allow dev, block master/main ──────────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+pr\s+merge\b'; then
  PR_NUM=$(echo "$COMMAND" | grep -oP '\bgh\s+pr\s+merge\s+\K\d+')
  if [ -n "$PR_NUM" ]; then
    PR_BASE=$(gh pr view "$PR_NUM" --json baseRefName -q .baseRefName 2>/dev/null || echo "unknown")
    if [ "$PR_BASE" = "dev" ]; then
      : # Allow merges to dev (TestPyPI/staging)
    else
      BLOCKED=true
      REASON="gh pr merge to '$PR_BASE' is blocked. Only merges to dev are allowed. Master merges must be done manually by Nathan."
    fi
  else
    BLOCKED=true
    REASON="gh pr merge without a PR number is blocked. Use: gh pr merge <number>"
  fi
fi

# ── Self-protection ──────────────────────────────────────────────

if echo "$COMMAND" | grep -qF 'release-guard' && \
   echo "$COMMAND" | grep -qPi '\b(rm|mv|cp|install|dd|tee|chmod|chown|unlink|truncate|shred|ln)\b|>\s'; then
  BLOCKED=true
  REASON="Cannot modify release guard files via shell."
fi

if echo "$COMMAND" | grep -qF 'hooks.json' && \
   echo "$COMMAND" | grep -qPi '\b(rm|mv|cp|install|dd|sed|awk|perl|python|ruby|node|tee|truncate|ln)\b|>\s'; then
  BLOCKED=true
  REASON="Cannot modify .claude/hooks.json via shell commands."
fi

# ── Output ───────────────────────────────────────────────────────

if [ "$BLOCKED" = true ]; then
  jq -n --arg reason "$(printf '🛑 RELEASE GUARD: %s\n\nFeature branch and dev branch pushes are allowed. Only master/main, tags, releases, and PR merges are blocked.\n\nTo push a feature branch: git push -u origin <branch>\nTo create a PR to dev: gh pr create --base dev --title "..." --body "..."\nFor master/tags/releases: Nathan runs these manually.' "$REASON")" \
    '{"decision": "block", "reason": $reason}'
else
  echo '{}'
fi
