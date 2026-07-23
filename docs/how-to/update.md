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
    Untether wraps agent CLIs (Claude Code, Codex, OpenCode, Pi, Gemini CLI, Amp) as subprocesses. Updating Untether does not update the agent CLIs. Update them separately:

    ```sh
    npm update -g @anthropic-ai/claude-code
    npm update -g @openai/codex
    npm update -g opencode-ai
    npm update -g @mariozechner/pi-coding-agent
    npm update -g @google/gemini-cli
    npm update -g @sourcegraph/amp
    ```

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
