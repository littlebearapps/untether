# Runners

Runner docs describe the **engine-specific** behaviour: event shapes, JSON streaming, and integration notes.

Interactive features (approval buttons, plan mode, AskUserQuestion, live sessions, steer) are **Claude Code-only**; Codex, OpenCode, Pi and Antigravity CLI run non-interactively.

- Claude Code: [Runner](claude/runner.md), [Stream JSON cheatsheet](claude/stream-json-cheatsheet.md), [Untether events](claude/untether-events.md)
- Codex: [Exec JSON cheatsheet](codex/exec-json-cheatsheet.md), [Untether events](codex/untether-events.md)
- OpenCode: [Runner](opencode/runner.md), [Stream JSON cheatsheet](opencode/stream-json-cheatsheet.md), [Untether events](opencode/untether-events.md)
- Pi: [Runner](pi/runner.md), [Stream JSON cheatsheet](pi/stream-json-cheatsheet.md), [Untether events](pi/untether-events.md)
- Antigravity CLI: [Runner](antigravity/runner.md), [Stream JSON cheatsheet](antigravity/stream-json-cheatsheet.md), [Untether events](antigravity/untether-events.md)

## Engine compatibility

| Feature | Claude Code | Codex CLI | OpenCode | Pi | Antigravity CLI¹⁰ | Gemini CLI⁷ | Amp⁷ |
|---------|:-----------:|:---------:|:--------:|:--:|:-:|:----------:|:---:|
| **Support status** | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| **Progress streaming** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Session resume** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Model override** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅¹ |
| **Model in footer** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅¹ |
| **Approval mode in footer** | ✅ | ~⁴ | — | — | ✅ | ~² | — |
| **Voice input** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Verbose progress** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Error hints** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Preamble injection** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Cost tracking** | ✅ | ~³ | ✅ | ~³ | ~³ | ~³ | ~³ |
| **Interactive permissions** | ✅ | — | —⁸ | — | —¹⁰ | — | — |
| **Approval policy** | ✅ | ~⁴ | — | — | ~¹⁰ | ~² | — |
| **Plan mode** | ✅ | — | — | — | —¹⁰ | — | — |
| **Ask mode (option buttons)** | ✅ | — | — | — | —¹⁰ | — | — |
| **Diff preview** | ✅ | — | — | — | — | — | — |
| **Auto-approve safe tools** | ✅ | — | — | — | — | — | — |
| **Plan outline gate** | ✅ | — | — | — | — | — | — |
| **Subscription usage** | ✅ | — | — | — | ~¹¹ | — | — |
| **Reasoning/effort levels** | ✅ | ✅ | — | — | ✅ | — | — |
| **Device re-auth (`/auth`)** | — | ✅ | — | — | — | — | — |
| **Live sessions & background tasks** | ✅ | — | — | — | —¹² | — | — |
| **Steer follow-ups** | ✅ | — | — | — | — | — | — |
| **Context % in status line** | ✅ | — | — | — | — | — | — |
| **Context compaction** | ✅ | — | —⁹ | ✅ | — | — | — |
| **Cross-env resume (`/continue`)** | ✅ | ✅ | ✅ | ✅⁵ | ✅ | ✅ | —⁶ |

¹ Amp's model override maps to `--mode` (`deep`/`free`/`rush`/`smart`), and the footer shows that mode (or `[amp] model` as a label).
² Defaults to full access (`--approval-mode=yolo`); `/config` → Approval mode can switch to edit files (`auto_edit`: edits allowed, no shell). The "read-only" option still runs with full access. A pre-run policy, not mid-run approval.
³ Token counts only; no USD cost (Gemini and Amp pass a cost through only when the CLI reports one).
⁴ `/config` → Approval policy toggles full auto (default; Codex's own sandbox setting) and safe (`--sandbox read-only`, edits blocked); only safe is shown in the footer. A pre-run policy, not mid-run approval.
⁵ For OAuth subscriptions in headless mode, Pi needs `provider = "openai-codex"` in `[pi]`.
⁶ Amp needs an explicit thread id; it has no "most recent" mode.
⁷ **Deprecated** — see [Deprecated runners](#deprecated-runners) below. The ticks describe what the integration does today, not a support commitment.
⁸ `opencode run` auto-rejects any tool your OpenCode permission rules set to `ask` (it never auto-approves) and denies OpenCode's question and plan tools. Set rules to `allow` for tools you want run unattended.
⁹ OpenCode compacts long sessions itself, but `opencode run` doesn't report it, so Untether can't show it.
¹⁰ `/config` → Permission mode picks Workspace (default: agy's own policy, which allows file edits and blocks shell, web and MCP calls; not a sandbox) or Full access (`--dangerously-skip-permissions`). A pre-run policy, not mid-run approval: a blocked tool shows a ⚠️ row. Ask me and Plan first (approval buttons and a plan gate) arrive in a later 0.36.1 release. Read the [terms note](antigravity/runner.md) before using a Google account sign-in.
¹¹ `/usage` shows agy's quota groups on demand; there is no quota footer.
¹² No live sessions or background-task panel. While agy holds its answer for a background command, the progress message shows ⏳ instead of a stall warning.

Reasoning levels: Claude `low`, `medium`, `high`, `xhigh`, `max` (`xhigh` needs Claude Code v2.1.114+); Codex `low`, `medium`, `high`, `xhigh`; Antigravity `low`, `medium`, `high`, narrowed to what the chat's model accepts.
OpenCode support means the 1.x CLI (npm `opencode-ai`); 2.x (`@opencode/cli`) is refused before spawning ([#970](https://github.com/littlebearapps/untether/issues/970)).

### Footer metadata line

Final messages (and progress messages once the engine reports it) end with a `🏷` line built by
`format_meta_line` in `src/untether/markdown.py`: the context (`dir: project @branch`, when bound), then
`|`, then model · effort · permission mode · trigger source, joined with ` · `. For example
`🏷 opus 5 · medium · plan` or `🏷 dir: api @main | gpt-5.5 · high · safe`. Model comes from each
engine's `StartedEvent.meta`; effort only from Claude, Codex and Antigravity overrides; the mode only from
Claude (`system/init.permissionMode`), Codex safe mode, Antigravity (`workspace` / `full access`) and Gemini's approval tiers. Triggered runs add
their source: `⏰ cron:<id>` / `⏰ at:<token>` for crons and `/at`, `⚡ webhook:<id>` for webhooks.

## ⚠️ Deprecated runners

Both are **deprecated and no longer supported** as of **v0.36.0**. They still
load, but get no bug or feature fixes (security and doc-accuracy fixes only),
are excluded from the release test matrix, and may be removed in a future
release — no removal is scheduled. When a cross-engine sweep breaks one, the
test is `xfail`/`skip`ped rather than the runner fixed.

- Gemini (⚠️ deprecated — upstream EOL for individual accounts 2026-06-18, [#720](https://github.com/littlebearapps/untether/issues/720)): [Runner](gemini/runner.md), [Stream JSON cheatsheet](gemini/stream-json-cheatsheet.md), [Untether events](gemini/untether-events.md)
- AMP (⚠️ deprecated — integration unmaintained, [#458](https://github.com/littlebearapps/untether/issues/458)): [Runner](amp/runner.md), [Stream JSON cheatsheet](amp/stream-json-cheatsheet.md), [Untether events](amp/untether-events.md)

