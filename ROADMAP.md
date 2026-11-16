# Roadmap

This roadmap reflects the project's direction based on recent development and community feedback. It is not a commitment — priorities may shift as the ecosystem evolves. Website: [untether.cc](https://untether.cc).

## Latest release — v0.36.1

Most installs need no config change, but read [Upgrading to v0.36.1](docs/how-to/update.md#upgrading-to-v0361) first: in a group chat, changing settings now needs a group admin, and scheduled Antigravity runs can be held once after the upgrade.

- **Antigravity CLI engine** — Google's [Antigravity CLI](https://antigravity.google) (`agy` 1.3.1 or newer), the successor to Gemini CLI, as its own `antigravity` engine, not a Gemini rename: progress, resume and `/continue`, model and effort, quota groups and AI credits in `/usage`, and background commands and subagents in the progress message ([#558](https://github.com/littlebearapps/untether/issues/558)). Read the [Google terms note](docs/how-to/switch-engines.md#antigravity-cli) before using a Google account sign-in
- **Antigravity permission modes and Telegram buttons** — Workspace by default, Full access only when you pick it, **Plan first** with Approve Plan / Deny / Let's discuss, and, with Untether's agy plugin (`untether agy-gate install`), **Ask me** approval cards, option buttons for its questions and `/steer` into a running turn ([#980](https://github.com/littlebearapps/untether/issues/980), [#981](https://github.com/littlebearapps/untether/issues/981))
- **Safer group chats and triggers** — only group admins can change settings, webhook runs never inherit Claude's `bypassPermissions`, and webhook servers behind a tunnel or proxy can be marked `exposed` ([#1042](https://github.com/littlebearapps/untether/issues/1042), [#1142](https://github.com/littlebearapps/untether/issues/1142), [#1119](https://github.com/littlebearapps/untether/issues/1119))
- **Engine-neutral approvals** — the store of pending approvals and questions moved out of the Claude runner so other engines can use it, starting with Antigravity

## Near-term

- **Codex app-server transport and parity (v0.36.2)** — an opt-in `[codex] transport = "app-server"` that brings Telegram approvals with diff previews, live sessions with steer, plan mode and option buttons, subscription usage and background-terminal rows to Codex, built on the engine-neutral approval/question registry that v0.36.1 extracted from the Claude runner ([#960](https://github.com/littlebearapps/untether/issues/960)–[#968](https://github.com/littlebearapps/untether/issues/968)). The same release carries security-hardening follow-ups from the April 2026 agent-orchestration audit and per-agent cost attribution for background agents ([#877](https://github.com/littlebearapps/untether/issues/877))
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
