# Audit: PitchDocs Context Guard Interference with Untether

**Date**: 2026-03-09
**Severity**: Medium — causes content loss in Telegram sessions
**Affected**: Untether Telegram bridge + PitchDocs Claude Code plugin (context-guard)

## Incident

A user in the BIP project chat (via Untether production, `@hetz_lba1_bot`) asked Claude Code to find and outline a backlinks document. Claude completed the task successfully (rc=0, 46.7s, 3 tool calls) but the user received only this 170-character response:

> No files were modified in this interaction — I only read the backlinks doc and outlined it in the chat. The hook fired as a false positive. No context doc updates needed.

The actual document outline was generated in an intermediate assistant turn but was **replaced** by this hook-response message in the final output. The user never saw the outline.

## Root Cause

Two compounding issues create the content loss:

### 1. PitchDocs Stop hook false positive

The `context-guard-stop.sh` hook (installed by PitchDocs `/context-guard install`) fires at session end and checks whether structural files were modified without corresponding context document updates.

**The detection mechanism**:
```bash
CHANGED_FILES=$(git status --porcelain 2>/dev/null | awk '{print $NF}')
```

This checks ALL dirty files in the working tree — not just files modified in the current Claude Code session. In the BIP project, PitchDocs had been recently installed, leaving untracked infrastructure files:
- `.claude/rules/context-quality.md` — matches structural pattern `.claude/rules/*.md`
- `.claude/hooks/*` — hook scripts themselves
- `.claude/settings.json` — plugin settings

Meanwhile, `CLAUDE.md` had already been updated and committed in a previous session, so it appeared clean in `git status`. The hook logic:
1. Found structural files dirty → `HAS_STRUCTURAL=true`
2. Found no context docs dirty → `HAS_CONTEXT=false`
3. Returned `"decision": "block"` with a nudge to update context docs

**This is a false positive** — context docs were already up to date. The structural "changes" were just the hook infrastructure itself, not actual project structure changes.

### 2. Content displacement in Untether

When a Stop hook returns `"decision": "block"`, Claude Code gets one more turn to address the concern before stopping. In a terminal session this is fine — the user can scroll up to see earlier output. But in Untether's Telegram model:

1. Intermediate assistant text appears as **progress message edits** (each new turn replaces the previous)
2. The `result.result` text from the final `CompletedEvent` becomes the **persistent final message**
3. If Claude's final turn addresses a hook concern instead of user-requested content, that meta-commentary becomes the only thing the user sees
4. The actual content (the outline) was in an earlier turn and is lost

## Cross-Project Comparison

All 4 LBA projects with context-guard installed use **identical hook scripts**. The difference is git working tree state:

| Project | Structural files dirty? | Context docs dirty? | Hook fires? | Hook blocks? |
|---------|------------------------|-------------------|-------------|-------------|
| **BIP** | YES — untracked `.claude/rules/context-quality.md` | NO — `CLAUDE.md` already committed | YES | **YES (false positive)** |
| **Scout** | NO — only `scout-db-export.sql`, `test-probe` | N/A | NO — fast exit | No |
| **Brand Copilot** | YES — 113 dirty files including structural | YES — `CLAUDE.md` also dirty | YES | **No** — context doc also dirty |
| **littlebearapps.com** | N/A — no context-guard installed | N/A | N/A | N/A |

**Pattern**: The false positive occurs when:
1. PitchDocs infrastructure is freshly installed but not committed to git
2. Context docs were already updated in a prior session (clean in `git status`)
3. The current session is read-only (no actual file modifications)

## PitchDocs Recommendations

### P1: Add Untether session detection (high priority)

Stop hooks that block at session end are fundamentally incompatible with Untether's single-message output model. The hook should detect Untether sessions and skip blocking.

**Proposed change** in `context-guard-stop.sh`, after the `stop_hook_active` check:

```bash
# Skip blocking in Untether sessions — Stop hook blocks displace
# user-requested content in the Telegram final message.
[ -n "${UNTETHER_SESSION:-}" ] && echo '{}' && exit 0
```

`UNTETHER_SESSION` is set by Untether's runner environment for all Claude Code subprocess invocations.

### P2: Fix false positive on hook infrastructure files (high priority)

The hook should not trigger on its own infrastructure. Options:

**Option A — Exclude hook infrastructure from structural check** (recommended):
```bash
case "$FILE" in
  .claude/hooks/*) continue ;;          # Hook scripts themselves
  .claude/settings.json) continue ;;     # Plugin settings
  # ... existing structural patterns ...
esac
```

**Option B — Use tracked-only file detection**:
Replace `git status --porcelain` with `git diff --name-only` + `git diff --cached --name-only` to only check tracked files that were actually modified, excluding untracked new files.

**Option C — Auto-commit infrastructure on install**:
After `/context-guard install`, automatically `git add` and commit the hook infrastructure files so they don't pollute `git status` in subsequent sessions.

### P3: Improve context doc freshness detection (medium priority)

The current logic assumes that if context docs aren't dirty, they haven't been updated. But this fails when context docs were updated and committed in a previous session. A more robust check could:
- Compare context doc last-modified timestamps against structural file timestamps
- Check if context docs were updated in the last N commits
- Use a marker file (`.claude/.context-guard-last-audit`) to track when context was last verified

### P4: Reduce hook intrusiveness in read-only sessions (low priority)

If the current session made no file modifications (all tool calls were Read, Grep, Glob, etc.), the Stop hook should not fire. This would require Claude Code to expose session-modified files to the hook, which isn't currently available.

## Untether Recommendations

### U1: Enhance preamble with hook awareness (implementing now)

Add explicit guidance to the Untether preamble telling Claude that hook concerns must never displace user-requested content:

```
- If hooks fire at session end, your final response MUST still contain the user's
  requested content. Hook concerns are secondary — briefly note them AFTER the main
  content, never instead of it.
```

This is advisory and may not always be followed, but it gives Claude clear prioritisation guidance.

### U2: Consider content accumulation (future — optional)

A more robust approach would be to accumulate all assistant text from the session and include it in the final message, rather than only showing the `result.result` text. This would prevent content loss regardless of what the final turn contains. However, this would significantly change the message format and could make messages very long.

## Hook Script Reference

**File**: `context-guard-stop.sh` (PitchDocs v1.19.1)
**Trigger**: Claude Code `Stop` event (session end)
**Behaviour**: Returns `"decision": "block"` when structural files in `git status` have no matching context doc updates
**Infinite loop guard**: Checks `stop_hook_active` flag — allows stop on second attempt
**Structural patterns checked**: `commands/*.md`, `.claude/skills/*/SKILL.md`, `.claude/agents/*.md`, `.claude/rules/*.md`, `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `tsconfig*.json`, `wrangler.toml`, `vitest.config*`, `jest.config*`, `eslint.config*`, `biome.json`, `.claude-plugin/plugin.json`
**Context docs checked**: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `.github/copilot-instructions.md`, `llms.txt`
