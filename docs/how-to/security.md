# Security hardening

Untether gives remote access to coding agents on your server, so locking down who can interact with the bot and what files they can access is important. This guide covers the key security controls — all manageable from [Telegram](https://telegram.org) on any device.

## Restrict access

By default, anyone who can message your bot can start agent runs. To restrict access to specific Telegram users, set `allowed_user_ids`:

=== "untether config"

    ```sh
    untether config set transports.telegram.allowed_user_ids "[12345, 67890]"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram]
    allowed_user_ids = [12345, 67890]
    ```

When this list is non-empty, only the listed user IDs can interact with the bot. Messages from everyone else are silently ignored.

To find your Telegram user ID:

```sh
untether chat-id
```

Send a message in the target chat and Untether prints the chat ID and sender ID.

!!! warning "Empty list means open access"
    If `allowed_user_ids` is empty (the default), anyone who discovers your bot's username can start runs. Always set this in production.

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

If you need triggers to reach local services, you can configure an allowlist (see the [triggers reference](../reference/triggers/triggers.md)).

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
