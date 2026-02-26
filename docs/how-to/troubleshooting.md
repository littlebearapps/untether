# Troubleshooting

If something isn’t working, rerun with debug logging enabled:

```sh
untether --debug
```

Then check `debug.log` for errors and include it when reporting issues.

You can also run a preflight check:

```sh
untether doctor
```

This validates your Telegram token, chat id, topics setup, file transfer permissions, and voice transcription configuration.

## Claude Code credentials

The `/usage` command reads your Claude Code OAuth credentials to fetch subscription usage data. If you see **"No Claude credentials found"**, run `claude login` in your terminal to authenticate.

Credential storage varies by platform:

| Platform | Storage | Path |
|----------|---------|------|
| Linux | Plain-text file | `~/.claude/.credentials.json` |
| macOS | macOS Keychain | Entry: `Claude Code-credentials` |

Untether checks both locations automatically. If `/usage` still fails after logging in, check that the Claude CLI is working by running `claude` directly.
