#!/bin/bash
# release-guard.sh — PreToolUse hook for Bash tool
# Blocks direct pushes to master/main, tag creation and pushes, and
# `gh release create`. Merging the dev→master release PR is allowed only via
# `gh pr merge <n>` with green CI, and always asks Nathan to confirm (#917).
# Asks before restarting a non-dev Untether service.
# Feature branch pushes are ALLOWED.
# Registered in .claude/settings.json (#915).
# DO NOT MODIFY — protected by release-guard-protect.sh

set -euo pipefail
# Fail closed: an internal error exits 2, which blocks the call (exit 1 would let it through).
trap 'echo "release-guard.sh: internal error at line $LINENO — blocked to be safe" >&2; exit 2' ERR

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$COMMAND" ] && echo '{}' && exit 0
# gh's global repo flag can sit before the subcommand (`gh -R o/r pr merge 2`).
# Drop it so every gh check below sees `gh <subcommand>`. The value must be a
# plain [HOST/]OWNER/REPO followed by whitespace, so the strip can never
# swallow shell syntax (`gh -R x;git push origin master`).
# It can also sit between the command group and the subcommand
# (`gh pr -R o/r merge 2`), so strip it in both places (two passes).
ORIG_COMMAND="$COMMAND"
GH_REPO_RE="(\s+(-R|--repo)(=|\s+)[\"']?[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){0,2}[\"']?)+"
for _ in 1 2; do
  COMMAND=$(printf '%s' "$COMMAND" | sed -E "s#\bgh((\s+[a-z][a-z-]*)?)${GH_REPO_RE}(\s|\$)#gh\1\7#g")
done

BLOCKED=false
REASON=""

# Any other `gh [group] -R/--repo <value>` before the subcommand ($VAR, odd
# quoting, shell metacharacters) can't be checked reliably — fail closed.
if printf '%s' "$COMMAND" | grep -qP '\bgh(\s+[a-z][a-z-]*)?\s+(-R|--repo)\b'; then
  BLOCKED=true
  REASON="gh -R/--repo with a value that isn't a plain owner/repo is blocked. Use a literal owner/repo, or put -R after the subcommand."
fi
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
  REASON="git tag creation is blocked. Tags are created by auto-tag-on-master.yml when the release PR merges."
fi

# ── gh release create ────────────────────────────────────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+release\s+create\b'; then
  BLOCKED=true
  REASON="gh release create is blocked. release.yml creates the GitHub release when the release PR merges."
fi

# ── gh api writes that merge, release or move refs ───────────────

if echo "$COMMAND" | grep -qPi '\bgh\s+api\b' && \
   echo "$COMMAND" | grep -qPi '/(pulls/\d+/merge|merges|releases|git/refs|git/tags)\b' && \
   echo "$COMMAND" | grep -qP '(^|\s)(-X|--method|-f|-F|--field|--raw-field|--input)\b'; then
  BLOCKED=true
  REASON="gh api writes to merge, release, tag or ref endpoints are blocked. Use gh pr merge <number>."
fi

# The GraphQL API reaches the same operations without a REST path.
if echo "$COMMAND" | grep -qPi '\bgh\s+api\s+graphql\b' && \
   echo "$COMMAND" | grep -qPi '\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch|createRef|updateRefs?|deleteRef|createRelease|updateRelease)\b'; then
  BLOCKED=true
  REASON="gh api graphql merge, release or ref mutations are blocked. Use gh pr merge <number>."
fi

# ── Manually dispatching the release pipeline — ask first ────────
#
# The #376 fallback (`gh workflow run release.yml --ref vX.Y.Z`) publishes to
# PyPI, so it needs the same confirmation as the release merge.

if echo "$COMMAND" | grep -qPi '\bgh\s+workflow\s+run\b.*\b(release|auto-tag-on-master)(\.ya?ml)?\b'; then
  ASK=true
  ASK_REASON="🚀 RELEASE: this dispatches the PyPI release pipeline by hand. Approve only if Nathan explicitly approved publishing this version."
fi

# ── gh pr merge — dev freely; master only as a confirmed release ──
#
# dev → TestPyPI: allowed. master/main is the release gate (auto-tag →
# release.yml → PyPI → fleet). Claude may merge only the dev→master release PR,
# only with green CI, and always as "ask" so Nathan confirms in the permission
# prompt (#917). `--admin` bypasses GitHub's review and CI rules, which is why
# this hook re-checks CI itself.

