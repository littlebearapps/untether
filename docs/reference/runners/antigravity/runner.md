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
| Tested against | agy **1.3.2** (fixtures captured on 1.3.1 and 1.3.2) |
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
    [--conversation=<id> | --continue] [--model=<model>] [--effort=<level>] [--dangerously-skip-permissions]
```

- **The prompt goes on stdin**, as one line `{"event":"user","message":{"content":"…"}}`, and stdin is then closed.
  It is never on the command line, so it can't be read from the process list or mistaken for a flag.
- `--disable-slash-commands` makes a prompt that starts with `/` an ordinary message. Untether adds no prefix.
- `--print-timeout 0` is agy's default (no limit) made explicit. If agy ever hits a print timeout it reports
  success with a partial answer, so Untether logs `antigravity.print_timeout` when it sees that line.
- `--dangerously-skip-permissions` is passed only in Full access (see [Permission modes](#permission-modes)).
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
itself passes the id to agy in the joined form `--conversation=<id>`. `/continue` uses `agy --continue`, which picks
the most recent conversation for the project directory. agy also considers a parent or child directory's
conversations there, so run `/continue` from the directory you used in the terminal.

If the conversation id is unknown, agy prints a warning and silently starts a new conversation. Untether stops agy
before your message runs somewhere you didn't pick, and replies: `That Antigravity conversation no longer exists …
send your message again to start a new one.` The chat's saved session is cleared.

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
| **Workspace** (default) | `workspace` | (none) | agy's own `request-review` policy: file edits in the project and temp folders go ahead; shell, web and MCP calls are blocked by agy. Not a sandbox |
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
  agy before any tool. If the settings check itself fails, the run goes ahead and the second check still applies.
- **Widened settings are shown.** `permissions.allow` rules or `allowNonWorkspaceAccess` in agy's settings add a
  ⚠️ row (once per project and settings change), and the footer label becomes
  `workspace (agy allows files outside the project)`.

### When agy blocks a tool

A blocked tool doesn't fail in agy's stream: the step ends normally with no output, and the final event lists
`denied_actions`. Untether turns that into a row and an explanation:

- the step becomes `⚠️ Blocked: shell command (RunCommand) — <command>` (also `MCP tool`, `web fetch`,
  `browser action`, `file outside the project`);
- the final reply ends with a paragraph that depends on how the run started, for example in a chat:
  `⚠️ Antigravity was blocked from using a shell command — headless runs can't ask for approval, so it stopped
  there. To allow it: /config → Permission mode → Full access, or add an allow rule such as command(git status) to
  agy's settings file.` A cron is told to set `permission_mode = "full"` on the cron; a webhook is told it never
  gets Full access; in Full access the paragraph points at agy's own `permissions.deny` rules.

Exact commands are the reliable form of allow rule. agy 1.3.x stops the turn at the first blocked tool.

### agy hooks, plugins and MCP servers

No mode stops agy's own hooks, plugins, custom agents or `.agents/mcp_config.json` MCP servers from running their
commands: agy starts them without a tool call. A run can also write those files for the next run. Untether watches
for this, as a tripwire and not a sandbox:

- **First sight.** The first attended run in a project that carries such config shows `⚠️ This project has agy
  hooks, plugins or MCP servers that Untether doesn't manage (…). They run their own commands in every mode,
  including Workspace. Review them if you didn't add them.` The state is remembered per project in
  `antigravity_seen_config.json` beside `untether.toml`.
- **Changed since someone looked.** A cron or webhook run is refused while the config differs from what an
  attended run last showed: `agy's workspace config changed since someone last ran Antigravity in this chat's
  project (…). Send any message in the chat to review it; the schedule runs again after that.`
- **Couldn't check.** If the check can't finish (too many files, unreadable files, links leading outside the
  project and home directory, a config file that isn't strict JSON, a command path built from a variable), every
  attended run shows a ⚠️ row and scheduled runs are held until a check succeeds.
- **Changed during the run.** After the run Untether checks again and adds `⚠️ Antigravity changed … — agy (or
  another engine) will load it on the next run. Review it before sending another message.` This also covers files
  other engines act on: `.claude/`, `.mcp.json`, `.envrc`, `.github/workflows/`, `.git/hooks/`, `.husky/`,
  `.vscode/tasks.json`, `.pre-commit-config.yaml`, `CLAUDE.md`, `AGENTS.md` and `GEMINI.md`. That part only warns;
  it never refuses a run.

What is tracked: every file under each `.agents/` folder from the working directory up to the project root (except
the `skills/` and `rules/` instruction folders), the scripts those manifests name by a literal path (wherever the
name exists: beside the manifest, in the working directory or at the project root), the plugin folders and manifests
a custom agent's `plugins:`, `hooks:` or `agents:` front matter points at, and agy's user-level manifests under
`~/.gemini/config/` (by file size and timestamps only; Untether doesn't read them).

Known limits:

- Only scripts named by a literal path are followed. Code reached indirectly (a package manager script, a module
  name, a `PATH` lookup, a download) isn't tracked, and a folder passed to a hook or MCP command as an argument
  isn't walked.
- User-level config is checked by timestamp, not content, and agy's own `settings.json` isn't part of the check
  because agy rewrites it itself. Both sit outside the project.
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
- Cost budgets (`[cost_budget]`) count dollars, so they never trip for Antigravity.

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

agy sends one answer per run and holds it until background commands finish (up to 30 minutes). While it waits:

- a shell command that has been running for more than 5 seconds is retitled `⏳ background: <command>`;
- where a stall warning would otherwise appear, the progress message shows `⏳ Antigravity is waiting for 1 background task
  (N min) — agy holds its answer until they finish (up to 30 min).` The run is not auto-cancelled for it
  (`subprocess.background_wait` in the logs, stall reason `background_waiting`);
- when the run ends or is cancelled, Untether also stops the child processes it saw agy start.

This release treats every running shell command as possible background work, so a foreground command that hangs
delays ordinary stall warnings for up to 31 minutes. Tasks that agy leaves running as daemons after its answer
aren't tracked.

## Known gaps

- **No approval buttons, plan gate, question buttons, steering or live sessions yet.** Ask me and Plan first
  arrive in a later 0.36.1 release.
- **Subagents** appear as one row; their own steps and output aren't shown.
- **Images.** A Telegram photo reaches agy as a saved file path, not as image input.
- **Long turns can end `CANCELED`** upstream; retry or split the task.
- **Quota and MCP start-up hangs.** A used-up quota on a Google sign-in is stopped by the stderr match above. An
  MCP server that hangs at start-up is left to the ordinary stall warnings and watchdog.

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
`antigravity.argv.invalid_value`.

## See also

- [Stream JSON cheatsheet](stream-json-cheatsheet.md) and [Untether events](untether-events.md)
- [Switch engines](../../../how-to/switch-engines.md) — install and sign-in
- [Error reference](../../errors.md) — hints for common errors
