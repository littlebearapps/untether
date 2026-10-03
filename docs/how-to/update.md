# Update Untether

Untether publishes releases to [PyPI](https://pypi.org/project/untether/). To upgrade to the latest version:

=== "uv (recommended)"

    ```sh
    uv tool upgrade untether
    ```

=== "pipx"

    ```sh
    pipx upgrade untether
    ```

Check your current version:

```sh
untether --version
```

After upgrading, restart the service if running as a systemd unit:

```sh
systemctl --user restart untether
```

!!! note "Agent CLIs are separate"
    Untether wraps agent CLIs (Claude Code, Codex, OpenCode, Pi, and the deprecated Gemini CLI and Amp) as subprocesses. Updating Untether does not update the agent CLIs. Update them separately:

    ```sh
    npm update -g @anthropic-ai/claude-code
    npm update -g @openai/codex
    npm update -g opencode-ai
    npm update -g @mariozechner/pi-coding-agent
    npm update -g @google/gemini-cli
    npm update -g @sourcegraph/amp
    ```

## Upgrading to v0.35.5

See the [v0.35.5 changelog entry](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0355) for the full list. Behaviour changes that may affect operators:

- **`auto` permission mode renamed `plan-auto` (breaking).** `/planmode auto` now selects Claude Code's own classifier-gated auto mode. Per-chat settings migrate automatically; if `untether.toml` sets `permission_mode = "auto"` and you want the old behaviour, change it to `"plan-auto"`. See [Plan mode](plan-mode.md). ([#741](https://github.com/littlebearapps/untether/issues/741))
- **`/planmode off` (Accept edits) now asks before shell commands.** Prompting modes (`acceptEdits`, `default`, `manual`) previously approved every tool silently. Anything the mode doesn't cover now shows Approve / Deny buttons unless your Claude Code settings allow it. Crons and webhooks can't wait for a tap — see the next point. ([#749](https://github.com/littlebearapps/untether/issues/749))
- **Unattended cron and webhook runs deny instead of waiting (breaking, Claude).** A tool approval, plan approval or question in a cron or webhook run is denied at once and listed in the run's final (`🔒 unattended (cron:<id>) · denied Write ×2 — nobody to approve`, logged as `permission.unattended_deny`). Crons in `plan` or a prompting mode, **including crons with no `permission_mode`** (they inherit the chat's `/planmode`, then the engine default `plan`), now end with a plan or a report instead of doing the work. Give every Claude cron that should act on its own an explicit `permission_mode` (`plan-auto`, `auto` or `bypassPermissions`); webhooks follow the chat's `/planmode`. The startup message lists the crons this affects. A reply to an unattended run continues with normal buttons, and `/at` runs still ask. See [Webhooks and cron](webhooks-and-cron.md). ([#835](https://github.com/littlebearapps/untether/issues/835), [#836](https://github.com/littlebearapps/untether/issues/836))
- **`/new` and `/cancel` in a forum topic only touch that topic.** `/new` in General no longer cancels every topic's runs, and a no-reply `/cancel` in one topic can't stop another topic's run. Cron and webhook runs belong to General. See [Topics](topics.md#reset-a-topic-session). ([#826](https://github.com/littlebearapps/untether/issues/826))
- **`/export` sends a file.** The transcript arrives as a `.md` or `.json` document instead of an inline message cut at 3,000 characters. See [Export sessions](export-sessions.md). ([#418](https://github.com/littlebearapps/untether/issues/418))
- **Claude sessions stay open after the answer while background work runs.** Background-task, Monitor and scheduled-wake-up turns arrive as their own messages, and follow-ups go into the same session instead of a fresh one. Set `[watchdog] live_sessions = false` to restore the old behaviour. See [Troubleshooting](troubleshooting.md#messages-arrive-after-the-run-finished). ([#776](https://github.com/littlebearapps/untether/issues/776))
- **Claude per-run costs are now per run.** A resumed session's earlier spend no longer counts against each run's budget or `/stats`, so per-run and daily figures may drop. ([#778](https://github.com/littlebearapps/untether/issues/778))
- **Claude rate-limit and stall signals are quieter and more accurate.** Routine `rate_limit_event` usage snapshots no longer show a fake `⏳ Rate limited` wait; only a real rejection latches until its reset time, and a `⚠️ 5h limit N% used` heads-up appears at most once per window. API retry back-offs show as `🔁 API error … retrying in Ns`. Live-session idle holds no longer raise stall warnings, and `session.summary` reports them as `peak_live_idle_seconds`, so `peak_idle_seconds` drops on live sessions — adjust any log alerts that key on it. ([#790](https://github.com/littlebearapps/untether/issues/790), [#792](https://github.com/littlebearapps/untether/issues/792), [#787](https://github.com/littlebearapps/untether/issues/787))
- **`extra_args` refuses approval and sandbox bypass flags (breaking, security).** A `[engines.claude]` or `[engines.codex]` `extra_args` that carries `--dangerously-skip-permissions`, `--allowedTools`, `--yolo`, `--sandbox danger-full-access`, `-C`/`--cd` and similar now fails to load: the default engine won't start, and any other engine is disabled until you remove the flag (the error names it). Use the named keys instead (`allowed_tools`, `permission_mode`). See [Security → Engine CLI flags](security.md#engine-cli-flags-extra_args). ([#209](https://github.com/littlebearapps/untether/issues/209))
- **Codex safe mode now actually restricts Codex (behaviour change, security).** Before v0.35.5 the Codex **safe** approval policy did not restrict anything — `codex exec` ignored the flag — and on codex-cli 0.149.0+ every safe-mode run failed at startup. Safe now uses Codex's read-only sandbox: file edits, writes, tests, builds, package installs and shell network access are blocked, including cache and `/tmp` writes. If you want Codex to edit files or run tests, switch the chat to **Full auto** in `/config` → Approval policy. ([#830](https://github.com/littlebearapps/untether/issues/830))
- **A plan approval now covers one reply (Claude).** Approving a plan used to skip per-tool approvals for every later message and background wake-up in the same open session, for up to four hours. It now ends with the reply it was given in, and plan mode switches back on when that reply ends. Set `[watchdog] rearm_plan_mode = false` to go back to an approval lasting until the session closes. See [Plan mode](plan-mode.md#auto-approval-after-plan-approval). ([#383](https://github.com/littlebearapps/untether/issues/383))
- **File deny globs match at the project root (security).** The default `deny_globs` now also cover a root-level `key.pem`, `id_rsa`, `.npmrc`, `.netrc` or `.ssh/config`, and a project-root `.env.example` now matches `**/.env.*`, so `/file get` and outbox delivery skip it. Deny globs are also checked after following symlinks. See [Security → File transfer deny globs](security.md#file-transfer-deny-globs). ([#831](https://github.com/littlebearapps/untether/issues/831), [#390](https://github.com/littlebearapps/untether/issues/390))
- **`/browse` needs a project.** It no longer falls back to the process working directory (your home directory under systemd). Bind the chat to a project or set `default_project`. Hidden paths and deny globs are now refused there too. See [Browse files](browse-files.md). ([#389](https://github.com/littlebearapps/untether/issues/389))
- **Crons and webhooks with a `project` but no `engine` use that project's engine.** They used to run on the global default engine. Set `engine` on the trigger if you relied on the old behaviour. ([#862](https://github.com/littlebearapps/untether/issues/862))
- **Codex no longer offers the `minimal` reasoning level.** A saved `minimal` is ignored with a one-line note and the run uses Codex's default. ([#416](https://github.com/littlebearapps/untether/issues/416))
- **Voice transcription has a default vocabulary hint.** Unless you set `voice_transcription_prompt`, Untether now biases transcription towards engine and project names such as Claude, `CLAUDE.md` and Codex. Set it to `""` to send no hint. See [Voice notes](voice-notes.md#improve-recognition-of-names). ([#703](https://github.com/littlebearapps/untether/issues/703), [#789](https://github.com/littlebearapps/untether/issues/789))
- **Gemini CLI and Amp are deprecated**, targeted for removal in 0.36.0.

New in v0.35.5 and worth a look after upgrading: [steering a running Claude run](steer-follow-ups.md) with `/steer`, the [background-task status message and context-window percentage](verbose-progress.md) in Claude runs, approval [diff previews](interactive-approval.md#diff-previews) as a proper diff block, and a [per-cron model and effort](schedule-tasks.md#pick-a-model-per-cron) (`model` / `reasoning` on `[[triggers.crons]]`, [#743](https://github.com/littlebearapps/untether/issues/743)).

## Upgrading to v0.35.4

See the [v0.35.4 changelog entry](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0354) for the full list. Behaviour changes that may affect operators:

- **Voice transcription is now SSRF-validated.** If `voice_transcription_base_url` points at a loopback or private-network endpoint (e.g. a local Whisper server at `http://localhost:8000/v1`), transcription is now **refused** unless you allowlist it — add `voice_transcription_url_allowlist = ["127.0.0.0/8"]` to `[transports.telegram]`. The default public path (`api.openai.com`) is unaffected. ([#381](https://github.com/littlebearapps/untether/issues/381))
- **Webhooks with `auth = "none"` are refused on non-loopback hosts.** An unauthenticated webhook bound to a public interface is now dropped at startup and on hot-reload (polling, commands, and crons keep running); loopback binds are still allowed. To keep an unauthenticated webhook on a public host, set `[triggers] allow_unauthenticated_webhooks = true`. ([#382](https://github.com/littlebearapps/untether/issues/382))
- **The pre-spawn RAM guard is now concurrency-aware.** The block threshold rises with the number of runs already in flight (`prespawn_ram_per_run_reserve_mb`, default 750), and an optional hard ceiling (`max_concurrent_engine_runs`, default `0` = unlimited) caps concurrent engine subprocesses. On small VPS hosts this stops the OOM killer SIGKILLing a live session — see the sizing note under [config → watchdog](../reference/config.md#watchdog). ([#589](https://github.com/littlebearapps/untether/issues/589))
- **Empty-resume recovery (Claude).** A resume that returns an empty 0-turn result now auto-recovers on a fresh session instead of silently doing nothing, and post-result force-killed sessions are quarantined proactively. No config needed; opt out via `[auto_continue] empty_resume_fresh = false`. ([#631](https://github.com/littlebearapps/untether/issues/631), [#632](https://github.com/littlebearapps/untether/issues/632))
- **The Claude plan-mode progressive cooldown was retired.** The upstream `ExitPlanMode` re-issue loop it worked around is fixed (CLI 2.1.215); "Pause & Outline Plan" now holds the session open on a text-based outline gate. No action needed. ([#570](https://github.com/littlebearapps/untether/issues/570))

## Upgrading to v0.35.2

See the [v0.35.2 changelog entry](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md#v0352) for the full change list. Behaviour changes that may affect operators upgrading from v0.35.1 or earlier:

- **Claude/Pi subprocess env is now allowlisted.** Arbitrary process env no longer leaks to agent CLIs. If a plugin or MCP server depends on a specific variable, confirm it's on the allowlist — see [Env allowlist (Claude/Pi)](../reference/env-vars.md#env-allowlist-claudepi). ([#198](https://github.com/littlebearapps/untether/issues/198), [#361](https://github.com/littlebearapps/untether/issues/361))
- **`CLAUDE_STREAM_IDLE_TIMEOUT_MS` default raised to `300000` (5 min).** The old 60 s default killed long-thinking runs. Set the var explicitly to restore the old value. ([#342](https://github.com/littlebearapps/untether/issues/342))
- **`[security] env_audit = true` by default.** Any leaked env var logs `claude.env_audit.leaked_var` WARNING and subprocesses spawn under `env -i`. Set to `false` in `untether.toml` to restore legacy behaviour. ([#361](https://github.com/littlebearapps/untether/issues/361))
- **`run_once` crons persist fired state** to `run_once_fired.json` (sibling to `untether.toml`). They no longer re-fire on reload or restart. Delete the file to re-arm. ([#317](https://github.com/littlebearapps/untether/issues/317))
- **Webhook port bind failure no longer crashes the bot.** Check logs for `triggers.server.bind_failed`. Remediation: `ss -tlnp | grep <port>` to find the conflicting process, then set `port = <N>` in `[triggers]`. ([#320](https://github.com/littlebearapps/untether/issues/320))
- **Engine subprocess cleanup walks the process tree.** Orphaned `workerd` processes (seen at 37 GB RSS in pre-0.35.2 incidents) are now signalled alongside the parent. ([#275](https://github.com/littlebearapps/untether/issues/275))

## Checking for updates

Visit the [PyPI page](https://pypi.org/project/untether/) or the [changelog](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md) to see what's new.
