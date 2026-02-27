# Plan mode

Plan mode controls how Claude Code handles permission requests when running through Untether. You can require manual approval for plan transitions, auto-approve them, or skip the plan phase entirely.

## Permission modes

| Mode | `/planmode` command | CLI flag | Behaviour |
|------|-------------------|----------|-----------|
| **Plan** | `/planmode on` | `--permission-mode plan` | All tool calls and plan transitions require Telegram approval |
| **Auto** | `/planmode auto` | `--permission-mode plan` | Tools are auto-approved; ExitPlanMode is also auto-approved (no buttons) |
| **Accept edits** | `/planmode off` | `--permission-mode acceptEdits` | No approval buttons — Claude runs without interruption |

**Plan** is the most interactive mode. You see every file edit, shell command, and plan transition as inline buttons.

**Auto** is the recommended default for most users. Tools run without interruption, but Claude still goes through a plan phase. ExitPlanMode is silently approved so you don't need to tap a button for every plan-to-execution transition.

**Accept edits** skips permission control entirely. Use this when you trust the agent to make changes autonomously.

## Setting the mode

Toggle per chat:

```
/planmode on       # enable plan mode
/planmode auto     # plan mode with auto-approved transitions
/planmode off      # disable plan mode
/planmode          # toggle: if currently on/auto, turn off; otherwise turn on
/planmode show     # show current mode
/planmode clear    # remove override, use engine config default
```

Mode is stored per chat and persists across sessions. New runs in the chat use the configured mode.

## "Pause & Outline Plan"

When Claude tries to exit plan mode (ExitPlanMode), you see three buttons instead of two:

- **Approve** — let Claude proceed to execution
- **Deny** — block and ask Claude to explain
- **Pause & Outline Plan** — require a written plan first

Tapping "Pause & Outline Plan" tells Claude to stop and write a comprehensive plan as a visible message in the chat. The plan must include:

1. Every file to be created or modified (full paths)
2. What changes will be made in each file
3. The order and phases of execution
4. Key decisions, trade-offs, and risks
5. The expected end result

This is useful when you want to review the approach before Claude starts making changes.

After Claude writes the outline, **Approve Plan / Deny** buttons appear automatically in Telegram. Tap "Approve Plan" to let Claude proceed, or "Deny" to stop and provide feedback. You no longer need to type "approved" — the buttons handle it.

## Progressive cooldown

After you tap "Pause & Outline Plan", a cooldown window prevents Claude from immediately retrying ExitPlanMode:

| Click count | Cooldown |
|-------------|----------|
| 1st | 30 seconds |
| 2nd | 60 seconds |
| 3rd | 90 seconds |
| 4th+ | 120 seconds (maximum) |

During the cooldown, any ExitPlanMode attempt is automatically denied, but **Approve Plan / Deny buttons** are shown in Telegram so you can approve the plan as soon as you've read it. The cooldown resets when you explicitly Approve or Deny.

This prevents the agent from bulldozing through when you've asked it to slow down and explain its approach, while still giving you a one-tap way to approve once you're satisfied.

## Related

- [Interactive approval](interactive-approval.md) — how approval buttons and diff previews work
- [Configuration](../reference/config.md) — setting default permission mode in `untether.toml`
