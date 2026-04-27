# Environment variables

Untether supports a small set of environment variables for logging and runtime behavior.

## Logging

| Variable | Description |
|----------|-------------|
| `TAKOPI_LOG_LEVEL` | Minimum log level (default `info`; `--debug` forces `debug`). |
| `TAKOPI_LOG_FORMAT` | `console` (default) or `json`. |
| `TAKOPI_LOG_COLOR` | Force color on/off (`1/true/yes/on` or `0/false/no/off`). |
| `TAKOPI_LOG_FILE` | Append JSON lines to a file. `--debug` defaults this to `debug.log`. |
| `TAKOPI_TRACE_PIPELINE` | Log pipeline events at `info` instead of `debug`. |

## CLI behavior

| Variable | Description |
|----------|-------------|
| `TAKOPI_NO_INTERACTIVE` | Disable interactive prompts (useful for CI / non-TTY). |
| `UNTETHER_CONFIG_PATH` | Override config file location (default `~/.untether/untether.toml`). Useful for running multiple instances or testing with alternate configs. |

## Engine-specific

| Variable | Description |
|----------|-------------|
| `PI_CODING_AGENT_DIR` | Override Pi agent session directory base path. |

## Runner environment

These variables are set automatically by Untether in the engine subprocess environment. They are not user-configurable.

| Variable | Set by | Description |
|----------|--------|-------------|
| `UNTETHER_SESSION` | Claude runner | Set to `1` for all Claude Code subprocess invocations. Enables Claude Code plugins to detect Untether sessions and adjust behaviour — for example, skipping blocking Stop hooks that would displace user-requested content in Telegram. |
| `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | Claude runner | Claude Code's stdout idle timeout. Default raised to `300000` (5 min) in v0.35.2 ([#342](https://github.com/littlebearapps/untether/issues/342)) — matches undici's idle-body timeout. The old 60 s default killed long-thinking runs. **As of v0.35.3 ([#438](https://github.com/littlebearapps/untether/issues/438))**, this is preferably set via `[watchdog] claude_stream_idle_timeout_ms` in `untether.toml` (range 30 s – 30 min). Shell-set `CLAUDE_STREAM_IDLE_TIMEOUT_MS` still wins via `setdefault`. Failures with `API Error: Stream idle timeout - partial response received` now classify as Type A (mid-generation — raising helps) or Type B (cold-start zero-byte — raising does NOT help; upstream API outage). |

!!! note "Not a security concern"
    `UNTETHER_SESSION` is a simple signal variable, not a credential or secret. It tells Claude Code plugins that the session is running via Telegram so they can avoid interfering with Untether's single-message output model. Plugins like [PitchDocs](https://github.com/littlebearapps/lba-plugins) check for this variable and skip blocking hooks that would otherwise consume the final response with meta-commentary instead of the user's requested content. See the [PitchDocs interference audit](../audits/pitchdocs-context-guard-interference.md) for the full analysis.

## Env allowlist (Claude/Pi)

As of v0.35.2, arbitrary process env vars are **not** forwarded to Claude/Pi subprocesses. Only an internal allowlist (things like `PATH`, `HOME`, `LANG`, Anthropic/OpenAI/Pi credentials, `BWS_ACCESS_TOKEN` (added as a default in v0.35.3), and a small set of CLI-specific knobs including `CLAUDE_STREAM_IDLE_TIMEOUT_MS`, `MCP_TOOL_TIMEOUT`, `MAX_MCP_OUTPUT_TOKENS`) is passed through. ([#198](https://github.com/littlebearapps/untether/issues/198))

When `[security] env_audit = true` (default — see [config reference](config.md#security)), any non-allowlisted var observed in the parent process logs a `claude.env_audit.leaked_var` WARNING and the subprocess spawns under `env -i KEY=VAL …` so the leak is actually scrubbed rather than just reported. ([#361](https://github.com/littlebearapps/untether/issues/361))

### Extending the allowlist (v0.35.3+)

If a plugin or MCP server depends on a specific variable, add it to the allowlist via TOML config — no fork, no re-install ([#409](https://github.com/littlebearapps/untether/issues/409)):

```toml title="~/.untether/untether.toml"
[security]
env_extra_allow = ["OP_SERVICE_ACCOUNT_TOKEN", "DOPPLER_TOKEN"]   # exact names
env_extra_prefix_allow = ["VAULT_", "INFISICAL_"]                  # families
```

Names must match `[A-Z_][A-Z0-9_]*`. Untether emits one `env_policy.user_extension` INFO log per process at first runner spawn so the addition is visible in `journalctl`. The runtime audit also honours these so user-allowed names aren't false-flagged as leaks. See [security guide](../how-to/security.md#engine-subprocess-env-allowlist) for the full discussion.

If you'd rather the new variable ship as a default for every Untether user, open a PR adding it to `_EXACT_ALLOW` / `_PREFIX_ALLOW` in `src/untether/utils/env_policy.py`. Set `[security] env_audit = false` to restore the legacy unconditional-pass-through behaviour (not recommended).

