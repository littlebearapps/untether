# AI Context File Quality Standards

When generating or updating AI context files (CLAUDE.md, AGENTS.md, GEMINI.md, .cursorrules, copilot-instructions.md, .windsurfrules, .clinerules), follow these standards.

## Cross-File Consistency

All context files for a project must agree on:

- Language and framework version
- Key commands (test, build, lint, deploy)
- Directory structure and key file paths
- Naming conventions and coding standards
- Critical rules and constraints

When updating one context file, check if the same information appears in others and update them too.

## Path Verification

Every file path mentioned in a context file must exist on disk. Before writing a context file, verify referenced paths:

```bash
test -f "path/to/file" || echo "WARN: path does not exist"
```

Never reference deleted files, renamed modules, or moved directories without checking first.

## Version Accuracy

Context files must reference the correct:

- Language runtime version (from `.nvmrc`, `engines`, `requires-python`, `go.mod`)
- Framework version (from `package.json`, `pyproject.toml`)
- Test runner (jest vs vitest vs pytest vs go test)
- Linter/formatter (eslint vs biome, ruff vs flake8)

## Command Accuracy

Every command listed in a context file (test, build, lint, deploy) must be runnable. Verify against `package.json` scripts, `Makefile` targets, or `pyproject.toml` scripts before writing.

## Sync Points

When these project changes occur, update the corresponding context files:

| Change | Files to Update |
|--------|----------------|
| New command or skill | AGENTS.md, CLAUDE.md, llms.txt |
| New dependency | All context files referencing tech stack |
| File rename or move | All context files referencing file paths |
| Test runner change | All context files listing test commands |
| New rule or convention | All context files listing coding standards |
| Architecture change | AGENTS.md, CLAUDE.md (architecture section) |
| New agent | AGENTS.md |

## Tool Compatibility

Not all context files work in all tools:

| File | Works In | Does NOT Work In |
|------|----------|-----------------|
| `AGENTS.md` | Claude Code, OpenCode, Codex CLI, Gemini CLI | — |
| `CLAUDE.md` | Claude Code, OpenCode (fallback) | Cursor, Copilot |
| `.cursorrules` | Cursor | Claude Code, OpenCode |
| `.github/copilot-instructions.md` | GitHub Copilot | Claude Code, Cursor |
| `.windsurfrules` | Windsurf | Claude Code, Cursor |
| `.clinerules` | Cline | Claude Code, Cursor |
| `GEMINI.md` | Gemini CLI | Claude Code, Cursor |
| `.claude/rules/*.md` | Claude Code only | OpenCode, Codex CLI, Cursor |
| Claude Code hooks | Claude Code only | OpenCode, Codex CLI, all others |
