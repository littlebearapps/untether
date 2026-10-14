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

Headless agy can't stop mid-run to ask, so the mode is chosen before the run: `/config` → **Permission mode**, or
`permission_mode` under `[antigravity]`. See
[Antigravity CLI — Permission mode](../../../how-to/interactive-approval.md#antigravity-cli-permission-mode) for the
user guide.

| Mode | Value | Flag | What happens |
|---|---|---|---|
| **Workspace** (default) | `workspace` | `--mode=accept-edits` `--agent=untether-workspace` | agy's own accept-edits mode under its `request-review` policy, run as Untether's [no-shell agent](#the-workspace-agent): file edits in the project and temp folders go ahead; the model has no shell, web, MCP or subagent tool; files outside the project are blocked by agy. Not a sandbox. Without `--mode`, headless agy refuses every write outside a temp folder (agy 1.3.3) |
| **Full access** | `full` | `--dangerously-skip-permissions` | Every tool is approved, including shell, network and files outside the project |
| **Ask me** | `ask` | `--dangerously-skip-permissions`, agy's default agent, no `--mode` — **only** once [Untether's agy gate](#untethers-agy-gate) is ready for the project | Untether's gate is the only check: reads and edits inside the project run on their own; shell, web, MCP, browser, subagents, timers, files outside the project and protected files get an approval card in Telegram. Refused before agy starts when the gate isn't ready. See [Ask me](#ask-me) |
| **Plan first** | `plan` | `--mode=plan` `--agent=untether-workspace`, no `--disable-slash-commands` | agy's own plan mode under the [no-shell agent](#the-workspace-agent): it reads the project, writes a plan and stops; nothing in the project changes. The plan is posted with **Approve Plan / Deny / Let's discuss**; Approve resumes the conversation in Workspace. See [Plan first](#plan-first) |

Rules the runner enforces:

- **Order of precedence:** the chat or topic setting (or a cron's own `permission_mode`), then
  `[antigravity] permission_mode`, then Workspace.
- **Fails closed.** An unknown stored value runs as Workspace and logs `antigravity.permission_mode.unknown` once.
  An unknown value in `untether.toml` is a config error. `permission_mode = "full"` in `untether.toml` logs
  `antigravity.full_access_from_toml` once.
- **Scheduled runs never inherit Full access or Ask me.** A cron gets either only from its own `permission_mode`
  (`"full"` / `"ask"`); a chat's or `untether.toml`'s choice never reaches it. Otherwise the run is Workspace
  (`antigravity.unattended_full_downgraded` / `antigravity.unattended_ask_downgraded`).
- **The bypass flag has one source.** `decide_antigravity_mode` (in `runners/antigravity.py`) is the only place that
  grants `--dangerously-skip-permissions`; `build_args` only reads its decision. Without a ready gate, Ask me never
  gets the flag, even when `build_args` is called directly.
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

### Ask me

Ask me ([#980](https://github.com/littlebearapps/untether/issues/980), phase 14) needs
[Untether's agy gate](#untethers-agy-gate) on the host. The run starts only when all of these hold, checked before
agy starts (otherwise it is refused with a plain reply and nothing runs; `antigravity.permission_mode.gate_refused
reason=…`):

| Check | Refusal (`prespawn_blocked`) |
|---|---|
| The plugin is installed | `gate_missing`: run `untether agy-gate install` on the host |
| The plugin is unedited and current, agy hasn't disabled it, and `agy -p /hooks` lists it for the project; the planted-config scan finished | `gate_not_ready`, with the reason (e.g. `agy plugin enable untether-gate`) |
| The project doesn't contain Untether's own package, its venv or the plugin folder (REVIEW-2 B3) | `gate_self_protect` |
| No hook group of another source (`-p /hooks`, any event) and nothing in the planted-config scan: the project's `.agents/` hooks, plugins, MCP servers and agents, agy's user-level plugins and agents. User-level `hooks.json` is judged by `-p /hooks`; user-level `mcp_config.json` by `agy mcp list` (an existing file with no servers, as on lba-1, is fine; a failed listing counts as "has servers") | `gate_foreign_config`, naming the paths |
| The run's socket is listening | `gate_not_ready` |

Then agy runs with `--dangerously-skip-permissions` on its default agent and no `--mode` (the no-shell Workspace agent
would leave nothing to ask about, and `--mode` takes one value). The gate is **required** for the run: every tool
call gets an explicit `allow` or `deny` (never "no decision", which would let agy run it), and the liveness check and
the stepIdx tripwire stop the run (see [Liveness, tripwire](#liveness-tripwire-stop)). Ask me runs are always one
agy process per message (no warm session).

**Reading a call the way agy does.** agy is a Go program, and Go's JSON decoder matches field names
case-insensitively, so the gate maps every argument onto agy's own spelling first (`commandline` is `CommandLine`) and
**denies** a call outright when two keys fold to the same name, a key isn't plain printable ASCII, or the arguments
aren't an object (`antigravity.gate.ambiguous_args`; agy is told to call again with each argument once). The hook
denies a payload with duplicate JSON keys, or keys that differ only in case, before it reaches Untether. Only **plain absolute paths** (no `~`, `.` /
`..` parts, doubled or trailing slashes, control characters) can be on the safe list, so Untether and agy can't
resolve a path differently, and a safe-listed tool may carry only the argument names agy uses for it (from live
hook payloads, agy 1.3.3 / 1.3.4); anything else asks.

**What runs without asking** (`antigravity/gate_policy.py`, the safe list; exact tool names): `command_status`,
`list_permissions`, `wait`, `wait_5_seconds`, `finish` (when they name no path); `view_file`, `list_dir`,
`find_by_name`, `grep_search` when every path they name is inside the project or the conversation's own artifact
folder (none: agy searches its workspace) and, for the two searches, no value starts with `-` (security review 3 M4:
it could reach a search program as a flag); and `write_to_file`, `replace_file_content`, `multi_replace_file_content`
when every path argument (`TargetFile` and any other `…Path` / `…File` / `…Dir` key, realpath'd — the nearest
existing parent for a new file, so a symlink out of the project resolves outside it) is inside the project or the
artifact folder (`~/.gemini/antigravity-cli/brain/<conversation id>/`, a UUID-shaped id, the folder itself under
agy's `brain/`, equal to the payload's `artifactDirectoryPath` when it names one; edits only to top-level `*.md` /
`*.md.metadata.json` and `scratch/`, never `.system_generated/`), the payload's `workspacePaths[0]` is the project,
the file isn't protected, and no part of the path below the project folder is a symlink (it could be repointed
before agy acts). A folder below the project holding its own `.git` (submodule, nested repo, git worktree) and every
configured project's `@branch` worktree root (`worktrees_dir`, from the live config) count as **outside** the project
(security review 3 H1). Reads follow the same link rule. An existing target must be a regular file with a single name
(no hard link) — or, for a read, a folder. Plain absolute paths exclude lone surrogates and U+FFFD (Go reads an
unpaired surrogate as U+FFFD).
Shell commands are never parsed: every `run_command` asks. **The safe list is best effort** against a model that
can make links: the check runs when the hook fires and agy acts a moment later, so a shell command you approve (or a
daemon it started) can change what a later "in-project" path means. Approving a shell command is approving that. A
non-string path, a NUL byte, arguments that aren't an object, or any error while checking asks. A subagent reporting back to its parent
(`send_message` to a conversation of this run, with only agy's `Recipient` and `Message` fields) is internal and runs
too.

**What asks:** everything else — `run_command`, `send_command_input`, `manage_task`, `schedule`, `run_workflow`,
`notebook_execution`, `read_url_content`, `search_web`, `call_mcp_tool`, MCP resources, every browser tool,
`invoke_subagent` and the other agent tools, `generate_image`, `sed_file` and `notebook_edit` (path argument not yet
probed), any tool this version doesn't know, any call from a conversation that isn't this run's or a known
subagent's, and edits that are outside the project, protected or in Untether's own files. **Protected**, matched
case-folded at any depth (`_PROTECTED_DIR_NAMES` / `_PROTECTED_DIR_PAIRS` / `_PROTECTED_FILE_NAMES`): folders
`.agents/`, `.gemini/`, `.claude/`, `.codex/`, `.opencode/`, `.pi/`, `.cursor/`, `.git/`, `.husky/`, `.lefthook/`,
`.vscode/`, `.devcontainer/`, `.github/workflows/`, `.github/actions/`; files `.mcp.json`, `opencode.json`,
`opencode.jsonc`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `AGENTS.override.md`, `GEMINI.md`,
`.pre-commit-config.yaml`, `lefthook.yml` / `.yaml` (and `.lefthook.*`, `lefthook-local.*`), `.envrc`, `mise.toml`,
`.mise.toml`, `mise.local.toml`; the configured `files.outbox_dir` (from the project root); and Untether's default
file deny globs (`.env*`, `*.pem`, `*.key`, SSH keys, `.netrc`, `.npmrc`, `.pypirc`). **Untether's own files**:
its package, venv, `sys.base_prefix`, the plugin folder, and this instance's config file and its folder. The gate
also asks for in-project edits when the run's `diff_preview` option is on, but `/config` → **Diff preview** is
Claude-only (`DIFF_PREVIEW_SUPPORTED_ENGINES`), so an Antigravity chat can't switch it on today.

**The card** is registered in the engine-neutral registry (`kind="tool"`, the `ia:` callback prefix) and posted as a
⚠️ row with buttons: `Permission Request [agy] - <what>` (`shell command`, `write a file (outside this project)`,
a read's reason too since [#1083](https://github.com/littlebearapps/untether/issues/1083),
`MCP tool <server>/<tool>`, `start subagents` …), then a preview built from agy's own `toolCall.args`:

- `run_command`: `$ <command>`, first and verbatim (a fenced block when it has several lines or is long; past 1,500
  characters only its first and last 600 with a warning that the middle isn't shown, and no "Always allow"; a call
  with no command line says so), `in <dir>` when `Cwd` isn't the project,
  `moves to the background after N s if still running` (`WaitMsBeforeAsync`), and a ⚠️ line for `IsDaemon` (a daemon
  approved once keeps running after the turn).
- `replace_file_content` / `multi_replace_file_content`: `📝 path` and `-` / `+` lines (the first two chunks, then
  `…N more chunks`); `write_to_file`: `+` lines and `(overwrites an existing file)`.
- `invoke_subagent`: each subagent's role and its prompt in full (and any other field of the spec); each one's own
  tool calls come back as their own cards (P-H8), labelled `from subagent: <role>`.
- anything else (security review 3 M1): **every** argument but `toolAction` / `toolSummary`, each in full — inline
  code when short and single-line, otherwise a fenced block (past 1,500 characters its start and end with
  `⚠️ long value (N characters): … aren't shown`), within a 3,000-character card budget after which a ⚠️ line names
  every argument left out. The tool's own arguments come first (`_TOOL_KEYS`, agy 1.3.4's schemas from hook payloads:
  `read_url_content` `Url`; `schedule` `Prompt`, `CronExpression`, `DurationSeconds` …; `define_subagent`
  `system_prompt`, the `enable_*` switches …; `_PRIMARY_KEYS` where the schema is unknown: `open_browser_url` `Url`,
  `send_command_input` `Input`, `call_mcp_tool` `ServerName` / `ToolName`), then any argument that tool's schema
  doesn't have, each marked `⚠️ •` under a `⚠️ arguments agy's <tool> doesn't have: …` line. A value whose key looks
  like a secret (`token`, `secret`, `password`, `apikey` …) is shown as `•••` with "hidden: its name looks like a
  secret — Deny unless you expected one here". `define_subagent` adds `⚠️ the new subagent gets …` for each
  `enable_*` switch that is on.
- every string from the call is shown with control and invisible format characters (bidi overrides, zero-width
  spaces) as `⟨U+XXXX⟩`, and file paths can't carry a line break into the card.
- every argument that changes what runs and isn't already in the preview is listed in full (`• BypassSandbox =
  \`true\``), one that isn't among that tool's known argument names first and marked ⚠️; an edit whose path resolves
  elsewhere says `→ resolves to …`; a `Cwd` that isn't a plain absolute path is flagged. agy's descriptive fields
  (`Description`, `Instruction`, `toolSummary` …) are never shown as what runs on a command or edit card.
- **Always allow** is offered only for a shell command shown in full, with no extra arguments and a plain absolute
  `Cwd` (or none). A field agy decodes to its Go zero value counts as absent ([#1085](https://github.com/littlebearapps/untether/issues/1085):
  `RunPersistent` / `BypassSandbox` / `IsDaemon` exactly `false`, an empty `RequestedTerminalID`, or `null`), for the
  offer and for matching a grant; the card still lists them, and any other value keeps the button off. The command and
  `Cwd` may not hold a control, format, private-use, unassigned or surrogate character or any
  space but the ASCII one (tabs and line feeds are fine). Grants compare the command trimmed of ASCII spaces and line
  feeds only (security review 3 M2: Python's `strip()` also drops U+3000 and U+001C–U+001F).
- agy's own one-line summary last, escaped: `agy says: …` (model text, never in place of the arguments).

Buttons: **✅ Approve**, **❌ Deny** and, for a shell command, **✅ Always allow this command here** (exact
`CommandLine` and `Cwd`, for this chat and agy conversation, in memory only, at most 50; a run of the conversation
in another mode, `/new` or a restart clears it; never used by cron or webhook runs; logged as a hash). Approve →
`allow`; Deny → `deny` with "The user denied this in Telegram (Untether). They can't see your tool calls — explain
in your reply what you were trying to do and ask how to proceed. Don't retry the same call." agy shows it as the
step's error (`tool call denied by pre-tool hook: …`), which Untether's row shows as `blocked: denied in Telegram`.
No answer within the wait (`untether agy-gate install --wait-s`, default 900 s, shortened by `[antigravity]
approval_timeout_s`) → `deny` "No answer in Telegram within 15 min — the tool was blocked. …" and the card reads
`⏱️ Timed out: denied after 15 min`. The hook going away withdraws the card. The usual reminder, push and
"waiting for approval" stall suppression apply (the engine state reports the held request). At most 20 cards a minute
per run. The originator check (`approval_originator_only`, #388) applies to these buttons.

**Unattended runs** (#835): a cron whose own `permission_mode = "ask"` runs gated, and every tool off the safe list
is denied at once with Untether's unattended text, logged `permission.unattended_deny engine=antigravity`, with one
`🔒 Unattended run — denied …` row and the final's `🔒` footer. A chat's Ask me never reaches a cron or webhook (it
runs as Workspace).

**agy's reported mode:** under the bypass flag agy's first event says `permission_mode: always-proceed`. A stricter
value is warned once (`antigravity.permission_mode.bypass_unreported`); any other value stops the run before a tool
(`antigravity.permission_mode.mismatch`).

**Switching modes needs `/new` across agents** (R3-3, D38): Workspace and Plan first start a conversation on the
no-shell agent, Ask me and Full access on agy's default agent, and agy keeps a conversation on the agent it started
with. A chat switched from Workspace to Ask me therefore has no shell to ask about until `/new`; the `/config` toast,
the page and `/planmode` say so. Ask me ↔ Full access and Workspace ↔ Plan first need no `/new`.

### Plan first

Plan first ([#981](https://github.com/littlebearapps/untether/issues/981)) uses agy's native plan mode and approves
by resuming the conversation; there is no hook gate and nothing is held open.

1. **The plan turn.** `--mode=plan --agent=untether-workspace`, under agy's `request-review` policy (the same
   `toolPermission` checks as Workspace). `--disable-slash-commands` is left off, because it turns `--mode=plan`
   into a no-op; a prompt that starts with `/` is sent as `Task: /…` instead, since agy would otherwise run it as
   one of its own slash commands and end the session. agy writes one plan artifact into the conversation's own
   folder, `~/.gemini/antigravity-cli/brain/<conversation id>/` (with a `<name>.metadata.json` sidecar that says
   `"requestFeedback": true`), and ends the turn by itself. Probed on agy 1.3.3: 5 of 5 plan turns stopped with
   the project untouched (2026-10-12), and again on 2026-10-13.
2. **Finding the plan.** agy's `result` looks like any other turn's, and `init.permission_mode` is
   `request-review` for plan and Workspace runs alike. The only signal is a finished `write_to_file` step whose
   `TargetFile` resolves (after `realpath`) to a `.md` file directly inside that folder. Untether reads that one
   file and its sidecar, nothing else under `~/.gemini`: never a link out of the folder, another conversation's
   folder, `walkthrough.md` or `task.md`, and never a file whose sidecar says `requestFeedback` is not true. The
   plan is capped at 8,000 characters in Telegram.
3. **The card.** The final shows `📋 Plan — <file>`, the plan, then agy's own reply. Right after it comes a card
   with **✅ Approve Plan**, **❌ Deny** and **💬 Let's discuss**. The card answers only in the chat it was posted
   in, only once, and only while it is the conversation's newest plan (a new plan turn retires older cards);
   cards last 24 hours and don't survive a restart. With `approval_originator_only` on, only the person whose
   message started the plan run can answer it.
4. **Approve** queues a follow-up in the chat, like a reply: agy resumes the same conversation with
   `--mode=accept-edits` (Workspace) and the prompt `The plan is approved. Implement it now.`, in the plan run's
   own project and branch (refused if that directory changed meanwhile). The footer reads
   `workspace · plan approved` and the final starts `📋 Plan (approved) — <file>`. The chat stays in Plan first,
   so the next message plans again.
5. **Deny** runs nothing. The conversation is kept: a message carries on planning, `/new` starts over.
6. **Let's discuss** runs nothing either. Your next message is a plan turn on the same conversation; agy rewrites
   the plan (probed 2026-10-13: the same file, project untouched) and a new card appears.
7. **No plan written** (the model answered instead): the answer is shown as usual with a one-line note that there
   is nothing to approve yet (`antigravity.plan.no_artifact`).
8. **Cron and webhook runs** ([#835](https://github.com/littlebearapps/untether/issues/835)): nobody can approve,
   so the plan is posted and nothing runs. There is no card; the footer says
   `🔒 unattended (cron:…) · denied plan approval — nobody to approve`, with a hint the first time
   (`permission.unattended_deny`, `reason=plan_mode`). To let a cron make changes, give it
   `permission_mode = "workspace"`.

Limits:

- The approved run is Workspace: file edits in the project, no shell. agy keeps a conversation on the agent it
  started with, so there is no way to carry a plan out with a shell in the same conversation.
- A conversation that started in Full access keeps agy's default agent; a plan turn there may try to carry out the
  plan straight away. agy itself soft-denies the edits (no `accept-edits`), but send `/new` after switching Full
  access → Plan first. A shell call blocked in such a plan turn adds that `/new` hint to the Plan first notice
  ([#1060](https://github.com/littlebearapps/untether/issues/1060)).
- agy lets any mode write in temp folders, so a project under `/tmp` gets no such protection.
- A project that defines its own agents runs plan turns on agy's default agent, as Workspace does.
- "Stops by itself" is the model following agy's plan-mode instructions; agy doesn't enforce it beyond its normal
  permission checks, which block project writes in a plan run.
- No question buttons while planning: the no-shell agent has no `ask_question` tool.

### The Workspace agent

A blocked tool ends an agy 1.3.x turn, and agy's default agent often opens with a shell command (`ls -la`), so a
Workspace reply used to be nothing but the blocked notice
([#1037](https://github.com/littlebearapps/untether/issues/1037)). agy has no per-run system prompt and no way to
take a tool away from its default agent, but a custom agent's `tools:` list is the model's whole toolset. So:

- Before a Workspace run Untether makes sure `~/.gemini/config/agents/untether-workspace/agent.md` (agy's
  user-level agent folder) holds its agent: file tools (`view_file`, `list_dir`, `find_by_name`,
  `grep_search`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`), `ask_question` (R3-2, see
  [Questions](#questions)) and a short system prompt.
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
  can't delete or rename a file either (a "rename" is a copy). Its advice names both routes, since the one agent
  file serves every run: in a chat, `/config` → Permission mode → Full access, then `/new`; a cron gets Full access
  only from its own `permission_mode = "full"`, and a webhook never does
  ([#1055](https://github.com/littlebearapps/untether/issues/1055)). Nothing in an agy run says it was started
  by a cron, so a cron or webhook run in Workspace starts its prompt with one line from Untether saying it is a
  scheduled run and which route applies (the cron's own `permission_mode = "full"`, or the chat in Full access for
  a webhook); the log has `antigravity.prompt.scheduled_note`. Chat, Full access and Plan first runs don't get it.

!!! warning "A conversation keeps the agent it started with"
    agy ignores `--agent` on a resume and reports nothing about it in `init`. A conversation that started in
    Workspace has no shell tool even after the chat is switched to Full access, and one that started in Full
    access (or before this change) keeps agy's default agent in Workspace and Plan first. **After switching to or
    from Full access, send `/new`;** Workspace ↔ Plan first share the agent and need none. The `/config` toasts and
    page, `/planmode`, the agent's own text and the blocked notice all say so. Untether keeps no per-conversation record of this. Re-checked on agy 1.3.3 for
    [#1060](https://github.com/littlebearapps/untether/issues/1060): a Full access conversation resumed with `--agent=untether-workspace` still called
    `run_command` (blocked), while `init` reported `agent: untether-workspace` and the full tool list.

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

In Plan first a blocked write is plan mode itself: agy refuses every file change except the plan, inside the project
too, so the row reads `⚠️ Blocked: file change (WriteToFile)` and the paragraph says
`Plan first doesn't change files until you approve the plan.` A blocked read in Plan first is a file outside the
project, as in Workspace (reads inside the project go ahead), and is labelled that way
([#1073](https://github.com/littlebearapps/untether/issues/1073)).

With the Workspace agent a shell block is rare: it needs a conversation that began without the agent. When a
resumed run that named the agent still has a shell call blocked, that is what happened, so the paragraph says so
and asks for `/new` (or Full access) instead of "Full access, then /new" ([#1060](https://github.com/littlebearapps/untether/issues/1060)). Exact commands are the
reliable form of allow rule. agy 1.3.x stops the turn at the first blocked tool.

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
  When Untether's own agy plugin folder has changed, the row leaves its files out (the plugin-changed row reports
  them; the scan still tracks them) and is skipped when nothing else is left
  ([#1083](https://github.com/littlebearapps/untether/issues/1083)).
- **Changed since someone looked.** A cron or webhook run is refused while the config differs from what an
  attended run last showed: `agy's workspace config changed since someone last ran Antigravity in this chat's
  project (…), so this scheduled run is held. The next message someone sends in the chat will run with the
  changed config and show what changed in a ⚠️ row; the schedule runs again after that. Check those files first
  if you didn't change them.` There is no preview: that next message runs agy, and so the changed hooks, before
  the row appears. Read the listed files on the host first if the change is unexpected. When Untether's own
  plugin (`untether-gate`) isn't what it installed, the scan tracks its files too, but that hold says so instead
  (`gate_plugin_held_message`): the gate check (`_gate_setup`) holds every scheduled run until the plugin is
  reinstalled, so a chat message doesn't release it
  ([#1078](https://github.com/littlebearapps/untether/issues/1078)).
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
  Claude Code's own worktrees under `.claude/worktrees/` are left out: each is a separate checkout with its own
  config, and walking them used to run the check out of time on every run
  ([#1051](https://github.com/littlebearapps/untether/issues/1051)).

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
  timestamps only (Untether doesn't read them). The exceptions are Untether's own files, each by exact path and only
  as a regular file: `agents/untether-workspace/agent.md` (rewritten to Untether's text before every run that
  selects it), and the gate plugin's `plugins/untether-gate/plugin.json`, `hooks.json` and `.untether-managed`, left
  out only while all three are byte-identical to what this Untether version writes (its own files, the one thing
  the check reads under `~/.gemini`; a manifest that matches a changed `hooks.json` proves nothing, so then all
  three are tracked, see [Untether's agy gate](#untethers-agy-gate)). Anything else in or beside those folders, or a
  link in their place, is tracked.

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
  real `hooks/` and `config` live outside the checkout and aren't watched. Config inside a Claude Code worktree
  (`.claude/worktrees/<name>/`) belongs to that worktree and is checked only when a run uses it as its project.
- Instruction files (`AGENTS.md`, `.agents/skills/`, `.agents/rules/`) steer the model but run nothing by
  themselves, so they don't hold scheduled runs. Root-level ones are in the after-run check.
- A process still writing after the check can change files before agy reads them; the after-run check reports it.

## Untether's agy gate

The transport for everything interactive on agy ([#980](https://github.com/littlebearapps/untether/issues/980),
phase 13): an opt-in agy plugin whose hooks talk to the run that started agy over a per-run Unix socket. Today it
carries **question buttons** (`ask_question`), **steer** ([#981](https://github.com/littlebearapps/untether/issues/981))
and, in [Ask me](#ask-me) only, **approval cards**. In Workspace, Plan first and Full access it changes nothing about
what agy may do: every other tool call gets no decision from Untether, so agy's own permission check decides exactly
as without the plugin.

### Install

`untether agy-gate install` (host-wide, never from Telegram, REVIEW-2 m3) stages `plugin.json`, `hooks.json` and
`.untether-managed` in a temp folder and runs `agy plugin validate` then `agy plugin install <folder>` (R3-1, agy's
own plugin manager; agy copies the folder to `~/.gemini/config/plugins/untether-gate/` and lists it in
`import_manifest.json`). `uninstall` runs `agy plugin uninstall untether-gate`. Both refuse a folder without
`.untether-managed` or with files edited since (`--force` overrides); `--wait-s` sets how long a question waits
(default 900 s; the hook's agy timeout is that plus 60 s). `status [--project <dir>] [--canary]` shows the install,
runs agy's own `-p /hooks` listing in a project (0 tokens), and with `--canary` sends one trivial turn (≈ 13 k tokens)
to prove the hooks run. Nothing else under `~/.gemini` is written. agy leaves an empty `plugins/` folder and
`import_manifest.json` behind after an uninstall (P-H0).

`hooks.json` registers one named group with three hooks:

```
PreToolUse (matcher "*", timeout wait + 60), PreInvocation (15 s), Stop (15 s):
[ -n "$UNTETHER_AGY_GATE" ] || exit 0; [ -x "$UNTETHER_AGY_GATE_PY" ] || <missing>; exec "$UNTETHER_AGY_GATE_PY" -I -m untether.antigravity.gate_hook <Event>
```

`<missing>` is `{ printf '%s' '{"decision": "deny", "reason": "The Untether gate interpreter … was blocked. …"}'; exit 0; }`
for `PreToolUse` and `exit 0` for the other two (plugin version 2, security review 3 M3; version 1 had no
interpreter check, so an install from 0.36.1rc1–rc3 reads as "not current" and must be redone with
`untether agy-gate install`). The shell guard means your own agy sessions (no marker) never start Python. The
interpreter comes from the env of the Untether instance that started agy, so instances sharing a `HOME` each run
their own code.

### Per run

1. **Precheck** (cached an hour per agy binary + version, plugin files, cwd, the planted-config digest and the stat
   of agy's user-level hook / plugin state files; a failure for five minutes): the plugin folder must hold exactly
   what this Untether version writes (byte compare with its own templates, never with the manifest) and agy must not
   have it disabled (`agy plugin disable`); then `agy -p /hooks` in the run's cwd must list one enabled
   `untether-gate` group sourced from the plugin folder with the three commands byte for byte and a long enough
   timeout. Other hook groups agy lists are recorded (`foreign_sources`) and so is a project that contains
   Untether's own code (`self_protect`); [Ask me](#ask-me) refuses on them. A failed precheck refuses an Ask me run
   and only turns questions and steer off for any other (`antigravity.gate.precheck_failed reason=…`): Workspace,
   Plan first and Full access don't rely on the gate.
2. **Socket.** Bound before agy starts at `$XDG_RUNTIME_DIR/untether/<inst8>/agy-<run12>.sock` (or
   `$TMPDIR/untether-<uid>/<inst8>/`), 0600 in 0700 folders whose owner and mode are verified; `<inst8>` hashes the
   instance's config path. Stale sockets of the same instance older than a minute with no listener are removed once
   per process. Unlinked when the run ends, on every path.
3. **Env, agy only.** `UNTETHER_AGY_GATE=1`, `UNTETHER_AGY_GATE_SOCK`, `UNTETHER_AGY_GATE_TOKEN` (32 random bytes,
   never logged — `token_len` only), `UNTETHER_AGY_GATE_PY`, `UNTETHER_AGY_GATE_DEADLINE_S`. Any `UNTETHER_AGY_GATE*`
   in Untether's own environment is dropped first, and the zero-token `-p` probes never carry them.
4. **Each hook call** connects, sends one line `{v, token, event, pid, ppid, payload}`, reads one line back. The
   server checks the token (constant time) and, on Linux, the peer (`SO_PEERCRED`): same uid, the request's `pid`
   equal to the peer's, a descendant of the agy pid this run spawned (set when agy's stdout is read; an Ask me run
   refuses every connection before that), and `/proc/<pid>/cmdline` exactly `[<Untether's interpreter>, -I, -m,
   untether.antigravity.gate_hook, <the event it sends>]` with that interpreter as `/proc/<pid>/exe` (security
   review 3 L1; `antigravity.gate.peer_refused why=lineage|command|interpreter|pid|no_agy_pid`). A refused peer gets
   no reply. `PreToolUse` fails **closed**: no socket, a bad reply or a timeout prints a `deny`; `ask` / `force_ask` /
   `permissionOverrides` / `overwrite` are never printed. `PreInvocation` and `Stop` fail open (`{}`). In Ask me an
   `ask_question` Untether can't read is denied (security review 3 L4), never left without a decision.

### Questions

A `PreToolUse` for `ask_question` from the run's conversation (or a known subagent's, P-H8) becomes the question
card through the engine-neutral registry ([#960](https://github.com/littlebearapps/untether/issues/960)): options are
agy's plain strings (at most four, 60 characters each), plus **Other (type reply)**; a text reply in the chat answers
too. The hook is held until the answer, which goes back as `{"decision":"deny","reason":"The user answered via
Telegram: \"…\". Use this answer and continue. …"}` (E3: agy shows the reason to the model and the turn carries on);
the card becomes `❓ answered: …`. No answer by the deadline: `deny` "No answer from the user in Telegram; use your
best judgement and say what you assumed." and `⏱️ No answer — agy carried on`. The hook going away (agy's own timeout,
agy stopped) withdraws the card. Cron and webhook runs are answered with the unattended text and no card; Ask mode
off answers "Questions are turned off in this chat …". At most 20 cards a minute per run. The question step itself
arrives as `step_type: unknown` (`ERROR` when answered, `DONE` when skipped) and is not shown.

The Workspace agent has `ask_question` since R3-2: without the gate (no plugin, or a run whose precheck failed) agy
skips the unanswered question and the turn goes on (probed on agy 1.3.4, n = 2).

### Steer

With Follow-up = steer (or `/steer <text>`), a message for a chat whose agy run is in flight and hasn't sent its
result is queued on the run (`live_sessions.steer_backend`: a steer-only target, not a live session) and acknowledged
`↪️ Steered into the current run.` The next `PreInvocation` for the run's own conversation (never a subagent's)
answers `{"injectSteps":[{"userMessage":"Message from the user, sent via Telegram while you were working: …"}]}`,
at most eight per call (`antigravity.steer.injected`). Each injected message appears as one `user_input` step
(P-H10); that step marks it delivered (`antigravity.steer.delivered step=<n>`) and adds a
`↪️ steer received: …` row; it never starts a turn. A steer still queued or not seen when agy sends its result gets
the bridge's `⚠️ The session ended before this message ran — please send it again.` (so does `/cancel`), logged as
`antigravity.steer.not_run` ([#1083](https://github.com/littlebearapps/untether/issues/1083); Claude's equivalent is
`claude.live_session.followup_not_run`).

### Liveness, tripwire, `Stop`

`PreInvocation` fires before every model call, so a model step (`agent_response`, `tool`, `subagent`, `finish`) with
no `PreInvocation` before it means the hooks don't run: `antigravity.gate.not_running`, and steer is off for the run.
Each parent-stream tool step must get its `PreToolUse` within 3 s (`antigravity.gate.tripwire`, INFO). Both stop the
run (ERROR, SIGTERM) only in [Ask me](#ask-me), whose safety rests on the gate: the final then says "Telegram
approvals aren't running for this agy run …" or "… stopped responding during this agy run …". Duplicate `(conversationId, stepIdx)` requests are refused (`antigravity.gate.duplicate_request`, P-H15).
`Stop` with `fullyIdle: false` changes the `⏳ background` block's last line to say agy has finished its turn and is
waiting for background work (#1061).

Same-uid caveat (narrowed, not closed): agy's tool processes inherit its env (agy 1.3.4 can't keep variables from
them) and are agy's direct children, so a command the user already allowed could read the socket path and token and
exec the hook's own command line to pass the peer check. It can only *ask* (a spoofed question or approval card, a
drained steer queue, a pre-claimed step number, a used-up card budget), never grant itself a tool. See [Security](../../../how-to/security.md#antigravity-telegram-questions-and-steer).

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
  run. Each run also adds up its own model calls (the `usage` on its `agent_response` steps, which matches the
  difference exactly), and the footer uses that figure ([#1062](https://github.com/littlebearapps/untether/issues/1062)): the first reply after a cancelled run
  shows only its own tokens, although agy's running total also holds the cancelled run's. Only a run with no
  per-step usage falls back to the difference, and the first one Untether sees of a conversation started elsewhere
  is then labelled "thread total".
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
session (the commands, file changes and answers), kept in memory for the 20 most recently active sessions. Its
`Model:` line is the model the chat set (`--model`, which agy echoes as `init.model`), or `agy default` when none
was set: agy names no model in its stream then ([#1058](https://github.com/littlebearapps/untether/issues/1058)).

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
- when the run ends or is cancelled, Untether also stops the child processes it saw agy start;
- the `⏳ background` block ends with `agy sends its answer when this work finishes (up to 30 min)`
  ([#1061](https://github.com/littlebearapps/untether/issues/1061)). agy prints nothing while it holds its answer, and when it is stopped during the hold its
  `result.response` is empty even though the answer's model call ran (agy 1.3.3), so the answer can't be shown from
  the stream. A `/cancel` while a listed task is still running adds a note to the cancelled message: the answer
  wasn't shown and the conversation kept it (a resumed "repeat your previous answer" returns it word for word). The
  answer text would need agy's transcript file, which Untether doesn't read.

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
background, but agy (1.3.3, 1.3.4) doesn't print that value. A command is listed when the model carries on while it is still
running or stops it with `manage_task` (certain), or after it has run for 5 seconds (a guess). So a normal blocking command that takes longer
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

- **No live sessions.** Approval cards are Ask me's ([Ask me](#ask-me)); Plan first has its own Approve / Deny /
  Let's discuss card (see [Plan first](#plan-first)). Approval cards, question buttons and steer need
  [Untether's agy gate](#untethers-agy-gate) on the host.
- **Ask me is not offered in a project with agy config Untether doesn't manage** (other hook groups, `.agents/`
  plugins, MCP servers or agents, user-level plugins or MCP servers). There is no "trust this config" option yet.
- **Ask me's previews for `sed_file`, `notebook_edit`, web and MCP tools are generic** (their argument shapes are
  unprobed, P-H13); `sed_file` and `notebook_edit` always ask.
- **Full access has no log-only gate** (the diff rows phase 14 §7 describes); it never claimed gating.
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
| Telling the user to send `/new` after switching between Workspace / Plan first and Ask me / Full access | — (custom wording only, no state; R3-3 extended it to Ask me) | agy ignores `--agent` on a resume and `init` doesn't say which agent a resumed conversation uses (`init.agent` echoes the flag; `init.tools` lists every tool). Re-checked 2026-10-13, agy 1.3.3, [#1060](https://github.com/littlebearapps/untether/issues/1060) |
| A background task agy stopped itself reads as stopped | `tool_info.error` on the step's DONE ("context canceled") and `manage_task`'s `Action: kill` / `TaskId: <cid>/task-<step>`; the kill is also proof the command was in the background, so a kill sooner than the 5 s guess below still lists it | — (native). agy sends the kill's step DONE and the kill together, and a `system_message` notice right after (1.3.4; on 1.3.3 it came after the next tool). Checked 2026-10-13, agy 1.3.3 and 1.3.4, [#1062](https://github.com/littlebearapps/untether/issues/1062) |
| Telling a cron or webhook run's model it is unattended ([#1055](https://github.com/littlebearapps/untether/issues/1055)) | — (**custom**: one line Untether puts before the prompt of an unattended Workspace run (and, #980, of a cron or webhook whose own `permission_mode` is `ask`: nobody can approve, so everything off the safe list is refused), `utils/antigravity_agent.scheduled_run_note`) | agy has no per-run system prompt (no flag, env var or stream-json field) and one agent file serves every run, so the model can't tell a cron from a chat; with both routes in the agent file it gave the chat's in 2 of 2 live cron fires (rc4 QA). Not in the preamble, which `[preamble] enabled = false` turns off. Checked 2026-10-13, agy 1.3.4. Retire when agy takes a per-run system prompt |
| agy 1.3.4 (self-updated 2026-10-13) | — (no change needed) | `agy changelog` and `--help` re-read; the CLI drift suite is green and `PROBED_CLI_VERSION` is 1.3.4. Relevant changes: an unknown `--agent` name now ends a headless run (rc 1, `ERROR` result naming the available agents) instead of quietly running the default agent, so a missing Workspace agent fails closed (pinned by the drift suite); `--model` with a base model id and no `--effort` now runs at that model's default level, and `/effort --model=<base id>` answers with its levels instead of the 1.3.3 "requires --effort" refusal (Untether still reads either); model-request retries now wait at most 30 s. Stream events, flags and `manage_task` shapes unchanged. Checked 2026-10-13 |
| Footer tokens for one run | `step_update.usage` on the run's `agent_response` steps, summed | `result.usage` is the conversation's running total; its difference also counts a cancelled run that never reported a result. The sum matched the difference exactly on every capture. Checked 2026-10-13, agy 1.3.3, [#1062](https://github.com/littlebearapps/untether/issues/1062) |
| Showing an answer held for background work | Partly native: with the gate, the `Stop` hook's `fullyIdle: false` says the model has finished its turn and agy is waiting (the `⏳ background` note says so); the text itself is a **gap** | Nothing is printed during the hold; a SIGTERM during it gives `result.response: ""` though the model call ran; `Stop` carries no text (and a timer can still start another model call, probe pb2). The text is only in `transcript_full.jsonl` (rule 3, not read). Checked 2026-10-13, agy 1.3.3/1.3.4, [#1061](https://github.com/littlebearapps/untether/issues/1061) |
| Questions in Telegram ([#980](https://github.com/littlebearapps/untether/issues/980)) | agy's own `ask_question` tool, answered through a `PreToolUse` hook (`deny` + a reason that carries the answer, E3); the hook is installed with `agy plugin install` | Partly **custom**: headless agy skips `ask_question` and has no answer channel but a hook. The hook command, socket and plugin files are Untether's. Checked 2026-10-13, agy 1.3.3/1.3.4 |
| Steer into a running turn ([#981](https://github.com/littlebearapps/untether/issues/981)) | `PreInvocation` hook `injectSteps[].userMessage` | Partly **custom** (the queue and the socket). agy's own "Queued Messages: send immediately" is a global TUI setting (`queuedMessages` in `-p /config`), not per run, and its effect on stream-json input is unprobed; stdin `control_request` is refused (rc 2). Checked 2026-10-12, agy 1.3.3 |
| Ask me approvals ([#980](https://github.com/littlebearapps/untether/issues/980)) | `--dangerously-skip-permissions` plus a `PreToolUse` hook answering `allow` / `deny` (`deny` reason shown to the model and not retried, E2); agy's default agent | Partly **custom**: the safe list, path rules, approval card, "always allow" grants and unattended policy are Untether's. agy has no headless approval prompt, and a hook `allow` can't grant a tool in agy's default mode (E1), so the gate needs the bypass flag; `ask` / `force_ask` are auto-approved under bypass, so they are never returned (D22.3). `permissionOverrides` is unprobed for persistence (P-H11) and never emitted. Checked 2026-10-13, agy 1.3.4 (live: approve, deny, timeout, always-allow, unattended deny, a subagent's call) |
| User-level MCP servers in scope for Ask me | `agy mcp list` ("No MCP servers configured.") | The planted-config scan only stats `~/.gemini/config/mcp_config.json` (D33); an existing file says nothing about servers (lba-1 has an empty one). `-p /mcp` is refused in print mode, and `agy mcp list` doesn't list a project's `.agents/mcp_config.json` (that stays with the scan). Checked 2026-10-13, agy 1.3.4 |
| A hook that fails blocks the tool (security review 3 M3) | Native: agy 1.3.4 under `--dangerously-skip-permissions` blocks the tool (`TOOL_ERROR`, "JSON hook … failed" / "failed to unmarshal result from hook") for a `PreToolUse` hook that exits 1, 2 or 127, is SIGKILLed, or prints non-JSON or truncated JSON; only empty output with exit 0 is "no decision" | Partly **custom** on top: the plugin's shell guard prints a `deny` itself when `UNTETHER_AGY_GATE_PY` isn't executable, so the guarantee doesn't rest on that agy behaviour alone. Probed 2026-10-13, agy 1.3.4, through the installed plugin's own command with a stand-in interpreter (`probes/1.3.4/`). Re-probe on an agy minor upgrade (`gate_canary.py` notes it) |
| Keeping the run's token from the commands agy runs (security review 3 L1) | — (**gap**) | agy 1.3.4 has no flag or setting to drop variables from a tool's environment (`--help`, `--sandbox` only restricts the terminal). Probed 2026-10-13: a `run_command` is a direct child of agy and sees the `UNTETHER_AGY_GATE*` variables, exactly as a hook does. Untether instead checks the connecting process (below agy's pid, the hook's exact command line); the residual is in [Security](../../../how-to/security.md#antigravity-telegram-questions-and-steer) |
| Search arguments as flags (security review 3 M4) | Native: agy 1.3.4 runs `git grep --untracked --no-recurse-submodules --fixed-strings -l -- <Query> <Includes…>` in a git repo and searches itself elsewhere; `find_by_name` searches itself. A `Query` of `--pre=<script>`, an `Includes` of `--pre=…`, a `Pattern` / `Excludes` of `--exec=…` ran nothing | Untether still refuses the safe list to any `grep_search` / `find_by_name` value starting with `-` (another version or backend could read it as a flag). Probed 2026-10-13, agy 1.3.4 (argv-logging `git` / `rg` / `find` shims on `PATH`; the Workspace agent — the default agent offers neither tool to the model in print mode) |
| Installing the gate's hooks | `agy plugin validate` / `install` / `uninstall` (R3-1) | A hand-dropped folder is loaded but not listed by `agy plugin list` and can't be enabled or disabled (P-H0) |
| Background rows for timers and subagents ([#978](https://github.com/littlebearapps/untether/issues/978)) | The stream's own steps: `schedule` (open for the timer's life), `invoke_subagent` (`Subagents[]` / `subagent_info.subagents[]`) | Nothing custom to rule out. `/tasks` and `/agents` are TUI only. Checked 2026-10-12, agy 1.3.3 |
| When a `run_command` counts as background | Partly native: `WaitMsBeforeAsync` / `IsDaemon` if the step's parameters carry them; a later model step while the command is still open (agy runs tool calls one at a time). Otherwise **custom**: 5 s since the step opened | The 1.3.3 and 1.3.4 streams carry only `CommandLine` (the per-call value, 5000 / 10000 / 500 / 0, is in hook payloads only), so headless stream mode fails the "available on Untether's surface" test. A `manage_task` kill naming the task also counts. Checked 2026-10-12, re-checked 2026-10-13 on agy 1.3.4. Retire when the stream prints the value or the approval gate's hook supplies it |
| Which subagent a completion belongs to | Partly native ([#1086](https://github.com/littlebearapps/untether/issues/1086)): a timer with a `TimerCondition` cut short by a message prints `Timer cancelled early: … met by a message from <sender>.` on its `DONE`, which names the child; `manage_subagents` `kill_all` / `kill` ends the rows it stops. Otherwise **custom**: first started, first finished | The parent stream's `system_message` step has no content (no sender, re-checked on 1.3.5) and the stream drops `TimerCondition` from the `schedule` parameters. The child's id is in agy's transcript file (`sender=`), which Untether doesn't read (rule 3: no engine state files). First in, first out is wrong whenever the second child finishes first (seen live on 1.3.5). Checked 2026-10-12, agy 1.3.3; re-checked 2026-10-14, agy 1.3.5. Retire when the stream names the child or the gate's hook reports it |
| `/continue` resumes only this project's conversation ([#1039](https://github.com/littlebearapps/untether/issues/1039)) | — (**custom**: reads one agy file, `~/.gemini/antigravity-cli/cache/last_conversations.json`, for the exact run directory and passes the native `--conversation=<id>`) | `--continue` is native but fails the "meets the feature's real requirement" test: it also resumes a conversation from a parent or child directory (probe P-L5, one run each way), and `--help` lists no flag, setting or env var that limits it to the current directory. Reading an engine state file is the exception recorded in decision D33, limited to this file. An id listed under more than one path is refused, because agy also records the directory a conversation was resumed from. Checked 2026-10-12, agy 1.3.3. Retire when agy scopes `--continue` to the current directory or adds a flag for it |
| Telling a timer's or command's completion notice from a subagent's | — (**custom**: one `system_message` is expected after each finished background command or timer and after each `manage_task` kill, and dropped when the next tool starts) | Same content-less `system_message`. A timer cut short by a message gets no notice of its own: that message is its wakeup, so the one expected is spent on it ([#1086](https://github.com/littlebearapps/untether/issues/1086)). Still a guess when a timer fires on time: agy holds every later line while the `schedule` step is open, so a subagent's result that arrived meanwhile is printed before the timer's own notice and is the one spent (1.3.5). Checked 2026-10-12, agy 1.3.3; re-checked 2026-10-14, agy 1.3.5. Retires with the row above |
| Context window for `~N% ctx` ([#979](https://github.com/littlebearapps/untether/issues/979)) | — (**custom**: a seeded table, `utils/antigravity_context.py`, of the input limits on Google's own model pages; an unknown model gets no percentage) | Fails "available in headless / stream-json mode". `contextWindow` in `-p /config` is a user setting, not a readout: empty with no `--model` and with each of five models tried. No key in 14 run streams names a window, a limit or a maximum. `/context` is refused in print mode ("it opens the interactive context breakdown"). The binary has a status-line payload (`context_window.context_window_size`) and `ContextWindowMetadata.max_context_tokens`; neither reaches print mode. `agy models` and `-p /model` list no window. The `gemini-3.1-pro` row assumes agy's id is the API model `gemini-3.1-pro-preview`. Checked 2026-10-12, agy 1.3.3. **Retire when agy reports a window in the stream** |
| Tokens in context for `~N% ctx` | `step_update.usage` on the latest finished `agent_response` step: `input_tokens` + `cache_read_tokens` | `result.usage` is a running sum over every model call and every earlier turn, so it is not the context size. agy has no context-usage request and no compaction signal, so the figure is not corrected for compaction. Checked 2026-10-12, agy 1.3.3 (`step_update.usage` was seen on 1.3.1 and 1.3.3) |
| Which model a run used, for `~N% ctx` | `init.model` only | It is present only when `--model` is passed (it echoes the flag); with no flag nothing in the stream names the model, so no percentage is shown. `-p /model` answers with the default model for 0 tokens but is a second process answering for the account, not for the run, and a hook payload's `modelName` needs the approval gate; neither is used. The id Untether would pass is not used either: it is not proof of what agy ran. Checked 2026-10-12, agy 1.3.3 |
| AI credits on `/usage` ([#979](https://github.com/littlebearapps/untether/issues/979)) | `agy -p /credits` (0 tokens; `remaining_credits`, `upgrade_uri`) | Nothing custom. Checked 2026-10-12, agy 1.3.3. Not checked on an API-key sign-in |
| Quota line under an answer | The cached answer of `agy -p /usage` | Partly custom: agy has no push or cheap in-stream quota value (`/usage` is refused inside a stream-json session), and a probe before each final would delay it, so the line only reads what `/usage` cached (15 min at most). Checked 2026-10-12, agy 1.3.3. Revisit if the stream starts carrying quota |
| Plan first ([#981](https://github.com/littlebearapps/untether/issues/981)) | `--mode=plan` under the no-shell agent; approve = `--conversation=<id> --mode=accept-edits`; revise = `--conversation=<id> --mode=plan` | Partly **custom**: agy has no headless "plan ready" signal (`result` and `init.permission_mode` are the same as any turn's) and no way to answer a plan from print mode, so Untether detects the plan from the finished `write_to_file` into `brain/<conversation id>/`, reads that one file and its sidecar (realpath-contained; the D33 exception extended to this file), and keeps the pending-card store itself. agy 1.3.2's `artifactReviewPolicy` is a settings key (changing it would edit the user's agy settings). Checked 2026-10-12/13, agy 1.3.3. Retire when the stream carries the plan or agy can wait for a review headless |
| `/export` | — (**custom**: Untether's in-memory record of the events it saw) | agy writes `transcript_full.jsonl` under `brain/<id>/` but has no export command or flag for it, and reading it is rule 3 (an engine state file, holding whole file contents). Not read. Checked 2026-10-12, agy 1.3.3 |

Retire or revisit when agy can continue a turn after a soft-denial, takes a per-run system prompt or tool list, or
lets `--agent` change on a resume.

## Configuration

```toml
[antigravity]
model = "gemini-3.8-flash"     # optional; passed as --model=<id>
cmd = "~/.local/bin/agy"       # optional; default: agy on PATH, then ~/.local/bin/agy
permission_mode = "workspace"  # workspace (default) | ask | plan | full
approval_timeout_s = 900       # optional; Ask me's approval wait, 60-3000 s, never longer than the plugin's
```

See the [config reference](../../config.md#antigravity). Unknown keys are ignored.

## Log events

`antigravity.session.started` (`agy_version`, `resumed`), `antigravity.run.timing`, `antigravity.denied_actions`,
`antigravity.auth.required` / `.killed`, `antigravity.account.blocked`, `antigravity.quota.exhausted`,
`antigravity.credits.low`, `antigravity.conversation.missing`, `antigravity.permission_mode.mismatch`,
`antigravity.workspace_config_present` / `_changed` / `.unattended_refused`, `antigravity.config_check.fetched` /
`.failed`, `antigravity.effort.dropped`, `antigravity.tos_notice.shown`, `antigravity.version.unsupported`,
`antigravity.argv.invalid_value`, `antigravity.continue.resolved` / `.refused` (`reason`) / `.lookup_failed`,
`antigravity.workspace_agent.installed` / `.unavailable`, `antigravity.plan.detected` / `.card` / `.no_artifact` /
`.superseded` / `.sidecar_missing` / `.unreadable` / `.decision` (`decision`, `path=resume`) / `.tap_refused` /
`.cwd_changed`,
`antigravity.gate.listening` (`token_len`) / `.closed` / `.precheck` / `.precheck_failed` (`reason`) / `.request`
(`hook_event`, `tool`, `step`, `child`, `foreign`) / `.decision` (`decision`, `kind`, `tool`, `source` =
`policy` / `user` / `always` / `timeout` / `unattended` / `internal` / `rate`) / `.card` (`request_id`, `tool`, `why`)
/ `.always_granted` (`command_sha`) / `.withdrawn` / `.user_mcp` / `.ambiguous_args` / `.policy_failed` /
`.bypass_without_gate` / `.not_running` / `.tripwire` /
`.duplicate_request` / `.peer_refused` / `.token_refused` / `.plugin_changed` / `.socket_failed` /
`.handler_failed`, `antigravity.permission_mode.gate_refused` (`reason`), `antigravity.unattended_ask_downgraded`,
`permission.unattended_deny` (`engine=antigravity`, `permission_mode=ask`), `antigravity.steer.queued` / `.injected` / `.delivered` (`step`), `ask_question.shown` /
`.answered` / `.withdrawn` (`engine=antigravity`),
`antigravity.background.started` / `.finished` (`task_id`, `status`, `attribution=fifo` for a subagent) / `.outlived`
(a command still running when the answer arrived), `antigravity.quota.fetched` / `.failed`,
`antigravity.credits.fetched` / `.failed`, `antigravity.context.window_miss` (DEBUG, once per run: the model has
no known window) / `.over_window` (WARN: more tokens than the table's window, shown as 100 %). `runner.completed`
carries `context_pct` when a percentage was shown.

## See also

- [Stream JSON cheatsheet](stream-json-cheatsheet.md) and [Untether events](untether-events.md)
- [Switch engines](../../../how-to/switch-engines.md) — install and sign-in
- [Error reference](../../errors.md) — hints for common errors
