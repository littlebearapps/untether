# Interactive approval

When Claude Code runs in permission mode, Untether shows inline buttons in Telegram so you can approve or deny tool calls from your phone.

## When buttons appear

Buttons appear when Claude Code wants to:

- **Edit or create a file** (Edit, Write, MultiEdit)
- **Run a shell command** (Bash)
- **Exit plan mode** (ExitPlanMode)
- **Ask you a question** (AskUserQuestion)

Other tool calls (Read, Glob, Grep, WebSearch, etc.) are auto-approved in plan mode — they don't change anything, so you won't be interrupted for them. In **Accept edits** (`/planmode off`), any action the mode doesn't cover asks for approval, whatever the tool ([#749](https://github.com/littlebearapps/untether/issues/749)).

## The three buttons

When a permission request arrives, you see a message with the tool name and a compact diff preview, plus three buttons:

| Button | What it does |
|--------|-------------|
| **Approve** | Let Claude Code proceed with the action |
| **Deny** | Block the action and ask Claude Code to explain what it was about to do |
| **Pause & Outline Plan** | Stop Claude Code and require a written plan before continuing (only appears for ExitPlanMode) |
| **Let's discuss** | Talk about the plan before approving or denying (only appears after outline is written) |

Buttons clear immediately when you tap them — no waiting for a spinner.

<img src="../assets/screenshots/approval-buttons-howto.jpg" alt="Approval message with Approve / Deny / Pause & Outline Plan buttons" width="360" loading="lazy" />

<div markdown>

!!! untether "Untether"
    ▸ Permission Request [CanUseTool] - tool: Edit (file_path=src/main.py)<br>
    📝 src/main.py<br>
    `- import sys`<br>
    `+ import sys`<br>
    `+ from pathlib import Path`

<div class="tg-buttons">
<span class="tg-btn">Approve</span>
<span class="tg-btn">Deny</span>
<span class="tg-btn">Pause &amp; Outline Plan</span>
</div>

</div>

## Diff previews

For tools that modify files, the approval message includes a compact diff so you can see what's about to change before deciding:

- **Edit**: 📝 file path, removed lines (`- old`) and added lines (`+ new`), up to 4 lines each
- **Write**: 📝 file path, then the first 8 lines of content to be written
- **Bash**: `$ command` (up to 200 characters)

This lets you make informed approve/deny decisions without leaving Telegram.

You can toggle diff previews on or off via `/config` → **Diff preview**. When OFF, approval messages show the tool name and buttons only — no inline diffs. Useful on slow connections or when you trust the agent enough to skim by tool name alone.

!!! untether "Untether"
    ▸ Permission Request [CanUseTool] - tool: Edit (file_path=src/main.py)<br>
    📝 src/main.py<br>
    `- import sys`<br>
    `+ import sys`<br>
    `+ from pathlib import Path`

<img src="../assets/screenshots/approval-diff-preview.jpg" alt="Approval message with compact diff preview showing removed and added lines" width="360" loading="lazy" />

