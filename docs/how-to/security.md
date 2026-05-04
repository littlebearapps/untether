# Security hardening

Untether gives remote access to coding agents on your server, so locking down who can interact with the bot and what files they can access is important. This guide covers the key security controls — all manageable from [Telegram](https://telegram.org) on any device.

## Restrict access

`allowed_user_ids` is **required** as of v0.35.3 ([#377](https://github.com/littlebearapps/untether/issues/377)). Set it to a non-empty list of Telegram user IDs:

=== "untether config"

    ```sh
    untether config set transports.telegram.allowed_user_ids "[12345, 67890]"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram]
    allowed_user_ids = [12345, 67890]
    ```

Only listed user IDs can interact with the bot. Messages from everyone else are silently ignored. In group chats, `allowed_user_ids` also governs button press validation — unauthorised users cannot tap Approve/Deny buttons on another user's tool requests. See [Group chat](group-chat.md#button-press-validation) for details.

To find your Telegram user ID:

```sh
untether chat-id
```

Send a message in the target chat and Untether prints the chat ID and sender ID.

!!! danger "Open-bot opt-out (dev/demo only)"
    If you genuinely need an open bot for a hackathon, demo, or local-only dev, you can opt out with `allow_any_user = true` under `[transports.telegram]`. Untether logs this at INFO every boot (`security.allow_any_user`) so the deviation is visible in `journalctl`. Never enable this on a host reachable from production traffic — anyone who learns the bot username gains command access.

!!! warning "Pre-v0.35.3 deployments"
    Before v0.35.3 the empty default was a silent insecure default — bots ran with no allowlist filter and a single warning log line. Upgrading to v0.35.3 surfaces this as a hard `ConfigError` at startup. If your bot fails to start with `[transports.telegram] allowed_user_ids is empty`, populate the list (recommended) or set `allow_any_user = true` to keep the prior behaviour.

## Protect your bot token

Your Telegram bot token grants full control over the bot. Keep it safe:

- **Never commit it to git** — add your config path to `.gitignore`
- **Never share it publicly** — anyone with the token can impersonate your bot
- **Restrict file permissions** on your config file:

```bash
chmod 600 ~/.untether/untether.toml
```

If you store your config in a non-standard location, set the `UNTETHER_CONFIG_PATH` environment variable:

```bash
export UNTETHER_CONFIG_PATH=/path/to/untether.toml
```

!!! tip "Automatic log redaction"
    Untether automatically redacts bot tokens, OpenAI API keys (`sk-...` and `sk-proj-...` since v0.35.3 — [#213](https://github.com/littlebearapps/untether/issues/213)), and GitHub tokens (`ghp_`, `ghs_`, `github_pat_`) from all structured log output. Even if a token appears in engine output or error messages, it is replaced with `[REDACTED]` before being written to logs. The Telegram voice transcription API key is wrapped in `SecretStr` so it never appears in `repr()`/tracebacks/structlog ([#378](https://github.com/littlebearapps/untether/issues/378)). Stderr path sanitisation also covers macOS (`/Users/<user>/`, `/private/var/...`), container roots (`/app/`, `/workspace/`), and other absolute paths beyond `/home/<user>/` (`/var/`, `/tmp/`, `/opt/`, `/srv/`, `/etc/`, `/usr/local/`, `/root/`) since v0.35.3 ([#208](https://github.com/littlebearapps/untether/issues/208)); path:line markers (`:42`) survive sanitisation so stack traces remain useful.

!!! tip "Pi session directory permissions ([#207](https://github.com/littlebearapps/untether/issues/207))"
    Pi engine session directories are created with explicit `0o700` mode (and any pre-existing dir gets `chmod`'d to `0o700` on first use) so other users on shared hosts can't read Pi session JSONL files. Applies as of v0.35.3 — no operator action needed.

## Engine subprocess env allowlist

Claude and Pi engine subprocesses do **not** inherit Untether's full environment. Only allowlisted variables (OS essentials, AI/cloud provider keys, Claude/MCP/Node/Python/UV/NPM namespaces, git/ssh auth) pass through — random third-party tokens that happen to live in your shell (`AWS_*`, `STRIPE_*`, `DATABASE_URL`, personal app tokens, etc.) are **not** available to the engine or its MCP servers. This reduces the blast radius of any tool call or MCP that exfiltrates process env.

If a new engine or MCP genuinely needs a variable that isn't allowlisted (symptom: hangs at init, silent `KeyError` in logs), you have two options:

1. **Recommended for most users (v0.35.3+)**: extend the allowlist via TOML config — no fork, no re-install:

    ```toml title="~/.untether/untether.toml"
    [security]
    env_extra_allow = ["OP_SERVICE_ACCOUNT_TOKEN", "DOPPLER_TOKEN"]
    env_extra_prefix_allow = ["VAULT_", "INFISICAL_"]
    ```

    Names must match `[A-Z_][A-Z0-9_]*`. Untether logs `env_policy.user_extension` once per process at first runner spawn so the addition is visible in `journalctl`. The runtime audit also honours these so user-allowed names aren't false-flagged as leaks. See [config: `[security]`](../reference/config.md#security) ([#409](https://github.com/littlebearapps/untether/issues/409)).

2. **For names that benefit every Untether user**: add to `_EXACT_ALLOW` / `_PREFIX_ALLOW` in `src/untether/utils/env_policy.py` and submit a PR. `BWS_ACCESS_TOKEN` (Bitwarden Secrets Manager) was promoted into the built-in defaults in v0.35.3 by exactly this path.

Other engines (Codex, Gemini, OpenCode, AMP) still inherit the full parent env — extending the allowlist to them is tracked in [#332](https://github.com/littlebearapps/untether/issues/332).

### Boundary enforcement on Claude exec ([#361](https://github.com/littlebearapps/untether/issues/361))

The Claude runner additionally wraps its exec with `env -i KEY=VAL …` so the resolved environment at exec time is **exactly** the allowlist — even if upstream Claude Code, a wrapper script, or PAM `/etc/environment` would otherwise inject host vars after the parent's `env=` kwarg is honoured. The wrap is always on and not configurable. Allowlisted KEY=VALUE pairs are redacted (`KEY=***`) in the `subprocess.spawn` structured log so the wrap doesn't itself leak provider keys into journald.

### Runtime env audit ([#361](https://github.com/littlebearapps/untether/issues/361))

`[security] env_audit = true` (default) enables a one-shot `/proc/<claude_pid>/environ` sample on first `system.init`. Any non-allowlisted name observed emits a `claude.env_audit.leaked_var` structured WARNING (dedup per session per name). On a clean host the audit is silent. See [config: `[security]`](../reference/config.md#security) to disable.

### Known upstream limitation

The boundary fix and audit confirm Untether's spawn env is clean. **However, Claude Code itself can re-introduce host vars at the Bash-tool subprocess level** — for example, if Claude invokes Bash via `bash -l` or `bash -i`, host shell rc files (`~/.profile`, `~/.bashrc`) get sourced, and any `export FOO=…` lines in those files leak into the Bash-tool subprocess. Untether's audit only samples Claude's process env, not its descendants.

Operator mitigation: keep host-level secrets out of `~/.bashrc` / `~/.profile`. Move them into project-scoped tools that only activate when you opt in (e.g. [direnv](https://direnv.net/) `.envrc`, [bws](https://bitwarden.com/help/secrets-manager-cli/) on demand, per-project `.env` files loaded by your editor's run config). The blast radius is then bounded to projects you explicitly opted into.

## File transfer deny globs

File transfer includes a deny list that blocks access to sensitive paths. The defaults are:

```toml title="~/.untether/untether.toml"
[transports.telegram.files]
deny_globs = [".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"]
```

Add more patterns as needed:

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram.files]
    deny_globs = [
        ".git/**",
        ".env",
        ".envrc",
        "**/*.pem",
        "**/.ssh/**",
        "**/*.key",
        "**/secrets/**",
        "**/.aws/**",
    ]
    ```

!!! tip "Defence in depth"
    Deny globs protect against accidental file exfiltration via `/file get`. They do not prevent the coding agent itself from reading files — the agent runs with full filesystem access in the project directory.

## Secure webhook endpoints

If you use webhooks to trigger runs from external services, always configure authentication:

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [[triggers.webhooks]]
    id = "github-push"
    path = "/hooks/github"
    auth = "hmac-sha256"
    secret = "whsec_your_github_secret"
    ```

Available authentication modes:

| Mode | Use case |
|------|----------|
| `hmac-sha256` | GitHub webhooks (recommended) |
| `hmac-sha1` | Legacy GitHub webhooks |
| `bearer` | Simple shared secret |
| `none` | Local testing only |

!!! warning "Never use `auth = \"none\"` in production"
    Without authentication, anyone who can reach the webhook endpoint can trigger arbitrary agent runs on your server.

## Bind webhook server to localhost

The webhook server should only listen on localhost. Put it behind a reverse proxy (nginx, Caddy) with TLS for external access:

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [triggers.server]
    host = "127.0.0.1"
    port = 9876
    ```

The server includes rate limiting (token-bucket, per-webhook and global) and timing-safe secret comparison by default.

## SSRF protection for outbound requests

Trigger features that make outbound HTTP requests (webhook forwarding, cron data fetching) include SSRF (Server-Side Request Forgery) protection. All outbound URLs are validated against blocked IP ranges:

- Loopback (`127.0.0.0/8`, `::1`)
- Private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
- Link-local (`169.254.0.0/16`, including cloud metadata endpoints)
- IPv6 unique-local and link-local
- IPv4-mapped IPv6 addresses (prevents bypass via `::ffff:127.0.0.1`)

DNS resolution is checked after hostname lookup to prevent DNS rebinding attacks (hostname resolves to a private IP).

If you need triggers to reach local services, route traffic through a reverse proxy on a non-private address. The SSRF allowlist is available as a code-level parameter in `triggers/ssrf.py` but is not currently exposed as a TOML setting.

## Untrusted payload marking

All webhook payloads and cron-fetched data are automatically prefixed with `#-- EXTERNAL WEBHOOK PAYLOAD --#` before being injected into the agent prompt. This signals to AI agents that the content is untrusted external input and should not be treated as instructions. The same prefix is applied to fetched cron data (`#-- EXTERNAL FETCHED DATA --#`).

## Run untether doctor

After any configuration change, run the built-in preflight check:

```sh
untether doctor
```

This validates:

- Telegram bot token is valid
- Chat ID is reachable
- Topics setup (if enabled)
- File transfer permissions and deny globs
- Voice transcription configuration
- Engine availability

Fix any issues reported before putting the instance into production.

## Related

- [Configuration](../reference/config.md) — full config reference for all security settings
- [Webhooks and cron](webhooks-and-cron.md) — webhook authentication and server configuration
- [Group chat and multi-user setup](group-chat.md) — access control in group chats
- [File transfer](file-transfer.md) — file transfer permissions and deny globs
