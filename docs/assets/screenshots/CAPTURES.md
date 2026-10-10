# Screenshot Capture Checklist

Filenames, descriptions, and capture notes for each screenshot. All images go in
`docs/assets/screenshots/`. Crop tightly to the relevant UI element — no status
bars, no keyboard, no notification tray.

**Bot:** `@untether_dev_bot` (dev instance)
**Theme:** Light Telegram theme
**Content:** Real tasks against the Untether repo (public, nothing sensitive)

**v0.36.0 set:** the shots marked *(v0.36 demo take)* are frames from one recording on 7 October 2026
(Claude Code, the weather-cli demo project, footer `opus 5.5 · xhigh` with `% ctx`). They are the full iPhone
frame at 650×1420, not cropped to the message, so they match `hero-collage.jpg`.

## Tier 0: README (3 images)

- [x] `hero-voice-to-result.jpg` — Voice waveform bubble → transcript → progress message. *(v0.36 demo take. The README hero is `hero-collage.jpg`.)*
- [x] `approval-diff-preview.jpg` — Edit approval showing `- old` / `+ new` lines with Approve / Deny buttons. *(v0.36 demo take.)*
- [x] `plan-outline-approve.jpg` — Outline text + Approve Plan / Deny / Let's discuss buttons. *(v0.36 demo take; same frame as `post-outline-buttons.jpg`.)*

## Tier 1: Website extras (4 images)

