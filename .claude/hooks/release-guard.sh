#!/bin/bash
# release-guard.sh — PreToolUse hook for Bash tool
# Blocks pushes to master/main, tag creation and pushes, releases, and PR
# merging outside dev. Asks before restarting a non-dev Untether service.
# Feature branch pushes are ALLOWED.
# Registered in .claude/settings.json (#915).
# DO NOT MODIFY — protected by release-guard-protect.sh

set -euo pipefail
# Fail closed: an internal error exits 2, which blocks the call (exit 1 would let it through).
trap 'echo "release-guard.sh: internal error at line $LINENO — blocked to be safe" >&2; exit 2' ERR

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$COMMAND" ] && echo '{}' && exit 0

BLOCKED=false
REASON=""
ASK=false
ASK_REASON=""

# A branch name is master/main only as a whole token: `origin main`,
# `HEAD:main`, `refs/heads/main` — never `fix/main-menu` or `maintenance`.
BRANCH_RE='(?<![\w/.-])(refs/heads/)?(master|main)(?![\w/.-])'

# ── git push — block only if targeting master/main ────────────────

if echo "$COMMAND" | grep -qPi '\bgit\b.*\bpush\b' && \
   ! echo "$COMMAND" | grep -qPi '\bgit\s+stash\b'; then

  # Broad operations that could affect master or trigger releases
  if echo "$COMMAND" | grep -qPi '\bpush\b.*--(all|mirror|tags|follow-tags)'; then
    BLOCKED=true
    REASON="git push with --all/--mirror/--tags/--follow-tags is blocked."
  fi

  # Explicitly mentions master/main as push target
  if echo "$COMMAND" | grep -qPi "\\bpush\\b.*${BRANCH_RE}"; then
    BLOCKED=true
    REASON="git push to master/main is blocked."
  fi

  # Refspec targeting master/main (e.g. HEAD:master, feature:refs/heads/main)
  if echo "$COMMAND" | grep -qP ':(refs/heads/)?(master|main)(?![\w/.-])'; then
    BLOCKED=true
    REASON="git push with refspec targeting master/main is blocked."
  fi

  # Pushing a version tag by name (git push origin v1.2.3 / refs/tags/...)
  if echo "$COMMAND" | grep -qP '\bpush\b.*((?<![\w/.-])v\d+\.\d+|refs/tags/)'; then
    BLOCKED=true
    REASON="git push of a version tag is blocked. Tags are created by auto-tag-on-master.yml."
  fi

  # No explicit branch target — check if current branch is master/main
  if [ "$BLOCKED" = false ]; then
    PUSH_ARGS=$(echo "$COMMAND" | grep -oP '(?i)\bpush\b\K[^;&|]*' | head -1 || true)
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

# ── gh api writes that merge, release or move refs ───────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+api\b' && \
   echo "$COMMAND" | grep -qPi '/(pulls/\d+/merge|merges|releases|git/refs|git/tags)\b' && \
   echo "$COMMAND" | grep -qP '(^|\s)(-X|--method|-f|-F|--field|--raw-field|--input)\b'; then
  BLOCKED=true
  REASON="gh api writes to merge, release, tag or ref endpoints are blocked. Use gh pr merge <number> for dev-targeting PRs."
fi

# ── gh pr merge — allow dev, block master/main ──────────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+pr\s+merge\b'; then
  PR_NUM=$(echo "$COMMAND" | grep -oP '\bgh\s+pr\s+merge\s+\K\d+' || true)
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

# The project .claude/settings.json registers these hooks. The user-level
# ~/.claude/settings.json is not covered here (only disableAllHooks is).
PROJECT_CMD=$(echo "$COMMAND" | sed -E "s#(~|\\\$HOME|\\\$\\{HOME\\}|${HOME})/\\.claude/settings#USER_SETTINGS#g")
if echo "$PROJECT_CMD" | grep -qP '\.claude/settings\.json|hooks\.json' && \
   echo "$PROJECT_CMD" | grep -qPi '\b(rm|mv|cp|install|dd|sed|awk|perl|python3?|ruby|node|tee|truncate|ln|checkout|restore)\b|>\s'; then
  BLOCKED=true
  REASON="Cannot modify .claude/settings.json (the hook registration) via shell commands."
fi

if echo "$COMMAND" | grep -qF 'disableAllHooks' && \
   echo "$COMMAND" | grep -qPi '\b(cp|mv|install|dd|sed|awk|perl|python3?|ruby|node|jq|tee|truncate|ln|claude)\b|>\s?'; then
  BLOCKED=true
  REASON="disableAllHooks would switch off the release guard. Only Nathan sets it."
fi

# ── Restarting a non-dev Untether service — ask first ────────────
#
# Staging (untether.service) runs a PyPI/TestPyPI wheel, so restarting it
# never tests local code; the fleet hosts are production. Restarting from
# inside an active Untether session also drops the final message. The
# legitimate paths are scripts/staging.sh install, pipx upgrade and
# scripts/fleet-rollout.sh, which restart internally.

if echo "$COMMAND" | grep -qP '\bsystemctl\b.*\b(restart|start|stop|kill|reload|try-restart|reload-or-restart)\b.*(?<![\w-])untether(\.service)?(?![\w.-])' || \
   echo "$COMMAND" | grep -qP '\blaunchctl\b.*\b(kickstart|stop|start|bootout|unload)\b.*com\.littlebearapps\.untether(?![\w.-])'; then
  if ! echo "$COMMAND" | grep -qP 'staging\.sh\s+install|pipx\s+(upgrade|install)\b.*\buntether\b'; then
    ASK=true
    ASK_REASON="⚠️ This restarts a non-dev Untether service (staging or a fleet host). Staging runs a PyPI/TestPyPI wheel, so local code changes have no effect on it — test with: systemctl --user restart untether-dev. Upgrade with scripts/staging.sh install or scripts/fleet-rollout.sh. Never restart from inside an active Untether session."
  fi
fi

# ── Output ───────────────────────────────────────────────────────
#
# Claude Code PreToolUse hook output schema (current as of 2026-04-15):
#   {
#     "hookSpecificOutput": {
#       "hookEventName": "PreToolUse",
#       "permissionDecision": "deny" | "ask" | "allow",
#       "permissionDecisionReason": "<text>"
#     }
#   }
# The legacy {"decision":"block","reason":...} shape is silently ignored, so
# blocks return as no-ops. See https://code.claude.com/docs/en/hooks for the
# spec.

if [ "$BLOCKED" = true ]; then
  REASON_FULL=$(printf '🛑 RELEASE GUARD: %s\n\nFeature branch and dev branch pushes are allowed. Only master/main, tags, releases, and PR merges are blocked.\n\nTo push a feature branch: git push -u origin <branch>\nTo create a PR to dev: gh pr create --base dev --title "..." --body "..."\nFor master/tags/releases: Nathan runs these manually.' "$REASON")
  jq -n --arg r "$REASON_FULL" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
elif [ "$ASK" = true ]; then
  jq -n --arg r "$ASK_REASON" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: $r
    }
  }'
else
  echo '{}'
fi