if echo "$COMMAND" | grep -qPi '\bgh\s+pr\s+merge\b'; then
  PR_NUM=$(echo "$COMMAND" | grep -oP '\bgh\s+pr\s+merge\s+\K\d+' || true)
  # The lookup below checks littlebearapps/untether; gh merges in whatever repo
  # -R/--repo, GH_REPO or a `cd` selects. Refuse anything that could differ.
  OTHER_REPO=$(printf '%s' "$ORIG_COMMAND" | grep -oP '(?:^|\s)(?:-R|--repo)(?:=|\s+)["'"'"']?\K[^\s"'"'"']+' | grep -vxP '(github\.com/)?littlebearapps/untether' || true)
  if [ -n "$OTHER_REPO" ] || printf '%s' "$ORIG_COMMAND" | grep -qP '\bGH_REPO=|\bGH_HOST=|(^|[\s;&|(])(cd|pushd)\s'; then
    BLOCKED=true
    REASON="gh pr merge here may only target littlebearapps/untether from this checkout (no other -R/--repo, GH_REPO, GH_HOST or cd). Merge other repos' PRs from their own checkout."
  elif [ -z "$PR_NUM" ]; then
    BLOCKED=true
    REASON="gh pr merge without a PR number is blocked. Use: gh pr merge <number>"
  else
    PR_JSON=$(gh pr view "$PR_NUM" --repo littlebearapps/untether --json baseRefName,headRefName,title,statusCheckRollup 2>/dev/null || echo '{}')
    PR_BASE=$(echo "$PR_JSON" | jq -r '.baseRefName // "unknown"' 2>/dev/null || echo "unknown")
    if [ "$PR_BASE" = "dev" ]; then
      : # Allow merges to dev (TestPyPI/staging)
    elif [ "$PR_BASE" = "master" ] || [ "$PR_BASE" = "main" ]; then
      PR_HEAD=$(echo "$PR_JSON" | jq -r '.headRefName // ""')
      PR_TITLE=$(echo "$PR_JSON" | jq -r '.title // ""')
      CHECKS_TOTAL=$(echo "$PR_JSON" | jq -r '[.statusCheckRollup[]?] | length')
      CHECKS_NOT_GREEN=$(echo "$PR_JSON" | jq -r '[.statusCheckRollup[]? | (.conclusion // .state // "") | ascii_upcase | select(. != "SUCCESS" and . != "SKIPPED" and . != "NEUTRAL")] | length')
      if [ "$PR_HEAD" != "dev" ]; then
        BLOCKED=true
        REASON="Only the dev→master release PR may be merged to $PR_BASE (PR #$PR_NUM's head is '$PR_HEAD')."
      elif echo "$COMMAND" | grep -qP '(^|\s)(-d|--delete-branch)\b'; then
        BLOCKED=true
        REASON="Merging the release PR with --delete-branch would delete dev. Drop the flag."
      elif [ "$CHECKS_TOTAL" = "0" ] || [ "$CHECKS_NOT_GREEN" != "0" ]; then
        BLOCKED=true
        REASON="Release PR #$PR_NUM has $CHECKS_NOT_GREEN of $CHECKS_TOTAL checks not green (pending, failed or none reported). Wait for CI to pass, then retry."
      else
        ASK=true
        ASK_REASON="🚀 RELEASE: merge PR #$PR_NUM \"$PR_TITLE\" (dev → $PR_BASE). This publishes to PyPI (auto-tag → release.yml) and makes it the stable release. CI: $CHECKS_TOTAL checks green. Approve only if Nathan explicitly approved this release."
      fi
    else
      BLOCKED=true
      REASON="gh pr merge blocked: couldn't confirm PR #$PR_NUM's base branch ('$PR_BASE')."
    fi
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
# spec. A hook "ask" forces the prompt even in auto mode (CLI ≥ 2.1.211); in an
# unattended -p run nobody can answer it, so the call is denied.

if [ "$BLOCKED" = true ]; then
  REASON_FULL=$(printf '🛑 RELEASE GUARD: %s\n\nAllowed: feature/dev branch pushes, PRs to dev, gh pr merge <n> for dev PRs, and — with Nathan'"'"'s explicit approval and green CI — gh pr merge <n> --squash --admin for the dev→master release PR (the hook asks him to confirm).\nBlocked: direct pushes to master/main, tags, gh release create.' "$REASON")
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
