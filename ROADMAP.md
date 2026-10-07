# Roadmap

This roadmap reflects the project's direction based on recent development and community feedback. It is not a commitment — priorities may shift as the ecosystem evolves. Website: [untether.cc](https://untether.cc).

## Latest release — v0.36.0

The stable release after 0.35.4 (its release candidates were numbered 0.35.5rc1–rc20, then 0.36.0rc1–rc3). It carries breaking changes, so read [Upgrading to v0.36.0](docs/how-to/update.md#upgrading-to-v0360) before upgrading; the [CHANGELOG](CHANGELOG.md) has the full list.

- **Live Claude sessions** — a Claude session stays open after its reply while background tasks, subagents, `Monitor` or `ScheduleWakeup` keep working; their results arrive as their own messages with a live background-task status, and follow-ups can be queued or steered into the running session ([#776](https://github.com/littlebearapps/untether/issues/776), [#777](https://github.com/littlebearapps/untether/issues/777), [#775](https://github.com/littlebearapps/untether/issues/775))
- **Context-window visibility** — Claude's context use in the status line (`62% ctx`) and 🗜️ compaction rows ([#819](https://github.com/littlebearapps/untether/issues/819))
- **Claude Code's own `auto` mode** — Untether's old plan-auto-approve mode renamed `plan-auto` so Claude Code's classifier-gated `auto` mode is reachable, and `/planmode off` now asks before shell commands, web fetches and MCP tools ([#741](https://github.com/littlebearapps/untether/issues/741), [#749](https://github.com/littlebearapps/untether/issues/749))
- **Loop mode owns Claude's schedules** — with Loop mode on, Untether fires `/loop` iterations itself within the `[loop]` caps; with it off, Claude can no longer schedule recurring tasks on its own ([#925](https://github.com/littlebearapps/untether/issues/925))
- **Safer unattended, shared and sandboxed runs** — cron and webhook runs deny approvals instead of waiting, approval buttons only work in their own chat (optionally only for the person who started the run), `extra_args` refuses approval- and sandbox-bypass flags, and Codex safe mode is a real read-only sandbox ([#835](https://github.com/littlebearapps/untether/issues/835), [#388](https://github.com/littlebearapps/untether/issues/388), [#209](https://github.com/littlebearapps/untether/issues/209), [#830](https://github.com/littlebearapps/untether/issues/830))
- **Stop at limit** — `[cost_budget] auto_cancel` pauses new runs once the daily budget is reached, and today's total survives restarts ([#896](https://github.com/littlebearapps/untether/issues/896), [#898](https://github.com/littlebearapps/untether/issues/898))
- **Everyday polish** — reply and quote context passed to the agent ([#736](https://github.com/littlebearapps/untether/issues/736)), `/export` as a file ([#418](https://github.com/littlebearapps/untether/issues/418)), per-cron model and effort, a better voice vocabulary, and an outbox that only sends the current run's files
- **OpenCode 2.x guard** — Untether supports the OpenCode 1.x CLI and refuses 2.x with a clear message instead of running it outside its process control ([#970](https://github.com/littlebearapps/untether/issues/970))
- **Gemini CLI and Amp deprecated** — both stay included and still load, but are no longer supported: no fixes, excluded from testing, and they may be removed in a future release. See [deprecated engines](README.md#deprecated-engines) ([#720](https://github.com/littlebearapps/untether/issues/720), [#458](https://github.com/littlebearapps/untether/issues/458))

## Near-term

- **Antigravity CLI engine (v0.36.1)** — new engine backend for Google's [Antigravity CLI](https://antigravity.google) (`agy`), the successor to Gemini CLI. A **distinct engine**, not a Gemini rename: authentication, CLI flags, and session semantics all differ, so it registers under its own `antigravity` engine id ([#558](https://github.com/littlebearapps/untether/issues/558))
- **Codex app-server transport and parity (v0.36.2)** — an opt-in `[codex] transport = "app-server"` that brings Telegram approvals with diff previews, live sessions with steer, plan mode and option buttons, subscription usage and background-terminal rows to Codex, built on an engine-neutral approval/question registry extracted from the Claude runner ([#960](https://github.com/littlebearapps/untether/issues/960)–[#968](https://github.com/littlebearapps/untether/issues/968)). The same release carries security-hardening follow-ups from the April 2026 agent-orchestration audit and per-agent cost attribution for background agents ([#877](https://github.com/littlebearapps/untether/issues/877))
- **OpenCode ACP transport and parity (v0.36.3)** — cheap `opencode run` parity first (`/reasoning`, plan agent, opt-in bypass), then an `[opencode] mode = "acp"` transport with Telegram approvals, diffs, cancel, context %, compaction and subagent rows, AskUserQuestion via ACP forms, and live sessions with steer ([#971](https://github.com/littlebearapps/untether/issues/971)–[#974](https://github.com/littlebearapps/untether/issues/974); the groundwork, [#969](https://github.com/littlebearapps/untether/issues/969) and [#970](https://github.com/littlebearapps/untether/issues/970), shipped in v0.36.0). Also long-tail hardening: dependency hygiene, observability, pre-flight cost gating and bot-token rotation
- **Additional transport backends** — Discord and Slack transports via the plugin system
- **Improved onboarding diagnostics** — expand `untether doctor` with network, permission, and engine health checks

## Mid-term

- **Web dashboard** — browser-based UI for monitoring active runs and session history
- **Multi-user support** — per-user permissions and session isolation in group chats (v0.36.0 added the first piece: `approval_originator_only`, so only the person who started a run can answer its approvals)
- **Agent orchestration** — chain multiple engines in a single workflow (e.g., Claude for planning, Codex for execution)
- **Cost tracking enhancements** — per-project budgets, weekly summaries (historical reporting shipped via `/stats` in v0.30.0; Stop at limit in v0.36.0)

## Shipped

- **Live sessions, steer and Loop mode** — Claude sessions that stay open for background work, steered follow-ups, and Untether-run `/loop` schedules (shipped in v0.36.0)
- **Gemini CLI engine** — full integration with Google's Gemini CLI via stream-json (added in v0.32.0; ⚠️ **deprecated in v0.36.0** and no longer supported — upstream end-of-life for individual accounts)
- **Amp engine** — full integration with Sourcegraph's Amp coding agent via stream-json (added in v0.32.0; ⚠️ **deprecated in v0.36.0** and no longer supported — integration unmaintained)
- **Webhook-driven workflows** — trigger agent runs from CI/CD events, GitHub webhooks, or external services (shipped as the triggers system with cron and webhook support)
- **Session statistics** — `/stats` command for per-engine run counts, actions, and duration across today/week/all-time (shipped in v0.30.0)
- **Device re-authentication** — `/auth` command for headless Codex re-auth via Telegram (shipped in v0.30.0)

## Future

- **Retire the Gemini CLI and Amp engines** — no removal is scheduled; both stay included (deprecated) until a future release drops them, along with the Amp-only `/threads` command ([#722](https://github.com/littlebearapps/untether/issues/722))
- **Self-hosted relay mode** — run the Telegram bridge on a remote server with secure tunnelling to local agents
- **Additional transports** — Matrix, WhatsApp, or other messaging platforms via the plugin system

Pi RPC mode and the Pi extension bridge ([#170](https://github.com/littlebearapps/untether/issues/170), [#180](https://github.com/littlebearapps/untether/issues/180)) are no longer planned; Pi keeps its current `--mode json` runner.

## Contributing

Have a feature idea? Share it in [Discussions → Ideas](https://github.com/littlebearapps/untether/discussions/categories/ideas) — ideas that gather support become tracked issues. Found a bug? [Open an issue](https://github.com/littlebearapps/untether/issues/new/choose).
