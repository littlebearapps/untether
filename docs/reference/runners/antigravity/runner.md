# Antigravity CLI runner

The `antigravity` engine runs Google's Antigravity CLI (`agy`) for one message at a time and streams its progress to
Telegram. It is a separate engine from the deprecated `gemini` engine, with its own id, sign-in, flags and session
format. Built on [#766](https://github.com/littlebearapps/untether/pull/766) by
[@manuelnaranjo](https://github.com/manuelnaranjo); tracked in
[#558](https://github.com/littlebearapps/untether/issues/558).

!!! warning "Read this before using Antigravity with a Google account sign-in"
    Google's [Antigravity terms](https://antigravity.google/terms) say: "You must not abuse, harm, interfere with, or
    disrupt the Service. This includes, but is not limited to, using the Service in connection with products not
    provided by us. Using third party software, tools, or services to access the Service (e.g. using OpenClaw with
    Antigravity OAuth) is a breach of this Agreement. Such actions may be grounds for suspension or termination of
    your Antigravity and/or Gemini CLI accounts."

    The [Antigravity FAQ](https://antigravity.google/docs/faq) adds: "Using third-party software, tools, or services
    to access Antigravity is a violation of our Terms of Service and severely degrades the experience for legitimate
    product users. Such actions can result in suspension or termination of your account. To use a third-party coding
    agent with Gemini, we recommend using a Gemini Enterprise or Google AI Studio API key."

    Untether runs Google's own `agy` binary on your machine and never reads or reuses your Google credentials, but it
    is still third-party software driving the CLI, and Google does block accounts (agy shows an appeal link when it
    happens). **We recommend a Gemini API key or an Enterprise sign-in** (see [Sign-in routes](#sign-in-routes)). If
    you use a Google account sign-in anyway, you accept that risk for that account. Google's own
    [headless docs](https://antigravity.google/docs/cli/headless) do describe scripted and CI use of `agy -p`. This
    isn't legal advice.

## Versions and platforms

| | |
|---|---|
| Tested against | agy **1.3.3** (fixtures captured on 1.3.1 and 1.3.2; the drift probes pass on 1.3.3) |
| Minimum | agy **1.3.1**. An older agy is refused before anything starts: `🛑 Antigravity CLI <version> is older than 1.3.1, which this Untether version needs. Run agy update on the host, then retry.` |
| Tested platform | Linux x86_64 with a file-stored sign-in token. macOS, Linux hosts with a desktop keyring, and ARM64 are untested |

- The version check runs `agy --version` once per agy binary (cached until the file changes). If the version can't
  be read, the run goes ahead: a slow or odd `--version` never blocks a run.
- agy updates itself in place, roughly every 15 minutes. To pin a version, install it into a directory the service
  user can't write. The version in use is logged as `agy_version` on every `antigravity.session.started` line, and
  a version newer than the tested one logs `antigravity.version.newer_than_probed`.
- agy doesn't need to be installed on every host. Where it's missing, the startup message lists `antigravity`
  under "not installed".

## Install and select

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Untether looks for `agy` on `PATH`, then at `~/.local/bin/agy`. Set `cmd` under `[antigravity]` to use another path.
Select the engine with `/agent set antigravity`, or for one message with the `/antigravity` directive.

## Sign-in routes

Untether never signs agy in. Pick a route on the host; the first two avoid the terms problem above.

1. **Gemini API key.** Set `"modelProvider": "gemini"` in agy's settings file
   (`~/.gemini/antigravity-cli/settings.json`) **and** put `GEMINI_API_KEY` in Untether's environment. The
   environment variable alone does nothing, and agy ignores `GOOGLE_API_KEY` and `.env` files. `GOOGLE_GEMINI_BASE_URL`
   is optional. Google bills this route per token.
2. **Gemini Enterprise / Google Cloud.** Set `AGY_ADC_AUTH=true` with `GOOGLE_CLOUD_QUOTA_PROJECT` and
   `GOOGLE_CLOUD_LOCATION`, using Application Default Credentials. `agy --project` names an Antigravity project (a
   workspace grouping), not a Google Cloud project, and isn't a sign-in setting.
3. **Google account.** Run `agy` once in a terminal on the host and finish the sign-in (over SSH, paste the code).
   agy stores the token in the OS keyring, or in a file under `~/.gemini/antigravity-cli/` when no keyring is
   reachable. On a Linux desktop with a keyring, Untether must be able to see `DBUS_SESSION_BUS_ADDRESS`. Read the
   warning at the top of this page first.

Untether passes agy a filtered environment: the standard [engine allowlist](../../../how-to/security.md#engine-subprocess-env-allowlist)
plus four agy-only names (`DBUS_SESSION_BUS_ADDRESS`, `AGY_ADC_AUTH`, `GOOGLE_CLOUD_QUOTA_PROJECT`,
`GOOGLE_GEMINI_BASE_URL`) and anything you add with `[security] env_extra_allow`. `GEMINI_API_KEY`,
`GOOGLE_CLOUD_LOCATION` and `XDG_RUNTIME_DIR` are already on the standard list. Proxy variables such as
`HTTPS_PROXY` and `NO_PROXY` are not: add them with `env_extra_allow` if agy needs them. `NO_COLOR=1` is set unless
you set it yourself.

### One-time notice in chat

On a host where agy uses a Google account sign-in, the first successful Antigravity answer in each chat ends with:

> ⚠️ This host signs Antigravity in with a Google account. Google's Antigravity terms say third-party tools such as
> Untether mustn't use that sign-in, and Google may suspend the account. A Gemini API key or Enterprise sign-in avoids
> this: https://littlebearapps.com/help/untether/switch-engines/#antigravity-cli (Shown once in this chat.)

Untether decides this from agy's own `-p /config` report (`modelProvider: "gemini"` means the API-key route) and
from `AGY_ADC_AUTH=true`. If that check fails, the host is treated as a Google sign-in host, so an API-key host can
see the notice once. Shown chats are remembered in `antigravity_notices.json` beside `untether.toml`.

## How a run is started

One `agy` process per message:

```text
agy --input-format stream-json --output-format stream-json --print-timeout 0 --disable-slash-commands
    [--conversation=<id>] [--model=<model>] [--effort=<level>]
    (--mode=accept-edits --agent=untether-workspace | --dangerously-skip-permissions)
```

- **The prompt goes on stdin**, as one line `{"event":"user","message":{"content":"…"}}`, and stdin is then closed.
  It is never on the command line, so it can't be read from the process list or mistaken for a flag.
- `--disable-slash-commands` makes a prompt that starts with `/` an ordinary message. Untether adds no prefix.
- `--print-timeout 0` is agy's default (no limit) made explicit. If agy ever hits a print timeout it reports
  success with a partial answer, so Untether logs `antigravity.print_timeout` when it sees that line.
- The last arguments are the permission mode: `--mode=accept-edits --agent=untether-workspace` for Workspace,
  `--dangerously-skip-permissions` only in Full access (see [Permission modes](#permission-modes)). Both Workspace
  values are fixed; nothing a user types reaches `--mode` or `--agent`.
- **Values are always joined to their flag** (`--model=<model>`, `--conversation=<id>`, `--effort=<level>`), and
  each is checked first. agy reads a separate value that starts with a dash as another flag, so Untether never
  passes one as its own argument. A model id may use ASCII letters, digits and `. _ - : / @ +`, must start with a
  letter or digit and be at most 128 characters; a conversation id may use letters, digits, `_` and `-`; effort is
  one of agy's level names. A model or conversation id that fails the check is refused before agy starts
  (`antigravity.argv.invalid_value` in the logs, without the raw value):

    > 🛑 That model id isn't valid for Antigravity, so nothing was started. A model id uses letters, digits and
    > . _ - : / only, with no spaces and no leading dash (for example gemini-3.8-flash). Set one with /model set,
    > or go back to the default with /model clear.

    > 🛑 That Antigravity conversation id isn't valid, so nothing was started. Send /new to start a fresh
    > conversation.

### Resume

Final messages end with the resume line:

```text
`agy --conversation <conversation_id>`
```

That line is what you copy into a terminal. Replying to it (or chat mode) resumes the conversation; Untether
itself passes the id to agy in the joined form `--conversation=<id>`.

### `/continue`

`/continue` resumes the conversation agy last ran from **this project's own directory**, including one you started
in a terminal there. Untether doesn't pass agy's `--continue`: on agy 1.3.3 that flag also resumes a conversation
from a parent or a child directory, and Untether's projects are often nested
([#1039](https://github.com/littlebearapps/untether/issues/1039)). It reads the one file where agy records the last
conversation per directory, `~/.gemini/antigravity-cli/cache/last_conversations.json`, takes the entry for the run
directory and passes `--conversation=<id>`.

- **The path must match exactly.** The run directory is resolved to its real path (absolute, symlinks resolved, no
  trailing slash) and compared with agy's entries as written. A parent, child or sibling directory never matches,
  and letter case is never folded. An entry agy recorded under a symlinked spelling of the directory doesn't match
  either; start the conversation from the real path.
- **No entry, no run.** Untether replies `No Antigravity conversation for this project yet — send a normal message
  to start one.` and agy isn't started.
- **A conversation listed under two directories isn't resumed.** After a cross-directory resume agy writes the
  other directory into that file too, so an entry means "last run from here", not "started here". When this
  directory's conversation id also appears under any other path, Untether can't tell which project it belongs to
  and replies `Antigravity's most recent conversation in this folder has also been run from another folder …`.
  This is what an earlier 0.36.1 pre-release `/continue`, or `agy --continue` in a terminal, can leave behind. It
  clears once either directory has a newer conversation of its own. If the other directory's entry is replaced
  first, the leftover entry is no longer detectable and is resumed as this directory's last conversation.
- **The file is treated as untrusted.** If it is unreadable, not a regular file, over 1 MiB, not JSON, not an
  object of strings, or the entry isn't a conversation id, Untether replies `Untether couldn't read Antigravity's
  record of recent conversations …` and agy isn't started. It never falls back to `--continue`.

A refusal is logged as `antigravity.continue.refused` with a `reason`; none of them changes the chat's saved
session. It arrives as a plain reply in place of the progress message, with no `error · antigravity` header and no
quota line, and the run's `session.summary.no_events` is logged at INFO with `prespawn_blocked=continue_unresolved`
rather than as a WARNING ([#1047](https://github.com/littlebearapps/untether/issues/1047)). To resume a particular
conversation instead, reply to its resume line.

agy's file can still list a conversation that has since been deleted. Untether then stops agy as it does for any
unknown id (below) and replies `The conversation Antigravity had recorded for this project no longer exists, so
nothing was resumed. …`. The id came from agy's record, not from the chat, so the chat's saved session is kept and
the reply carries no resume line. The same holds for every engine: a failed `/continue` never clears the chat's
saved session.

If the conversation id is unknown, agy prints a warning and silently starts a new conversation. Untether stops agy
before your message runs somewhere you didn't pick, and replies: `That Antigravity conversation no longer exists …
send your message again to start a new one.` The chat's saved session is cleared, unless the id came from
`/continue` (above).

### A project is required

agy edits files in its working directory, so Untether refuses to run it without a project, and never in the home
directory, `/` or the bot's own directory:

> Antigravity needs a project — bind this chat with /ctx set … or a [projects.*] entry. It won't run in the bot's
> own directory, because it can edit files there.

## Permission modes

Headless agy can't stop to ask, so the mode is chosen before the run: `/config` → **Permission mode**, or
`permission_mode` under `[antigravity]`. See
[Antigravity CLI — Permission mode](../../../how-to/interactive-approval.md#antigravity-cli-permission-mode) for the
user guide.

| Mode | Value | Flag | What happens |
|---|---|---|---|
| **Workspace** (default) | `workspace` | `--mode=accept-edits` `--agent=untether-workspace` | agy's own accept-edits mode under its `request-review` policy, run as Untether's [no-shell agent](#the-workspace-agent): file edits in the project and temp folders go ahead; the model has no shell, web, MCP or subagent tool; files outside the project are blocked by agy. Not a sandbox. Without `--mode`, headless agy refuses every write outside a temp folder (agy 1.3.3) |
| **Full access** | `full` | `--dangerously-skip-permissions` | Every tool is approved, including shell, network and files outside the project |
| **Ask me** | `ask` | — | Not available yet: refused before agy starts, until Untether's approval gate ships in a later 0.36.1 release |
| **Plan first** | `plan` | — | Not available yet: refused the same way |

Rules the runner enforces:

- **Order of precedence:** the chat or topic setting (or a cron's own `permission_mode`), then
  `[antigravity] permission_mode`, then Workspace.
- **Fails closed.** An unknown stored value runs as Workspace and logs `antigravity.permission_mode.unknown` once.
  An unknown value in `untether.toml` is a config error. `permission_mode = "full"` in `untether.toml` logs
  `antigravity.full_access_from_toml` once.
- **Scheduled runs never inherit Full access.** A cron gets it only from its own `permission_mode = "full"`; a
  webhook run never does. Otherwise the run is Workspace (`antigravity.unattended_full_downgraded`).
- **agy's own settings are checked.** Before a Workspace run Untether asks agy for its settings with a zero-token
  `agy -p /config` (cached for an hour per agy version and settings file). If `toolPermission` is anything other
  than `request-review` or `strict`, the run is refused: `agy's own settings change its permission checks
  (toolPermission: always-proceed), so Untether won't run Workspace mode — set it back to request-review or pick
  Full access in /config.` The same check runs on the `permission_mode` agy reports in its first event, and stops
  agy before any tool.
- **One check may be missing, never both** ([#1044](https://github.com/littlebearapps/untether/issues/1044)). Either check can come back empty: `agy -p /config` can fail
  or time out (a failure is cached for five minutes) or return no `toolPermission`, and agy's first event can lack
  `permission_mode` or be missing altogether.
    - *One missing, someone in the chat:* the run goes ahead on the other check and shows a ⚠️ row saying which one
      was missing (`antigravity.permission_mode.unconfirmed`).
    - *Both missing:* nothing has confirmed the setting, so agy is stopped at its first event (or at its first step
      when there is no first event): `Untether couldn't confirm agy's tool-permission setting: …`
      (`antigravity.permission_mode.unverified`).
    - *Cron or webhook run:* held before agy starts when the `agy -p /config` check alone gave no value
      (`Untether couldn't read agy's tool-permission setting before this run (…), so this scheduled run is held.`),
      because nobody is there to read a ⚠️ row. The next fire checks again. `/at` and `/loop` runs follow the
      attended rule.
    - Full access is unaffected: it doesn't depend on the setting.
- **Widened settings are shown.** `permissions.allow` rules or `allowNonWorkspaceAccess` in agy's settings add a
  ⚠️ row (once per project and settings change), and the footer label becomes
  `workspace (agy allows files outside the project)`.

### The Workspace agent

A blocked tool ends an agy 1.3.x turn, and agy's default agent often opens with a shell command (`ls -la`), so a
Workspace reply used to be nothing but the blocked notice
([#1037](https://github.com/littlebearapps/untether/issues/1037)). agy has no per-run system prompt and no way to
take a tool away from its default agent, but a custom agent's `tools:` list is the model's whole toolset. So:

- Before a Workspace run Untether makes sure `~/.gemini/config/agents/untether-workspace/agent.md` (agy's
  user-level agent folder) holds its agent: file tools only (`view_file`, `list_dir`, `find_by_name`,
  `grep_search`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`) and a short system prompt.
  A changed file is rewritten (`antigravity.workspace_agent.installed`). The file does nothing unless an agy run
  names it.
- The run then passes `--agent=untether-workspace`. Full access never does.
- agy picks an agent by name and also loads agents from a project's `.agents/` folders, so a repo could ship its
  own `untether-workspace`. Untether doesn't try to recognise one, since that would mean reading agent files the way
  agy does. The rule is structural: if any `.agents/` agy loads for the run has an `agents`, `agents.json`, `plugins`
  or `plugins.json` entry (whatever is in it), Untether doesn't pass `--agent` (`reason=project_agents`), and that
  project runs Workspace on agy's default agent as before. Rules, skills, hooks and MCP config don't count. It fails
  closed (`reason=project_unchecked`) when `.agents` is a symlink, isn't a folder, can't be listed or has more than
  2,000 entries. The user-level agent folder is checked by entry name only (nothing under `~/.gemini` is opened): a
  second `untether-workspace` there (`reason=user_agent`), or anything beside Untether's `agent.md` in its folder
  (`reason=user_unchecked`), drops the flag too. Not covered: a user-level agent or plugin that declares the name
  inside a differently named file; that is the user's own configuration, not a project's.
- If the file can't be put in place (unwritable folder, or a symlink anywhere on the path), the run goes ahead
  without `--agent` and logs `antigravity.workspace_agent.unavailable` once per reason. That run behaves as before:
  a shell attempt ends the turn with the blocked notice.
- Asked for a shell command, the agent says it needs Full access; nothing is blocked, so there is no ⚠️ row. It
  can't delete or rename a file either (a "rename" is a copy).

!!! warning "A conversation keeps the agent it started with"
    agy ignores `--agent` on a resume and reports nothing about it in `init`. A conversation that started in
    Workspace has no shell tool even after the chat is switched to Full access, and one that started in Full
    access (or before this change) keeps agy's default agent in Workspace. **After switching Workspace → Full
    access, send `/new`.** The `/config` toast and page, the agent's own text and the blocked notice all say so.
    Untether keeps no per-conversation record of this.

### When agy blocks a tool

A blocked tool doesn't fail in agy's stream: the step ends normally with no output, and the final event lists
`denied_actions`. Untether turns that into a row and an explanation:

- the step becomes `⚠️ Blocked: shell command (RunCommand) — <command>` (also `MCP tool`, `web fetch`,
  `browser action`, `file outside the project`);
- the final reply ends with a paragraph that depends on how the run started, for example in a chat:
  `⚠️ Antigravity was blocked from using a file outside the project — headless runs can't ask for approval, so it
  stopped there. To allow it: /config → Permission mode → Full access, then /new.` A cron is told to set
  `permission_mode = "full"` on the cron; a webhook is told it never gets Full access; in Full access the paragraph
  points at agy's own `permissions.deny` rules. Only a blocked shell command also gets
  `or add an allow rule such as command(git status) to agy's settings file`.

With the Workspace agent a shell block is rare: it needs a conversation that began without the agent. Exact
commands are the reliable form of allow rule. agy 1.3.x stops the turn at the first blocked tool.

### agy hooks, plugins and MCP servers

No mode stops agy's own hooks, plugins, custom agents or `.agents/mcp_config.json` MCP servers from running their
commands: agy starts them without a tool call. A run can also write those files for the next run. Untether watches
for this, as a tripwire and not a sandbox:

- **First sight.** The first attended run in a project that carries such config shows `⚠️ This project has agy
  hooks, plugins or MCP servers that Untether doesn't manage (…). They run their own commands in every mode,
  including Workspace. Review them if you didn't add them.` The state is remembered per project in
  `antigravity_seen_config.json` beside `untether.toml`. Only a run started by a person's message records the
  config as seen. An `/at` or `/loop` re-fire still runs and shows the row, every time, but records nothing, so
  it can't release held crons while nobody is watching ([#1044](https://github.com/littlebearapps/untether/issues/1044)).
- **Changed since someone looked.** A cron or webhook run is refused while the config differs from what an
  attended run last showed: `agy's workspace config changed since someone last ran Antigravity in this chat's
  project (…), so this scheduled run is held. The next message someone sends in the chat will run with the
  changed config and show what changed in a ⚠️ row; the schedule runs again after that. Check those files first
  if you didn't change them.` There is no preview: that next message runs agy, and so the changed hooks, before
  the row appears. Read the listed files on the host first if the change is unexpected.
- **Couldn't check.** If the check can't finish (too many files, unreadable files, links leading outside the
  project and home directory, a config file that isn't strict JSON, a custom agent whose front matter Untether
  can't read with certainty, a command or command path built from a variable), every attended run shows a ⚠️ row
  and scheduled runs are held until a check succeeds.
- **Changed during the run.** After the run Untether checks again and adds `⚠️ Antigravity changed … — agy (or
  another engine) will load it on the next run. Review it before sending another message.` This also covers files
  other engines act on: `.claude/`, `.mcp.json`, `.envrc`, `.github/workflows/`, `.git/hooks/`, `.git/config`,
  `.husky/`, `.vscode/tasks.json`, `.pre-commit-config.yaml`, `CLAUDE.md`, `AGENTS.md` and `GEMINI.md`. That part
  only warns; it never refuses a run. `.git/config` is there for `core.hooksPath`, `core.fsmonitor`, aliases and
  filters; ordinary git commands that write it (adding a remote, setting a branch's upstream) raise the same row.

What is tracked ([#1043](https://github.com/littlebearapps/untether/issues/1043)):

- Every file under each `.agents/` folder from the working directory up to the outermost git checkout above it
  (except the `skills/` and `rules/` instruction folders). A `.git` in a sub-folder, whether a folder, a worktree or
  submodule `gitdir:` file, or a stray file, never drops the `.agents/` of the repository around it. The project
  root itself is the nearest folder whose `.git` is a folder or a `gitdir:` file.
- The scripts those manifests name by a literal path, wherever the name exists: beside the manifest, in the working
  directory, at the project root or at the outermost checkout. `.agents/settings.json` is read the same way.
- Custom agents in both layouts, `agents/<name>.md` and `agents/<name>/agent.md` (any `.md` below an `agents`
  folder): the plugin folders and manifests their `plugins:`, `hooks:` or `agents:` front matter points at.
- Folders a JSON manifest names. A bare name such as `"tools"` is followed like `"./tools"` when it sits directly
  under a `plugins`, `hooks`, `agents` or `mcp` key, or anywhere in `plugins.json` / `agents.json`; any other bare
  string is looked up beside its own manifest only, so a plugin called `docs` doesn't pull the project's `docs/`
  folder into the check.
- agy's user-level manifests under `~/.gemini/config/`, including agents in both layouts, by file size and
  timestamps only (Untether doesn't read them). The one exception is Untether's own
  `agents/untether-workspace/agent.md`: that exact file is left out, because Untether rewrites it to its own text
  before every run that selects it. Anything else in or beside that folder, or a link in its place, is tracked.

Known limits:

- Only scripts named by a literal path are followed. Code reached indirectly (a package manager script, a module
  name, a `PATH` lookup, a download, a variable passed as an argument such as `sh $SCRIPT`) isn't tracked, and a
  folder passed to a hook or MCP command as an argument isn't walked. A command that is itself a variable or a
  substitution (`$RUNNER`, `` `cat cmd` ``) makes the check fail instead.
- Agent front matter is read line by line, not with a YAML parser. Plain `key: value` lines, block lists, comments
  and block scalars are followed. A `plugins`, `hooks`, `agents` or `mcp` key that is quoted, nested under another
  key or inside a flow mapping, and a value using an anchor, alias or tag, make the check fail ("couldn't check")
  instead of being read as nothing. Which of these shapes agy itself accepts is not verified.
- What agy loads is inferred from its documentation and probes, not asked of agy: whether it reads a project-level
  `settings.json`, how it resolves a bare name, and which `.git` it stops at are assumptions, each taken in the
  direction of checking more.
- User-level config is checked by timestamp, not content, and agy's own `settings.json` isn't part of the check
  because agy rewrites it itself. Both sit outside the project.
- The after-run check of other engines' files looks at the project root only, and for a worktree or submodule the
  real `hooks/` and `config` live outside the checkout and aren't watched.
- Instruction files (`AGENTS.md`, `.agents/skills/`, `.agents/rules/`) steer the model but run nothing by
  themselves, so they don't hold scheduled runs. Root-level ones are in the after-run check.
- A process still writing after the check can change files before agy reads them; the after-run check reports it.

## Errors

Every failure ends in one message. Classes where agy would otherwise wait or retry forever are stopped on the
stderr line that announces them (SIGTERM, then SIGKILL after 3 s). The saved session and any partial answer are
kept.

| agy stderr line (matched case-insensitively on the first 4 KiB) | Untether's reply |
|---|---|
| starts with `authentication required`, `error: authentication required` or `waiting for authentication` | `Antigravity CLI isn't signed in on this host — agy wanted a browser sign-in, which a bot can't complete. …`, ending with a link to the sign-in guide, plus a hint naming the API-key and Enterprise routes |
| contains `verify your account`, or `terms of service` with `block`, `appeal`, `violat` or `suspend` | `Google is asking this account to verify itself or appeal a Terms of Service block. Sign in with agy in a terminal on the host to see Google's link. Untether won't retry.` |
| contains `individual quota reached` | `This Antigravity quota is used up. /usage shows when each group resets; Untether won't retry.` |
| contains `ai credits balance is too low` | `Antigravity's AI credits balance is too low to continue. Top up or wait for the quota reset (/usage).` |
| starts with `warning: conversation "` and contains `" not found` | The "conversation no longer exists" reply above; the saved session is cleared |

The sign-in and unknown-conversation lines were captured from agy 1.3.x. The account, quota and credits wording
comes from agy's changelog and hasn't been seen live; if agy words one differently, that run falls back to the
ordinary error or stall handling.

The sign-in prefixes must start the line, so an MCP server's own "authentication required" chatter can't stop a
run. On agy 1.3.2 a signed-out run ends by itself in about 6 seconds with `authentication failed or timed out`,
which maps to the same sign-in reply; the stderr match is the backstop for versions that wait.

Other lines Untether notes without stopping the run:

| agy stderr line | Effect |
|---|---|
| `AGY_ERROR: {json}` | Parsed defensively (16 KiB limit; the key names are undocumented). If agy exits 3 with no result: `agy reported a model/agent error (<status>, retryable=<true/false/unknown>; …)`. A result that arrived first still wins |
| contains `flags provided but not defined` (exit 2) | agy rejected a flag: `antigravity.argv.rejected` is logged and the reply carries the version-mismatch hint |
| starts with `warning: unrecognized --mode value` | Logged once as `antigravity.mode.rejected` |
| starts with `[agy] print timeout after` | Logged once as `antigravity.print_timeout` |
| starts with `jetski: no output produced`, `warning: ignoring unsupported stream input` | Noted at debug level |

Result statuses: `SUCCESS` is the answer; `ERROR` with `interrupted` is `Antigravity was interrupted — the
conversation can be resumed by replying to its resume line.`; any other `ERROR` shows agy's text, trimmed to three
lines; `CANCELED`, `WAITING` or a status a newer agy adds becomes `antigravity ended with status <status>: …`.

**Redaction.** A signed-out agy prints a Google sign-in URL carrying one-time secrets. Every piece of agy text
that reaches Telegram or a log at INFO or above passes through one redaction step first: whole URLs, bearer and
Google tokens, query-string-shaped text, secret-looking `key=value` pairs and absolute paths are replaced. Raw
stderr lines are logged at DEBUG only.

## Tokens and `/usage`

- agy reports tokens, not a cost. Its `usage` is a running total for the whole conversation, so Untether records
  each run's difference: the footer shows this run's tokens, and `/usage` shows the session total and the last
  run. The first run Untether sees of a conversation started elsewhere is labelled "thread total".
- `/usage` in an Antigravity chat shows agy's own quota groups (for example a 5-hour and a weekly limit per model
  group, each with a bar, percentage used and reset time), then the last session's token totals. The groups are
  separate pools and are shown separately.
- The quota is fetched only when you send `/usage`, with a zero-token `agy -p /usage`, cached for 60 seconds. Runs
  never fetch it. `/usage debug` adds cache age, the last error kind, the CLI path and the agy version.
- Under the quota groups `/usage` shows `AI credits: 2,500 remaining` from a zero-token `agy -p /credits`, cached
  for 5 minutes, with agy's own top-up link when that link is an `https://antigravity.google/` address ([#979](https://github.com/littlebearapps/untether/issues/979)).
  It is asked only after the quota was read. If it can't be read, that is one line
  (`AI credits: couldn't read (timeout).`) and the rest of `/usage` still shows; a signed-out agy or an answer
  without a balance shows no credits line.
- Cost budgets (`[cost_budget]`) count dollars, so they never trip for Antigravity.

### Quota line under an answer

An Antigravity final can end with a quota line, on the same switch as Claude's: `[footer]
show_subscription_usage` or `/config` → **Cost & usage** → **Subscription usage** ([#979](https://github.com/littlebearapps/untether/issues/979)).

- With the switch on, each quota group gets a line such as `⚡ Gemini Models 5h: 12% | wk: 3%`. A limit at 50 % or
  more also shows the time to its reset.
- With it off, a group shows only once one of its limits is at 70 % or more: `⚡` from 70 %, `⚠️` from 90 %, and
  `🛑 Gemini Models 5h limit hit — resets in 1h 14m` at 100 %. Either limit of a group can trigger it.
- **The line never starts agy.** It is built from what your last `/usage` fetched, and only while that is under 15
  minutes old; a figure more than a minute old says so (`· 9m ago`). With nothing recent there is no line, so send
  `/usage` to see current numbers. A run never waits on a quota check.

`/stats auth` adds a line for Antigravity, for example `antigravity: ℹ️ set to use Google sign-in` (or `a Gemini
API key`, or `Enterprise ADC`). It is the route agy is set to use, read from the `agy -p /config` answer a run
already cached. It does not start agy and does not test the sign-in, and it is there only while that answer is
cached: for an hour after a run fetched it, so not straight after a restart.

## Context use (`~N% ctx`)

The header of an Antigravity progress message and final can end with `~N% ctx`, for example
`done · antigravity · 1m 36s · step 10 · ~3% ctx` ([#979](https://github.com/littlebearapps/untether/issues/979)). It uses the same header path and the same switch as
Claude's figure (`[progress] show_context_usage`). The `~` is there because it is not a number agy reports.

**What the figure is.** The tokens the model read on its latest call, divided by the context window Google
publishes for that model:

- *Tokens read*: `input_tokens` + `cache_read_tokens` of the turn's most recent finished model call (the `usage` on
  an `agent_response` step). Untether does not use the `usage` on `result` for this. That one adds up every model
  call of the turn and, on a resumed conversation, the earlier turns as well: one probe turn made five calls of
  7,902 to 9,501 input tokens and reported 42,593; the next turn on the same conversation ended on a call of 11,290
  and reported 84,958.
- *Window*: a small table in Untether, because agy reports no window (see the register below). It holds only
  figures read from Google's own model pages on 2026-10-12:

| agy model id (any effort suffix) | Window used | Where the figure is from |
|---|---|---|
| `gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.6-flash` | 1,048,576 | Gemini API model pages (input token limit) |
| `gemini-3.1-pro` | 1,048,576 | The page for the API model `gemini-3.1-pro-preview`. That agy's `gemini-3.1-pro` is that model is assumed from the name |
| `claude-opus-5-5`, `claude-sonnet-5-5` | 1,000,000 | Google Cloud's pages for its hosted Claude models |
| `gpt-oss-120b` | 131,072 | Google Cloud's page for its hosted `gpt-oss-120b` |

**When it is shown.**

| Situation | Header |
|---|---|
| The chat has a model set (`/model set <id>` or `[antigravity] model`), and it is in the table | `~N% ctx` |
| No model is set, so agy uses its own default | No percentage. agy names the model in its output only when Untether passed `--model`, and Untether shows nothing rather than assume which model ran |
| The model isn't in the table | No percentage. Untether never guesses a window or falls back to a default |
| A resumed conversation, model set | `~N% ctx`, from the latest call, so it covers the earlier turns too |
| agy older than 1.3.2 | No percentage (it doesn't name the model) |
| `[progress] show_context_usage = false` | No percentage |

**Read it with these limits in mind.**

- It is measured against the model's **published** window. No Google page states the window Antigravity itself
  gives a conversation.
- agy compacts a conversation on its own schedule and doesn't say when. Its binary carries a checkpoint setting
  around 256,000 tokens, which is not verified. On a 1,048,576 window that would be about 24 %, so the figure may
  never get high, and a low figure does not mean compaction is far off. Untether does not model compaction and
  shows no compaction row for Antigravity; after one, the figure simply drops.
- A trivial Workspace turn reads about 7,000 tokens, so most chats on a Gemini model show `~1% ctx`.
- A subagent's own calls aren't in the parent's output, so they aren't counted.
- The cache-read part comes from one agy 1.3.1 capture (7,312 input + 8,127 cached, between calls of 14,580 and
  15,860). Every 1.3.3 capture so far reports 0 cached tokens.

## `/export`

`/export` works in an Antigravity chat as it does for every engine: it renders what Untether itself saw of the
session (the commands, file changes and answers), kept in memory for the 20 most recently active sessions.

- **It is empty after Untether restarts**, and it never includes turns you ran in a terminal. See
  [Export sessions](../../../how-to/export-sessions.md).
- Untether does not read agy's own transcript, which is
  `~/.gemini/antigravity-cli/brain/<conversation id>/.system_generated/logs/transcript_full.jsonl` (not
  `transcript.jsonl`, as agy's hook docs and [#979](https://github.com/littlebearapps/untether/issues/979)'s first description say). An export built from that file would
  survive a restart; it was left out for now because it means reading a private agy file that also holds whole
  file contents.

## Effort

`/config` → **Effort** offers `low`, `medium` and `high`, passed as `--effort`. Which of them a model accepts
varies, so when the Effort page opens Untether asks agy with a zero-token `agy -p /effort [--model=<model>]`
(10 second limit, remembered per agy binary and model) and shows only the levels that model takes. A model with a
fixed effort shows no level buttons. If agy can't be asked, the page shows all three with a note.

- A level the model is known to refuse is dropped for that run, with `⚠️ Effort <level> isn't available for
  <model>, so agy used its default`.
- No check runs per message. On a model whose Effort page you haven't opened, a refused level makes agy exit with
  an error and the hint `That effort level isn't available for this Antigravity model — pick another in /config →
  Effort, or use a model id without an effort suffix.`
- A model id that already names its effort (ending `-low`, `-medium`, `-high` …) is passed without `--effort`.
- The footer shows model · effort · mode, for example `🏷 gemini-3.8-flash · high · workspace`.

Run `agy models` on the host for the model list. Untether never reads agy's settings file to guess a default.

## Background commands

agy sends one answer per run and holds it until background commands and timers finish (up to 30 minutes). While
it waits:

- the progress message lists what is still running in a `⏳ background (N)` block (see below);
- a shell command that counts as background is also retitled `⏳ background: <command>` on the next line agy
  prints;
- where a stall warning would otherwise appear, the progress message shows `⏳ Antigravity is waiting for 1 background task
  (N min) — agy holds its answer until they finish (up to 30 min).` The run is not auto-cancelled for it
  (`subprocess.background_wait` in the logs, stall reason `background_waiting`);
- when the run ends or is cancelled, Untether also stops the child processes it saw agy start.

Every running shell command is treated as possible background work, so a foreground command that hangs delays
ordinary stall warnings for up to 31 minutes.

### Background tasks and subagents in the status panel

Since 0.36.1 ([#978](https://github.com/littlebearapps/untether/issues/978)) the same `⏳ background (N)` block
that Claude runs use shows agy's work, one row each with its elapsed time, and `/ping` counts them:

| Row | From | Shown | Ends |
|---|---|---|---|
| `🐚 <command>` | a `run_command` step | once the command counts as background (next paragraph) | when its step ends |
| `🐚 timer (20s)` | a `schedule` step | at once: agy keeps the step open for the timer's whole life | when the timer fires |
| `🤖 <role> (<type>)` | each subagent of an `invoke_subagent` step | at once | when agy reports a subagent has finished |

Everything comes from the lines agy prints. Untether does not read agy's own transcript or task files.

**When a command counts as background.** agy decides per command how long to wait before letting it run on in the
background, but agy 1.3.3 doesn't print that value. A command is listed when the model carries on while it is still
running (certain), or after it has run for 5 seconds (a guess). So a normal blocking command that takes longer
than 5 seconds is shown as a background row until it ends. This affects the display only. If a future agy prints
the value (`WaitMsBeforeAsync`, `IsDaemon`), Untether uses it instead of the 5 seconds.

**Which subagent finished.** agy tells the parent conversation that a subagent has finished without saying
which one. With several running, Untether marks them finished in the order they were started, so two rows can be
swapped for a moment; the count is right. A subagent that failed is shown as finished (agy doesn't report the
difference). Subagent rows never delay a stall warning.

**Where rows appear.** The [Workspace agent](#the-workspace-agent) has no shell, timer or subagent tool, so in
practice rows appear in Full access.

**Daemon tasks outlive the run.** When agy starts a command as a daemon task (a dev server, say), it sends its
answer straight away and leaves the command running after it exits (agy 1.3.3). Its row disappears when the answer
arrives; the process does not. Untether's end-of-run clean-up stops only the child processes it saw agy start,
which may or may not include a daemon, so don't rely on it either way: check for the process yourself and stop it
when you're done. There is no setting for this yet.

Turn the block off with `[progress] show_background_tasks = false`.

## Known gaps

- **No approval buttons, plan gate, question buttons, steering or live sessions yet.** Ask me and Plan first
  arrive in a later 0.36.1 release.
- **Subagents** appear as one action row for the spawn and one status-panel row each; their own steps, output,
  token use and failures aren't shown (agy doesn't put them in the parent's stream).
- **Images.** A Telegram photo reaches agy as a saved file path, not as image input.
- **Long turns can end `CANCELED`** upstream; retry or split the task.
- **Quota and MCP start-up hangs.** A used-up quota on a Google sign-in is stopped by the stderr match above. An
  MCP server that hangs at start-up is left to the ordinary stall warnings and watchdog.
- **Context use is an estimate, and only with a model set.** See [Context use](#context-use-n-ctx). There is no
  compaction row: agy doesn't report compaction.
- **`/export` is lost on restart** and has no terminal turns (see [`/export`](#export)).
- **The quota line under an answer is only as fresh as your last `/usage`** (at most 15 minutes).

## Native vs custom register

The record required by `.claude/rules/runner-development.md` → "Native-first". Checked 2026-10-11 against agy 1.3.3
(`--help`, `agy changelog`, the docs at antigravity.google, and live probes in a scratch repo under `$HOME`).

| Untether feature | Native route used | What was checked and ruled out |
|---|---|---|
| Workspace = edits in the project | `--mode=accept-edits` | No flag: writes outside temp folders are refused. `--sandbox`: shell is still soft-denied |
| Workspace has no shell tool ([#1037](https://github.com/littlebearapps/untether/issues/1037)) | A custom agent's `tools:` list, chosen with `--agent`; its body is the system prompt | No per-run system prompt (no flag, env var or stream-json field). `permissions.allow` rules are global settings only, prefix-matched, and chained commands still match, so they are not a safe read-only list. An agent's `commandExecutionPolicy: sandbox` doesn't stop the soft-denial. `--add-dir` pointing at a folder with the agent moves agy's workspace there. A workspace `.agents/agents/` file works but writes into the user's repo (rejected, D20) |
| The agent file on disk | — (custom: Untether writes one file under `~/.gemini/config/agents/`) | agy has no way to pass an agent definition per run; a plugin is the other documented place and arrives with the approval gate |
| Telling the user to send `/new` after Workspace → Full access | — (custom wording only, no state) | agy ignores `--agent` on a resume and `init` doesn't say which agent a resumed conversation uses |
| Background rows for timers and subagents ([#978](https://github.com/littlebearapps/untether/issues/978)) | The stream's own steps: `schedule` (open for the timer's life), `invoke_subagent` (`Subagents[]` / `subagent_info.subagents[]`) | Nothing custom to rule out. `/tasks` and `/agents` are TUI only. Checked 2026-10-12, agy 1.3.3 |
| When a `run_command` counts as background | Partly native: `WaitMsBeforeAsync` / `IsDaemon` if the step's parameters carry them; a later model step while the command is still open (agy runs tool calls one at a time). Otherwise **custom**: 5 s since the step opened | The 1.3.3 stream carries only `CommandLine` (the per-call value, 5000 / 10000 / 0, is in hook payloads only), so headless stream mode fails the "available on Untether's surface" test. Checked 2026-10-12. Retire when the stream prints the value or the approval gate's hook supplies it |
| Which subagent a completion belongs to | — (**custom**: first started, first finished) | The parent stream's `system_message` step has no content. The child's id is in agy's transcript file (`sender=`), which Untether doesn't read (rule 3: no engine state files). Checked 2026-10-12, agy 1.3.3. Retire when the stream names the child or the gate's hook reports it |
| `/continue` resumes only this project's conversation ([#1039](https://github.com/littlebearapps/untether/issues/1039)) | — (**custom**: reads one agy file, `~/.gemini/antigravity-cli/cache/last_conversations.json`, for the exact run directory and passes the native `--conversation=<id>`) | `--continue` is native but fails the "meets the feature's real requirement" test: it also resumes a conversation from a parent or child directory (probe P-L5, one run each way), and `--help` lists no flag, setting or env var that limits it to the current directory. Reading an engine state file is the exception recorded in decision D33, limited to this file. An id listed under more than one path is refused, because agy also records the directory a conversation was resumed from. Checked 2026-10-12, agy 1.3.3. Retire when agy scopes `--continue` to the current directory or adds a flag for it |
| Telling a timer's or command's completion notice from a subagent's | — (**custom**: one `system_message` is expected after each finished background command or timer, and dropped when the next tool starts) | Same content-less `system_message`. Checked 2026-10-12, agy 1.3.3. Retires with the row above |
| Context window for `~N% ctx` ([#979](https://github.com/littlebearapps/untether/issues/979)) | — (**custom**: a seeded table, `utils/antigravity_context.py`, of the input limits on Google's own model pages; an unknown model gets no percentage) | Fails "available in headless / stream-json mode". `contextWindow` in `-p /config` is a user setting, not a readout: empty with no `--model` and with each of five models tried. No key in 14 run streams names a window, a limit or a maximum. `/context` is refused in print mode ("it opens the interactive context breakdown"). The binary has a status-line payload (`context_window.context_window_size`) and `ContextWindowMetadata.max_context_tokens`; neither reaches print mode. `agy models` and `-p /model` list no window. The `gemini-3.1-pro` row assumes agy's id is the API model `gemini-3.1-pro-preview`. Checked 2026-10-12, agy 1.3.3. **Retire when agy reports a window in the stream** |
| Tokens in context for `~N% ctx` | `step_update.usage` on the latest finished `agent_response` step: `input_tokens` + `cache_read_tokens` | `result.usage` is a running sum over every model call and every earlier turn, so it is not the context size. agy has no context-usage request and no compaction signal, so the figure is not corrected for compaction. Checked 2026-10-12, agy 1.3.3 (`step_update.usage` was seen on 1.3.1 and 1.3.3) |
| Which model a run used, for `~N% ctx` | `init.model` only | It is present only when `--model` is passed (it echoes the flag); with no flag nothing in the stream names the model, so no percentage is shown. `-p /model` answers with the default model for 0 tokens but is a second process answering for the account, not for the run, and a hook payload's `modelName` needs the approval gate; neither is used. The id Untether would pass is not used either: it is not proof of what agy ran. Checked 2026-10-12, agy 1.3.3 |
| AI credits on `/usage` ([#979](https://github.com/littlebearapps/untether/issues/979)) | `agy -p /credits` (0 tokens; `remaining_credits`, `upgrade_uri`) | Nothing custom. Checked 2026-10-12, agy 1.3.3. Not checked on an API-key sign-in |
| Quota line under an answer | The cached answer of `agy -p /usage` | Partly custom: agy has no push or cheap in-stream quota value (`/usage` is refused inside a stream-json session), and a probe before each final would delay it, so the line only reads what `/usage` cached (15 min at most). Checked 2026-10-12, agy 1.3.3. Revisit if the stream starts carrying quota |
| `/export` | — (**custom**: Untether's in-memory record of the events it saw) | agy writes `transcript_full.jsonl` under `brain/<id>/` but has no export command or flag for it, and reading it is rule 3 (an engine state file, holding whole file contents). Not read. Checked 2026-10-12, agy 1.3.3 |

Retire or revisit when agy can continue a turn after a soft-denial, takes a per-run system prompt or tool list, or
lets `--agent` change on a resume.

## Configuration

```toml
[antigravity]
model = "gemini-3.8-flash"     # optional; passed as --model=<id>
cmd = "~/.local/bin/agy"       # optional; default: agy on PATH, then ~/.local/bin/agy
permission_mode = "workspace"  # workspace (default) | full; ask and plan are refused for now
```

See the [config reference](../../config.md#antigravity). Unknown keys are ignored.

## Log events

`antigravity.session.started` (`agy_version`, `resumed`), `antigravity.run.timing`, `antigravity.denied_actions`,
`antigravity.auth.required` / `.killed`, `antigravity.account.blocked`, `antigravity.quota.exhausted`,
`antigravity.credits.low`, `antigravity.conversation.missing`, `antigravity.permission_mode.mismatch`,
`antigravity.workspace_config_present` / `_changed` / `.unattended_refused`, `antigravity.config_check.fetched` /
`.failed`, `antigravity.effort.dropped`, `antigravity.tos_notice.shown`, `antigravity.version.unsupported`,
`antigravity.argv.invalid_value`, `antigravity.continue.resolved` / `.refused` (`reason`) / `.lookup_failed`,
`antigravity.workspace_agent.installed` / `.unavailable`,
`antigravity.background.started` / `.finished` (`task_id`, `status`, `attribution=fifo` for a subagent) / `.outlived`
(a command still running when the answer arrived), `antigravity.quota.fetched` / `.failed`,
`antigravity.credits.fetched` / `.failed`, `antigravity.context.window_miss` (DEBUG, once per run: the model has
no known window) / `.over_window` (WARN: more tokens than the table's window, shown as 100 %). `runner.completed`
carries `context_pct` when a percentage was shown.

## See also

- [Stream JSON cheatsheet](stream-json-cheatsheet.md) and [Untether events](untether-events.md)
- [Switch engines](../../../how-to/switch-engines.md) — install and sign-in
- [Error reference](../../errors.md) — hints for common errors
