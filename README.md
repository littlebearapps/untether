<p align="center">
  <img src="https://raw.githubusercontent.com/littlebearapps/untether/master/docs/assets/untether-logo-full.svg" height="200" alt="Untether" />
</p>

<p align="center">
  <strong>Telegram bridge for AI coding agents.</strong><br>
  Send tasks by voice or text, stream progress live, and approve changes — from your phone, anywhere.
</p>

<p align="center">
  🌐 <a href="https://untether.cc"><strong>untether.cc</strong></a> · 📖 <a href="#-documentation">Help guides</a>
</p>

<p align="center">
  <a href="https://github.com/littlebearapps/untether/actions/workflows/ci.yml"><img src="https://github.com/littlebearapps/untether/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://pypi.org/project/untether/"><img src="https://img.shields.io/pypi/v/untether" alt="PyPI" /></a>
  <a href="https://pypi.org/project/untether/"><img src="https://img.shields.io/pypi/dm/untether" alt="PyPI Downloads" /></a>
  <a href="https://pypi.org/project/untether/"><img src="https://img.shields.io/pypi/pyversions/untether" alt="Python" /></a>
  <a href="https://github.com/littlebearapps/untether/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License" /></a>
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> · <a href="#-what-you-get">Features</a> · <a href="#-supported-engines">Engines</a> · <a href="#-documentation">Docs</a> · <a href="#-contributing">Contributing</a>
</p>

---

Your AI coding agents need a terminal, but you don't need to sit at one. Untether runs on your machine (or server) and connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [OpenCode](https://opencode.ai) and [Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent) to a Telegram bot. Send a task from your phone, watch the agent work in real time, tap a button when it needs permission, and read the result when it's done — no desk, no SSH, no screen sharing. The agent keeps running if you close Telegram or lose signal.

<p align="center">
  <img src="https://raw.githubusercontent.com/littlebearapps/untether/master/docs/assets/screenshots/hero-collage.jpg" alt="Send tasks by voice, approve changes remotely, configure from Telegram" width="100%" />
</p>
<p align="center"><sub>* Feature availability varies by engine — see <a href="#engine-compatibility">engine compatibility</a></sub></p>

---

## ⚡ Quick start