!!! note "After plan approval"
    When you approve a plan outline (see [Plan mode](plan-mode.md#auto-approval-after-plan-approval)), diff previews are skipped for the rest of the session — tools are auto-approved since you already reviewed the plan.

## Answering questions

When Claude Code calls `AskUserQuestion`, Untether renders the question with interactive option buttons in Telegram:

- **Option buttons** — tap any option to answer instantly. Claude Code receives your choice and continues.
- **"Other (type reply)"** — tap this to type a custom answer. Send your reply as a regular message and Untether routes it back to Claude Code.
- **Multi-question flows** — if Claude Code asks multiple questions, they appear one at a time (e.g. "1 of 3"). Answer each to step through the sequence.
- **Deny** — tap Deny to dismiss the question. Claude Code proceeds with its default assumptions.

Toggle ask mode on or off via `/config` → Ask mode. When off, questions are auto-denied and Claude Code proceeds with defaults.

<img src="../assets/screenshots/ask-text-reply-howto.jpg" alt="AskUserQuestion with option buttons and &quot;Other (type reply)&quot;" width="360" loading="lazy" />

<div markdown>

!!! untether "Untether"
    ❓ Which test framework should I use?

<div class="tg-buttons">
<span class="tg-btn">pytest</span>
<span class="tg-btn">unittest</span>
</div>
<div class="tg-buttons">
<span class="tg-btn">Other (type reply)</span>
<span class="tg-btn">Deny</span>
</div>

</div>

## Push notifications

When approval buttons appear, Untether sends a separate notification message so you don't miss it — even if your phone is locked or you're in another app.

## Ephemeral cleanup

Approval-related messages (notifications, button messages) are automatically deleted when the run finishes, keeping your chat clean.

## Auto-approve configuration

Which tools need approval depends on the permission mode. In plan mode, plan-auto and auto, only `ExitPlanMode` and `AskUserQuestion` reach you; everything else is decided by Claude Code. In Accept edits (and `default`/`manual` set in `untether.toml`), every tool Claude Code would prompt for becomes an Approve / Deny message. Allow rules in your Claude Code settings, or `[engines.claude] allowed_tools`, pre-approve tools in any mode.

To change this behaviour, adjust the permission mode. See [Plan mode](plan-mode.md) for details.

## Engine-specific approval policies

Claude Code is the only engine with interactive mid-run approval buttons. Other engines offer pre-run policies that control what the agent is allowed to do before it starts:

### Codex CLI — Approval policy

Toggle via `/config` → **Approval policy**:

| Policy | CLI flag | Behaviour |
|--------|----------|-----------|
| **Full auto** (default) | (none) | Codex uses your own Codex sandbox setting (`sandbox_mode` in `~/.codex/config.toml`, or the project's trust level). For a trusted project that is usually `workspace-write`, so Codex can edit files there |
| **Safe** | `codex exec --sandbox read-only` | Reads and read-only commands only. Edits and writes fail and are reported back to the model; shell network access is blocked |

This is a pre-run policy — Codex doesn't pause mid-run to ask for permission. `codex exec` never asks for approval, so the policy picks Codex's sandbox before the run starts.

!!! warning "Safe blocks every sandboxed write"
    The read-only sandbox blocks **all** filesystem writes from Codex's commands, not just edits inside the project: caches (`.pytest_cache`, `__pycache__`, `npm`/`uv` caches), build output and `/tmp` too. Tests, builds and package installs fail in Safe. Switch the chat to **Full auto** when you want Codex to run them. MCP tools run outside Codex's command sandbox, so Safe does not restrict what an MCP server does.

!!! note "Before v0.35.5"
    Safe used to pass `--ask-for-approval untrusted`. `codex exec` never applied that flag, so Safe ran exactly like Full auto, and codex-cli 0.149.0+ rejects it outright, so every Safe run failed at startup ([#830](https://github.com/littlebearapps/untether/issues/830)).

### Gemini CLI — Approval mode

Toggle via `/config` → **Approval mode**:

| Mode | CLI flag | Behaviour |
|------|----------|-----------|
| **Read-only** (default) | (none) | Write tools blocked — Gemini can only read files |
| **Edit files** | `--approval-mode auto_edit` | File reads and writes OK, shell commands blocked |
| **Full access** | `--approval-mode yolo` | All tools approved — full autonomy |

This is also a pre-run policy. Gemini CLI doesn't have interactive mid-run approval.

Both policies persist per chat via `/config` and can be cleared back to the default. See [Inline settings](inline-settings.md) for the full `/config` menu reference.

## Related

- [Plan mode](plan-mode.md) — control when and how approval requests appear
- [Inline settings](inline-settings.md) — `/config` menu for toggling approval policies
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Claude Code runner](../reference/runners/claude/runner.md) — technical details of the control channel
