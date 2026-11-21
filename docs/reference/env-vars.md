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
| `UNTETHER_SETTINGS_CACHE` | Set to `0` (or `false`/`off`/`no`) to turn off the settings parse cache ([#506](https://github.com/littlebearapps/untether/issues/506)). By default `untether.toml` is parsed once per edit: every read compares the file's bytes (and the `UNTETHER__*` env vars) with the last parse and re-parses only when they differ, so edits still apply on the next read. Turning the cache off re-parses on every read, as before 0.36.0. Set it in a systemd drop-in (`Environment=UNTETHER_SETTINGS_CACHE=0`) for a per-host rollback. |
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

### Bot token from the environment { #bot-token-env }

`UNTETHER__TRANSPORTS__TELEGRAM__BOT_TOKEN` sets the bot token and wins over
`bot_token` in `untether.toml`, so the file doesn't have to hold it (leave
`bot_token` out of `[transports.telegram]`). With systemd, keep it in a file only
your user can read and load it into the unit:

```ini
# systemctl --user edit untether
[Service]
EnvironmentFile=%h/.config/untether/secrets.env
```

where `secrets.env` (mode `600`) holds
`UNTETHER__TRANSPORTS__TELEGRAM__BOT_TOKEN=…`. Restart the service from a terminal
afterwards. Untether never passes `UNTETHER__*` variables to an engine process, for
any engine ([#1219](https://github.com/littlebearapps/untether/issues/1219)); before 0.36.2, Claude Code, Pi and
Antigravity received them through the `UNTETHER_` allowlist prefix, and Codex and
OpenCode through Untether's whole environment (which they no longer get, [#1241](https://github.com/littlebearapps/untether/issues/1241)).

For webhook secrets, see `secret_env` / `secret_file` in the
[Triggers reference](triggers/triggers.md#triggerswebhooks). A `secret_file` works
with systemd's `LoadCredential=` (point it at `%d/<name>`, the credentials
directory, written owner-only by systemd).

## Voice transcription

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | API key for voice transcription when `voice_transcription_api_key` is unset. `untether doctor` checks for it. |
| `OPENAI_BASE_URL` | Transcription endpoint when `voice_transcription_base_url` is unset (otherwise `api.openai.com`). Not SSRF-checked, unlike the config key. |

## Engine-specific

| Variable | Description |
|----------|-------------|
| `PI_CODING_AGENT_DIR` | Override Pi agent session directory base path (default `~/.pi/agent`). |
| `GEMINI_API_KEY` | Antigravity CLI's API-key sign-in. Only used when agy's settings file also sets `"modelProvider": "gemini"`; `GOOGLE_API_KEY` is ignored by agy. `GOOGLE_GEMINI_BASE_URL` optionally overrides the endpoint. |
| `AGY_ADC_AUTH` | Set to `true` for Antigravity CLI's Gemini Enterprise / Google Cloud sign-in (Application Default Credentials), with `GOOGLE_CLOUD_QUOTA_PROJECT` and `GOOGLE_CLOUD_LOCATION`. |
| `DBUS_SESSION_BUS_ADDRESS` | Lets Antigravity CLI reach a Linux desktop keyring holding a Google account sign-in, and Codex its keyring credential store. Passed to agy and Codex only. |
| `CLAUDE_CONFIG_DIR` | Claude Code's config directory. Untether reads it to recognise plan files under `$CLAUDE_CONFIG_DIR/plans/` (as well as `.claude/plans/`) and to keep a separate rate-limit state per Claude config directory. |

## Runner environment

These variables are set (or removed) automatically by Untether in the engine subprocess environment.

| Variable | Set by | Description |
|----------|--------|-------------|
| `UNTETHER_SESSION` | Claude runner | Always set to `1` for Claude Code subprocesses. Enables Claude Code plugins to detect Untether sessions and adjust behaviour — for example, skipping blocking Stop hooks that would displace user-requested content in Telegram. |
| `UNTETHER_TRIGGER` | every runner | `cron:<id>` or `webhook:<id>` for a cron or webhook run; absent for a person's message, a reply to a trigger run, `/at` and loop fires. Any value inherited from Untether's own environment is removed first ([#1120](https://github.com/littlebearapps/untether/issues/1120)). |
| `UNTETHER_RUN_ID` | every runner | A new UUID for each cron or webhook fire, the same one logged as `run_id` on `triggers.dispatch.starting` and `runner.completed`. Same rules as `UNTETHER_TRIGGER` ([#1120](https://github.com/littlebearapps/untether/issues/1120)). |
| `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | Claude runner | Claude Code's stdout idle timeout. Default raised to `300000` (5 min) in v0.35.2 ([#342](https://github.com/littlebearapps/untether/issues/342)) — matches undici's idle-body timeout. The old 60 s default killed long-thinking runs. **As of v0.35.3 ([#438](https://github.com/littlebearapps/untether/issues/438))**, this is preferably set via `[watchdog] claude_stream_idle_timeout_ms` in `untether.toml` (range 30 s – 30 min). Shell-set `CLAUDE_STREAM_IDLE_TIMEOUT_MS` still wins via `setdefault`. Failures with `API Error: Stream idle timeout - partial response received` now classify as Type A (mid-generation — raising helps) or Type B (cold-start zero-byte — raising does NOT help; upstream API outage). |
| `CLAUDE_ENABLE_STREAM_WATCHDOG` | Claude runner | Defaults to `1` (turns on Claude Code's own stream watchdog — [#322](https://github.com/littlebearapps/untether/issues/322)). A value already in Untether's environment wins. |
| `MCP_TOOL_TIMEOUT` | Claude runner | Defaults to `120000` (ms). A value already in Untether's environment wins. |
| `MAX_MCP_OUTPUT_TOKENS` | Claude runner | Defaults to `12000`. A value already in Untether's environment wins. |
| `CLAUDE_CODE_DISABLE_CRON` | Claude runner | Set to `1` (overriding any inherited value) when Untether resumes a session that may still hold a Claude Code scheduled task created by an earlier version (0.35.4, or 0.35.5rc19 and earlier), a `-p` chat or `[loop] own_schedule = false`, for up to 7 days, so the CLI can't restart that task. Logged as `claude.cron_suppressed` ([#926](https://github.com/littlebearapps/untether/issues/926)). Not set otherwise. |
| `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_ENTRYPOINT` | Claude runner | **Removed** from the Claude subprocess environment when Untether inherits them (for example, when it is started from a Claude Code session's shell), so Claude Code treats the run as its own session and picks its own entrypoint ([#1124](https://github.com/littlebearapps/untether/issues/1124)). |
| `ANTHROPIC_API_KEY` | Claude runner | **Removed** from the Claude subprocess environment unless `[claude] use_api_billing = true`, so Claude Code uses its subscription login. |
| `NO_COLOR`, `CI` | Pi runner | Default to `1` so Pi's output carries no ANSI codes. A value already in Untether's environment wins. |

!!! note "Not a security concern"
    `UNTETHER_SESSION` is a simple signal variable, not a credential or secret. It tells Claude Code plugins that the session is running via Telegram so they can avoid interfering with Untether's single-message output model. Plugins like [PitchDocs](https://github.com/littlebearapps/lba-plugins) check for this variable and skip blocking hooks that would otherwise consume the final response with meta-commentary instead of the user's requested content. See the [PitchDocs interference audit](../audits/pitchdocs-context-guard-interference.md) for the full analysis.

## Env allowlist (Claude/Pi/Antigravity/Codex/OpenCode) { #env-allowlist-claudepi }

As of v0.35.2, arbitrary process env vars are **not** forwarded to Claude/Pi subprocesses (nor, from v0.36.1, to Antigravity CLI's, or from v0.36.2 to Codex's and OpenCode's) ([#198](https://github.com/littlebearapps/untether/issues/198), [#1241](https://github.com/littlebearapps/untether/issues/1241)). Only the built-in allowlist in `src/untether/utils/env_policy.py` (plus your `[security]` extras) is passed through:

- **Exact names:** OS essentials (`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TMP`, `TEMP`, `TZ`); CLI output (`NO_COLOR`, `CI`, `FORCE_COLOR`, `COLORTERM`, `CLICOLOR`, `CLICOLOR_FORCE`); `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`; language runtimes (`PYTHONPATH`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `PYTHONIOENCODING`, `NODE_PATH`, `NODE_OPTIONS`, `LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`, `DYLD_FALLBACK_LIBRARY_PATH`); git/SSH (`SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GIT_CONFIG_GLOBAL`, `GIT_SSH_COMMAND`); provider keys (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`, `GEMINI_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `FIREWORKS_API_KEY`); `GITHUB_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `GH_TOKEN`; `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`; `BWS_ACCESS_TOKEN` (a default since v0.35.3); `UNTETHER_SESSION`; `PROJECT_ROOT`, `DIRENV_DIR`.
- **Prefixes:** `CLAUDE_`, `CLAUDE_CODE_`, `MCP_`, `MAX_MCP_`, `LC_`, `UV_`, `NPM_`, `PNPM_`, `NODE_`, `PIP_`, `UNTETHER_`.

The Antigravity runner uses the same allowlist and your `[security]` extras, plus four names passed to agy only: `DBUS_SESSION_BUS_ADDRESS`, `AGY_ADC_AUTH`, `GOOGLE_CLOUD_QUOTA_PROJECT` and `GOOGLE_GEMINI_BASE_URL` ([#558](https://github.com/littlebearapps/untether/issues/558)). Proxy variables (`HTTPS_PROXY`, `NO_PROXY`) are not on the list; add them with `env_extra_allow` if agy needs them.

The Codex and OpenCode runners also use the allowlist and your `[security]` extras ([#1241](https://github.com/littlebearapps/untether/issues/1241)), plus:

- **Both:** proxy and CA-certificate names (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` in upper and lower case; `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `CARGO_HTTP_CAINFO`, `BUNDLE_SSL_CA_CERT`, `npm_config_cafile`; `NODE_EXTRA_CA_CERTS` already passes by the `NODE_` prefix).
- **Codex:** prefix `CODEX_` (`CODEX_HOME`, `CODEX_API_KEY`, `CODEX_CA_CERTIFICATE`, …), `RUST_LOG`, `DBUS_SESSION_BUS_ADDRESS`, `OPENAI_FEDERATION_RULE_ID`, `OPENAI_IDENTITY_TOKEN_FILE`, `OPENAI_WORKLOAD_IDENTITY_CONTEXT`.
- **OpenCode:** prefix `OPENCODE_`; **every name ending in `_API_KEY`** (so an unrelated `STRIPE_API_KEY` in Untether's environment reaches OpenCode too); and a dated snapshot of the other provider names OpenCode 1.19.6 knows (taken 2026-11-18 from its models.dev catalogue and provider loaders: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_PROFILE`, `AWS_BEARER_TOKEN_BEDROCK`, `GOOGLE_VERTEX_PROJECT`, `GOOGLE_VERTEX_LOCATION`, `HF_TOKEN`, `DATABRICKS_TOKEN`, `GITLAB_TOKEN`, `AICORE_SERVICE_KEY` and about 30 more; the full list is `_OPENCODE_PROVIDER_ENV` in `src/untether/runners/opencode.py`).

On the first Codex or OpenCode run after a start, Untether logs `env_policy.dropped engine=<engine> names=[…]` (names only, never values) so you can see what to add to `env_extra_allow`.

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

<!-- verified codex 0.170.0 / opencode 1.19.6, 2026-11-18 (#1241); recheck on either CLI bump -->

Since v0.36.2, Untether spawns Codex and OpenCode with the [env allowlist](#env-allowlist-claudepi) plus their own extras, not its full process environment ([#1241](https://github.com/littlebearapps/untether/issues/1241)). The deprecated, unsupported Gemini CLI and AMP engines get the same treatment ([#1354](https://github.com/littlebearapps/untether/issues/1354)), so no engine inherits Untether's full environment. A variable either engine reads under a name you chose (a Codex `model_providers.<id>.env_key`, an MCP server's `env_vars`, `bearer_token_env_var` or `env_http_headers`, an `opencode.json` `{env:NAME}`) needs `[security] env_extra_allow`.

What an **MCP server** sees is decided by the engine, not by Untether, and the two engines differ:

| Engine | Engine process gets | Its local (stdio) MCP servers get | To pass `BWS_ACCESS_TOKEN` to an MCP server |
|---|---|---|---|
| Claude | Untether's allowlist (see above) | whatever Claude Code passes on | Nothing — `BWS_ACCESS_TOKEN` is allowlisted for the engine process by default |
| OpenCode | Untether's allowlist plus OpenCode's extras (see above) | the engine's env, plus the server's `environment` map | Nothing — `BWS_ACCESS_TOKEN` is allowlisted by default (any other name needs `env_extra_allow`) |
| Codex | Untether's allowlist plus Codex's extras (see above) | **a clean env**: only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `USER`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `TZ` (plus custom CA-certificate vars), plus names in `env_vars` (read from Codex's env, so the name must also pass Untether's allowlist or `env_extra_allow`) and pairs in `env` | Add `env_vars = ["BWS_ACCESS_TOKEN"]` to the server's `[mcp_servers.<name>]` table |

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

