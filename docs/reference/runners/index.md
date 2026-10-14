# Runners

Runner docs describe the **engine-specific** behaviour: event shapes, JSON streaming, and integration notes.

Interactive features (approval buttons, plan mode, AskUserQuestion, live sessions, steer) are fullest in **Claude Code**. **Antigravity CLI** has Plan first, and with Untether's agy plugin on the host also approval buttons (Ask me), question buttons and steer, but no live sessions. Codex, OpenCode and Pi run non-interactively. Antigravity CLI (`antigravity`) is a separate engine from the deprecated Gemini CLI (`gemini`).

- Claude Code: [Runner](claude/runner.md), [Stream JSON cheatsheet](claude/stream-json-cheatsheet.md), [Untether events](claude/untether-events.md)
- Codex: [Exec JSON cheatsheet](codex/exec-json-cheatsheet.md), [Untether events](codex/untether-events.md)
- OpenCode: [Runner](opencode/runner.md), [Stream JSON cheatsheet](opencode/stream-json-cheatsheet.md), [Untether events](opencode/untether-events.md)
- Pi: [Runner](pi/runner.md), [Stream JSON cheatsheet](pi/stream-json-cheatsheet.md), [Untether events](pi/untether-events.md)
- Antigravity CLI: [User guide](../../how-to/antigravity.md), [Runner](antigravity/runner.md), [Stream JSON cheatsheet](antigravity/stream-json-cheatsheet.md), [Untether events](antigravity/untether-events.md)

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
| **Interactive permissions** | ✅ | — | —⁸ | — | ✅¹⁰ | — | — |
| **Approval policy** | ✅ | ~⁴ | — | — | ~¹⁰ | ~² | — |
| **Plan mode** | ✅ | — | — | — | ✅¹⁰ | — | — |
| **Ask mode (option buttons)** | ✅ | — | — | — | ✅¹⁴ | — | — |
| **Diff preview** | ✅ | — | — | — | — | — | — |
| **Auto-approve safe tools** | ✅ | — | — | — | — | — | — |
| **Plan outline gate** | ✅ | — | — | — | — | — | — |
| **Subscription usage** | ✅ | — | — | — | ~¹¹ | — | — |
| **Reasoning/effort levels** | ✅ | ✅ | — | — | ✅ | — | — |
| **Device re-auth (`/auth`)** | — | ✅ | — | — | — | — | — |
| **Live sessions & background tasks** | ✅ | — | — | — | ~¹² | — | — |
| **Steer follow-ups** | ✅ | — | — | — | ✅¹⁴ | — | — |
| **Context % in status line** | ✅ | — | — | — | ~¹³ | — | — |
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
¹⁰ `/config` → Permission mode picks Workspace (default: `--mode=accept-edits` with Untether's no-shell agent, `--agent=untether-workspace` — file tools only, so no shell, web or MCP calls and no deleting or renaming files; not a sandbox), Plan first, Ask me or Full access (`--dangerously-skip-permissions`, never the default unless you choose it). Workspace and Full access are pre-run policies, where a blocked tool (such as a file outside the project) shows a ⚠️ row. **Plan first** ([#981](https://github.com/littlebearapps/untether/issues/981)) is agy's own `--mode=plan`; the plan is answered with Approve / Deny / Let's discuss, and Approve resumes in Workspace. **Ask me** ([#980](https://github.com/littlebearapps/untether/issues/980), needs Untether's agy plugin on the host) is mid-run approval: reads and in-project edits run, and shell, web, MCP, subagents and files outside the project get Approve / Deny cards with the command or a diff. After switching a chat between Workspace / Plan first and Ask me / Full access, send `/new`: agy keeps a conversation on the agent it started with. Read the [terms note](antigravity/runner.md) before using a Google account sign-in.
¹¹ `/usage` shows agy's quota groups and remaining AI credits on demand. With `[footer] show_subscription_usage` on, a quota line follows each answer (with it off, only once a limit reaches 70 %), built only from what the last `/usage` fetched in the past 15 minutes; a run never fetches the quota itself ([#979](https://github.com/littlebearapps/untether/issues/979)).
¹² No live sessions: each message is its own `agy` run. Background commands, timers and subagents are listed in the `⏳ background (N)` block of the progress message, from agy's stream only ([#978](https://github.com/littlebearapps/untether/issues/978)); there is no status message after the answer, and a daemon command can outlive the run. While agy holds its answer for a background command, the progress message shows ⏳ instead of a stall warning.
¹³ An estimate, shown as `~N% ctx`: the tokens of the latest model call against the window Google publishes for the model. Only when the chat has a model set that Untether has a published figure for; agy reports neither a window nor compaction ([#979](https://github.com/littlebearapps/untether/issues/979); [details](antigravity/runner.md#context-use-n-ctx)).
¹⁴ Needs [Untether's agy plugin](../../how-to/antigravity.md#telegram-questions-and-steer) on the host (`untether agy-gate install`; [#980](https://github.com/littlebearapps/untether/issues/980), [#981](https://github.com/littlebearapps/untether/issues/981)). agy's `ask_question` gets the same option buttons as Claude's questions; a steer reaches the agy run before its next step. Without the plugin, questions are skipped and follow-ups queue.

Reasoning levels: Claude `low`, `medium`, `high`, `xhigh`, `max` (`xhigh` needs Claude Code v2.1.114+); Codex `low`, `medium`, `high`, `xhigh`; Antigravity `low`, `medium`, `high`, narrowed to what the chat's model accepts.
OpenCode support means the 1.x CLI (npm `opencode-ai`); 2.x (`@opencode/cli`) is refused before spawning ([#970](https://github.com/littlebearapps/untether/issues/970)).

### Footer metadata line

Final messages (and progress messages once the engine reports it) end with a `🏷` line built by
`format_meta_line` in `src/untether/markdown.py`: the context (`dir: project @branch`, when bound), then
`|`, then model · effort · permission mode · trigger source, joined with ` · `. For example
`🏷 opus 5 · medium · plan` or `🏷 dir: api @main | gpt-5.5 · high · safe`. Model comes from each
engine's `StartedEvent.meta`; effort only from Claude, Codex and Antigravity overrides; the mode only from
Claude (`system/init.permissionMode`), Codex safe mode, Antigravity (`workspace` / `plan first` / `ask me` / `full access`) and Gemini's approval tiers. Triggered runs add
their source: `⏰ cron:<id>` / `⏰ at:<token>` for crons and `/at`, `⚡ webhook:<id>` for webhooks.

## ⚠️ Deprecated runners

Both are **deprecated and no longer supported** as of **v0.36.0**. They still
load, but get no bug or feature fixes (security and doc-accuracy fixes only),
are excluded from the release test matrix, and may be removed in a future
release — no removal is scheduled. When a cross-engine sweep breaks one, the
test is `xfail`/`skip`ped rather than the runner fixed.

- Gemini (⚠️ deprecated — upstream EOL for individual accounts 2026-06-18, [#720](https://github.com/littlebearapps/untether/issues/720)): [Runner](gemini/runner.md), [Stream JSON cheatsheet](gemini/stream-json-cheatsheet.md), [Untether events](gemini/untether-events.md)
- AMP (⚠️ deprecated — integration unmaintained, [#458](https://github.com/littlebearapps/untether/issues/458)): [Runner](amp/runner.md), [Stream JSON cheatsheet](amp/stream-json-cheatsheet.md), [Untether events](amp/untether-events.md)

