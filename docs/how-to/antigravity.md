# Use Antigravity CLI

Antigravity CLI (`agy`) is Google's successor to Gemini CLI. From v0.36.1 Untether runs it as its own `antigravity`
engine: send a task from Telegram, watch the progress, and resume the conversation later from chat or a terminal.

It suits you if you want Google's Gemini models (and the Claude and GPT models some Antigravity plans include) next
to your other engines, and you're happy to choose what the agent may do **before** a run starts. Antigravity has no
approval buttons yet, so Untether runs it with a pre-run permission mode instead. If you need to approve each tool
call from your phone, use Claude Code for that work.

## Before you start

You need:

- **agy 1.3.1 or newer** on the machine that runs Untether (tested on 1.3.4). An older agy is refused before
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
  up the most recent conversation run from this project's own directory, including one you started in a terminal
  there. It never picks up a conversation from a parent or child folder: if this folder has none, Untether says so
  and starts nothing. See [Cross-environment resume](cross-environment-resume.md).

agy sends one answer per run. If it started a background command or a timer, it holds the answer until that
finishes (up to 30 minutes) and the progress message shows `⏳ Antigravity is waiting for 1 background task …`.
That isn't a stall.

**Background work.** Commands still running in the background, timers and subagents are listed in a
`⏳ background (N)` block in the progress message, one row each with its elapsed time, and `/ping` counts them.
In practice the rows appear in Full access, because Workspace has no shell, timer or subagent tool. A command that
agy starts as a daemon (a dev server, say) can keep running after the answer; Untether doesn't reliably stop it, so
check for it and stop it yourself when you're done. Details:
[Background tasks and subagents](../reference/runners/antigravity/runner.md#background-tasks-and-subagents-in-the-status-panel).

**An answer held for background work.** agy sends its one answer only when its background work has finished (up to
30 minutes), and nothing of it shows before then, so the block ends with `agy sends its answer when this work
finishes (up to 30 min)`. If you `/cancel` during that wait, agy doesn't hand over the answer it was holding; the
cancelled message says so. The conversation keeps it, though: send a message such as "repeat your last answer" to see
it.

**After `/cancel`.** The output of a turn you cancelled can appear at the start of agy's next reply in the same
conversation. That is how agy behaves (seen on agy 1.3.3), not something Untether adds; send `/new` if you want a
clean start.

<!-- TODO: capture screenshot: antigravity-workspace-final — final message of a Workspace run with the model · effort · workspace footer and the agy resume line -->
<!-- <img src="../assets/screenshots/antigravity-workspace-final.jpg" alt="Antigravity final message with footer and agy --conversation resume line" width="360" loading="lazy" /> -->

## Permission modes

Headless agy can't stop mid-run to ask by itself, so you choose the mode before the run: `/config` →
**Permission mode**. **Ask me** adds approval buttons through Untether's own agy plugin.

| Mode | What Antigravity can do |
|---|---|
| **Workspace** (default) | Read, create and edit files in the project. No shell, web, MCP or subagent tools |
| **Full access** | Everything: every tool is approved, including shell commands, network access and files outside the project |
| **Plan first** | Read the project and write a plan, changing nothing. You get **✅ Approve Plan / ❌ Deny / 💬 Let's discuss**; Approve carries the plan out in Workspace mode. Also `/planmode on` / `off` ([how it works](interactive-approval.md#antigravity-plan-first)) |
| **Ask me** | Read and edit files inside the project on its own; ask you in Telegram, with the command or a diff, before shell commands, web, MCP, subagents, timers and files outside the project. Needs [Untether's agy plugin](#telegram-questions-and-steer) on the host ([more below](#ask-me)) |

!!! warning "After switching to Full access or Ask me, send `/new`"
    agy keeps a conversation on the tools it started with. A conversation that began in **Workspace** (or **Plan
    first**) keeps its no-shell tools after you switch the chat to **Full access** or **Ask me**, even though the
    footer says `full access` / `ask me` — so in Ask me it has no shell to ask you about.

    **Send `/new`, then your message.** The new mode applies from the next new conversation. The `/config` toast
    says the same: `Permission mode: ask me — send /new if you're switching from Workspace or Plan first (not from Full access)`.

**Switching back to Workspace needs `/new` too.** A conversation that began in Full access keeps its shell tool after
you switch to Workspace. When it reaches for the shell, Workspace blocks it and the turn ends on a ⚠️ notice that
says to send `/new` (or pick Full access again). The same goes for Ask me → Workspace and for either → Plan first.
The toasts say so: `Permission mode: workspace — send /new if you're switching from Full access or Ask me (not from Plan first)` (and the
same for plan first), and **Clear override** says `Permission mode: cleared — send /new if this switched between
Workspace / Plan first and Ask me / Full access`. Moving between Workspace and Plan first, or between Ask me and Full
access, needs no `/new`: each pair uses the same tools.

<!-- TODO: capture screenshot: antigravity-permission-mode — /config → Permission mode page for Antigravity, with Workspace / Full access / Ask me / Plan first and the "send /new" warning line -->
<!-- <img src="../assets/screenshots/antigravity-permission-mode.jpg" alt="Antigravity Permission mode page with the send /new warning" width="360" loading="lazy" /> -->

<!-- TODO: capture screenshot: antigravity-full-access-toast — the toast after picking Full access: "Permission mode: full access — send /new if you're switching from Workspace or Plan first (not from Ask me)" -->
<!-- <img src="../assets/screenshots/antigravity-full-access-toast.jpg" alt="Toast after picking Full access telling you to send /new" width="360" loading="lazy" /> -->

To set a default for every Antigravity chat, use `permission_mode` under `[antigravity]`
(see [Configuration](#configuration)). A chat's own `/config` choice wins over it.

### Ask me

Ask me ([#980](https://github.com/littlebearapps/untether/issues/980)) is Antigravity's equivalent of Claude Code's
approval buttons. agy runs with all its tools, and Untether's agy plugin holds each tool call until Untether answers:

- **Runs on its own:** reading, listing and searching files inside the project; creating and editing files there
  (and agy's own notes for the conversation).
- **Asks you first:** reading files outside the project, shell commands, web fetches and searches, MCP tools, browser actions, subagents, timers, files
  outside the project, and protected files inside it at any depth (`.agents/`, `.git/`, `.claude/`, `.codex/`,
  `.opencode/`, `.vscode/`, `.github/workflows/`, `.husky/`, `.env` files, `.envrc`, keys, `CLAUDE.md`, `AGENTS.md`
  and similar files other tools run or read as instructions). A nested checkout (a folder with its own `.git`, such
  as a submodule) and Untether's own `@branch` worktrees in `.worktrees/` count as outside the project. So does
  any tool agy adds that Untether doesn't know yet, and a search whose text starts with `-`.

The card shows what would run: `$ git status` with its folder for a shell command (and a warning when it would start
a daemon that keeps running after the turn), the lines added and removed for an edit, each subagent's task for a
subagent, and every argument of any other tool in full — the tool's own first (a web page's address), with ⚠️ on
anything agy's tool doesn't have, on a value hidden because its name looks like a secret, and on anything too long
to show whole. agy's own one-line description comes last as `agy says: …`. Tap **✅ Approve** or **❌ Deny**; for a shell
command, **✅ Always allow this command here** lets that exact command (in that folder) run without asking for the
rest of the conversation. A Deny, or no answer within 15 minutes, blocks the tool and agy is told not to retry it;
the row reads `blocked: denied in Telegram`. A subagent's own tool calls ask too, labelled with the subagent's role.

<!-- TODO: capture screenshot: antigravity-ask-me-card — an Ask me approval card for `$ git status` with Approve / Deny / Always allow this command here -->
<!-- <img src="../assets/screenshots/antigravity-ask-me-card.jpg" alt="Antigravity Ask me approval card for a shell command" width="360" loading="lazy" /> -->

Ask me needs [Untether's agy plugin](#telegram-questions-and-steer) on the host. Until it is installed, the button
reads **🔒 Ask me — set up** and opens the plugin's status page. Untether checks the plugin before every Ask me run and
**refuses the run** (nothing starts) when:

- the plugin isn't installed, is disabled (`agy plugin disable`), was edited, or agy doesn't load it for the project;
- the project contains Untether's own code or config (for example a checkout of Untether itself, or the folder
  holding `untether.toml`), which agy could edit to switch approvals off;
- agy loads hooks, plugins, agents or MCP servers there that Untether doesn't manage (in the project's `.agents/`
  folder or your own `~/.gemini/config/`). They run their own commands without asking, so Ask me can't promise to
  ask. An empty `mcp_config.json` is fine: Untether asks agy (`agy mcp list`) whether any server is configured.

Workspace and Full access still work in those projects. To shorten the wait for an answer, set
`approval_timeout_s` under `[antigravity]` (60 s or more, never longer than the plugin's own wait).

### What Workspace can and can't do

Workspace runs use a no-shell agent that Untether manages. Its tools are file tools, plus `ask_question`.

| | In Workspace |
|---|---|
| Read, search, create and edit files in the project | Yes |
| Run a shell command (tests, `git`, a build) | No. Antigravity says in plain words that this needs Full access; nothing runs |
| Delete or rename a file | No. A "rename" is a copy: the new file is written and the original stays |
| Web fetches, MCP tools, subagents | No |
| Files outside the project | Blocked by agy, with a ⚠️ notice |
| Ask you a question | Yes, as buttons with [Untether's agy plugin](#telegram-questions-and-steer); without it the question is skipped and agy carries on |

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

## Telegram questions and steer

With **Untether's agy plugin** installed on the host, an Antigravity run can:

- **Ask you a question.** When agy calls its `ask_question` tool, you get the same question card as Claude Code:
  option buttons, or **Other (type reply)**, or just reply with text. agy carries on with your answer. Turn it off
  per chat with `/config` → **Ask mode**.
- **Take a message while it works.** With `/config` → **Follow-up** set to **steer** (or `/steer <text>`), a message
  you send during a run goes into that run before agy's next step, and the progress message shows
  `↪️ steer received: …`. If the run ends before agy takes another step, you get
  `⚠️ The session ended before this message ran — please send it again.`

It works in every permission mode; in Workspace, Plan first and Full access nothing about what agy may do
changes. **Ask me** needs it: the plugin is what holds each tool call for your approval. Without the plugin,
Antigravity works as before: questions are skipped (agy carries on with its own default), follow-ups queue, and Ask
me is refused.

**Install it on the host** (not from Telegram — it is shared by every agy on that machine):

```sh
untether agy-gate install          # via agy's own `agy plugin install`
untether agy-gate status           # what's installed
untether agy-gate status --project ~/my-project   # does agy load it there? (agy -p /hooks, no model turn)
untether agy-gate uninstall
```

Install writes only `~/.gemini/config/plugins/untether-gate/` (`plugin.json`, `hooks.json` and Untether's
`.untether-managed` record). Its hooks start with a check for a marker that only Untether-started agy runs carry, so
your own `agy` sessions never run any Untether code. Install and uninstall refuse to touch a plugin folder that
Untether didn't install or that was edited since (`--force` overrides). No restart is needed: Untether checks the
plugin before each run (cached for an hour per project). `/config` → **Permission mode** → **🔐 Telegram
approvals** shows the same status, read-only, with a **Re-check** button.

**Scheduled runs** never show a question: cron and webhook runs answer it themselves ("nobody can answer
questions… proceed with reasonable defaults").

**If the plugin folder was changed** after install, attended runs show a ⚠️ row and run without questions or
steer, and scheduled runs are held, because its hooks run their own commands in every agy session on the host.
Run `untether agy-gate status`, then `untether agy-gate install --force` if you didn't change it.

**After upgrading Untether, run `untether agy-gate install` again** whenever the new version changes the plugin's
files — 0.36.1rc4 does (its hooks now block a tool themselves when Untether's Python can't start). Until you do,
`untether agy-gate status` says `plugin version 1 from an earlier Untether`, Ask me is refused
("the plugin is from another Untether version — run `untether agy-gate install` on the host to update it"), attended runs show `⚠️ Untether's agy plugin (untether-gate) is from another Untether version …` and run
without questions or steer, and scheduled agy runs are held.

## Models and effort

- **Model.** `/model set <id>` sets the chat's model and `/model clear` goes back to the default. Run `agy models`
  on the host for the list of ids. Prefer a base id: one that already names its effort (ending `-low`, `-medium`
  or `-high`) fixes the level.
- **Effort.** `/config` → **Effort** offers Low, Medium and High, narrowed to what the chat's model accepts. A
  Gemini Pro id, for example, offers Low and High only, and a fixed-effort model shows no level buttons.

If a saved level isn't one the model accepts, the run drops it and shows
`⚠️ Effort <level> isn't available for <model>, so agy used its default`. See
[Antigravity effort](model-reasoning.md#antigravity-effort).

**Context use.** With a model set for the chat, the first line of a progress message and answer can end with
`~3% ctx`: the tokens the model last read, as a share of the context window Google publishes for that model. It
is an estimate. agy doesn't report a window, and it compacts a conversation on its own schedule without saying so,
so a low figure doesn't mean compaction is far off. With no model set (agy's default), or a model Untether has no
published figure for, no percentage is shown. Details:
[Context use](../reference/runners/antigravity/runner.md#context-use-n-ctx).

<!-- TODO: capture screenshot: antigravity-effort-limited — /config → Effort page for a model with limited levels (a Gemini Pro id showing Low and High only) -->
<!-- <img src="../assets/screenshots/antigravity-effort-limited.jpg" alt="Effort page for an Antigravity model that offers Low and High only" width="360" loading="lazy" /> -->

## Usage and quota

Send `/usage` in an Antigravity chat to see agy's quota groups (for example a 5-hour and a weekly limit per model
group, each with a bar, the percentage used and the reset time), your remaining AI credits, then the last
session's token totals. Checking costs no tokens.

- Footers show tokens for the run; there is no dollar cost.
- A quota line can follow an answer. Turn it on in `/config` → **Cost & usage** → **Subscription usage** to see
  `⚡ Gemini Models 5h: 12% | wk: 3%` after each run; with it off, a line appears only once a limit reaches 70 %.
  The line is taken from your last `/usage`, for up to 15 minutes, so it never slows an answer down. Send `/usage`
  again for current numbers.
- `/stats auth` shows which sign-in route agy is set to use, for up to an hour after an Antigravity run (nothing
  straight after a restart). It doesn't start agy or test the sign-in.
- `[cost_budget]` limits count dollars, so they never trip for Antigravity.
- When a quota is used up, the run ends with `This Antigravity quota is used up. /usage shows when each group
  resets`, and Untether doesn't retry.

Sample output: [Cost budgets → Antigravity](cost-budgets.md#antigravity).

<!-- TODO: capture screenshot: antigravity-usage-quota — /usage in an Antigravity chat showing the quota groups with bars and reset times, then the last session's tokens -->
<!-- <img src="../assets/screenshots/antigravity-usage-quota.jpg" alt="/usage in an Antigravity chat showing agy quota groups" width="360" loading="lazy" /> -->

## Scheduled runs

Cron and webhook runs work with Antigravity, with three rules that differ from chat:

- **They never inherit Full access or Ask me.** A cron gets either only from its own `permission_mode`
  (`"full"` or `"ask"`). A webhook run never gets Full access. Otherwise the run is Workspace, whatever the chat or
  `untether.toml` says. A cron with `permission_mode = "ask"` has nobody to tap Approve: reads and in-project edits
  run, everything that would ask is denied at once, and the answer ends with a `🔒 unattended … nobody to approve`
  footer.
- **They are held when the project's agy config changed** since someone last ran Antigravity in that chat (next
  section). The next message someone sends in the chat runs with the changed config and shows what changed in
  a ⚠️ row; the schedule runs again after that. An `/at` or `/loop` run doesn't release it. Check the files the
  message lists first if you didn't change them. When the change is to Untether's own agy plugin, the message says
  so, and only reinstalling it on the host (`untether agy-gate install --force`) releases the hold.
- **A Workspace run is held while agy's own tool-permission setting can't be read.** Untether checks it with
  `agy -p /config` before the run; if that fails or returns no value, the scheduled run replies `Untether couldn't
  read agy's tool-permission setting before this run …` and nothing starts. Nothing needs changing: the next fire
  checks again. A run you start from the chat goes ahead with a ⚠️ row instead, as long as agy reports the setting
  when it starts.

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
  `.github/workflows/`): a ⚠️ row after the run asks you to review it before sending another message. Claude
  Code's worktrees in `.claude/worktrees/` aren't part of this check: each one is its own checkout.
- **Scheduled runs** are held while the config differs from what a run someone started from the chat last showed,
  or while Untether can't finish the check. A changed config is released by the next message a person sends in
  the chat; a check that can't finish (too many files, or a config file Untether can't read with certainty) holds
  them until it succeeds.

This is a tripwire with known limits, not isolation. The limits are listed in the
[runner reference](../reference/runners/antigravity/runner.md#agy-hooks-plugins-and-mcp-servers).

## Not supported yet

- Ask me in a project with agy config Untether doesn't manage: there is no "trust this config" option yet.
  Approval buttons (**Ask me**), question buttons and `/steer` need [Untether's agy plugin](#telegram-questions-and-steer).
- Live sessions: each message is its own agy run (a steer goes into the run that is in flight).
- Images as image input: a Telegram photo reaches agy as a saved file path.
- Subagent detail: each subagent gets a row in the `⏳ background` block, but its own steps, output and token use
  aren't shown, and a subagent that failed looks the same as one that finished.
- `/export` after a restart: the export comes from Untether's own memory of the session, so it is empty once
  Untether restarts and never includes turns run in a terminal.

The full comparison with other engines is in the
[engine compatibility matrix](../reference/runners/index.md).

## If something goes wrong

- [Antigravity says it isn't signed in](troubleshooting.md#antigravity-says-it-isnt-signed-in)
- [Antigravity says it can't run shell commands after I switched to Full access](troubleshooting.md#antigravity-says-it-cant-run-shell-commands-after-i-switched-to-full-access)
- [Antigravity was blocked from using a shell command or a file outside the project](troubleshooting.md#antigravity-was-blocked-from-using-a-shell-command-or-a-file-outside-the-project)
- [Antigravity seems stuck after answering](troubleshooting.md#antigravity-seems-stuck-after-answering)
- [A scheduled Antigravity run was refused because its workspace config changed](troubleshooting.md#a-scheduled-antigravity-run-was-refused-because-its-workspace-config-changed)
- [A scheduled Antigravity run says it couldn't read agy's tool-permission setting](troubleshooting.md#a-scheduled-antigravity-run-says-it-couldnt-read-agys-tool-permission-setting)
- ["Antigravity needs a project"](troubleshooting.md#antigravity-needs-a-project)

Everything else is under [Troubleshooting → Antigravity CLI](troubleshooting.md#antigravity-cli), and each message
is listed in the [error reference](../reference/errors.md#antigravity-messages).

## Configuration

All four keys are optional.

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
    permission_mode = "workspace"  # workspace (default) | ask | plan | full
    approval_timeout_s = 900       # Ask me: how long a card waits (60 s or more)
    ```

`permission_mode = "full"` makes Full access the default for every Antigravity chat that hasn't picked a mode.
Chats with an existing Workspace conversation still need `/new`. Sign-in is configured in agy and the service
environment, not here. Full key reference: [Config reference](../reference/config.md#antigravity).

## Related

- [Switch engines](switch-engines.md#antigravity-cli) — install, sign-in routes and the Google terms note
- [Interactive approval](interactive-approval.md#antigravity-cli-permission-mode) — permission modes in detail
- [Security](security.md#antigravity-permission-modes-and-project-config) — what each mode does and doesn't protect
- [Antigravity runner reference](../reference/runners/antigravity/runner.md) — flags, errors, log events
