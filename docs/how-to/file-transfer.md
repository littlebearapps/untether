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
    📄 saved `docs/spec.pdf` (42 KB)

<img src="../assets/screenshots/file-put.jpg" alt="Photos uploaded and auto-saved with confirmation" width="360" loading="lazy" />

## Fetch a file (`/file get`)

Send:

```
/file get <path>
```

Directories are zipped automatically.

!!! untether "Untether"
    📎 `src/main.py` (1.2 KB)

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

- **Deny globs** — files matching `.git/**`, `.env`, `.envrc`, `**/*.pem`, `**/.ssh/**` (and any custom deny globs) are silently skipped
- **Size limit** — files larger than 50 MB are skipped
- **Path traversal** — symlinks pointing outside the project root are rejected
- **File count** — capped at `outbox_max_files` per run (default 10)
- **Auto-cleanup** — sent files are deleted after delivery by default, preventing sensitive data accumulation
- **Successful runs only** — outbox is not scanned on errored or cancelled runs

### Engine compatibility

All engines support outbox delivery — any agent that can write files to disk can use it.

| Engine | Works out of the box? | Notes |
|--------|-----------------------|-------|
| Claude Code | Yes | Best file generation capability |
| Codex CLI | Yes | — |
| OpenCode | Yes | — |
| Pi | Yes | — |
| Gemini CLI | Needs config | Set approval mode to "Full access" via `/config` → Approval mode |
| AMP | Yes | — |

!!! tip "Gemini CLI permissions"
    Gemini CLI defaults to read-only approval mode. To enable file creation (and outbox delivery), set the approval mode to "Full access" via `/config` → **Approval mode** in the Gemini chat.

### Limitations

- **Flat scan only** — only files directly in `.untether-outbox/` are sent; subdirectories are skipped. Agents can zip nested structures if needed.
- **Successful runs only** — if the agent errors or is cancelled, the outbox is not scanned.
- **No real-time delivery** — files are sent after the run completes, not during.

<!-- TODO: capture screenshot of outbox delivery in Telegram -->

## Related

- [Commands & directives](../reference/commands-and-directives.md)
- [Config reference](../reference/config.md)
