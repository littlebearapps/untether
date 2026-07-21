# Step 3 — Research

Always research before proposing a fix. The Untether bug you're chasing is
often a known upstream engine quirk, a previously-fixed regression, or a
documented config gotcha.

## 3a. Untether docs

| Path | What it covers |
|---|---|
| `docs/reference/runners/claude/runner.md` | Claude Code runner spec — CLI invocation, control channel, permission modes |
| `docs/reference/runners/claude/stream-json-cheatsheet.md` | Claude JSONL event shapes |
| `docs/reference/runners/claude/untether-events.md` | Claude → Untether event translation rules |
| `docs/reference/runners/codex/*.md` | Codex runner spec + JSONL + translation |
| `docs/reference/runners/opencode/*.md` | OpenCode runner spec + JSONL + translation |
| `docs/reference/runners/pi/*.md` | Pi runner spec + JSONL + translation |
| `docs/reference/runners/gemini/*.md` | Gemini runner spec + JSONL + translation |
| `docs/reference/runners/amp/*.md` | AMP runner spec + JSONL + translation |
| `docs/reference/transports/telegram.md` | Telegram transport — Bot API client, outbox, voice, forum topics |
| `docs/reference/modes.md` | Workflow modes (assistant / workspace / handoff) |
| `docs/reference/dev-instance.md` | Dev vs staging service quickref + staging workflow |
| `docs/reference/integration-testing.md` | Per-tier integration test playbook |
| `.claude/rules/*.md` | Project rule files (auto-load on edits to matching paths) |
| `.claude/skills/*/SKILL.md` | Project skills — telegram-bot-api, jsonl-subprocess-runner, claude-stream-json, codex-opencode-pi, untether-architecture, release-coordination |

Read the relevant file before proposing any change. Untether docs are
deliberately concrete — they contain code paths, event names, config keys.

## 3b. Closed Untether issues (regression detection)

```bash
SINCE=$(date -u -d '90 days ago' +%Y-%m-%d)
gh issue list --repo littlebearapps/untether \
  --search "closed:>=${SINCE}" --state closed \
  --json number,title,closedAt,labels,milestone \
  --limit 100
```

For each plausible match (title contains a similar fragment, same labels):
1. Read the closing PR via `gh issue view <N> --comments`.
2. Identify the fix commit SHA.
3. Run `git show <SHA>` to see what changed.
4. Check whether the current symptom indicates the fix lapsed (regression) or
   the fix was incomplete (gap). Both classes need explicit flagging in the
   Debug Report.

## 3c. Upstream engine repos

Many Untether bugs are downstream-visible upstream bugs. Search the engine's
issue tracker before proposing a fix in Untether code:

| Engine | Repo | Search command |
|---|---|---|
| Claude Code | `anthropics/claude-code` | `gh search issues "repo:anthropics/claude-code <error fragment>" --limit 10` |
| Codex CLI | `openai/codex` | `gh search issues "repo:openai/codex <error fragment>" --limit 10` |
| OpenCode | `sst/opencode` | `gh search issues "repo:sst/opencode <error fragment>" --limit 10` |
| Pi | (inflection-ai / pi-cli) | Search Inflection's docs + GitHub — Pi's source is sometimes closed. Fall back to web search. |
| Gemini CLI | `google-gemini/gemini-cli` | `gh search issues "repo:google-gemini/gemini-cli <error fragment>" --limit 10` |
| AMP CLI | `sourcegraph/amp` | `gh search issues "repo:sourcegraph/amp <error fragment>" --limit 10` |

Known long-standing upstream issues already tracked in Untether memory:
- **Claude Code `last_event_type=user` exit** — upstream bugs anthropics/claude-code#34142, #30333 (auto-continue is the Untether mitigation).
- **ExitPlanMode loop in Claude CLI v2.1.72–2.1.74** — fixed upstream; do not patch in Untether.

Check `MEMORY.md` → `project_*_upstream_bug.md` notes before assuming an
upstream bug is novel.

## 3d. Python ecosystem libraries (Context7 + GitHub)

For library-specific issues:

| Library | Used for | Search |
|---|---|---|
| `anyio` | Async runtime, task groups, cancellation | Context7 `mcp__context7__resolve-library-id` → `query-docs`; or `gh search issues "repo:agronholm/anyio <fragment>"` |
| `msgspec` | JSONL parsing, msgspec.Struct schemas | Context7; `gh search issues "repo:jcrist/msgspec <fragment>"` |
| `structlog` | Logging | `gh search issues "repo:hynek/structlog <fragment>"` |
| `httpx` | Telegram Bot API HTTP client | `gh search issues "repo:encode/httpx <fragment>"` |
| `pty` (stdlib) | Control channel | Python docs + `bpo`/`gh:python/cpython` search |
| `aiohttp` | Webhook server | `gh search issues "repo:aio-libs/aiohttp <fragment>"` |
| `pydantic` / `pydantic-settings` | TOML config models | Context7 + GitHub issues |

For Context7 (when available):
```
mcp__context7__resolve-library-id(libraryName="anyio")
mcp__context7__query-docs(libraryID="<id>", query="<question>")
```

## 3e. Web search (jina)

For recent incidents, community workarounds, or unresolved upstream bugs:
```
mcp__jina__search_web(query="<error message verbatim>", limit=10)
mcp__jina__read_url(url="<top result>")
```

Use sparingly — for Untether-internal bugs, the canonical sources above are
almost always sufficient.

## 3f. Cross-host fleet baseline

If a behaviour is reported on one host but not another, gather both sides:

```bash
# Compare versions
for host in lba-1 nsd channelo mac; do
  case "$host" in
    lba-1) pipx list --short | grep untether ;;
    *) ssh "$host" "pipx list --short 2>/dev/null | grep untether" ;;
  esac
done

# Compare attestation markers
ls ~/.untether-dev/integration-test-pass-*.json
```

A version mismatch + a single-host symptom strongly suggests the issue was
fixed (or introduced) in the version range between the two hosts.

## 3g. CHANGELOG correlation

The fastest way to find a regression's root cause: grep CHANGELOG.md for the
affected area and date-bound the search to the version range exhibiting the
bug.

```bash
# Latest stable + last 5 rc bumps
head -200 CHANGELOG.md

# Entries touching a specific area
grep -nE '<area>|<event_name>|#<issue>' CHANGELOG.md
```

## Output

Research findings appear under `### Research conducted` in the Debug Report
template. Cite specific URLs, PR numbers, commit SHAs, and rule-file
line numbers. "I searched" without a citation does not count.
