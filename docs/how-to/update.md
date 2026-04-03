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

## Checking for updates

Visit the [PyPI page](https://pypi.org/project/untether/) or the [changelog](https://github.com/littlebearapps/untether/blob/master/CHANGELOG.md) to see what's new.
