# Group chat and multi-user setup

Untether works in Telegram group chats, letting multiple people interact with coding agents from any device — iPhone, iPad, Android, Mac, Windows, Linux, or Telegram Web. This guide covers adding the bot to a group, restricting access, and configuring trigger behaviour.

## Add the bot to a group

Add your Untether bot to a Telegram group like any other member. If you plan to use forum topics, promote the bot to admin with **Manage Topics** permission.

!!! tip "Forum topics"
    If you want each thread to have its own project/branch context and session, enable topics in the group settings and see [Topics](topics.md) for the full setup.

## Restrict access with allowed_user_ids

By default, anyone in the group can interact with the bot. To restrict access to specific users, set `allowed_user_ids`:

=== "untether config"

    ```sh
    untether config set transports.telegram.allowed_user_ids "[12345, 67890]"
    ```

=== "toml"

    ```toml title="~/.untether/untether.toml"
    [transports.telegram]
    allowed_user_ids = [12345, 67890]
    ```

When `allowed_user_ids` is non-empty, only listed Telegram user IDs can start runs and interact with the bot. Messages from other users are silently ignored.

To find your Telegram user ID, run:

```sh
untether chat-id
```

Then send a message — Untether prints the chat ID and your user ID.

## Per-sender session isolation

In group chats, each user gets their own independent session. User A's conversation history and context are completely separate from User B's — there is no cross-talk between sessions.

## Set trigger mode for groups

By default, the bot responds to every message (`all` mode). In busy groups, switch to `mentions` mode so the bot only responds when @mentioned:

```
/trigger mentions
```

| Command | Behaviour |
|---------|-----------|
| `/trigger` | Show the current trigger mode |
| `/trigger all` | Respond to every message |
| `/trigger mentions` | Only respond to @bot_name mentions |
| `/trigger clear` | Reset to the default (`all`) |

!!! tip "Mentions mode"
    In `mentions` mode, start your message with `@your_bot_name` or include the mention anywhere in the text. The bot ignores messages that don't mention it.

## Admin-only commands

In group chats, certain commands require admin or creator status:

- `/model` — change the model
- `/reasoning` — change reasoning level
- `/agent` — change the default engine
- `/trigger` — change trigger mode

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
