---
title: "Untether — Frequently Asked Questions"
description: "Common questions about Untether: installation, supported engines, costs, privacy, troubleshooting, and design choices."
---

# Frequently Asked Questions

> Quick answers to the questions users ask most often. Also surfaced at
> <https://littlebearapps.com/help/untether/faq/>.

## What is Untether?

Untether is a Telegram bridge for AI coding agents. It runs on your computer (or a server you control) and forwards messages between Telegram and the agent CLI of your choice — Claude Code, Codex, OpenCode or Pi. (Gemini CLI and Amp are still included, but deprecated and no longer supported.)

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

Already have a bot token? Answer **yes** when the wizard asks whether you have one, paste it in, and the BotFather walkthrough is skipped. To re-run the wizard later, use `untether --onboard`. Full walkthrough: [Install and onboard](https://littlebearapps.com/help/untether/install/).

## Which AI coding agents does Untether support?

Untether supports four agent CLIs out of the box:

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — complex refactors, architecture, long context. Most interactive features (plan mode, ask mode, diff preview, the Pause & Outline plan gate) are Claude-specific.
- **[Codex](https://github.com/openai/codex)** — fast edits, shell commands, OpenAI subscription via ChatGPT login.
- **[OpenCode](https://opencode.ai)** — 75+ providers via Models.dev, local model support.
- **[Pi](https://github.com/mariozechner/pi-coding-agent)** — multi-provider auth, conversational style.

Two further engines are still included and still load, but are **deprecated and no longer supported** — no bug fixes, no testing, and they may be removed in a future release. Don't start new work on them:

- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** — Google [retired Gemini CLI for individual accounts (free, Google AI Pro and Ultra) on 18 June 2026](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/) and replaced it with [Antigravity CLI](https://antigravity.google). Gemini CLI still works with paid Gemini API keys and Enterprise licences, and the engine still loads, but Untether no longer tests it or fixes bugs in it. Antigravity CLI support ships as a separate `antigravity` engine in v0.36.1.
- **[Amp](https://ampcode.com)** — Untether's Amp integration is no longer maintained. Amp remotely refuses clients it considers out of date, and Untether does not track that cadence, so a working setup can stop working without notice. This is a decision about our integration, not about Amp itself.

You can switch between engines per-message by prefixing with `/<engine>` (e.g. `/claude`, `/codex`). Each chat or topic can also have its own default engine. The full per-engine feature matrix is in the [README](https://github.com/littlebearapps/untether#-supported-engines).

## Do I need an API key to use Untether?

In most cases, no. Untether uses whatever authentication your agent CLI already has — your existing Claude Pro/Max subscription via OAuth, your ChatGPT Plus/Pro/Business plan via the Codex device-auth flow, or your OpenCode/Pi provider login. If `claude auth status` works on your machine, Untether will use the same authentication.

The two [deprecated engines](#which-ai-coding-agents-does-untether-support) are the exception: Gemini CLI no longer authenticates individual or free Google accounts at all (upstream EOL, 18 June 2026), and Amp requires a current client that Untether does not track. Both fail with an authentication or version error rather than falling back to anything — Untether never silently reroutes a run to a different provider.

API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) are only needed if you specifically want API billing instead of a subscription, or for engines that don't offer subscription auth (e.g. some OpenCode providers). Untether itself doesn't make any API calls — it just spawns the agent CLI as a subprocess.

The one exception is voice transcription: Untether ships with optional Whisper-via-Groq support. That's a separate API key (`voice_transcription_api_key`) which is masked in logs as `SecretStr` and only sent to your configured transcription endpoint — as are credentials embedded in a configured URL (`https://user:pass@host/v1`), which are masked in every log line.

## Where does my code and data go?

Untether runs entirely on your machine (or your server). Your repo, your environment, your authenticated agent — Untether is just a transport.

- **Telegram** sees the messages you exchange with your bot — that's the user-content channel by design. Messages are encrypted in transit but Telegram does have access to them on its servers, so treat the bot like any other chat: don't paste production secrets into prompts.
- **Your agent CLI** sees whatever you send in the message plus your project's filesystem (subject to whatever permission controls the engine has — Claude's `--permission-mode`, Codex's sandbox (`--sandbox`), etc.).
- **The agent's vendor** (Anthropic / OpenAI / Google / Sourcegraph / etc.) sees whatever the agent CLI sends to its API — same as if you ran the CLI directly in a terminal.
- **Untether itself** doesn't phone home, doesn't send analytics, doesn't have a remote service. Crash logs stay on your machine. The bot token, allowlisted user IDs, and any optional voice-transcription API key live in your local `untether.toml` and are masked in operational logs.

If you want stricter sandboxing, run Untether inside a container or on a VM. The whole bridge is one Python process and a few state files in `~/.untether/`.

## How do I approve tool calls from my phone?

When Claude Code wants to do something that needs approval — run a shell command with `/planmode off`, say, or start on a plan in plan mode — Untether posts the request to your Telegram chat with inline buttons. A tool request gets ✅ Approve / ❌ Deny (file edits come with a diff preview); a plan gets ✅ Approve Plan / ❌ Deny / 📋 Pause & Outline Plan. Tap a button and the agent continues immediately. If a background agent asks after Claude has already replied, the request arrives as its own message with the same buttons. A second tap on a button that's already been answered (or on a request Claude Code has since withdrawn) only tells you so; it never sends a second answer. Buttons only work in the chat where they appear. In a group, any allowed user can answer by default; set `approval_originator_only = true` under `[transports.telegram]` so only the person who started the run can. Scheduled crons and webhooks can't wait for a tap, so anything they would ask about is denied straight away and listed in the run's final message — give every Claude cron that should act on its own an explicit `permission_mode` (see [Schedule tasks](https://littlebearapps.com/help/untether/schedule-tasks/)).

If you click "Pause & Outline Plan", Claude writes a plain-language summary of what it's about to do, and you get a second round of buttons: ✅ Approve Plan / ❌ Deny / 💬 Let's discuss. Approving here also auto-approves the next plan-exit so you don't get prompted twice for the same plan. An approval covers that reply only: your next message, and any background wake-up in plan mode, starts in plan mode again — once any background agents that reply started have finished their work (until then, replies say `⚠️ Not re-planned`).

Per-chat permission mode (`/planmode on/plan-auto/auto/off`, or `/config → Permission mode`) controls when the buttons appear:

- **on** — Claude plans without editing files, and you approve the plan before changes start.
- **plan-auto** — plan mode, with the plan transition approved for you, so no buttons appear.
- **auto** — Claude Code's own auto mode: a classifier approves routine work and blocks risky actions such as sending sensitive data to external endpoints. Questions the agent asks you still come through as buttons. Auto mode needs a model that supports it: on one that doesn't (such as Haiku), Claude Code quietly runs in its ordinary ask-first mode instead, so Untether shows a `⚠️ Asked for auto mode — Claude Code is running default` line in the run and sends the remaining permission requests to Telegram for approval instead of approving them. Shell commands and file edits stay pre-approved by the default tool allowlist in that case, so pick a model that supports auto mode for anything you wouldn't let run unchecked.
- **off** — no plan phase; file edits and common filesystem commands run without asking, and other actions (most shell commands, web fetches, MCP tools) ask for approval unless your Claude Code settings allow them.

The **plan-auto** mode was called `auto` before v0.36.0. It was renamed because Claude Code introduced its own `auto` mode, and the two names collided. If you set `permission_mode = "auto"` in `untether.toml` and want the old behaviour, change it to `"plan-auto"`. Untether logs one warning at startup (and again if a config reload changes the list) naming every engine setting and cron that uses `"auto"`. Per-chat settings you made through the buttons are migrated for you.

For non-Claude engines, approval is enforced per-engine pre-run — Codex runs inside its sandbox (`/config` → Approval policy: **safe** = read-only), the deprecated Gemini CLI uses `--approval-mode` — rather than via mid-run buttons. Full guide: [Interactive approval](https://littlebearapps.com/help/untether/interactive-approval/).

## What happens if my agent crashes or my phone loses signal mid-run?

Untether is built around the assumption that your phone is unreliable but your computer isn't. Two things matter here:

1. **Your agent keeps running.** It's a subprocess on your machine. It doesn't care whether your phone is connected, whether Telegram is open, or whether you've gone to sleep. Progress messages buffer locally; reconnection rendering is automatic.
2. **Untether catches the common failure modes.** If a Claude Code session exits prematurely after a tool result without processing it (a known upstream bug), Untether auto-resumes it. If a resume comes back empty — 0 turns and no answer, another upstream turn-state bug — Untether quarantines that session and automatically retries your message on a fresh one, telling you it did so. When Claude hands work to a background task or subagent and ends its turn, Untether keeps the session open (in any chat with a permission mode set through `/planmode`, `/config` or `[engines.claude] permission_mode` — the same control channel the approval buttons use): the task's result comes back as a new `🔔 Background task finished` message, and anything you send meanwhile goes into that same session rather than a new one. While tasks run, a `⏳ background (N)` block and a status message show each one's elapsed time and progress. A session waits up to 30 minutes after the last sign of background activity (the 30 minutes restart whenever an agent makes progress), or longer when Claude gave a background command a longer timeout (up to 2 hours) or scheduled a wake-up (up to 1 hour), with a 4-hour cap per session. If you `/cancel` or Untether restarts while background tasks are running, it stops them cleanly and tells you which ones, and a closing line says whether your next message continues the same session. If the bot is restarted while a run is in progress, ephemeral approval messages are cleaned up and orphaned progress messages get a `⚠️ interrupted by restart` marker. Stalls that look "alive but silent" trigger progressive warnings, and the watchdog auto-cancels truly dead processes.

Everything important — Telegram update offsets, active progress message references, trigger fire history — is persisted to disk so a restart picks up where you left off without dropping or duplicating messages.

## Can I change Claude's instructions while it's still working?

Yes, with Claude Code. By default a message you send while a run is working is queued: it runs as the next turn once the current one finishes. Send `/steer <text>` instead and the message goes straight into the running turn. Claude reads it the next time a tool finishes and folds it into the answer it's already writing. You get a `↪️ Steered into the current run.` reply, and the progress message shows when Claude has picked it up. To make steer the default for a chat, send `/steer` on its own or use `/config` → Follow-up. `/queue` switches back, and `/queue <text>` queues a single message. Steering needs a permission mode (`/planmode`) so the session stays live. Files, forwards and other engines always queue, and Untether tells you when a steer couldn't be delivered. Full guide: [Steer follow-ups](https://littlebearapps.com/help/untether/steer-follow-ups/).

## Why does Claude say its safeguards stopped a response?

Anthropic runs real-time safeguards on Claude's responses. When one flags a request (most often in security-related work), the response is stopped and Claude Code reacts in one of three ways: it retries once on the same model, it switches to a fallback model, or it ends the turn without an answer. Untether shows which one happened. The progress message gets a `🛡️ <model> safeguards stopped a response` line with the outcome (`retried once`, `switched to <model>`, or `not retried`), and the final reply gets a matching `🛡️ safeguards stopped …` footer line. A safeguard stop is never reported as an error, and if the turn ended with no answer Untether says so rather than sending an empty message.

The first time it happens in a session, the footer also carries a pointer to Anthropic's guidance. For cyber-security work that's the [real-time cyber safeguards article](https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude), which covers the Cyber Verification Program; otherwise it's Claude Code's [automatic model fallback](https://code.claude.com/docs/en/model-config#automatic-model-fallback) docs, which explain which model it switches to and the `switchModelsOnFlag` setting. If a request keeps getting stopped, rephrase it or pick a different model with `/model`. Untether doesn't bypass or retry around safeguards itself; it only makes the stop visible.

## How can I tell when Claude's context is filling up?

Look at the end of the status line, the first line of every progress message and reply: `done · claude · 1m 36s · step 10 · 62% ctx` means the conversation fills 62% of Claude's context window. The value updates as Claude works and is shown on follow-up turns too. It appears once Untether has learned the model's window size, which after a restart can be the first reply's final message. It reads a little lower than `/context` in the terminal while Claude is in the middle of tool calls, because Untether only sees the usage of Claude's last response. When Claude compacts the conversation, automatically as the window fills or because you sent `/compact` as a follow-up while the session is still live, the progress message shows a `🗜️ Compacting context…` row that becomes `🗜️ Context compacted · 182k → 41k tokens (auto)`. The value then disappears until Claude's next response, and comes back lower. A long compaction isn't treated as a stall. Codex and the other engines don't show either yet. To hide it, set `show_context_usage = false` under `[progress]` in `untether.toml`; the change applies to the next message without a restart. See [Verbose progress](https://littlebearapps.com/help/untether/verbose-progress/#read-the-status-line).

## How do I keep agents from spending too much money?

Untether ships per-run and per-day cost budgets. In `untether.toml`:

```toml
[cost_budget]
enabled = true
max_cost_per_run = 2.00      # USD; alert when a single run reaches this
max_cost_per_day = 10.00     # USD; ditto across a calendar day
warn_at_pct = 80             # warn when this % of budget is consumed
warn_run_above_usd = 20.00   # USD; alert on any single expensive run — works even without a budget
auto_cancel = false          # true = "Stop at limit": refuse new runs once the daily budget is reached
```

Budgets alert by default. Turn on **Stop at limit** (`auto_cancel = true`, or `/config → 💰 Cost & usage`) and, once today's total reaches `max_cost_per_day`, new runs are refused until local midnight: chats get a `🛑 Daily budget reached` notice with a one-shot **Run anyway** button, and crons and webhooks are skipped with a notice. A live Claude session also ends after the reply that passes `max_cost_per_run`, so background wake-ups and queued follow-ups add no more spend. Today's total is saved to disk, so a restart doesn't reset it to $0.

If you set no budget at all, Untether still flags a single run that costs more than `warn_run_above_usd` (default US$20) with a chat line and a `cost.run_outlier` log entry, so a costly session can't pass silently. When background agents were working since the previous reply, the line says so (`— includes spend by N background agents since the previous reply`), because their spend lands on whichever reply comes next. Set `notify_run_outlier = false` to keep the log entry without the chat line.

`/usage` shows your Claude subscription quota; in Codex and OpenCode chats it shows the token totals of the chat's last session. `/usage debug` shows OAuth token expiry, schema-mismatch counters, and cache freshness — useful when the subscription footer goes silent. `/stats` reports per-engine totals across today, this week, and all time.

Cost tracking is most accurate for Claude (full USD reporting via API metadata) and OpenCode. For Claude, the figure on each reply is what the session spent since the previous reply — Claude reports a running total for the whole session, so Untether records the difference (resumed sessions are no longer counted twice). That includes any background agents' spend since the previous reply, and the cost line then says so (`· incl. N bg agents`). Codex reports tokens only — as a running total for the whole thread, so Untether records each run's difference (resumed runs aren't counted twice). Pi, Gemini, and Amp report tokens-only. Claude Pro/Max subscribers can also show their subscription quota in the footer (`⚡ 5h: 45% | 7d: 30%`, with reset times once a window passes 50%), toggled in `/config → 💰 Cost & usage`; even with it off, a warning line appears as the 5-hour window fills. The other engines have no subscription indicator. See the [cost-budgets guide](https://littlebearapps.com/help/untether/cost-budgets/) for tuning.

## Does /loop work via Untether?

Yes, with **Loop mode** on (`/config → 🔁 Loop mode`). Untether then runs Claude's `/loop` schedules itself: it declines Claude Code's own scheduled task and fires each iteration when due with `claude --resume`, capped by `[loop]` `max_iterations`, `max_total_duration_hours` and `expiry_days`. A loop never runs uncapped inside an open session, and never comes back when the session is resumed.

With Loop mode off (the default), Claude can't create recurring or timed tasks: it tells you scheduling is off and points you to Loop mode, or to `/at <delay> <prompt>` for a one-off. In v0.35.4 and earlier, such a task kept firing while the session was open and came back each time you resumed it. Self-paced waits (`ScheduleWakeup`, a dynamic `/loop`) still work in both modes: Untether keeps the session open after its reply while a wake-up is pending (ScheduleWakeup's own limit is one hour), and each one arrives as a `⏰ Scheduled wake-up` message. A chain of them stops after `max_iterations` wake-ups.

`/cancel` (or `/new`) stops a loop for good: the next message resumes the session without the cancelled loop coming back. With Loop mode off there is nothing to stop beyond a pending self-paced wait, which `/cancel` and `/new` also end.

Be aware: autonomous loops consume API credits or your subscription quota. Set a budget in `/config → 💰 Cost & usage` *before* turning Loop mode on: loop fires count toward the same per-run and daily budgets. Budgets alert by default; turn on **Stop at limit** (`auto_cancel = true`) and loop fires are refused once the daily budget is reached. Otherwise the runaway caps in `[loop]` are what actually bound a loop. See the [Schedule tasks how-to](https://littlebearapps.com/help/untether/schedule-tasks/#loop-mode) for details.

## Can I schedule runs or trigger them from a webhook?

Yes, in three ways, and none needs a restart:

- **`/at 30m <prompt>`** — a one-shot delay (60 seconds to 24 hours) set from the chat; `/cancel` drops it.
- **Cron triggers** — `[[triggers.crons]]` entries in `untether.toml` with a cron expression, an optional `timezone`, and `run_once = true` for a job that should fire only once.
- **Webhooks** — `[[triggers.webhooks]]` start a run from an authenticated HTTP POST (GitHub, CI or any other service), and can also write the payload to disk or forward it.

Trigger-started runs show where they came from in the footer (`⏰ cron:<id>`, `⚡ webhook:<id>`), `/ping` summarises each chat's triggers, and `/config → ⏰ Triggers` lists them with a master pause/resume toggle. A cron or webhook that names a `project` but no `engine` runs on that project's default engine.

Give every Claude cron that should act on its own an explicit `permission_mode`. Nobody is there to tap a button when a cron or webhook fires, so Untether denies anything the run would have asked about — a plan approval, a question, or a tool its mode asks about — and lists the denials in the run's final message. Without a `permission_mode` the cron takes the chat's `/planmode`, then the engine default (`plan` unless you changed it), so it ends with a plan instead of doing the work. For unattended crons set `permission_mode = "plan-auto"`, `"auto"` or `"bypassPermissions"` on the cron itself; webhooks follow the chat's mode. Untether logs `trigger.unattended_approval_risk` (and the startup message lists such crons), and every denial logs `permission.unattended_deny`. `/at` runs aren't affected: their buttons work as usual. If Telegram is briefly unreachable when a trigger fires, Untether retries the announcement before giving up instead of silently skipping the run. Full guides: [Schedule tasks](https://littlebearapps.com/help/untether/schedule-tasks/) and [Webhooks and cron](https://littlebearapps.com/help/untether/webhooks-and-cron/).

## Can a scheduled run use a cheaper model than the chat?

Yes. Add `model = "sonnet"` (and optionally `reasoning = "low"`) to the `[[triggers.crons]]` entry, with `engine` set so the name matches. The cron's run uses that model and effort while the chat keeps its own `/model` for everything you send; a reply to the cron's message goes back to the chat's model. Webhooks can't pick a model yet. Details: [Schedule tasks — Pick a model per cron](https://littlebearapps.com/help/untether/schedule-tasks/#pick-a-model-per-cron).

## Can I send voice notes instead of typing?

Yes — record a voice message in Telegram and Untether transcribes it via a Whisper-compatible endpoint, then runs the transcribed text as a normal prompt. Configure in `untether.toml`:

```toml
[transports.telegram]
voice_transcription = true
voice_transcription_model = "whisper-large-v3-turbo"
voice_transcription_base_url = "https://api.groq.com/openai/v1"
voice_transcription_api_key = "gsk_..."   # SecretStr — masked in logs
voice_transcription_language = "en"       # optional ISO-639-1 hint
voice_transcription_prompt = "Trello, Untether, Claude Code"  # optional vocabulary bias
```

Groq's Whisper Large v3 Turbo is fast and cheap; any OpenAI-compatible Whisper endpoint works (including a self-hosted one). If you only ever speak one language, set `voice_transcription_language` (e.g. `"en"`) — without the hint, Whisper-family models occasionally guess the wrong language on very short voice notes. Untether already biases the decoder toward the terms every user speaks — Claude (as a bare name and as Claude Code), Codex, OpenCode, the agent context files (`CLAUDE.md`, `AGENTS.md`), plus Untether's own vocabulary — and it rewrites the misspellings Whisper still produces for the name ("Clawde", "Clawed Code", "Claw.md") to `Claude`, `Claude Code` and `CLAUDE.md` before the run starts. If transcription keeps mangling *your* project or tool names ("trollo" instead of Trello), set `voice_transcription_prompt` to a short comma-separated list of those names; your value replaces the built-in list, so include the engine names you care about too. Keep your list under about 500 characters (Untether rejects anything over 1000). Whisper's prompt window is ~224 tokens, and with OpenAI Whisper anything before the last 224 tokens is dropped. Keep it to genuinely high-frequency nouns, as the effect varies by model and an overstuffed prompt can make the model hallucinate those terms on short or silent clips. Set it to `""` to switch the bias off entirely. The API key is `SecretStr`-masked in `repr()` / `str()` / structlog so it never lands in journal or crash output. For safety, `voice_transcription_base_url` is SSRF-checked — a URL that resolves to a private/reserved address (e.g. a self-hosted Whisper on `localhost`, `10.x` or `192.168.x`) is rejected unless you explicitly allow its range with `voice_transcription_url_allowlist = ["10.0.0.0/8"]`. If a voice note is refused, Untether's reply names the blocked host and the exact allowlist entry to add, and the same warning appears in the log at startup. Full setup: [Voice notes](https://littlebearapps.com/help/untether/voice-notes/).

## Can agents send files back to me automatically?

Yes — agents can write files to `.untether-outbox/` during a run, and Untether delivers them as Telegram documents when the run finishes. No special tool call needed; just write to the directory and Untether picks them up on completion. Each delivered file gets a `📎` caption, the outbox is cleaned up after delivery, and items that can't be sent (matching a deny-glob, oversized, or unsupported entry types like sub-directories) are surfaced in the final message with a `📎 Outbox skipped:` block so nothing is silently dropped. Only files written or copied into the outbox during that run are sent: anything left over from an earlier session is moved once to `.untether-outbox/.skipped/` with a single notice instead of being attached to an unrelated answer, and `/file get .untether-outbox/.skipped` fetches those as a zip (set `outbox_stale_policy = "send"` for the old send-everything behaviour).

Configure in `untether.toml`:

```toml
[transports.telegram.files]
outbox_enabled = true
outbox_dir = ".untether-outbox"
outbox_max_files = 20   # the per-file cap is fixed at 50 MB
outbox_cleanup = true
outbox_notify_skipped = true
```

The deny-globs and per-file size cap are enforced before any send, so a misbehaving agent can't exfiltrate arbitrary paths or DOS your Telegram chat with huge attachments. Sub-directories are handled two ways: by default they're archived to `.untether-outbox/.skipped/` and listed as skipped, or set `outbox_deliver_directories = "zip"` (v0.35.4) to have each one bundled into a single `<name>.zip` document and delivered — recursive deny-globs, symlink pruning, and size caps still apply. All engines support it. Full setup: [File transfer](https://littlebearapps.com/help/untether/file-transfer/).

## Do I need to restart Untether after editing `untether.toml`?

Usually not. With `watch_config = true` at the top of `untether.toml` (it's off unless you set it), almost everything hot-reloads within a couple of seconds of saving the file. Untether watches the config file and re-applies changes in-place: cron and webhook triggers, voice-transcription settings, the allowed-user list, message timing, the file-transfer + outbox config (every `outbox_*` key is read again for each delivery; only turning `files.enabled` on for the first time needs a restart), the `show_resume_line` toggle, and every per-engine override. Per-run settings such as `[progress]`, `[watchdog]`, `[footer]` and `[cost_budget]` are re-read on every run anyway, so edits to those apply to the next run even without `watch_config`.

The exceptions are a handful of restart-only keys that affect process bring-up: `bot_token`, `chat_id`, `session_mode`, `topics`, and `message_overflow` (plus turning on `[triggers] enabled`). If you edit one of those, Untether logs a `restart_required=true` warning, broadcasts a message to the active project chats, and you'll need to restart Untether to apply the change. The notice names the exact command for your instance — for example `systemctl --user restart untether-dev` for a second instance, or `launchctl kickstart -k gui/<uid>/<label>` on macOS. When Untether can't tell which service runs it, the notice says so in general terms; sending `/restart` from Telegram also works whenever a service manager (systemd, launchd) keeps Untether running.

**For agents:** after editing `untether.toml`, **do NOT run `systemctl restart untether` from inside an active agent session**. With `watch_config = true`, Untether has already hot-reloaded the change; the restart is unnecessary and the graceful drain will time out (120s) trying to wait for your own session to finish, which silently drops your final answer message to the user. The reload-applied notification that arrives in the chat after your edit is your confirmation it took effect.

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

Untether follows semver: patch versions (e.g. `0.35.3 → 0.35.4`) are mostly bug fixes, and minor versions (`0.35.x → 0.36.0`) add features. While Untether is pre-1.0, a minor release can carry breaking changes, and occasionally a patch release does too when a fix needs one; these are listed under **breaking** in the changelog. Pre-release `rcN` wheels publish to TestPyPI for staging dogfooding. The [CHANGELOG](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md) lists every change with linked GitHub issues.

Upgrading to **v0.36.0** (published to TestPyPI as release candidates 0.35.5rc1–rc20)? Read [Upgrading to v0.36.0](https://littlebearapps.com/help/untether/update/#upgrading-to-v0360) first. Untether's old `auto` permission mode is now called `plan-auto` (`auto` now means Claude Code's own auto mode). `/planmode off` now asks before shell commands, web fetches and MCP tools. An `extra_args` that carries an approval- or sandbox-bypass flag now stops the engine loading. Codex **safe** mode is now a real read-only sandbox, so edits, tests and builds fail there. Unattended cron and webhook runs deny approvals instead of waiting, so give every Claude cron that should act on its own an explicit `permission_mode`. With Loop mode off, Claude can no longer schedule recurring tasks itself.

## How do I uninstall Untether?

```sh
uv tool uninstall untether
# or
pipx uninstall untether

rm -rf ~/.untether/
```

That removes the CLI, all state files (chat preferences, session resumes, trigger history), and your `untether.toml`. If you set up a systemd user unit, also `systemctl --user disable --now untether` and remove the unit file.

The Telegram bot itself lives on Telegram's side — to delete it entirely, talk to [@BotFather](https://t.me/BotFather), pick `/deletebot`, and select your bot. That step is optional; an inactive bot causes no harm beyond squatting the username. Full uninstall walkthrough: [Uninstall Untether](https://littlebearapps.com/help/untether/uninstall/).

## Where can I get help or report a bug?

- **Documentation** — [`docs/`](https://github.com/littlebearapps/untether/tree/master/docs) covers tutorials, how-to guides, engine references, and architecture.
- **Help centre** — <https://littlebearapps.com/help/untether/>
- **Bug reports and feature requests** — [GitHub Issues](https://github.com/littlebearapps/untether/issues) with the `bug` or `enhancement` label.
- **Security issues** — see [SECURITY.md](https://github.com/littlebearapps/untether/blob/master/SECURITY.md) for the responsible-disclosure path.

When filing an issue, include your Untether version (`untether --version`), the engine + version that reproduced the bug, and a relevant excerpt from `journalctl --user -u untether` (or the equivalent log path for your runtime). Sensitive paths and secrets are scrubbed from logs by default but spot-check before pasting.
