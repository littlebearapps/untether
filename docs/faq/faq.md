---
title: "Untether — Frequently Asked Questions"
description: "Common questions about Untether: installation, supported engines, costs, privacy, troubleshooting, and design choices."
---

# Frequently Asked Questions

> Quick answers to the questions users ask most often. Also surfaced at
> <https://littlebearapps.com/help/untether/faq/>.

## What is Untether?

Untether is a Telegram bridge for AI coding agents. It runs on your computer (or a server you control) and forwards messages between Telegram and the agent CLI of your choice — Claude Code, Codex, OpenCode, Pi, Gemini CLI, or Amp.

Your machine still does all the work. Untether is the wire between your phone and the agent, with progress streaming, interactive approval buttons, voice transcription, cost tracking, scheduled runs, and inline settings layered on top. The intent is simple: keep using the same agent you already use, but stop being chained to a terminal window when you want to walk the dog or watch the footy.

## How do I install Untether?

Untether is published to PyPI. With [`uv`](https://docs.astral.sh/uv/) installed:

```sh
uv tool install untether
untether
```

Or with `pipx`:

```sh
pipx install untether
untether
```

The first run launches a setup wizard that creates a Telegram bot via [BotFather](https://t.me/BotFather), picks one of three workflow modes (assistant, workspace, or handoff), and writes `~/.untether/untether.toml`. After the wizard finishes, send a message to your bot in Telegram and the agent runs on your machine.

Already have a bot token? Skip the BotFather step with `untether --bot-token YOUR_TOKEN`. Full walkthrough: [Install and onboard](https://untether.littlebearapps.com/tutorials/install/).

## Which AI coding agents does Untether support?

Untether supports six agent CLIs out of the box:

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — complex refactors, architecture, long context. Most interactive features (plan mode, ask mode, diff preview, progressive cooldown) are Claude-specific.
- **[Codex](https://github.com/openai/codex)** — fast edits, shell commands, OpenAI subscription via ChatGPT login.
- **[OpenCode](https://github.com/opencode-ai/opencode)** — 75+ providers via Models.dev, local model support.
- **[Pi](https://github.com/mariozechner/pi-coding-agent)** — multi-provider auth, conversational style.
- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** — Google Gemini models with configurable approval modes.
- **[Amp](https://ampcode.com)** — Sourcegraph's coding agent with mode selection.

You can switch between engines per-message by prefixing with `/<engine>` (e.g. `/claude`, `/codex`). Each chat or topic can also have its own default engine. The full per-engine feature matrix is in the [README](https://github.com/littlebearapps/untether#-supported-engines).

## Do I need an API key to use Untether?

In most cases, no. Untether uses whatever authentication your agent CLI already has — your existing Claude Pro/Max subscription via OAuth, your ChatGPT Plus/Pro/Business plan via the Codex device-auth flow, your Gemini account, your Amp Sourcegraph login. If `claude auth status` works on your machine, Untether will use the same authentication.

API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) are only needed if you specifically want API billing instead of a subscription, or for engines that don't offer subscription auth (e.g. some OpenCode providers). Untether itself doesn't make any API calls — it just spawns the agent CLI as a subprocess.

The one exception is voice transcription: Untether ships with optional Whisper-via-Groq support. That's a separate API key (`voice_transcription_api_key`) which is masked in logs as `SecretStr` and only sent to your configured transcription endpoint.

## Where does my code and data go?

Untether runs entirely on your machine (or your server). Your repo, your environment, your authenticated agent — Untether is just a transport.

- **Telegram** sees the messages you exchange with your bot — that's the user-content channel by design. Messages are encrypted in transit but Telegram does have access to them on its servers, so treat the bot like any other chat: don't paste production secrets into prompts.
- **Your agent CLI** sees whatever you send in the message plus your project's filesystem (subject to whatever permission controls the engine has — Claude's `--permission-mode`, Codex's `--ask-for-approval`, etc.).
- **The agent's vendor** (Anthropic / OpenAI / Google / Sourcegraph / etc.) sees whatever the agent CLI sends to its API — same as if you ran the CLI directly in a terminal.
- **Untether itself** doesn't phone home, doesn't send analytics, doesn't have a remote service. Crash logs stay on your machine. The bot token, allowlisted user IDs, and any optional voice-transcription API key live in your local `untether.toml` and are masked in operational logs.

If you want stricter sandboxing, run Untether inside a container or on a VM. The whole bridge is one Python process and a few state files in `~/.untether/`.

## How do I approve tool calls from my phone?

When Claude Code wants to run a tool that needs approval — write a file, run a shell command in plan mode, etc. — Untether posts the request to your Telegram chat with inline buttons: ✅ Approve / ❌ Deny / 📋 Pause & Outline Plan. Tap a button and the agent continues immediately.

If you click "Pause & Outline Plan", Claude writes a plain-language summary of what it's about to do, and you get a second round of buttons: ✅ Approve Plan / ❌ Deny / 💬 Let's discuss. Approving here also auto-approves the next plan-exit so you don't get prompted twice for the same plan.

Per-chat plan mode (`/planmode on/auto/off`) controls when the buttons appear:

- **on** — every plan transition prompts for approval.
- **auto** — plan transitions auto-approve, but tool approvals still appear.
- **off** — no plan phase; tools auto-execute (subject to engine policy).

For non-Claude engines, approval is enforced per-engine pre-run (Codex `--ask-for-approval`, Gemini `--approval-mode`) rather than via mid-run buttons. Full guide: [Interactive approval](https://untether.littlebearapps.com/how-to/interactive-approval/).

## What happens if my agent crashes or my phone loses signal mid-run?

Untether is built around the assumption that your phone is unreliable but your computer isn't. Two things matter here:

1. **Your agent keeps running.** It's a subprocess on your machine. It doesn't care whether your phone is connected, whether Telegram is open, or whether you've gone to sleep. Progress messages buffer locally; reconnection rendering is automatic.
2. **Untether catches the common failure modes.** If a Claude Code session exits prematurely after a tool result without processing it (a known upstream bug), Untether auto-resumes it. If the bot is restarted while a run is in progress, ephemeral approval messages are cleaned up and orphaned progress messages get a `⚠️ interrupted by restart` marker. Stalls that look "alive but silent" trigger progressive warnings, and the watchdog auto-cancels truly dead processes.

Everything important — Telegram update offsets, active progress message references, trigger fire history — is persisted to disk so a restart picks up where you left off without dropping or duplicating messages.

## How do I keep agents from spending too much money?

Untether ships per-run and per-day cost budgets. In `untether.toml`:

```toml
[cost_budget]
enabled = true
max_cost_per_run = 2.00      # USD; warn or auto-cancel if a single run exceeds this
max_cost_per_day = 10.00     # USD; ditto across a calendar day
warn_at_pct = 80             # warn when this % of budget is consumed
auto_cancel = true           # cancel the run when the threshold is hit
```

`/usage` shows the current run's cost; `/usage debug` shows OAuth token expiry, schema-mismatch counters, and cache freshness — useful when the subscription footer goes silent. `/stats` reports per-engine totals across today, this week, and all time.

Cost tracking is most accurate for Claude (full USD reporting via API metadata) and OpenCode. Codex, Pi, Gemini, and Amp report tokens-only. Subscription users (Claude Pro/Max, ChatGPT, Gemini, Amp) see a `5h: N% / 7d: N%` indicator instead of dollars. See the [cost-budgets guide](https://untether.littlebearapps.com/how-to/cost-budgets/) for tuning.

## Does /loop work via Untether?

By default, no — Claude Code's `/loop` and `ScheduleWakeup` are session-scoped, and the Untether subprocess exits when each turn finishes. Schedules registered by Claude don't fire afterwards.

To enable end-to-end /loop support, turn on **Loop mode** in `/config → 🔁 Loop mode`. When on, Untether observes Claude's schedule registrations and re-fires each iteration when due, spawning a fresh `claude --resume` subprocess per fire.

Be aware: autonomous loops consume API credits or your subscription quota. Set a budget in `/config → 💰 Cost & usage` *before* turning Loop mode on — the same daily cost cap applies to loop fires automatically. See the [Schedule tasks how-to](https://untether.littlebearapps.com/how-to/schedule-tasks/#loop-mode) for details.

## Can I send voice notes instead of typing?

Yes — record a voice message in Telegram and Untether transcribes it via a Whisper-compatible endpoint, then runs the transcribed text as a normal prompt. Configure in `untether.toml`:

```toml
[transports.telegram]
voice_transcription = true
voice_transcription_model = "whisper-large-v3-turbo"
voice_transcription_base_url = "https://api.groq.com/openai/v1"
voice_transcription_api_key = "gsk_..."   # SecretStr — masked in logs
```

Groq's Whisper Large v3 Turbo is fast and cheap; any OpenAI-compatible Whisper endpoint works (including a self-hosted one). The API key is `SecretStr`-masked in `repr()` / `str()` / structlog so it never lands in journal or crash output. Full setup: [Voice notes](https://untether.littlebearapps.com/how-to/voice-notes/).

## How do I update Untether?

If you installed with `uv`:

```sh
uv tool upgrade untether
```

If you installed with `pipx`:

```sh
pipx upgrade untether
```

Then restart the running bot to pick up the new wheel. If you're running interactively, send `/restart` from Telegram — it drains active runs first, then exits, and your launcher restarts the process. If you're running under systemd:

```sh
systemctl --user restart untether
```

Untether follows semver: patch versions (e.g. `0.35.2 → 0.35.3`) are bug fixes, minor versions (`0.34.x → 0.35.0`) add features, major versions break config or runner protocol. Pre-release `rcN` wheels publish to TestPyPI for staging dogfooding. The [CHANGELOG](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md) lists every change with linked GitHub issues.

## How do I uninstall Untether?

```sh
uv tool uninstall untether
# or
pipx uninstall untether

rm -rf ~/.untether/
```

That removes the CLI, all state files (chat preferences, session resumes, trigger history), and your `untether.toml`. If you set up a systemd user unit, also `systemctl --user disable --now untether` and remove the unit file.

The Telegram bot itself lives on Telegram's side — to delete it entirely, talk to [@BotFather](https://t.me/BotFather), pick `/deletebot`, and select your bot. That step is optional; an inactive bot causes no harm beyond squatting the username. Full uninstall walkthrough: [Uninstall Untether](https://untether.littlebearapps.com/how-to/uninstall/).

## Where can I get help or report a bug?

- **Documentation** — [`docs/`](https://github.com/littlebearapps/untether/tree/master/docs) covers tutorials, how-to guides, engine references, and architecture.
- **Help centre** — <https://untether.littlebearapps.com>
- **Bug reports and feature requests** — [GitHub Issues](https://github.com/littlebearapps/untether/issues) with the `bug` or `enhancement` label.
- **Security issues** — see [SECURITY.md](https://github.com/littlebearapps/untether/blob/master/SECURITY.md) for the responsible-disclosure path.

When filing an issue, include your Untether version (`untether --version`), the engine + version that reproduced the bug, and a relevant excerpt from `journalctl --user -u untether` (or the equivalent log path for your runtime). Sensitive paths and secrets are scrubbed from logs by default but spot-check before pasting.
