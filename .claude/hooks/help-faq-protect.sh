#!/bin/bash
# help-faq-protect.sh — PreToolUse hook for Bash tool
# Blocks deletion / move-out-of-place of `docs/faq/index.md`.
# The file is part of the marketing-site FAQPage Schema.org pipeline
# (issue #477). Removing it breaks the docs-sync mapping registered in
# `littlebearapps/littlebearapps.com:scripts/docs-sync.config.ts` and
# would silently regress AI-citation surface (ChatGPT, Perplexity,
# Google AI Overviews) on the next deploy.
#
# This hook deliberately does NOT block edits — the FAQ is meant to be
# updated as features land. It only blocks destructive ops (rm, git rm,
# mv away, redirected truncation).

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$COMMAND" ] && echo '{}' && exit 0

# Helper: emit Claude Code PreToolUse deny shape (2026+).
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

# Match the canonical path or any plausible relative form. The `-q` is
# safe — empty COMMAND is short-circuited above.
match_target='(^|[^A-Za-z0-9_/])docs/faq/(index\.md|\*|.\*|\.\.|.*\.md)?'

# 1. `rm` / `unlink` / `shred` removing the file or its directory.
if echo "$COMMAND" | grep -qE '(^|[^A-Za-z_])(rm|unlink|shred)([[:space:]]|$)'; then
  if echo "$COMMAND" | grep -qE "$match_target"; then
    deny "🛑 HELP-FAQ PROTECTION: docs/faq/index.md cannot be deleted.

This file backs the marketing-site FAQPage Schema.org pipeline
(see issue #477). Removing it silently regresses AI-citation
surface on the next docs-sync deploy.

You CAN edit it freely — the FAQ should be updated as features
land. To replace content, edit in-place; do not delete and recreate.

To genuinely retire the FAQ, raise an issue first to coordinate
the matching mapping removal in
\`littlebearapps/littlebearapps.com:scripts/docs-sync.config.ts\`."
  fi
fi

# 2. `git rm` removing the file.
if echo "$COMMAND" | grep -qE '\bgit\b[[:space:]]+rm\b'; then
  if echo "$COMMAND" | grep -qE "$match_target"; then
    deny "🛑 HELP-FAQ PROTECTION: docs/faq/index.md cannot be \`git rm\`'d.

The file backs the marketing-site FAQPage Schema.org pipeline (#477).
Edit in place instead. If retirement is genuinely needed, coordinate
with littlebearapps/littlebearapps.com first."
  fi
fi

# 3. `mv` away from docs/faq/.
if echo "$COMMAND" | grep -qE '(^|[^A-Za-z_])mv([[:space:]]|$)'; then
  if echo "$COMMAND" | grep -qE 'docs/faq/index\.md[[:space:]]+[^[:space:]]+'; then
    deny "🛑 HELP-FAQ PROTECTION: docs/faq/index.md cannot be moved.

The path is referenced by the marketing-site docs-sync config
(\`scripts/docs-sync.config.ts\` in littlebearapps/littlebearapps.com).
Renaming/moving silently breaks the FAQPage schema pipeline (#477).

Edit in place. To genuinely relocate, coordinate with the marketing
site first."
  fi
fi

# 4. Redirect truncation: `> docs/faq/index.md` (without `>>` append).
if echo "$COMMAND" | grep -qE '(^|[^>])>[[:space:]]*docs/faq/index\.md\b'; then
  deny "🛑 HELP-FAQ PROTECTION: shell redirect (\`>\`) would truncate docs/faq/index.md.

Use the Edit tool for in-place changes, or \`>>\` to append, so the
file's identity (and the FAQPage schema pipeline #477) is preserved.

If you need to fully replace the file content, use the Write tool —
that's an in-place rewrite, not a deletion."
fi

echo '{}'
