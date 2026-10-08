---
title: untether
---

# Untether documentation

Untether runs coding agents on your computer and bridges them to Telegram. Send a task from your phone while walking the dog, dictate the next one by voice at the gym, and review results on your tablet later — your agents keep working even if you close Telegram or lose signal. Scale from quick one-offs to multi-project workflows with topics and parallel worktrees.

It works with Claude Code, Codex, OpenCode (1.x) and Pi; approval buttons, plan mode, questions and steering are Claude Code features. Gemini CLI and Amp still load but are deprecated and no longer supported. Website: [untether.cc](https://untether.cc).

<div class="hero-demo">
<div class="hero-chat">
<div class="chat-messages"></div>
</div>
<div class="hero-terminal">
<div class="terminal-content"></div>
</div>
</div>

## Quick start

```bash
uv tool install -U untether
untether --onboard
```

Untether needs Python 3.12 or newer. Onboarding walks you through bot setup and asks how you want to work. [Full install guide →](tutorials/install.md)

Upgrading from v0.35.4 or earlier? v0.36.0 has breaking changes — read [Upgrading to v0.36.0](how-to/update.md#upgrading-to-v0360) first.

## Pick your workflow

<div class="grid cards" markdown>
-   :lucide-message-circle:{ .lg } **Assistant**

    ---

    Ongoing chat. New messages auto-continue; `/new` to reset.

    Best for: solo work, natural conversation flow.

    [Get started →](tutorials/first-run.md)

-   :lucide-folder-kanban:{ .lg } **Workspace**

    ---

    Forum topics bound to projects and branches.

    Best for: teams, organised multi-repo workflows.

    [Set up topics →](how-to/topics.md)

-   :lucide-terminal:{ .lg } **Handoff**

    ---

    Reply-to-continue. Copy resume lines to your terminal.

    Best for: explicit control, terminal-first workflow.

    [Get started →](tutorials/first-run.md)

</div>

You can change workflows later by editing `~/.untether/untether.toml`.

## Tutorials

Step-by-step guides for new users:

1. [Install & onboard](tutorials/install.md) — set up Untether and your bot
2. [First run](tutorials/first-run.md) — send a task, watch it stream, continue the conversation
3. [Interactive control](tutorials/interactive-control.md) — approve or deny Claude Code's actions, review plans, answer questions
4. [Projects & branches](tutorials/projects-and-branches.md) — target repos from anywhere, run on feature branches
5. [Multi-engine](tutorials/multi-engine.md) — use different engines for different tasks

## How-to guides

- [Chat sessions](how-to/chat-sessions.md), [Topics](how-to/topics.md), [Projects](how-to/projects.md), [Worktrees](how-to/worktrees.md)
- [Plan mode](how-to/plan-mode.md), [Interactive approval](how-to/interactive-approval.md), [Steer follow-ups](how-to/steer-follow-ups.md), [Cost budgets](how-to/cost-budgets.md)
- [Voice notes](how-to/voice-notes.md), [File transfer](how-to/file-transfer.md), [Export sessions](how-to/export-sessions.md)
- [Webhooks & cron](how-to/webhooks-and-cron.md), [Group chat](how-to/group-chat.md), [Schedule tasks](how-to/schedule-tasks.md)
- [Write a plugin](how-to/write-a-plugin.md), [Add a runner](how-to/add-a-runner.md), [Dev setup](how-to/dev-setup.md)

## Reference

Exact options, defaults, and contracts:

- [Commands & directives](reference/commands-and-directives.md)
- [Configuration](reference/config.md)
- [Specification](reference/specification.md) — normative behaviour

## Help and community

- [FAQ](faq/faq.md) and the [help centre](https://littlebearapps.com/help/untether/)
- Questions and ideas: [GitHub Discussions](https://github.com/littlebearapps/untether/discussions); bugs: [GitHub Issues](https://github.com/littlebearapps/untether/issues/new/choose)
- Website: [untether.cc](https://untether.cc)
