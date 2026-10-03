# Roadmap

This roadmap reflects the project's direction based on recent development and community feedback. It is not a commitment — priorities may shift as the ecosystem evolves.

## Near-term

- **Antigravity CLI engine** — new engine backend for Google's [Antigravity CLI](https://antigravity.google) (`agy`), the successor to Gemini CLI. A **distinct engine**, not a Gemini rename: authentication, CLI flags, and session semantics all differ, so it will register under its own `antigravity` engine id. Targeted for v0.35.6, ahead of the v0.36.0 Gemini/Amp removal ([#558](https://github.com/littlebearapps/untether/issues/558))
- **Retire the Gemini CLI and Amp engines** — both deprecated in v0.35.5 and scheduled for removal in v0.36.0, alongside the Amp-only `/threads` command. Gemini CLI reached end-of-life for individual and free Google accounts on 18 June 2026; the Amp integration is unmaintained. See [deprecated engines](README.md#deprecated-engines) ([#720](https://github.com/littlebearapps/untether/issues/720), [#458](https://github.com/littlebearapps/untether/issues/458), [#722](https://github.com/littlebearapps/untether/issues/722))
- **Additional transport backends** — Discord and Slack transports via the plugin system
- **Improved onboarding diagnostics** — expand `untether doctor` with network, permission, and engine health checks

## Mid-term

- **Web dashboard** — browser-based UI for monitoring active runs and session history
- **Multi-user support** — per-user permissions and session isolation in group chats
- **Agent orchestration** — chain multiple engines in a single workflow (e.g., Claude for planning, Codex for execution)
- **Cost tracking enhancements** — per-project budgets, weekly summaries (historical reporting partially shipped via `/stats` in v0.30.0)

## Shipped

- **Live Claude sessions** — a Claude session stays open after its reply while background tasks, subagents, `Monitor` or `ScheduleWakeup` keep working; their results arrive as their own messages with a live background-task status, and follow-ups can be queued or steered into the running session (shipped in v0.35.5; [#776](https://github.com/littlebearapps/untether/issues/776), [#777](https://github.com/littlebearapps/untether/issues/777), [#775](https://github.com/littlebearapps/untether/issues/775))
- **Context-window visibility** — Claude's context use in the status line (`62% ctx`) and 🗜️ compaction rows (shipped in v0.35.5; [#819](https://github.com/littlebearapps/untether/issues/819))
- **Claude Code's own `auto` mode** — Untether's old plan-auto-approve mode renamed `plan-auto` so Claude Code's classifier-gated `auto` mode is reachable (shipped in v0.35.5; [#741](https://github.com/littlebearapps/untether/issues/741))
- **Gemini CLI engine** — full integration with Google's Gemini CLI via stream-json (added in v0.32.0; ⚠️ **deprecated in v0.35.5**, removal in v0.36.0 — upstream end-of-life)
- **Amp engine** — full integration with Sourcegraph's Amp coding agent via stream-json (added in v0.32.0; ⚠️ **deprecated in v0.35.5**, removal in v0.36.0 — integration unmaintained)
- **Webhook-driven workflows** — trigger agent runs from CI/CD events, GitHub webhooks, or external services (shipped as the triggers system with cron and webhook support)
- **Session statistics** — `/stats` command for per-engine run counts, actions, and duration across today/week/all-time (shipped in v0.30.0)
- **Device re-authentication** — `/auth` command for headless Codex re-auth via Telegram (shipped in v0.30.0)

## Future

- **Self-hosted relay mode** — run the Telegram bridge on a remote server with secure tunnelling to local agents
- **Additional transports** — Matrix, WhatsApp, or other messaging platforms via the plugin system

## Contributing

Have a feature idea? [Open an issue](https://github.com/littlebearapps/untether/issues) — we'd love to hear what you'd find useful.
