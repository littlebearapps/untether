---
paths:
  - "CLAUDE.md"
  - "AGENTS.md"
  - "llms.txt"
  - ".claude/**"
  - "docs/reference/feature-catalog.md"
  - "docs/reference/test-catalog.md"
---

# AI Context File Quality Standards

Applies when editing `CLAUDE.md`, `AGENTS.md`, `llms.txt`, `.claude/rules/*`, `.claude/skills/*`, commands or agents.

## Context budget (the reason this rule exists)

Everything in `CLAUDE.md` and every rule without `paths:` loads into **every** session. Claude Code warns above ~150k
chars of instruction files in total, and adherence drops as files grow. In 2026-10 `CLAUDE.md` had grown to 91k chars
(487 lines) because feature and test lists were appended every rc.

- **`CLAUDE.md` ≤ ~200 lines / ~15k chars.** Only what an agent can't discover and needs in *every* session: the
  one-liner, architecture, commands, the critical never/always rules (dev vs staging, release guard), and pointers.
- **Feature detail → `docs/reference/feature-catalog.md`. Test-file detail → `docs/reference/test-catalog.md`.**
  Never re-grow a feature list or per-test-file list in `CLAUDE.md`.
- **Every `.claude/rules/*.md` needs `paths:` frontmatter** (a YAML list of globs). Rules without it load
  unconditionally. `applies_to:` is NOT a Claude Code key and is silently ignored. A rule that a workflow command
  loads explicitly ("Load `.claude/rules/x.md`") still needs `paths:`.
- **Rules state invariants (never/always + why), not mechanism walkthroughs.** Mechanism detail goes in
  `docs/reference/**` or a skill's supporting file (e.g. `.claude/skills/claude-stream-json/control-channel-internals.md`),
  which loads only when read.
- Don't restate one fact in several always-loaded places. Pick one home and point to it.
- `<!-- block comments -->` in `CLAUDE.md` are stripped before injection; use them for maintainer-only notes.
- `@path` imports don't save context: imported files load at launch too.

## Cross-file consistency

`CLAUDE.md`, `AGENTS.md` and the rules must agree on Python version, key commands (test/lint/build), paths, naming and
critical constraints. When you change one, grep the others for the same fact.

## Accuracy

- Every path you mention must exist: `test -e path || echo "WARN: $path missing"`.
- Every command you list must run (check `pyproject.toml`, `scripts/`, `.github/workflows/`).
- Versions (Python, ruff, pytest, engine CLIs) come from `pyproject.toml` / lockfiles, never memory.
- `AGENTS.md` is read by Codex/OpenCode/Gemini/Pi; `CLAUDE.md` and `.claude/rules/*` are Claude Code only.