You need **Python 3.12+**, [uv](https://docs.astral.sh/uv/) (or pipx), and at least one agent CLI on your `PATH` (`claude`, `codex`, `opencode` or `pi`).

```sh
uv tool install untether        # recommended
# or
pipx install untether

untether                        # first run starts the setup wizard
```

The wizard creates your Telegram bot (or takes an existing token), picks a [workflow mode](https://littlebearapps.com/help/untether/choose-a-mode/) — assistant, workspace or handoff — and connects your chat. Then message your bot:

> fix the failing tests in src/auth

Step-by-step: [Install and onboard](https://littlebearapps.com/help/untether/install/) → [First run](https://littlebearapps.com/help/untether/first-run/). Update with `uv tool upgrade untether` (or `pipx upgrade untether`), then send `/restart` from Telegram — see [Update Untether](https://littlebearapps.com/help/untether/update/).

> **Upgrading from 0.35.x?** v0.36.0 has breaking changes (the `auto` permission mode is now `plan-auto`, unattended crons deny approvals, Codex safe mode is a real read-only sandbox, and more). Read [Upgrading to v0.36.0](https://littlebearapps.com/help/untether/update/#upgrading-to-v0360) first.

---

## 🎯 What you get

- 📡 **Live progress** — tool calls, file changes and elapsed time stream into one message as the agent works ([verbose progress](https://littlebearapps.com/help/untether/verbose-progress/))
- 🔐 **Approvals from your phone (Claude)** — approve tool calls, plans and clarifying questions with inline buttons; four permission modes via `/planmode` ([interactive approval](https://littlebearapps.com/help/untether/interactive-approval/), [plan mode](https://littlebearapps.com/help/untether/plan-mode/))
- ↪️ **Live sessions and steering (Claude)** — background tasks stay visible, and `/steer` sends a follow-up into the running session ([steer follow-ups](https://littlebearapps.com/help/untether/steer-follow-ups/))
- 🎙️ **Voice notes** — dictate tasks; transcribed by any Whisper-compatible endpoint ([voice notes](https://littlebearapps.com/help/untether/voice-notes/))
- 📁 **Projects, branches and worktrees** — `/myproject @feat/thing` targets a repo and branch, with parallel runs in isolated worktrees ([projects](https://littlebearapps.com/help/untether/projects/), [worktrees](https://littlebearapps.com/help/untether/worktrees/))
- 💬 **Three workflow modes** — ongoing chat, forum topics per project, or reply-to-continue handoff ([choose a mode](https://littlebearapps.com/help/untether/choose-a-mode/))
- 💰 **Cost and usage tracking** — per-run and daily budgets, outlier alerts, optional stop-at-limit, `/usage` breakdowns ([cost budgets](https://littlebearapps.com/help/untether/cost-budgets/))
- ⏰ **Schedules and webhooks** — cron triggers, webhook triggers, one-shot `/at 30m …` runs and Claude Loop mode ([schedule tasks](https://littlebearapps.com/help/untether/schedule-tasks/), [webhooks and cron](https://littlebearapps.com/help/untether/webhooks-and-cron/))
- 🔄 **Terminal ↔ Telegram resume** — start in your terminal, pick it up with `/continue` ([cross-environment resume](https://littlebearapps.com/help/untether/cross-environment-resume/))
- ⚙️ **Settings and files from chat** — `/config` button menu, `/browse`, `/file put`/`get` and agent-delivered files ([inline settings](https://littlebearapps.com/help/untether/inline-settings/), [file transfer](https://littlebearapps.com/help/untether/file-transfer/))

Every command is listed in the [commands reference](https://littlebearapps.com/help/untether/commands-and-directives/).

---

## 🔌 Supported engines

| Engine | Install | Good at |
|--------|---------|---------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `npm i -g @anthropic-ai/claude-code` | Complex refactors, architecture, long context — and all interactive features |
| [Codex](https://github.com/openai/codex) | `npm i -g @openai/codex` | Fast edits, shell commands, quick fixes |
| [OpenCode](https://opencode.ai) | `npm i -g opencode-ai@1` | 75+ providers via Models.dev, local models (**1.x only** — 2.x is refused before it starts) |
| [Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent) | `npm i -g @mariozechner/pi-coding-agent` | Multi-provider auth, conversational |

Use your existing Claude or ChatGPT subscription — no extra API keys needed unless you want API billing. Switch engines per message (`/codex …`) or per chat (`/agent set claude`) — see [switch engines](https://littlebearapps.com/help/untether/switch-engines/).

### Engine compatibility

| Feature | Claude Code | Codex | OpenCode | Pi |
|---------|:-----------:|:-----:|:--------:|:--:|
| Progress streaming, resume, voice input, model override | ✅ | ✅ | ✅ | ✅ |
| Terminal resume (`/continue`) | ✅ | ✅ | ✅ | ✅² |
| Cost tracking | ✅ | ~¹ | ✅ | ~¹ |
| Reasoning / effort levels | ✅ | ✅ | — | — |
| Pre-run approval policy (`/config`) | ✅ | ✅ | — | — |
| Interactive approvals, plan mode, ask mode, diff preview | ✅ | — | —³ | — |
| Live sessions, background tasks, `/steer` | ✅ | — | — | — |
| Context % in status line, subscription usage | ✅ | — | — | — |
| Context compaction shown | ✅ | — | — | ✅ |
| Device re-auth (`/auth codex`) | — | ✅ | — | — |

¹ Token counts only, no USD cost. ² Pi needs `provider = "openai-codex"` for OAuth subscriptions in headless mode. ³ `opencode run` rejects tools your OpenCode rules set to `ask` — set them to `allow` for unattended use.

### Deprecated engines

[Gemini CLI](https://github.com/google-gemini/gemini-cli) and [Amp](https://ampcode.com) still ship and load, but are **deprecated and no longer supported** — no testing, no bug fixes, and they may be removed in a future release. Google retired Gemini CLI for individual accounts on 18 June 2026; its successor, Antigravity CLI, arrives as its own engine in v0.36.1. Amp remotely refuses clients it considers out of date, so a working setup can stop without notice. Details: [troubleshooting](https://littlebearapps.com/help/untether/troubleshooting/#why-does-my-gemini-run-stall-or-my-amp-run-fail-immediately).

---

## 🔒 Privacy and security

Untether runs entirely on your machine: no telemetry, no analytics, no phone-home, no auto-updates. Its own outbound calls are the Telegram Bot API, Anthropic's subscription-usage endpoint for Claude (using Claude Code's existing login) and, only if you enable it, a voice-transcription endpoint; agent CLIs call their own vendors' APIs. Config lives in `~/.untether/untether.toml` — it holds your bot token, so never commit it. Lock the bot to your Telegram user ID and read [security hardening](https://littlebearapps.com/help/untether/security/) and [where your data goes](https://littlebearapps.com/help/untether/faq/#where-does-my-code-and-data-go).

---

## 📖 Documentation

Full docs live in the [help centre](https://littlebearapps.com/help/untether/) (mirrored from [`docs/`](https://github.com/littlebearapps/untether/tree/master/docs)).

- **Tutorials** — [install](https://littlebearapps.com/help/untether/install/), [first run](https://littlebearapps.com/help/untether/first-run/), [conversation modes](https://littlebearapps.com/help/untether/conversation-modes/), [projects and branches](https://littlebearapps.com/help/untether/projects-and-branches/), [multi-engine workflows](https://littlebearapps.com/help/untether/multi-engine/)
- **How-to guides** — [all guides](https://github.com/littlebearapps/untether/tree/master/docs/how-to), including [group chats](https://littlebearapps.com/help/untether/group-chat/), [forum topics](https://littlebearapps.com/help/untether/topics/), [model and reasoning](https://littlebearapps.com/help/untether/model-reasoning/), [session export](https://littlebearapps.com/help/untether/export-sessions/) and [uninstall](https://littlebearapps.com/help/untether/uninstall/)
- **Reference** — [configuration](https://littlebearapps.com/help/untether/config/), [commands](https://littlebearapps.com/help/untether/commands-and-directives/), [engine runners](https://github.com/littlebearapps/untether/tree/master/docs/reference/runners), [architecture](https://littlebearapps.com/help/untether/architecture/)
- **Help** — [FAQ](https://littlebearapps.com/help/untether/faq/), [troubleshooting](https://littlebearapps.com/help/untether/troubleshooting/), [changelog](https://littlebearapps.com/help/untether/changelog/), [upgrading to v0.36.0](https://littlebearapps.com/help/untether/update/#upgrading-to-v0360)

---

## 🤝 Contributing

Found a bug? [Open an issue](https://github.com/littlebearapps/untether/issues/new/choose). Questions and ideas go to [GitHub Discussions](https://github.com/littlebearapps/untether/discussions) ([Q&A](https://github.com/littlebearapps/untether/discussions/categories/q-a), [Ideas](https://github.com/littlebearapps/untether/discussions/categories/ideas)); follow [Announcements](https://github.com/littlebearapps/untether/discussions/categories/announcements) for a plain-English summary of every release.

Want to contribute code? See [CONTRIBUTING.md](https://github.com/littlebearapps/untether/blob/master/CONTRIBUTING.md). To report a vulnerability privately, see [SECURITY.md](https://github.com/littlebearapps/untether/blob/master/SECURITY.md).

---

## 🙏 Acknowledgements

Untether is a fork of [takopi](https://github.com/banteg/takopi) by [@banteg](https://github.com/banteg), which provided the original Telegram-to-Codex bridge. Untether extends it with interactive permission control, multi-engine support, plan mode, cost tracking and much more.

## 📄 Licence

[MIT](https://github.com/littlebearapps/untether/blob/master/LICENSE) — made by [Little Bear Apps](https://github.com/littlebearapps) 🐶
