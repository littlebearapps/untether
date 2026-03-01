# Interactive control

This tutorial walks you through Untether's interactive permission system — approving, denying, and shaping agent actions from Telegram on your phone, tablet, or desktop. Untether works via [Telegram](https://telegram.org), which is available on iPhone, iPad, Android, Mac, Windows, Linux, and [Telegram Web](https://web.telegram.org).

**What you'll learn:** How to control Claude Code's actions in real time with Telegram buttons, how to request and review a plan before execution, and how to answer agent questions from anywhere.

!!! note "Claude Code only"
    Interactive approval is a Claude Code feature. Other engines (Codex, OpenCode, Pi) run non-interactively — they don't prompt for approval.

## 1. Understand permission modes

Untether offers three permission modes that control how much oversight you have:

| Mode | Command | What happens |
|------|---------|-------------|
| **Plan** | `/planmode on` | Every tool call shows Approve / Deny buttons. Full control. |
| **Auto** | `/planmode auto` | Tools are auto-approved. Plan transitions are also auto-approved. Hands-off. |
| **Accept edits** | `/planmode off` | No approval buttons at all. Claude runs autonomously. |

For this tutorial, we'll use **Plan** mode so you can see every interaction.

## 2. Enable plan mode

Open your Telegram chat with the bot and send:

```
/planmode on
```

<!-- SCREENSHOT: /planmode on response showing "plan mode: on" confirmation -->

The bot confirms that plan mode is now active. This setting is stored per chat and persists across sessions.

## 3. Send a task

Send Claude a task that will require file changes:

```
add a comment to the top of README.md explaining what this project does
```

<!-- SCREENSHOT: user sending a task message to the bot -->

Claude starts working and you'll see a progress message stream in.

## 4. See approval buttons

When Claude wants to modify a file, Untether intercepts the tool call and shows you what's about to happen. You'll see a message like:

<!-- SCREENSHOT: approval buttons showing Edit tool with diff preview — Approve / Deny / Pause & Outline Plan -->

The message includes:

- **Tool name** (e.g. Edit, Write, Bash)
- **Diff preview** — removed lines (`- old`) and added lines (`+ new`) so you can see what will change
- **Three buttons**: Approve, Deny, and Pause & Outline Plan

Your phone will also buzz with a push notification so you don't miss it.

## 5. Approve a tool call

Tap **Approve** to let Claude proceed with the action. The button clears instantly — no spinner, no waiting. Claude continues with its work.

<!-- SCREENSHOT: progress message after approving, showing the action completed -->

You may see several approval requests in a row as Claude works through multiple steps.

## 6. Deny a tool call

If something doesn't look right, tap **Deny** instead. Claude receives a denial message explaining that you've blocked the action and asking it to communicate via visible text instead.

<!-- SCREENSHOT: deny response — Claude acknowledging the denial and explaining its intent -->

This is useful when you want Claude to explain its reasoning before making changes. After denying, Claude will typically describe what it was trying to do and ask for guidance.

## 7. Use "Pause & Outline Plan"

The third button — **Pause & Outline Plan** — is the most powerful. It appears when Claude tries to exit plan mode (transition from planning to execution).

Tap it to require Claude to write a comprehensive plan as a visible message before doing anything. The plan must include:

1. Every file to be created or modified (full paths)
2. What changes will be made in each file
3. The execution order and phases
4. Key decisions and trade-offs
5. The expected end result

<!-- SCREENSHOT: Claude's written outline/plan appearing as visible text in the chat -->

After Claude writes the outline, **Approve Plan** and **Deny** buttons appear automatically — no need to type "approved":

<!-- SCREENSHOT: post-outline Approve Plan / Deny buttons in Telegram -->

- Tap **Approve Plan** to let Claude proceed with implementation
- Tap **Deny** to stop Claude and provide different direction

!!! tip "Progressive cooldown"
    After tapping "Pause & Outline Plan", a cooldown prevents Claude from immediately retrying. The cooldown starts at 30 seconds and escalates up to 120 seconds if Claude keeps retrying. This ensures the agent pauses long enough for you to read the outline.

## 8. Answer a question

Sometimes Claude needs to ask you something — like which approach to take or what naming convention to use. When Claude calls `AskUserQuestion`, you'll see the question in the chat with a ❓ prefix:

<!-- SCREENSHOT: AskUserQuestion message showing the question text with Approve/Deny buttons -->

Instead of tapping a button, **reply to the message with your answer as text**. Untether routes your reply back to Claude, which reads it and continues.

```
You: Use snake_case for all variable names
```

<!-- SCREENSHOT: user replying with text to an AskUserQuestion, Claude continuing -->

You can also tap **Deny** to dismiss the question if it's not relevant.

## 9. Switch to auto mode

Once you're comfortable with how Claude works, you might want less interruption. Switch to auto mode:

```
/planmode auto
```

<!-- SCREENSHOT: /planmode auto confirmation -->

In auto mode, tool calls (Edit, Write, Bash) are still auto-approved — Claude works without interruption. Plan transitions are also auto-approved, so you won't see ExitPlanMode buttons. The agent preamble still requests summaries and structured output.

## 10. Return to default

To turn off plan mode entirely:

```
/planmode off
```

This sets Claude to `acceptEdits` mode — no approval buttons at all. Claude runs autonomously, which is the fastest option for trusted tasks.

To check your current mode at any time:

```
/planmode show
```

<!-- SCREENSHOT: /planmode show output showing current mode and source -->

## What just happened

Key concepts:

- **Permission modes** control the level of oversight: plan (full control), auto (hands-off with plans), off (fully autonomous)
- **Approval buttons** appear inline in Telegram when Claude needs permission — Approve, Deny, or Pause & Outline Plan
- **Diff previews** show you exactly what will change before you approve
- **"Pause & Outline Plan"** forces Claude to write a visible plan before executing
- **AskUserQuestion** lets you answer Claude's questions with a text reply
- **Push notifications** ensure you don't miss approval requests, even from another app
- **Ephemeral cleanup** automatically removes button messages when the run finishes

## Troubleshooting

**Approval buttons don't appear**

Check that you're using Claude Code (`/claude` prefix or `/agent set claude`) and that plan mode is on (`/planmode show`). Other engines don't support interactive approval.

**Buttons appear but nothing happens when I tap them**

Check your internet connection. If the tap doesn't register, try again — Untether answers callbacks immediately so there should be no delay.

**Claude keeps retrying after I tap "Pause & Outline Plan"**

This is the progressive cooldown at work. Claude may retry ExitPlanMode during the cooldown window, but each retry is auto-denied. Wait for Claude to write the outline, then use the Approve Plan / Deny buttons that appear.

**I don't get push notifications for approval requests**

Make sure Telegram notifications are enabled for this chat. Untether sends a separate notification message when buttons appear, but Telegram's notification settings control whether you see it.

## Next

Now that you can control your agent interactively, learn how to target specific repos and branches.

[Projects and branches →](projects-and-branches.md)
