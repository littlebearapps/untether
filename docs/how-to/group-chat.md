# Group chat and multi-user setup

Untether works in Telegram group chats, letting multiple people interact with coding agents from any device. This guide covers adding the bot to a group, restricting access, and configuring when the bot listens (listen mode).

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

Then send the bot a message in a **private chat** — Untether prints `chat_id = …`, and in a private chat that number is your user ID. Each teammate can do the same to find theirs.

## Per-sender session isolation

In group chats, each user gets their own independent session. User A's conversation history and context are completely separate from User B's — there is no cross-talk between sessions.

## Button press validation

In group chats, approval buttons (Approve, Deny, Pause & Outline Plan) are validated against `allowed_user_ids`. If a group member who is not in the allowed list taps another user's approval buttons, the press is rejected — they cannot approve or deny tool calls on someone else's behalf.

This also applies to cancel buttons. Approval buttons also only work in the chat they were posted in: a callback for a pending request sent from any other chat is refused ([#388](https://github.com/littlebearapps/untether/issues/388)). (When `allow_any_user = true` is set as the dev/demo escape hatch, all group members can interact with any buttons since there's no allowlist to validate against.)

### Only the person who started the run can approve (opt-in)

By default any allowed user can answer any approval in the chat. To restrict that to the person whose message started the run, set:

```toml
[transports.telegram]
approval_originator_only = true
```

Then Claude Code's Approve / Deny / Pause & Outline / Let's discuss buttons, the background-agent approval message, and AskUserQuestion answers (option buttons or a typed reply) only work for that person. Anyone else sees `Only the person who started this run can answer this.`, and Untether logs a WARNING (`callback.not_originator` or `ask_user_question.not_originator`) with both user ids. Runs with no human originator (cron, webhook, `/at` and loop fires) can still be answered by any allowed user. In a live Claude session, the originator is whoever started the session. The setting hot-reloads ([#388](https://github.com/littlebearapps/untether/issues/388)).

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
    The old `/trigger` command was renamed to `/listen` to disambiguate from the webhook/cron triggers system. `/trigger` still works as a deprecated alias and shows a one-line deprecation notice — it will be removed in a future version.

!!! tip "What triggers a response in mentions mode"
    In `mentions` mode, the bot responds when any of these conditions are met:

    - **@mention** — include `@your_bot_name` anywhere in the message
    - **Reply to the bot** — reply to any message the bot sent
    - **Slash command** — use a known command like `/claude`, `/cancel`, `/usage`, or a project alias like `/myproject`

    All other messages are silently ignored.

!!! note "Per-topic overrides"
    In forum groups, you can set listen mode per topic. A topic override can only narrow the chat-level setting: setting `mentions` on a topic makes just that topic mentions-only, but a topic can't opt back into `all` while the chat-level mode is `mentions`. To keep coding topics answering every message, leave the chat on `all` and set `mentions` on the topics that should stay quiet. See [Topics](topics.md) for details.

## Admin-only commands

In group chats, certain commands require admin or creator status:

- `/model` — change the model
- `/reasoning` — change reasoning level
- `/agent` — change the default engine
- `/listen` — change listen mode (also accepts the deprecated `/trigger`)
- bare `/steer` / `/queue` — change the [follow-up mode](steer-follow-ups.md) (`/steer <text>` itself is open to everyone)
- `/config` — every button or typed action that **changes** a setting ([#1042](https://github.com/littlebearapps/untether/issues/1042))
- `/planmode` — change the Claude Code permission mode (`/planmode show` is open to everyone)
- `/verbose` — change verbose progress for the chat, in every form

In private chats, these commands are always available without restriction.

### The `/config` menu in a group

Anyone allowed to use the bot can open `/config`, move between its pages and read the current values. Changing something needs a group admin or the creator, the same as the text commands above. That covers every setting in the menu: permission or approval mode (including Antigravity **Full access**), engine and model, reasoning or effort, listen mode, follow-up mode, ask mode, diff preview, verbose, resume line, cost, budget and **Stop at limit**, loop mode, and **Pause triggers** / **Resume triggers**.

When someone who isn't an admin taps a button that would change a setting, the tap is answered with `changing settings is restricted to group admins.` and nothing changes. A typed action such as `/config vb:on` gets the same reply, and so do `/planmode` and `/verbose`, which change the same settings as the menu's Permission mode and Verbose pages. If Untether can't confirm the sender's role with Telegram, it refuses with `failed to verify settings permissions.` rather than letting the change through.

!!! note "Things to know"
    - `/config` settings apply to the whole chat. In a forum group they also apply to every topic that has no override of its own, which is why changing them is limited to admins.
    - **Pause triggers** stops every cron and webhook on the host, not only the ones that post to this chat.
    - An admin who posts anonymously ("Remain anonymous") can't be identified from a typed command, so `/config vb:on` typed that way is refused. Tapping the menu buttons works, because Telegram reports the real account for a button tap.
    - Each change is logged with the chat id and the sender's user id (`config.*` lines), and each refusal as a WARNING `command.admin_required`.

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
