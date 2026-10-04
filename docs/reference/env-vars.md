# Environment variables

Untether supports a small set of environment variables for logging and runtime behaviour.

## Logging

| Variable | Description |
|----------|-------------|
| `TAKOPI_LOG_LEVEL` | Minimum log level (default `info`; `--debug` forces `debug`). |
| `TAKOPI_LOG_FORMAT` | `console` (default) or `json`. |
| `TAKOPI_LOG_COLOR` | Force colour on (`1`/`true`/`yes`/`on`) or off (any other value). Unset: colour when stdout is a TTY. |
| `TAKOPI_LOG_FILE` | Append JSON lines to a file. `--debug` defaults this to `debug.log`. |
| `TAKOPI_TRACE_PIPELINE` | Log pipeline events at `info` instead of `debug` (`1`/`true`/`yes`/`on`). |

## CLI behaviour

| Variable | Description |
|----------|-------------|
| `TAKOPI_NO_INTERACTIVE` | Any non-empty value disables interactive prompts (useful for CI / non-TTY). |
| `UNTETHER_CONFIG_PATH` | Override config file location (default `~/.untether/untether.toml`). Useful for running multiple instances or testing with alternate configs. |
| `UNTETHER_SETTINGS_CACHE` | Set to `0` (or `false`/`off`/`no`) to turn off the settings parse cache ([#506](https://github.com/littlebearapps/untether/issues/506)). By default `untether.toml` is parsed once per edit: every read compares the file's bytes (and the `UNTETHER__*` env vars) with the last parse and re-parses only when they differ, so edits still apply on the next read. Turning the cache off re-parses on every read, as before 0.35.5. Set it in a systemd drop-in (`Environment=UNTETHER_SETTINGS_CACHE=0`) for a per-host rollback. |
| `NOTIFY_SOCKET` | Set by systemd for `Type=notify` units; Untether sends `READY=1` there once it is up and `STOPPING=1` when it starts draining. Not set by hand. |

## Config overrides { #config-overrides }

Any key on Untether's settings models can be set from the environment as
`UNTETHER__<SECTION>__<KEY>` (prefix `UNTETHER__`, nested sections joined with `__`,
case-insensitive). An env value wins over the same key in `untether.toml`; other keys
in that section still come from the file.

```sh
UNTETHER__WATCHDOG__LIVE_SESSIONS=false
UNTETHER__TRANSPORTS__TELEGRAM__FOLLOWUP_MODE=steer
UNTETHER__PROGRESS__VERBOSITY=verbose
```

This covers the top-level keys and the `[transports.telegram]`, `[projects.*]`,
`[plugins]`, `[footer]`, `[preamble]`, `[progress]`, `[watchdog]`, `[cost_budget]`,
`[auto_continue]`, `[loop]` and `[security]` sections. It does **not** reach the
engine tables (`[claude]` / `[engines.claude]`, …) or `[triggers]`, which are read
from the TOML file only. The settings cache re-parses when any `UNTETHER__*` value
changes ([#506](https://github.com/littlebearapps/untether/issues/506)), but the
service only sees its own environment, so set these in the unit (or a drop-in) and
restart.

!!! warning "Keep secrets in the config file"
    Variables starting with `UNTETHER_` are on the environment allowlist that Claude Code
    and Pi subprocesses inherit, so an `UNTETHER__*` override (a bot token or voice API key
    included) is visible to those engines. Put secrets in `untether.toml` (mode `600`)
    rather than in `UNTETHER__*` variables.

## Voice transcription

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | API key for voice transcription when `voice_transcription_api_key` is unset. `untether doctor` checks for it. |
| `OPENAI_BASE_URL` | Transcription endpoint when `voice_transcription_base_url` is unset (otherwise `api.openai.com`). Not SSRF-checked, unlike the config key. |

## Engine-specific

| Variable | Description |
|----------|-------------|
| `PI_CODING_AGENT_DIR` | Override Pi agent session directory base path (default `~/.pi/agent`). |
| `CLAUDE_CONFIG_DIR` | Claude Code's config directory. Untether reads it to recognise plan files under `$CLAUDE_CONFIG_DIR/plans/` (as well as `.claude/plans/`) and to keep a separate rate-limit state per Claude config directory. |

## Runner environment

These variables are set (or removed) automatically by Untether in the engine subprocess environment.

| Variable | Set by | Description |
|----------|--------|-------------|
| `UNTETHER_SESSION` | Claude runner | Always set to `1` for Claude Code subprocesses. Enables Claude Code plugins to detect Untether sessions and adjust behaviour — for example, skipping blocking Stop hooks that would displace user-requested content in Telegram. |
| `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | Claude runner | Claude Code's stdout idle timeout. Default raised to `300000` (5 min) in v0.35.2 ([#342](https://github.com/littlebearapps/untether/issues/342)) — matches undici's idle-body timeout. The old 60 s default killed long-thinking runs. **As of v0.35.3 ([#438](https://github.com/littlebearapps/untether/issues/438))**, this is preferably set via `[watchdog] claude_stream_idle_timeout_ms` in `untether.toml` (range 30 s – 30 min). Shell-set `CLAUDE_STREAM_IDLE_TIMEOUT_MS` still wins via `setdefault`. Failures with `API Error: Stream idle timeout - partial response received` now classify as Type A (mid-generation — raising helps) or Type B (cold-start zero-byte — raising does NOT help; upstream API outage). |
| `CLAUDE_ENABLE_STREAM_WATCHDOG` | Claude runner | Defaults to `1` (turns on Claude Code's own stream watchdog — [#322](https://github.com/littlebearapps/untether/issues/322)). A value already in Untether's environment wins. |
| `MCP_TOOL_TIMEOUT` | Claude runner | Defaults to `120000` (ms). A value already in Untether's environment wins. |
| `MAX_MCP_OUTPUT_TOKENS` | Claude runner | Defaults to `12000`. A value already in Untether's environment wins. |
| `CLAUDE_CODE_DISABLE_CRON` | Claude runner | Set to `1` (overriding any inherited value) when Untether resumes a session that may still hold a Claude Code scheduled task from before v0.35.5rc20, a `-p` chat or `[loop] own_schedule = false`, for up to 7 days, so the CLI can't restart that task. Logged as `claude.cron_suppressed` ([#926](https://github.com/littlebearapps/untether/issues/926)). Not set otherwise. |
| `ANTHROPIC_API_KEY` | Claude runner | **Removed** from the Claude subprocess environment unless `[claude] use_api_billing = true`, so Claude Code uses its subscription login. |
| `NO_COLOR`, `CI` | Pi runner | Default to `1` so Pi's output carries no ANSI codes. A value already in Untether's environment wins. |

!!! note "Not a security concern"
    `UNTETHER_SESSION` is a simple signal variable, not a credential or secret. It tells Claude Code plugins that the session is running via Telegram so they can avoid interfering with Untether's single-message output model. Plugins like [PitchDocs](https://github.com/littlebearapps/lba-plugins) check for this variable and skip blocking hooks that would otherwise consume the final response with meta-commentary instead of the user's requested content. See the [PitchDocs interference audit](../audits/pitchdocs-context-guard-interference.md) for the full analysis.

## Env allowlist (Claude/Pi)

As of v0.35.2, arbitrary process env vars are **not** forwarded to Claude/Pi subprocesses ([#198](https://github.com/littlebearapps/untether/issues/198)). Only the built-in allowlist in `src/untether/utils/env_policy.py` (plus your `[security]` extras) is passed through:

- **Exact names:** OS essentials (`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TMP`, `TEMP`, `TZ`); CLI output (`NO_COLOR`, `CI`, `FORCE_COLOR`, `COLORTERM`, `CLICOLOR`, `CLICOLOR_FORCE`); `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`; language runtimes (`PYTHONPATH`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `PYTHONIOENCODING`, `NODE_PATH`, `NODE_OPTIONS`, `LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`, `DYLD_FALLBACK_LIBRARY_PATH`); git/SSH (`SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GIT_CONFIG_GLOBAL`, `GIT_SSH_COMMAND`); provider keys (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`, `GEMINI_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `FIREWORKS_API_KEY`); `GITHUB_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `GH_TOKEN`; `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`; `BWS_ACCESS_TOKEN` (a default since v0.35.3); `UNTETHER_SESSION`; `PROJECT_ROOT`, `DIRENV_DIR`.
- **Prefixes:** `CLAUDE_`, `CLAUDE_CODE_`, `MCP_`, `MAX_MCP_`, `LC_`, `UV_`, `NPM_`, `PNPM_`, `NODE_`, `PIP_`, `UNTETHER_`.

The Claude runner always execs under `env -i KEY=VAL …`, so the resolved environment is exactly the allowlist ([#361](https://github.com/littlebearapps/untether/issues/361)). When `[security] env_audit = true` (default — see [config reference](config.md#security)), Untether also samples `/proc/<pid>/environ` once at session start and logs `claude.env_audit.leaked_var` for any non-allowlisted name it finds.

### Extending the allowlist (v0.35.3+)

If a plugin or MCP server depends on a specific variable, add it to the allowlist via TOML config — no fork, no re-install ([#409](https://github.com/littlebearapps/untether/issues/409)):

```toml title="~/.untether/untether.toml"
[security]
env_extra_allow = ["OP_SERVICE_ACCOUNT_TOKEN", "DOPPLER_TOKEN"]   # exact names
env_extra_prefix_allow = ["VAULT_", "INFISICAL_"]                  # families
```

Names must match `[A-Z_][A-Z0-9_]*`. Untether emits one `env_policy.user_extension` INFO log per process at first runner spawn so the addition is visible in `journalctl`. The runtime audit also honours these so user-allowed names aren't false-flagged as leaks. See [security guide](../how-to/security.md#engine-subprocess-env-allowlist) for the full discussion.

If you'd rather the new variable ship as a default for every Untether user, open a PR adding it to `_EXACT_ALLOW` / `_PREFIX_ALLOW` in `src/untether/utils/env_policy.py`. Setting `[security] env_audit = false` turns off the runtime audit only; the allowlist and the `env -i` wrap stay on.

## Env for Codex and OpenCode { #env-codex-opencode }

<!-- verified codex 0.157.1 / opencode 1.14.33, 2026-09-30 (#454); recheck on either CLI bump -->

Untether does not filter the Codex or OpenCode environment: both CLIs are spawned with Untether's full process environment (so are the deprecated Gemini CLI and AMP engines, removed in 0.36.0). `[security] env_extra_allow` has no effect on them (there is no allowlist to extend). Per-runner filtering for these engines is tracked in [#375](https://github.com/littlebearapps/untether/issues/375).

What an **MCP server** sees is decided by the engine, not by Untether, and the two engines differ:

| Engine | Engine process gets | Its local (stdio) MCP servers get | To pass `BWS_ACCESS_TOKEN` to an MCP server |
|---|---|---|---|
| Claude | Untether's allowlist (see above) | whatever Claude Code passes on | Nothing — `BWS_ACCESS_TOKEN` is allowlisted for the engine process by default |
| OpenCode | Untether's full env | the engine's full env, plus the server's `environment` map | Nothing, as long as Untether itself has it |
| Codex | Untether's full env | **a clean env**: only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `USER`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `TZ` (plus custom CA-certificate vars), plus names in `env_vars` (read from Codex's env, i.e. Untether's) and pairs in `env` | Add `env_vars = ["BWS_ACCESS_TOKEN"]` to the server's `[mcp_servers.<name>]` table |

```toml title="~/.codex/config.toml"
[mcp_servers.trello]
command = "/home/me/bin/trello-mcp.sh"
env_vars = ["BWS_ACCESS_TOKEN"]   # forward by name; don't paste the secret into `env`
```

```json title="opencode.json"
{
  "mcp": {
    "trello": {
      "type": "local",
      "command": ["/home/me/bin/trello-mcp.sh"]
    }
  }
}
```

!!! note "Untether's environment is not your shell's"
    The variable has to be in **Untether's** process environment, not just your interactive shell. Under systemd that means the unit's `EnvironmentFile=` — `contrib/untether.service` reads `%h/.untether/.env` — not `~/.bashrc` or a direnv `.envrc`. After adding a variable there, restart the service. OpenCode's `"environment": {"X": "{env:X}"}` substitutes an **empty string** when `X` is unset, so the variable arrives set-but-empty rather than missing.

See the [Codex MCP docs](https://developers.openai.com/codex/mcp) and [OpenCode MCP servers](https://opencode.ai/docs/mcp-servers/) for the full config schema.

## MCP wrapper scripts under `set -u` { #mcp-wrapper-set-u }

A wrapper script that runs `set -u` (or `set -eu`) exits with `VAR: unbound variable` the moment it references a variable that isn't set. The MCP server never starts, and the engine reports it as failed at session start; in Telegram you just see a run without those tools. Guard every variable reference:

| Form | Unset or empty | Use for |
|---|---|---|
| `${VAR:-}` | expands to empty, no error | optional values you test with `[ -n … ]` |
| `${VAR:-default}` | expands to `default` | values with a safe fallback |
| `: "${VAR:=default}"` | assigns `default` to `VAR` | defaults you reference several times |
| `: "${VAR:?message}"` | exits non-zero, prints `VAR: message` to stderr | required secrets — fail fast with a clear message |

Use the colon forms: they treat an empty value like an unset one, which matters for OpenCode's `{env:VAR}` substitution (see above).

```bash title="~/bin/trello-mcp.sh"
#!/usr/bin/env bash
set -euo pipefail
: "${BWS_ACCESS_TOKEN:?BWS_ACCESS_TOKEN is not set in the engine environment}"
: "${TRELLO_SECRET_ID:=00000000-0000-0000-0000-000000000000}"   # := assigns a default
TRELLO_TOKEN="$(bws secret get "$TRELLO_SECRET_ID" | jq -r .value)"
export TRELLO_TOKEN
exec trello-mcp-server
```

(Every variable the script touches is guarded. `export X="$(…)"` on one line would hide a failing `bws` call from `set -e`, so the assignment and the `export` are kept separate.)

```bash title="optional token"
if [ -n "${BWS_ACCESS_TOKEN:-}" ]; then
  exec my-mcp-server --token-from-bws
else
  exec my-mcp-server --read-only
fi
```

The `:?` message goes to the MCP server's stderr, which the engine writes to its **own** log (OpenCode: `mcp stderr: …`; Codex: `MCP server stderr (<program>): …`), not to Telegram. Check there when a server's tools are missing.

