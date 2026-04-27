# Group chat and multi-user setup

Untether works in Telegram group chats, letting multiple people interact with coding agents from any device. This guide covers adding the bot to a group, restricting access, and configuring trigger behaviour.

## Add the bot to a group

Add your Untether bot to a Telegram group like any other member. If you plan to use forum topics, promote the bot to admin with **Manage Topics** permission.

!!! tip "Forum topics"
    If you want each thread to have its own project/branch context and session, enable topics in the group settings and see [Topics](topics.md) for the full setup.

## Restrict access with allowed_user_ids

`allowed_user_ids` is required as of v0.35.3 ([#377](https://github.com/littlebearapps/untether/issues/377)) — see [security.md](security.md#restrict-access). Set it to a non-empty list of Telegram user IDs:

=== "untether config"

    ```sh
    untether config set transports.telegram.allowed_user_ids "[12345, 67890]"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram]
    allowed_user_ids = [12345, 67890]
    ```

Only listed Telegram user IDs can start runs and interact with the bot. Messages from other users are silently ignored.

To find your Telegram user ID, run:

```sh
untether chat-id
```

Then send a message — Untether prints the chat ID and your user ID.

## Per-sender session isolation

In group chats, each user gets their own independent session. User A's conversation history and context are completely separate from User B's — there is no cross-talk between sessions.

## Button press validation

In group chats, approval buttons (Approve, Deny, Pause & Outline Plan) are validated against `allowed_user_ids`. If a group member who is not in the allowed list taps another user's approval buttons, the press is rejected — they cannot approve or deny tool calls on someone else's behalf.

This also applies to cancel buttons. (When `allow_any_user = true` is set as the dev/demo escape hatch, all group members can interact with any buttons since there's no allowlist to validate against.)

## Set listen mode for groups

By default, the bot responds to every message (`all` mode). In busy groups, switch to `mentions` mode so the bot only responds when @mentioned:

```
/listen mentions
```

| Command | Behaviour |
|---------|-----------|
| `/listen` | Show the current listen mode |
| `/listen all` | Respond to every message |
| `/listen mentions` | Only respond to @bot_name mentions |
| `/listen clear` | Reset to the default (`all`) |

!!! note "Renamed from `/trigger` in v0.35.3"
    The old `/trigger` command was renamed to `/listen` to disambiguate from the webhook/cron triggers system. `/trigger` continues to work as a deprecated alias for one release cycle and shows a one-line deprecation notice — it will be removed in a future version.

!!! tip "What triggers a response in mentions mode"
    In `mentions` mode, the bot responds when any of these conditions are met:

    - **@mention** — include `@your_bot_name` anywhere in the message
    - **Reply to the bot** — reply to any message the bot sent
    - **Slash command** — use a known command like `/claude`, `/cancel`, `/usage`, or a project alias like `/myproject`

    All other messages are silently ignored.

!!! note "Per-topic overrides"
    In forum groups, you can set listen mode per topic. A topic override takes priority over the chat-level default. For example, set `mentions` on general chat but leave coding topics on `all`. See [Topics](topics.md) for details.

## Admin-only commands

In group chats, certain commands require admin or creator status:

- `/model` — change the model
- `/reasoning` — change reasoning level
- `/agent` — change the default engine
- `/listen` — change listen mode (also accepts the deprecated `/trigger`)

In private chats, these commands are always available without restriction.

## File transfer in groups

File transfer (`/file put`, `/file get`) in group chats requires admin or creator status by default. To allow specific non-admin users to transfer files, set the file-specific allowed list:

=== "untether config"

    ```sh
    untether config set transports.telegram.files.allowed_user_ids "[12345, 67890]"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram.files]
    enabled = true
    allowed_user_ids = [12345, 67890]
    ```

When `files.allowed_user_ids` is empty (the default), private chats are allowed and group usage requires admin privileges.

## Related

- [Topics](topics.md) — bind forum threads to projects and branches
- [Configuration](../reference/config.md) — full config reference
- [Security hardening](security.md) — restrict access and protect your instance
- [Route by chat](route-by-chat.md) — bind specific chats to projects
