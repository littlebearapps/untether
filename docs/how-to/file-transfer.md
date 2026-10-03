# File transfer

Send files to your repo or pull results back — directly from Telegram on any device. Useful when you need to share a spec, screenshot, or config file with your agent without opening a terminal.

## Enable file transfer

=== "untether config"

    ```sh
    untether config set transports.telegram.files.enabled true
    untether config set transports.telegram.files.auto_put true
    untether config set transports.telegram.files.auto_put_mode "upload"
    untether config set transports.telegram.files.uploads_dir "incoming"
    untether config set transports.telegram.files.allowed_user_ids "[123456789]"
    untether config set transports.telegram.files.deny_globs '[".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"]'
    ```

=== "toml"

    ```toml
    [transports.telegram.files]
    enabled = true
    auto_put = true
    auto_put_mode = "upload" # upload | prompt
    uploads_dir = "incoming"
    allowed_user_ids = [123456789]
    deny_globs = [".git/**", ".env", ".envrc", "**/*.pem", "**/.ssh/**"]
    ```

Notes:

- File transfer is **disabled by default**.
- If `allowed_user_ids` is empty, private chats are allowed and group usage requires admin privileges.
- Setting `deny_globs` **replaces** the built-in list. The defaults also cover `.env.*`, `*.key`, `id_rsa`, `id_ed25519`, `.netrc`, `.npmrc` and `.pypirc`; copy the full list from [Security → File transfer deny globs](security.md#file-transfer-deny-globs) and add to it rather than starting from the short example above.

## Local Bot API and larger files

To use a self-hosted Bot API server in local mode, configure its loopback
endpoint and the absolute data directory visible to Untether:

```toml
[transports.telegram]
bot_api_base_url = "http://localhost:8081"
bot_api_local_dir = "/var/lib/telegram-bot-api"

[transports.telegram.files]
enabled = true
max_download_bytes = 104857600 # 100 MiB; default 50 MiB, maximum 2 GiB
```

The endpoint receives your bot token; use only a server you trust. Endpoint
and data-directory changes require a restart. The size limit hot-reloads.
Mount the server's data directory at the same path if Untether runs in a
separate container.

Absolute cache paths are read only when their resolved location is inside
`bot_api_local_dir`. Without that setting, absolute paths are refused. Symlinks
that escape the directory, non-regular files and files exceeding the size limit
are rejected before reading. The read is bounded even if the file grows after
the size check; a growing file is refused rather than returned truncated.
Accepted files are still buffered in memory, so choose a limit that fits your
host. The public Bot API endpoint and the 50 MiB default remain unchanged.

Keep the Bot API cache and its parent directories writable only by trusted
local users. These path checks protect against an untrusted `getFile` response;
they are not a sandbox against a local process that can replace cache entries
between the path check and the read.

## Upload a file (`/file put`)

Send a document with a caption:

```
/file put <path>
```

Examples:

```
/file put docs/spec.pdf
/file put /happy-gadgets @feat/camera assets/logo.png
```

If you send a file **without a caption**, Untether saves it to `incoming/<original_filename>`.

!!! note "iOS: captions on documents"
    Telegram on iOS doesn't always show a caption field when sending files via the "File" picker — the file sends immediately. To add a `/file put <path>` caption on iOS, send photos (which always show the caption field) or use **Telegram Desktop / macOS**, which shows a caption field for all file types. Alternatively, skip the caption and let files auto-save to `incoming/`.

If the target file already exists, Untether auto-appends a numeric suffix (`_1`, `_2`, etc.) to avoid collisions — so `spec.pdf` becomes `spec_1.pdf`. Use `--force` to overwrite instead:

```
/file put --force docs/spec.pdf
```

!!! untether "Untether"
    saved `docs/spec.pdf` in `happy-gadgets` (42 KB)

!!! note "Path safety"
    Deny globs are checked against the path you give **and** the path it resolves to after following symlinks inside the project, so an in-root symlink can't route an upload into `.git/hooks` or onto `.env` ([#390](https://github.com/littlebearapps/untether/issues/390)). A refused path gets a reply naming the rule, such as ``path denied by rule: `**/.ssh/**` ``, with `(resolves to …)` added when a symlink led there. Paths that leave the project root are refused. If you upload through a symlinked folder, the confirmation shows the real path the file landed at (e.g. `inbox/a.txt` → `data/inbox/a.txt`).

<img src="../assets/screenshots/file-put.jpg" alt="Photos uploaded and auto-saved with confirmation" width="360" loading="lazy" />

## Fetch a file (`/file get`)

Send:

```
/file get <path>
```

Directories are zipped automatically.

!!! note "Path safety"
    The same double check applies to downloads: `/file get cfg.txt` is refused when `cfg.txt` is a symlink to `.env`, and a symlinked directory is zipped with every member checked against its real path. The file (or archive) keeps the name you asked for ([#390](https://github.com/littlebearapps/untether/issues/390)).

Untether replies with the file as a Telegram document (no caption).

<img src="../assets/screenshots/file-get.jpg" alt="/file get response showing fetched file as a document" width="360" loading="lazy" />

## Agent-initiated delivery (outbox)

Agents can send files to you automatically — plan docs, generated images, scripts, data exports — without you having to request them. The agent writes files to a special `.untether-outbox/` directory during its run, and Untether delivers them as Telegram documents when the run finishes.

```
┌─────────────┐    writes     ┌──────────────────┐    run ends    ┌───────────┐    sends     ┌──────────┐
│  Agent CLI   │ ──────────── │ .untether-outbox/ │ ────────────── │  Untether  │ ──────────── │ Telegram │
│ (any engine) │   files      │   plan.md         │   scan + send  │  outbox    │  📎 docs    │  client  │
└─────────────┘              │   diagram.svg     │                │  delivery  │             └──────────┘
                              └──────────────────┘                └───────────┘
```

Every agent session receives a preamble telling it about the outbox. The agent decides which files to share — you receive them as Telegram document messages with `📎 filename (size)` captions, arriving just after the final text response.

### Configuration

Outbox delivery is enabled by default when file transfer is enabled:

=== "untether config"

    ```sh
    untether config set transports.telegram.files.outbox_enabled true
    untether config set transports.telegram.files.outbox_dir ".untether-outbox"
    untether config set transports.telegram.files.outbox_max_files 10
    untether config set transports.telegram.files.outbox_cleanup true
    ```

=== "toml"

    ```toml
    [transports.telegram.files]
    enabled = true
    outbox_enabled = true            # default: true (when files.enabled)
    outbox_dir = ".untether-outbox"  # relative to project root
    outbox_max_files = 10            # max files per run (1–50)
    outbox_cleanup = true            # delete sent files after delivery
    ```

### How agents use it

Agents receive instructions in their session preamble. They create the directory and write files using their standard tools:

```bash
mkdir -p .untether-outbox
cp docs/plan.md .untether-outbox/
cp output/diagram.svg .untether-outbox/
```

When the run finishes successfully, Untether:

1. Scans `.untether-outbox/` for files (flat scan, no subdirectories)
2. Validates each file against deny globs, size limits, and path traversal rules
3. Sends valid files as Telegram documents with `📎 filename (size)` captions
4. Cleans up — deletes sent files and removes the empty directory

Files are sent in alphabetical order, one at a time, immediately after the agent's text response.

### Security

Outbox delivery reuses the same security rules as `/file get`:

- **Deny globs** — files matching `deny_globs` (by default `.git/**`, `.env` and `.env.*` files, `.envrc`, keys and certificates, `.ssh/**`, `.netrc`, `.npmrc`, `.pypirc`) are surfaced to the user as a `📎 Outbox skipped` notice rather than silently dropped (#524)
- **Size limit** — files larger than 50 MB are skipped (and surfaced via the same notice)
- **Symlinks** — every symlink in the outbox is skipped (reported as `symlink`), wherever it points
- **Empty files** — zero-byte files are skipped too
- **File count** — capped at `outbox_max_files` per run (default 10)
- **Auto-cleanup** — sent files are deleted after delivery by default, preventing sensitive data accumulation
- **Failed/auto-continued runs** — actual file delivery is still gated on a successful run, but skipped items (directories, deny-globbed files, oversized files) are surfaced even when the run fails or auto-continues, so you always learn what the agent intended to send. Opt out via `outbox_notify_skipped = false`.

### Engine compatibility

All engines support outbox delivery — any agent that can write files to disk can use it.

| Engine | Works out of the box? | Notes |
|--------|-----------------------|-------|
| Claude Code | Yes | Best file generation capability |
| Codex CLI | Yes | — |
| OpenCode | Yes | — |
| Pi | Yes | — |
| Gemini CLI (deprecated) | Needs config | Set approval mode to "Full access" via `/config` → Approval mode |
| AMP (deprecated) | Yes | — |

!!! tip "Gemini CLI permissions"
    Gemini CLI defaults to read-only approval mode. To enable file creation (and outbox delivery), set the approval mode to "Full access" via `/config` → **Approval mode** in the Gemini chat.

### Limitations

- **Sub-directories need opt-in** — by default only files directly in `.untether-outbox/` are sent; a subdirectory is archived to `.untether-outbox/.skipped/` and surfaced as `📎 Outbox skipped` (#524). Set `outbox_deliver_directories = "zip"` (#628) to bundle each one into a single `<name>.zip` document instead — recursive deny-globs, symlink pruning, and per-member/total/size caps apply.
- **Failed runs deliver no files** — the actual file send is still gated on a successful run, but the skipped-items notice fires either way so the user always learns what the agent intended to ship.
- **No real-time delivery** — files are sent after the run completes, not during.

<!-- TODO: capture screenshot of outbox delivery in Telegram -->

## Related

- [Commands & directives](../reference/commands-and-directives.md)
- [Config reference](../reference/config.md)
