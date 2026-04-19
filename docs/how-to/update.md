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
