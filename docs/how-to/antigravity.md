# Use Antigravity CLI

Antigravity CLI (`agy`) is Google's successor to Gemini CLI. From v0.36.1 Untether runs it as its own `antigravity`
engine: send a task from Telegram, watch the progress, and resume the conversation later from chat or a terminal.

It suits you if you want Google's Gemini models (and the Claude and GPT models some Antigravity plans include) next
to your other engines, and you're happy to choose what the agent may do **before** a run starts. Antigravity has no
approval buttons yet, so Untether runs it with a pre-run permission mode instead. If you need to approve each tool
call from your phone, use Claude Code for that work.

## Before you start

You need:

- **agy 1.3.1 or newer** on the machine that runs Untether (tested on 1.3.3). An older agy is refused before
  anything starts. agy updates itself; `agy update` forces it.
- **agy signed in** on that machine. Untether never signs in for you.
- **A project** for the chat. Antigravity edits files where it runs, so Untether won't run it in a chat with no
  project bound.

Install agy:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Untether looks for `agy` on `PATH`, then at `~/.local/bin/agy`.

!!! warning "Google's terms and Google account sign-ins"
    Google's [Antigravity terms](https://antigravity.google/terms) don't allow third-party tools to use a Google
    account sign-in, and Google may suspend the account. Untether runs Google's own `agy` binary and never reads your
    Google credentials, but it is still third-party software driving the CLI.

    **We recommend a Gemini API key or a Gemini Enterprise sign-in.** If you use a Google account sign-in anyway, you
    accept that risk for that account. Read the full note in
    [Switch engines](switch-engines.md#antigravity-cli) first. This isn't legal advice.

Pick one sign-in route:

| Route | What to set |
|---|---|
| **Gemini API key** (recommended) | `"modelProvider": "gemini"` in `~/.gemini/antigravity-cli/settings.json` **and** `GEMINI_API_KEY` in Untether's environment. You need both. Google bills this per token |
| **Gemini Enterprise / Google Cloud** (recommended) | `AGY_ADC_AUTH=true`, `GOOGLE_CLOUD_QUOTA_PROJECT` and `GOOGLE_CLOUD_LOCATION` in Untether's environment, with Application Default Credentials on the host |
| **Google account** | Run `agy` once in a terminal on the host and finish the sign-in. Chats on this route get a one-time notice about Google's terms on their first Antigravity answer |

The steps for each route are in [Switch engines](switch-engines.md#antigravity-cli), and the detail (keyrings,
environment allowlist, proxies) is in the
[runner reference](../reference/runners/antigravity/runner.md#sign-in-routes).

## Select the engine

```
/antigravity list the TODO comments in this repo
/agent set antigravity
```

- `/antigravity <prompt>` uses Antigravity for one message.
- `/agent set antigravity` makes it the default for the chat (or the topic, inside a forum topic).
- A project can default to it with `default_engine = "antigravity"`; see [Projects](projects.md).

If the chat has no project, bind one first with `/ctx set <project>`. Antigravity never runs in the home directory,
`/` or Untether's own directory.

<!-- TODO: capture screenshot: antigravity-config-home — /config home page in an Antigravity chat, showing Permission mode and Effort -->
<!-- <img src="../assets/screenshots/antigravity-config-home.jpg" alt="/config home page in an Antigravity chat with Permission mode and Effort" width="360" loading="lazy" /> -->

## Your first run

Send a task. The progress message is edited in place while agy works:

!!! untether "Untether"
    working · antigravity · 9s · step 2

    ✓ Read `README.md`<br>
    ✓ Edit `docs/notes.md`

When agy finishes, the final message carries the answer, a footer and the resume line:

!!! untether "Untether"
    done · antigravity · 21s · step 3

    Added a "Getting started" section to `docs/notes.md`.

    🏷 dir: my-project | workspace<br>
    🔢 21.4s · 16.8k/1.0k

    ↩️ `agy --conversation 6f1c2a9e-…`

- **The footer** shows the project and the permission mode (plus the model and effort once you set them), and
  this run's duration and tokens. Antigravity reports tokens, not
  dollars, so there is no cost figure.
- **The resume line** is what you copy into a terminal to carry on there.
- **To continue in Telegram**, just send another message in chat mode, or reply to the message. `/continue` picks
  up the most recent conversation for the project directory, including one you started in a terminal; see
  [Cross-environment resume](cross-environment-resume.md).

agy sends one answer per run. If it started a background command, it holds the answer until that finishes (up to 30
minutes) and the progress message shows `⏳ Antigravity is waiting for 1 background task …`. That isn't a stall.

<!-- TODO: capture screenshot: antigravity-workspace-final — final message of a Workspace run with the model · effort · workspace footer and the agy resume line -->
<!-- <img src="../assets/screenshots/antigravity-workspace-final.jpg" alt="Antigravity final message with footer and agy --conversation resume line" width="360" loading="lazy" /> -->

## Permission modes

Headless agy can't stop to ask, so you choose the mode before the run: `/config` → **Permission mode**.

| Mode | What Antigravity can do |
|---|---|
| **Workspace** (default) | Read, create and edit files in the project. No shell, web, MCP or subagent tools |
| **Full access** | Everything: every tool is approved, including shell commands, network access and files outside the project |
| **Ask me** · soon | Not available yet. Approval buttons arrive in a later 0.36.1 release |
| **Plan first** · soon | Not available yet |

!!! warning "After switching to Full access, send `/new`"
    agy keeps a conversation on the tools it started with. A conversation that began in **Workspace** keeps its
    no-shell tools after you switch the chat to **Full access**, even though the footer says `full access`.

    **Send `/new`, then your message.** Full access applies from the next new conversation. The `/config` toast says
    the same: `Permission mode: full access — send /new to start using it`.

Switching Full access → Workspace applies from your next message. A conversation that began in Full access keeps
agy's default tools, though, so send `/new` there too for a clean Workspace conversation.

<!-- TODO: capture screenshot: antigravity-permission-mode — /config → Permission mode page for Antigravity, with Workspace / Full access, the "· soon" modes and the "send /new" warning line -->
<!-- <img src="../assets/screenshots/antigravity-permission-mode.jpg" alt="Antigravity Permission mode page with the send /new warning" width="360" loading="lazy" /> -->

<!-- TODO: capture screenshot: antigravity-full-access-toast — the toast after picking Full access: "Permission mode: full access — send /new to start using it" -->
<!-- <img src="../assets/screenshots/antigravity-full-access-toast.jpg" alt="Toast after picking Full access telling you to send /new" width="360" loading="lazy" /> -->

To set a default for every Antigravity chat, use `permission_mode` under `[antigravity]`
(see [Configuration](#configuration)). A chat's own `/config` choice wins over it.

### What Workspace can and can't do

Workspace runs use a no-shell agent that Untether manages. Its tools are file tools only.

| | In Workspace |
|---|---|
| Read, search, create and edit files in the project | Yes |
| Run a shell command (tests, `git`, a build) | No. Antigravity says in plain words that this needs Full access; nothing runs |
| Delete or rename a file | No. A "rename" is a copy: the new file is written and the original stays |
| Web fetches, MCP tools, subagents | No |
| Files outside the project | Blocked by agy, with a ⚠️ notice |

When agy blocks something, the progress message shows a `⚠️ Blocked` row, the turn stops there, and the reply ends
with how to allow it:

!!! untether "Untether"
    ⚠️ Antigravity was blocked from using a file outside the project — headless runs can't ask for approval, so it
    stopped there. To allow it: /config → Permission mode → Full access, then /new.

<!-- TODO: capture screenshot: antigravity-blocked-outside-project — a Workspace run with a "⚠️ Blocked: file outside the project" row and the blocked notice at the end of the reply -->
<!-- <img src="../assets/screenshots/antigravity-blocked-outside-project.jpg" alt="Antigravity blocked from using a file outside the project" width="360" loading="lazy" /> -->

!!! warning "Workspace is not a sandbox"
    Workspace limits the tools the model has. It doesn't stop a run writing a file that something else executes
    later, and it doesn't stop agy's own hooks, plugins or project MCP servers (see
    [Project config Untether warns about](#project-config-untether-warns-about)). Details:
    [Security](security.md#antigravity-permission-modes-and-project-config).

**The managed agent file.** Untether keeps the no-shell agent at
`~/.gemini/config/agents/untether-workspace/agent.md` and rewrites it if it changes, so don't edit it. It does
nothing for your own interactive `agy` unless you pick that agent there.

**Projects with their own agy agents or plugins.** If the project has an `agents`, `agents.json`, `plugins` or
`plugins.json` entry under `.agents/`, Untether doesn't use its agent there, and Workspace runs on agy's default
agent. That agent has a shell tool which agy then blocks, so a shell attempt ends the turn with a
`⚠️ Antigravity was blocked from using a shell command…` notice. See
[Troubleshooting](troubleshooting.md#antigravity-workspace-agent-file-unavailable).

More detail on modes, allow rules and agy's own settings:
[Antigravity CLI — Permission mode](interactive-approval.md#antigravity-cli-permission-mode).

## Models and effort

- **Model.** `/model set <id>` sets the chat's model and `/model clear` goes back to the default. Run `agy models`
  on the host for the list of ids. Prefer a base id: one that already names its effort (ending `-low`, `-medium`
  or `-high`) fixes the level.
- **Effort.** `/config` → **Effort** offers Low, Medium and High, narrowed to what the chat's model accepts. A
  Gemini Pro id, for example, offers Low and High only, and a fixed-effort model shows no level buttons.

If a saved level isn't one the model accepts, the run drops it and shows
`⚠️ Effort <level> isn't available for <model>, so agy used its default`. See
[Antigravity effort](model-reasoning.md#antigravity-effort).

<!-- TODO: capture screenshot: antigravity-effort-limited — /config → Effort page for a model with limited levels (a Gemini Pro id showing Low and High only) -->
<!-- <img src="../assets/screenshots/antigravity-effort-limited.jpg" alt="Effort page for an Antigravity model that offers Low and High only" width="360" loading="lazy" /> -->

## Usage and quota

Send `/usage` in an Antigravity chat to see agy's quota groups (for example a 5-hour and a weekly limit per model
group, each with a bar, the percentage used and the reset time), then the last session's token totals. Checking
costs no tokens.

- Footers show tokens for the run; there is no dollar cost and no quota footer.
- `[cost_budget]` limits count dollars, so they never trip for Antigravity.
- When a quota is used up, the run ends with `This Antigravity quota is used up. /usage shows when each group
  resets`, and Untether doesn't retry.

Sample output: [Cost budgets → Antigravity](cost-budgets.md#antigravity).

<!-- TODO: capture screenshot: antigravity-usage-quota — /usage in an Antigravity chat showing the quota groups with bars and reset times, then the last session's tokens -->
<!-- <img src="../assets/screenshots/antigravity-usage-quota.jpg" alt="/usage in an Antigravity chat showing agy quota groups" width="360" loading="lazy" /> -->

## Scheduled runs

Cron and webhook runs work with Antigravity, with two rules that differ from chat:

- **They never inherit Full access.** A cron gets it only from its own `permission_mode = "full"`. A webhook run
  never does. Otherwise the run is Workspace, whatever the chat or `untether.toml` says.
- **They are held when the project's agy config changed** since someone last ran Antigravity in that chat (next
  section). Send any message in the chat to review it; the schedule runs again after that.

```toml
[[triggers.crons]]
id = "nightly-notes"
schedule = "0 2 * * *"
chat_id = -1001234567890
project = "myapp"          # Antigravity needs a project
engine = "antigravity"
permission_mode = "full"   # only if this cron needs shell commands
prompt = "Update docs/notes.md from today's commits."
```

See [Schedule tasks](schedule-tasks.md) and [Webhooks and cron](webhooks-and-cron.md).

## Project config Untether warns about

agy runs its own hooks, plugins, custom agents and `.agents/mcp_config.json` MCP servers in **every** mode,
Workspace included, and without a tool call. Untether can't stop them, so it tells you about them:

- **First run in a project that has such config:** a ⚠️ row says the project has agy hooks, plugins or MCP servers
  that Untether doesn't manage. Review them if you didn't add them.
- **A run changed that config** (or files other engines act on, such as `.claude/`, `.mcp.json` or
  `.github/workflows/`): a ⚠️ row after the run asks you to review it before sending another message.
- **Scheduled runs** are refused while the config differs from what an attended run last showed, or while Untether
  can't finish the check.

This is a tripwire with known limits, not isolation. The limits are listed in the
[runner reference](../reference/runners/antigravity/runner.md#agy-hooks-plugins-and-mcp-servers).

## Not supported yet

- Approval buttons, plan approval and question buttons (**Ask me** and **Plan first** arrive in a later 0.36.1
  release). `/planmode` is a Claude command.
- `/steer` and live sessions: each message is its own agy run.
- Images as image input: a Telegram photo reaches agy as a saved file path.
- Subagent detail: a subagent shows as one row.

The full comparison with other engines is in the
[engine compatibility matrix](../reference/runners/index.md).

## If something goes wrong

- [Antigravity says it isn't signed in](troubleshooting.md#antigravity-says-it-isnt-signed-in)
- [Antigravity says it can't run shell commands after I switched to Full access](troubleshooting.md#antigravity-says-it-cant-run-shell-commands-after-i-switched-to-full-access)
- [Antigravity was blocked from using a shell command or a file outside the project](troubleshooting.md#antigravity-was-blocked-from-using-a-shell-command-or-a-file-outside-the-project)
- [Antigravity seems stuck after answering](troubleshooting.md#antigravity-seems-stuck-after-answering)
- [A scheduled Antigravity run was refused because its workspace config changed](troubleshooting.md#a-scheduled-antigravity-run-was-refused-because-its-workspace-config-changed)
- ["Antigravity needs a project"](troubleshooting.md#antigravity-needs-a-project)

Everything else is under [Troubleshooting → Antigravity CLI](troubleshooting.md#antigravity-cli), and each message
is listed in the [error reference](../reference/errors.md#antigravity-messages).

## Configuration

All three keys are optional.

=== "untether config"

    ```sh
    untether config set antigravity.model "gemini-3.8-flash"
    untether config set antigravity.permission_mode "workspace"
    ```

=== "toml"

    ```toml
    [antigravity]
    model = "gemini-3.8-flash"     # passed to agy as the model; ids from `agy models`
    cmd = "~/.local/bin/agy"       # default: agy on PATH, then ~/.local/bin/agy
    permission_mode = "workspace"  # workspace (default) | full
    ```

`permission_mode = "full"` makes Full access the default for every Antigravity chat that hasn't picked a mode.
Chats with an existing Workspace conversation still need `/new`. Sign-in is configured in agy and the service
environment, not here. Full key reference: [Config reference](../reference/config.md#antigravity).

## Related

- [Switch engines](switch-engines.md#antigravity-cli) — install, sign-in routes and the Google terms note
- [Interactive approval](interactive-approval.md#antigravity-cli-permission-mode) — permission modes in detail
- [Security](security.md#antigravity-permission-modes-and-project-config) — what each mode does and doesn't protect
- [Antigravity runner reference](../reference/runners/antigravity/runner.md) — flags, errors, log events
