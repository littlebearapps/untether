# Screenshot Capture Checklist

Filenames, descriptions, and capture notes for each screenshot. All images go in
`docs/assets/screenshots/`. Crop tightly to the relevant UI element — no status
bars, no keyboard, no notification tray.

**Bot:** `@untether_dev_bot` (dev instance)
**Theme:** Light Telegram theme
**Content:** Real tasks against the Untether repo (public, nothing sensitive)

## Tier 0: README (3 images)

- [x] `hero-voice-to-result.jpg` — Voice waveform bubble → progress streaming (3-4 actions) → final result with footer. Crop to chat area only. *This single image tells the entire story.*
- [x] `approval-diff-preview.jpg` — Edit tool showing `- old` / `+ new` lines with Approve/Deny/Pause buttons. Crop to approval message + buttons.
- [x] `plan-outline-approve.jpg` — Outline text + Approve Plan / Deny buttons. Crop to outline + buttons.

## Tier 1: Website extras (4 images)

- [x] `browse-directory.jpg` — `/browse` inline keyboard with directory listing (use untether's own repo).
- [x] `usage-command.jpg` — `/usage` cost tracking output.
- [x] `multi-engine-switch.jpg` — Engine switching: `/claude` then `/codex` in same chat.
- [ ] `verbose-vs-compact.jpg` — Side-by-side or sequential compact vs verbose for same action.

## Tier 2: Tutorial screenshots (12 images)

- [x] `progress-streaming.jpg` — Progress message showing "working · codex · 12s" with action list.
- [x] `final-answer-footer.jpg` — Final answer with model/cost footer and resume line.
- [x] `cancel-button.jpg` — Cancel button on progress and the resulting "cancelled" status.
- [x] `deny-response.jpg` — Claude acknowledging a denial and explaining intent.
- [x] `plan-outline-text.jpg` — Claude's written outline/plan as visible text in chat.
- [x] `post-outline-buttons.jpg` — Post-outline Approve Plan / Deny buttons.
- [x] `ask-question-options.jpg` — AskUserQuestion with option buttons.
- [x] `ask-reply-continue.jpg` — User replying with text to AskUserQuestion, Claude continuing.
- [x] `chat-auto-resume.jpg` — Follow-up message auto-resuming without reply.
- [x] `stateless-reply-resume.jpg` — Stateless mode with user replying to a message with resume line.
- [ ] `botfather-newbot.jpg` — BotFather /newbot flow. **REDACT the bot token.**
- [ ] `onboarding-wizard.jpg` — Terminal showing the workflow selection step.

## Tier 3: How-to screenshots (16 images)

- [x] `approval-buttons-howto.jpg` — Approval message with Approve/Deny/Pause inline buttons + tool summary.
- [ ] `approval-diff-howto.jpg` — Diff preview on approval (Edit with `- old` / `+ new` lines).
- [x] `ask-text-reply-howto.jpg` — AskUserQuestion with option buttons and "Other (type reply)".
- [x] `exit-planmode-buttons.jpg` — ExitPlanMode with Approve/Deny/Pause buttons.
- [ ] `outline-approve-buttons.jpg` — Written outline + Approve Plan / Deny buttons below.
- [x] `cooldown-auto-deny.jpg` — Auto-denied ExitPlanMode during cooldown with Approve Plan / Deny buttons.
- [x] `cost-warning-alert.jpg` — Cost warning alert showing budget threshold exceeded.
- [x] `voice-transcription.jpg` — Voice note followed by transcribed text and agent output. (iPhone)
- [x] `file-put.jpg` — Document upload with `/file put` caption and saved confirmation. (iPhone)
- [x] `file-get.jpg` — `/file get` response with fetched file as document. (iPhone)
- [ ] `session-auto-resume.jpg` — Chat session auto-resume. (iPhone)
- [ ] `forum-topic-context.jpg` — Forum topic bound to project/branch with context footer. (MacBook)
- [x] `config-menu.jpg` — `/config` home page with inline keyboard buttons. (MacBook)
- [ ] `verbose-vs-compact.jpg` — Side-by-side or sequential compact vs verbose for same action. (MacBook)
- [ ] `webhook-notification.jpg` — Webhook-triggered run with rendered prompt and progress. (MacBook)
- [ ] `scheduled-message.jpg` — Telegram scheduled message picker for a task. (iPhone)

## Tier 4: Supporting screenshots (12 images)

- [x] `planmode-on.jpg` — `/planmode on` confirmation. (iPhone)
- [x] `planmode-auto.jpg` — `/planmode auto` confirmation. (iPhone)
- [x] `planmode-show.jpg` — `/planmode show` output. (iPhone)
- [x] `project-command.jpg` — `/<project>` command with ctx: footer. (iPhone)
- [ ] `branch-directive.jpg` — `@branch` directive response with ctx: project @branch footer. (iPhone)
- [x] `agent-resolution.jpg` — `/agent` command output showing engine resolution layers. (MacBook)
- [x] `engine-footer.jpg` — Engine directive in progress footer (e.g. /codex). (iPhone)
- [ ] `route-by-chat.jpg` — Chat bound to project, message routed with project context in footer. (iPhone)
- [x] `startup-message.jpg` — Bot startup message showing version and engine info.
- [ ] `project-init.jpg` — Terminal `untether init` showing project registration.
- [ ] `doctor-output.jpg` — `untether doctor` output with check results.
- [ ] `doctor-all-passing.jpg` — `untether doctor` with all checks passing.
- [ ] `journalctl-startup.jpg` — journalctl output showing untether-dev starting cleanly.
- [ ] `worktree-run.jpg` — Worktree run with @branch directive and project context in footer.

## Tier 5: v0.35.0 features (7 images)

- [ ] `config-menu-v035.jpg` — `/config` home page with 2-column toggle layout (replaces old `config-menu.jpg` when captured).
- [ ] `outline-formatted.jpg` — Formatted plan outline with headings/bold/code blocks in Telegram.
- [ ] `outline-buttons-bottom.jpg` — Approve/Deny buttons on the last chunk of a multi-message outline.
- [ ] `outbox-delivery.jpg` — Agent-sent files appearing as Telegram documents with `📎` captions.
- [ ] `orphan-cleanup.jpg` — Progress message showing "⚠️ interrupted by restart" after orphan cleanup.
- [ ] `continue-command.jpg` — `/continue` picking up a CLI session from Telegram.
- [ ] `config-cost-budget.jpg` — Cost & Usage sub-page with budget and auto-cancel toggles.

## Reuse map

Some screenshots appear in multiple doc pages. The filename column shows which
file to use; docs reference the same image via relative paths.

| Screenshot | Used in | Notes |
|-----------|---------|-------|
| `approval-diff-preview.jpg` | README, tutorials/interactive-control | Docs use this name, not `approval-diff-howto` |
| `plan-outline-approve.jpg` | README, tutorials/interactive-control | |
| `chat-auto-resume.jpg` | tutorials/conversation-modes, how-to/chat-sessions | Docs use this name, not `session-auto-resume` |
| `post-outline-buttons.jpg` | tutorials/interactive-control, how-to/interactive-approval | Docs use this name, not `outline-approve-buttons` |
| `project-command.jpg` | how-to/projects, how-to/route-by-chat | Docs use this name, not `route-by-chat` |
| `verbose-progress.jpg` | how-to/verbose-progress | Docs use this name, not `verbose-vs-compact` |
| `browse-directory.jpg` | how-to/browse-files (Tier 1 and Tier 3 share) | |
| `usage-command.jpg` | how-to/cost-budgets (Tier 1 and Tier 3 share) | |
