# Interactive approval

When Claude Code runs in permission mode, Untether shows inline buttons in Telegram so you can approve or deny tool calls from your phone.

## When buttons appear

Buttons appear when Claude wants to:

- **Edit or create a file** (Edit, Write, MultiEdit)
- **Run a shell command** (Bash)
- **Exit plan mode** (ExitPlanMode)
- **Ask you a question** (AskUserQuestion)

Other tool calls (Read, Glob, Grep, WebSearch, etc.) are auto-approved — they don't change anything, so you won't be interrupted for them.

## The three buttons

When a permission request arrives, you see a message with the tool name and a compact diff preview, plus three buttons:

| Button | What it does |
|--------|-------------|
| **Approve** | Let Claude proceed with the action |
| **Deny** | Block the action and ask Claude to explain what it was about to do |
| **Pause & Outline Plan** | Stop Claude and require a written plan before continuing (only appears for ExitPlanMode) |

Buttons clear immediately when you tap them — no waiting for a spinner.

## Diff previews

For tools that modify files, the approval message includes a compact diff so you can see what's about to change before deciding:

- **Edit**: shows removed lines (`- old`) and added lines (`+ new`), up to 4 lines each
- **Write**: shows the first 8 lines of content to be written
- **Bash**: shows the command to be run (up to 200 characters)

This lets you make informed approve/deny decisions without leaving Telegram.

## Answering questions

When Claude calls `AskUserQuestion`, the approval message shows the question text with a `?` prefix. Instead of tapping a button, **reply to the message with your answer as text**. Untether sends your reply back to Claude, which reads it and continues.

You can also tap Deny to dismiss the question.

## Push notifications

When approval buttons appear, Untether sends a separate notification message so you don't miss it — even if your phone is locked or you're in another app.

## Ephemeral cleanup

Approval-related messages (notifications, button messages) are automatically deleted when the run finishes, keeping your chat clean.

## Auto-approve configuration

You can configure which tools require approval and which are auto-approved. By default, only `ExitPlanMode` and `AskUserQuestion` require user interaction — all other tools are approved automatically.

To change this behaviour, adjust the permission mode. See [Plan mode](plan-mode.md) for details.

## Related

- [Plan mode](plan-mode.md) — control when and how approval requests appear
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Claude Code runner](../reference/runners/claude/runner.md) — technical details of the control channel