- [x] `browse-directory.jpg` — `/browse` inline keyboard with directory listing (use untether's own repo).
- [x] `usage-command.jpg` — `/usage` cost tracking output.
- [x] `multi-engine-switch.jpg` — Engine switching: `/claude` then `/codex` in same chat.
- [ ] `verbose-vs-compact.jpg` — Side-by-side or sequential compact vs verbose for same action.

## Tier 2: Tutorial screenshots (12 images)

- [x] `progress-streaming.jpg` — Progress message with the action list, footer and cancel button. *(v0.36 demo take.)*
- [x] `final-answer-footer.jpg` — Final answer with model/cost footer, subscription line and resume line. *(v0.36 demo take.)*
- [x] `cancel-button.jpg` — Cancel button on progress and the resulting "cancelled" status.
- [x] `deny-response.jpg` — Claude acknowledging a denial and explaining intent.
- [x] `plan-outline-text.jpg` — Claude's written outline/plan as formatted text in chat. *(v0.36 demo take; the outline frame cropped above the buttons.)*
- [x] `post-outline-buttons.jpg` — Post-outline `✅ Approve Plan` / `❌ Deny` / `💬 Let's discuss` buttons. *(v0.36 demo take.)*
- [x] `ask-question-options.jpg` — AskUserQuestion with option buttons. *(v0.36 demo take.)*
- [x] `ask-reply-continue.jpg` — User replying with text to AskUserQuestion, Claude continuing.
- [x] `chat-auto-resume.jpg` — Follow-up message auto-resuming without reply.
- [x] `stateless-reply-resume.jpg` — Stateless mode with user replying to a message with resume line.
- [ ] `botfather-newbot.jpg` — BotFather /newbot flow. **REDACT the bot token.**
- [ ] `onboarding-wizard.jpg` — Terminal showing the workflow selection step.

## Tier 3: How-to screenshots (15 images)

- [x] `approval-buttons-howto.jpg` — ExitPlanMode request with Approve Plan / Deny / Pause & Outline Plan buttons. *(v0.36 demo take; the `exit-planmode-buttons.jpg` frame cropped to the card.)*
- [ ] `approval-diff-howto.jpg` — Diff preview on approval (Edit with `- old` / `+ new` lines).
- [x] `ask-text-reply-howto.jpg` — AskUserQuestion with option buttons and "Other (type reply)". *(v0.36 demo take; the `ask-question-options.jpg` frame cropped to the card.)*
- [x] `exit-planmode-buttons.jpg` — ExitPlanMode with `✅ Approve Plan` / `❌ Deny` / `📋 Pause & Outline Plan` and the approval caption. *(v0.36 demo take.)*
- [ ] `outline-approve-buttons.jpg` — Written outline + Approve Plan / Deny buttons below.
- [x] `cost-warning-alert.jpg` — Cost warning alert showing budget threshold exceeded.
- [x] `voice-transcription.jpg` — Voice note followed by transcribed text and agent output. (iPhone)
- [x] `file-put.jpg` — Document upload with `/file put` caption and saved confirmation. (iPhone)
- [x] `file-get.jpg` — `/file get` response with fetched file as document. (iPhone)
- [ ] `session-auto-resume.jpg` — Chat session auto-resume. (iPhone)
- [ ] `forum-topic-context.jpg` — Forum topic bound to project/branch with context footer. (MacBook)
- [x] `config-menu.jpg` — `/config` home page with the settings summary, help/bug links and two-column buttons. *(v0.36 demo take.)*
- [ ] `verbose-vs-compact.jpg` — Side-by-side or sequential compact vs verbose for same action. (MacBook)
- [ ] `webhook-notification.jpg` — Webhook-triggered run with rendered prompt and progress. (MacBook)
- [ ] `scheduled-message.jpg` — Telegram scheduled message picker for a task. (iPhone)

## Tier 4: Supporting screenshots (12 images)

- [ ] `planmode-on.jpg` — `/planmode on` confirmation. (iPhone) **RECAPTURE (post-#747): the reply now reads "permission mode on for this chat: …" ([#783](https://github.com/littlebearapps/untether/issues/783)).**
- [ ] `planmode-auto.jpg` — `/planmode plan-auto` confirmation (the current file still shows the old `/planmode auto` reply). (iPhone) **RECAPTURE (post-#747) as `planmode-plan-auto.jpg`: `/planmode plan-auto` confirmation, for the #741 rename ([#783](https://github.com/littlebearapps/untether/issues/783)).**
- [ ] `planmode-show.jpg` — `/planmode show` output. (iPhone) *The current file is from the v0.36 demo take and shows the mode set to `on`.* **RECAPTURE (post-#747): `/planmode show` with the mode set to `plan-auto`, to match the tutorial text ([#783](https://github.com/littlebearapps/untether/issues/783)).**
- [x] `project-command.jpg` — `/<project>` command with ctx: footer. (iPhone)
- [ ] `branch-directive.jpg` — `@branch` directive response with ctx: project @branch footer. (iPhone)
- [x] `agent-resolution.jpg` — `/agent` command output showing engine resolution layers. (MacBook)
- [x] `engine-footer.jpg` — Engine directive in progress footer (e.g. /codex). (iPhone)
- [ ] `route-by-chat.jpg` — Chat bound to project, message routed with project context in footer. (iPhone)
- [ ] `startup-message.jpg` — Bot startup message showing version and engine info. **RECAPTURE: now includes help/bug links on separate line.**
- [ ] `project-init.jpg` — Terminal `untether init` showing project registration.
- [ ] `doctor-output.jpg` — `untether doctor` output with check results.
- [ ] `doctor-all-passing.jpg` — `untether doctor` with all checks passing.
- [ ] `journalctl-startup.jpg` — journalctl output showing untether-dev starting cleanly.
- [ ] `worktree-run.jpg` — Worktree run with @branch directive and project context in footer.

## Tier 5: v0.35.0 features (6 images)

- [ ] `outline-formatted.jpg` — Formatted plan outline with headings/bold/code blocks in Telegram.
- [ ] `outline-buttons-bottom.jpg` — Approve/Deny buttons on the last chunk of a multi-message outline.
- [x] `outbox-delivery.jpg` — Agent-sent file appearing as a Telegram document with a `📎` caption. *(v0.36 demo take.)*
- [ ] `orphan-cleanup.jpg` — Progress message showing "⚠️ interrupted by restart" after orphan cleanup.
- [ ] `continue-command.jpg` — `/continue` picking up a CLI session from Telegram.
- [x] `config-cost-budget.jpg` — Cost & usage sub-page with the Budget and Stop at limit buttons. *(v0.36 demo take.)*

## Tier 6: v0.36.0 features (5 images)

- [x] `steer-mid-run.jpg` — `/steer` sent while Claude is working, with the "↪️ Steered into the current run." reply. *(v0.36 demo take.)*
- [x] `background-tasks-panel.jpg` — `⏳ background (2)` status message with two agent rows. *(v0.36 demo take.)*
- [x] `config-permission-mode.jpg` — `/config` → Permission mode page with Off / On / Plan-auto / Auto. *(v0.36 demo take.)*
- [ ] `context-compacted.jpg` — a `🗜️ Context compacted` row in a progress message.
- [ ] `budget-stop-at-limit.jpg` — a run refused at the daily budget, with the Run anyway button.

## Reuse map

Some screenshots appear in multiple doc pages. The filename column shows which
file to use; docs reference the same image via relative paths.

| Screenshot | Used in | Notes |
|-----------|---------|-------|
| `approval-diff-preview.jpg` | tutorials/interactive-control, how-to/interactive-approval | Docs use this name, not `approval-diff-howto` |
| `plan-outline-approve.jpg` | not referenced | Same frame as `post-outline-buttons.jpg`, which the docs use |
| `chat-auto-resume.jpg` | tutorials/conversation-modes, how-to/chat-sessions | Docs use this name, not `session-auto-resume` |
| `post-outline-buttons.jpg` | tutorials/interactive-control, how-to/interactive-approval | Docs use this name, not `outline-approve-buttons` |
| `project-command.jpg` | tutorials/projects-and-branches, how-to/route-by-chat | Docs use this name, not `route-by-chat` |
| `config-menu.jpg` | how-to/inline-settings | |
| `outbox-delivery.jpg` | how-to/file-transfer | |
| `verbose-progress.jpg` | how-to/verbose-progress | Docs use this name, not `verbose-vs-compact` |
| `browse-directory.jpg` | how-to/browse-files (Tier 1 and Tier 3 share) | |
| `usage-command.jpg` | how-to/cost-budgets (Tier 1 and Tier 3 share) | |

## Retired

- `cooldown-auto-deny.jpg` — removed in v0.36.0 ([#783](https://github.com/littlebearapps/untether/issues/783)); the progressive cooldown it illustrated was retired in v0.35.4 ([#570](https://github.com/littlebearapps/untether/issues/570)), and the file was a byte-for-byte copy of `post-outline-buttons.jpg`. Don't recapture.
- `config-menu-v035.jpg` — never captured as a separate file. `config-menu.jpg` was recaptured for v0.36.0 and shows the 2-column layout with the help/bug links footer.
