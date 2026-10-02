# Telegram Transport

## Overview

`TelegramClient` is the single transport for Telegram writes. It owns a
`TelegramOutbox` that serialises send/edit/delete operations, applies
coalescing, and enforces rate limits + retry-after backoff.

This document captures current behaviour so transport changes stay intentional.

## Flow

1. Engine CLI emits JSONL events.
2. We render progress on every step and diff against the last output.
3. Only deltas enqueue a Telegram edit.
4. High-value messages enqueue a send.
5. All writes go through the outbox.

## HTTP client

`HttpBotClient` (`telegram/client_api.py`) talks to the Bot API over one
pooled `httpx` client:

| Call | Timeout |
|---|---|
| Message calls (send, edit, delete, answer callback, other JSON methods) | 30 s, with a 10 s connect timeout |
| Uploads (`sendDocument`) and file downloads | 120 s |
| `getUpdates` | the long-poll timeout + 20 s (70 s for the default 50 s poll) |

A short message timeout matters because one dead pooled connection would
otherwise block the chat's outbox for the full bulk timeout. A failed call is
retried **once**, on a fresh connection, when the request never left (connect
error, connect timeout or pool timeout) — or, for `editMessageText`,
`editMessageReplyMarkup`, `deleteMessage` and `answerCallbackQuery`, also after
a read/write timeout or a dropped connection, since repeating those is harmless.
A `sendMessage` that may already have reached Telegram is never repeated, so a
final can't be duplicated. Each retry logs WARNING `telegram.network_retry`; a
second failure logs ERROR `telegram.network_error`. `getUpdates` is never
retried here — the poll loop retries on its own
([#861](https://github.com/littlebearapps/untether/issues/861)).

## Incoming messages

`parse_incoming_update` accepts text messages (and media captions), voice
notes, documents, videos and photos (the largest size), stickers when sent
with a `/file` command, and inline-keyboard `callback_query` updates. Messages
from chats outside the allowed set are dropped.

### Voice transcription

If voice transcription is enabled, untether downloads the voice payload from Telegram,
transcribes it with OpenAI, and routes the transcript through the same command and
directive pipeline as typed text.

Configuration (under `[transports.telegram]`):

=== "untether config"

    ```sh
    untether config set transports.telegram.voice_transcription true
    untether config set transports.telegram.voice_transcription_model "gpt-4o-mini-transcribe"

    # local OpenAI-compatible transcription server (optional)
    untether config set transports.telegram.voice_transcription_base_url "http://localhost:8000/v1"
    untether config set transports.telegram.voice_transcription_api_key "local"
    ```

=== "toml"

    ```toml
    voice_transcription = true
    voice_transcription_model = "gpt-4o-mini-transcribe" # optional
    voice_transcription_base_url = "http://localhost:8000/v1" # optional
    voice_transcription_api_key = "local" # optional
    voice_transcription_url_allowlist = ["127.0.0.0/8"] # required for a loopback/private base_url (SSRF guard, #381)
    voice_transcription_language = "en" # optional ISO-639-1 hint
    voice_transcription_prompt = "Trello, Untether, Claude Code" # optional vocabulary bias
    voice_max_bytes = 10485760 # default 10 MB; larger notes are refused
    voice_show_transcription = true # default; echo "🎙 <transcript>" before the run
    ```

Set `OPENAI_API_KEY` in the environment (or `voice_transcription_api_key` in config).
If transcription is enabled but no API key is available or the audio download fails,
untether replies with a short error and skips the run.

To use a local OpenAI-compatible Whisper server, set `voice_transcription_base_url`
(and `voice_transcription_api_key` if the server expects one). This keeps engine
requests on their own base URL without relying on `OPENAI_BASE_URL`. If your server
requires a specific model name, set `voice_transcription_model` (for example,
`whisper-1`).

Since v0.35.4 the base URL is SSRF-validated ([#381](https://github.com/littlebearapps/untether/issues/381)) with the same guard as trigger requests ([Triggers → Security](../triggers/triggers.md#security)): a loopback or private-network host (such as `http://localhost:8000/v1`) is refused unless you allowlist it with `voice_transcription_url_allowlist` (a list of CIDR/IP strings, e.g. `["127.0.0.0/8"]`; a bare IP means that single address). The default public path (`base_url` unset) skips validation.

- **At config load**, a non-`http(s)` scheme, a blocked IP literal or a malformed allowlist entry is a config error; an IP-literal rejection names the `voice_transcription_url_allowlist` entry that would allow it.
- **At startup and after a hot-reload** that touches `voice_transcription`, `voice_transcription_base_url` or `voice_transcription_url_allowlist`, a background check resolves the host and logs WARNING `voice.base_url.not_permitted` (with the suggested allowlist entry) for a blocked host, `voice.base_url.check_failed` for a DNS failure, or INFO `voice.base_url.permitted`. It is log-only and never blocks startup.
- **On each voice note**, a refused endpoint gets a reply instead of a run ([#679](https://github.com/littlebearapps/untether/issues/679)). For a loopback or private address (RFC 1918, CGN/tailnet, IPv6 unique-local) it names the host and the exact entry to add — `127.0.0.0/8` for IPv4 loopback, otherwise the single address — as a `voice_transcription_url_allowlist = [...]` TOML line (the key hot-reloads). A link-local or cloud-metadata address is never suggested; the reply says to use a different host. A DNS failure says the host could not be resolved. The reply names the host only, never the full URL, path or credentials, and `ssrf.*` log lines redact URL userinfo.

If your voice notes are always in one language, set `voice_transcription_language`
to an ISO-639-1 code (for example, `en`). This is passed as the Whisper `language`
parameter and prevents wrong-language transcriptions on short utterances. Unset,
the provider auto-detects the language.

If transcription keeps mangling domain proper nouns ("trollo" → Trello), set
`voice_transcription_prompt` to a short comma-separated list of your project and
tool names ([#691](https://github.com/littlebearapps/untether/issues/691)). It is
passed as the transcription `prompt` parameter (vocabulary/context bias). Keep it
to genuinely high-frequency nouns — provider prompt windows are token-capped
(~224 tokens for Whisper), the effect is model-dependent, and an overstuffed
prompt can induce hallucinated terms on short or silent clips. Values longer
than 1000 characters are rejected at config load. The prompt text is never
logged.

The default list is short and its order doesn't matter much. Only if your
override is longer than Whisper's ~224-token prompt window (roughly 500
characters of a comma-separated list): OpenAI Whisper keeps only the **last**
224 tokens, so put your most important terms at the end. Other providers may
truncate or reject an over-long prompt, so keep it under that size.

Since v0.35.5 the key is **not** inert when unset
([#703](https://github.com/littlebearapps/untether/issues/703)): Untether ships a
product-generic default covering the terms every user speaks —

```
Claude, Claude Code, CLAUDE.md, AGENTS.md, Codex, OpenCode, Untether, Telegram, MCP, CLI, repo, changelog, PyPI
```

Deployment-specific nouns (your project names, hostnames, third-party tools) are
deliberately **not** in the default — add them yourself. Setting the key
**replaces** the default rather than extending it, so include the engine names
you care about in your own value (the default no longer lists Gemini, Amp or Pi,
[#789](https://github.com/littlebearapps/untether/issues/789)). Set it to an empty string (`""`) to disable the
bias entirely and omit the parameter, the same way `[preamble] text = ""` works.

### Listen mode (mentions-only)

Telegram’s bot privacy mode stops bots from seeing every message by default, but
**admins always receive all messages** in groups. If you promote untether to admin,
Telegram will deliver every update even when privacy mode is enabled.

To restore “only respond when invoked” behaviour, use listen mode:

- `all` (default): any message can start a run (subject to ignore rules).
- `mentions`: only start when explicitly invoked.

Explicit invocation includes any of:

- `@botname` mention in the message.
- `/<engine-id>` or `/<project-alias>` as the first token.
- Replying to a bot message.
- Built-in or plugin slash commands (for example `/agent`, `/model`, `/reasoning`, `/file`, `/listen`).

Note: In forum topics, some Telegram clients include `reply_to_message` on every
message, pointing at the topic’s root service message (`message_id ==
message_thread_id`). Untether treats those as implicit topic references, not
explicit replies, so they do not trigger mentions-only mode.

Commands:

- `/listen` shows the current mode and defaults.
- `/listen mentions` restricts runs to explicit invocations.
- `/listen all` restores the default behaviour.
- `/listen clear` clears a topic override (topics only).

`/trigger` continues to work as a deprecated alias for one release cycle ([#297](https://github.com/littlebearapps/untether/issues/297)) and prints a one-line deprecation notice on each invocation.

In group chats, changing listen mode requires the sender to be an admin.

State is stored in `telegram_chat_prefs_state.json` (chat default) and
`telegram_topics_state.json` (topic overrides) alongside the config file.

### Forwarded message coalescing

Telegram sends a "comment + forwards" burst as separate messages, with the comment
arriving first. Untether waits briefly so it can attach the forwarded messages and
run once.

Behaviour:

- When a prompt candidate arrives, Untether waits for `forward_coalesce_s` seconds
  of quiet for that sender + chat/topic.
- Forwarded messages arriving during the window are appended to the prompt
  (separated by blank lines) and do not start their own runs.
- Forwarded messages by themselves do not start runs.
- Another plain prompt from the same sender inside the window is **merged**:
  the texts are joined in order with a blank line and run once, anchored on the
  latest message (logged at INFO as `forward.prompt.merged` with
  `merged_count`). Earlier releases replaced the pending prompt and silently
  dropped its text ([#794](https://github.com/littlebearapps/untether/issues/794)).
- A prompt that can't share a run with the pending one — it replies to a
  different message, it's a voice transcript while the other isn't, one is a
  `/steer <text>` or `/queue <text>` override and the other isn't, it starts
  with a directive (`/codex …`, `/project …`, `@branch …`), or the chat's
  context changed in between — sends the pending prompt straight away as its own
  run (`forward.prompt.flushed` with a `reason`). Nothing is dropped.
- Slash commands are never merged into a prompt, and a command is a
  **barrier** for the window ([#807](https://github.com/littlebearapps/untether/issues/807)):
  - `/cancel`, `/new` and `/continue` **drop** the pending prompt and reply to
    it with `🗑️ Dropped 1 message sent just before /<cmd> — send it again if
    you still need it.` (or `Dropped N messages … send them again if you still
    need them.`; N counts merged prompts and attached forwards), logged as
    `forward.prompt.dropped` with `reason` and `merged_count`. Sending it first would only start a run for the command
    to kill, or run it in the session being left. Before 0.35.5rc14
    `/continue` dropped it silently and `/cancel` / `/new` let it run after
    them.
  - Every other command **flushes** the pending prompt first
    (`forward.prompt.flushed reason=command`). Ordering is best-effort: the
    prompt is dispatched before the command is handled, but its run starts
    asynchronously, so a `/model` or `/planmode` sent right behind it can
    still apply to it.
  - Prompt directives (`/<engine>`, `/<project>`) and `/steer <text>` are
    prompts, not commands: they follow the merge/flush rules above.
- Replies to a message whose run is still going bypass the window.

### Follow-up mode (steer / queue)

What a message sent while a Claude run is still working does is set by
`followup_mode` ([#775](https://github.com/littlebearapps/untether/issues/775)):

- `queue` (default): the message waits for the running turn to end, then runs
  as its own turn.
- `steer`: the message is written into the live Claude session straight away
  (`↪️ Steered into the current run.`). Mid-tool, Claude folds it into the turn
  it is already running; after the turn's last tool call it runs as the next
  turn in the same process.

Resolution: topic override → chat default → `[transports.telegram]
followup_mode` → `queue`. `/steer <text>` and `/queue <text>` override once;
bare `/steer` / `/queue` set the chat (or topic) default, as does the `/config`
Follow-up page. Only plain text and voice transcripts steer — files, media
groups, forwards and commands always queue, and a pending `AskUserQuestion`
takes the message as its answer first. Steering is Claude-only: other engines,
or a chat with no live Claude run, fall back to queueing with a one-line notice
(`↪️ Steer isn't supported on <engine> — queued instead.`). See
[Steer follow-ups](../../how-to/steer-follow-ups.md).

=== "toml"

    ```toml
    followup_mode = "queue" # or "steer"
    ```

Configuration (under `[transports.telegram]`):

=== "untether config"

    ```sh
    untether config set transports.telegram.forward_coalesce_s 1.0
    ```

=== "toml"

    ```toml
    forward_coalesce_s = 1.0 # set 0 to disable the delay
    ```

### Media group coalescing

When a user sends multiple documents as a Telegram media group (album), Telegram
delivers them as separate messages sharing a `media_group_id`. Untether buffers
these messages and processes them as a single batch once the group is complete.

Behaviour:

- Messages with a `media_group_id` are collected by the `MediaGroupBuffer`.
- After `media_group_debounce_s` seconds of quiet (no new messages in the same
  group), the buffer flushes and routes the group to `handle_media_group`.
- Each flush resets the debounce timer if new messages arrive before it fires.

Configuration (under `[transports.telegram]`):

=== "untether config"

    ```sh
    untether config set transports.telegram.media_group_debounce_s 1.0
    ```

=== "toml"

    ```toml
    media_group_debounce_s = 1.0 # set 0 to disable the delay
    ```

## Chat sessions

Session mode determines how conversations continue — this is the core difference between the three [workflow modes](../modes.md):

- **Assistant / Workspace** (`session_mode = "chat"`) — auto-resume; messages continue the last session automatically
- **Handoff** (`session_mode = "stateless"`) — reply-to-continue; each message starts a new run unless you reply to a previous one

Configuration (under `[transports.telegram]`):

=== "untether config"

    ```sh
    untether config set transports.telegram.show_resume_line true
    untether config set transports.telegram.session_mode "chat"
    ```

=== "toml"

    ```toml
    show_resume_line = true # set false to hide resume lines
    session_mode = "chat" # or "stateless"
    ```

Behaviour:

- Stores one resume token per engine per chat (per sender in group chats).
- Auto-resumes when no explicit resume token is present.
- Reply resume lines always take precedence and update the stored session for that engine.
- Reset with `/new`.

State is stored in `telegram_chat_sessions_state.json` alongside the config file.

Set `show_resume_line = false` to hide resume lines when untether can auto-resume
(topics or chat sessions) and a project context is resolved. Otherwise the resume
line stays visible so reply-to-continue still works.

## Message overflow

By default, untether splits long final responses across multiple messages to stay
under Telegram's 4096 character limit after entity parsing. You can opt into
trimming instead:

=== "untether config"

    ```sh
    untether config set transports.telegram.message_overflow "trim"
    ```

=== "toml"

    ```toml
    [transports.telegram]
    message_overflow = "trim" # trim | split
    ```

Split mode sends multiple messages (~3500 body characters each). Follow-up
chunks add a "continued (N/M)" header, and only the last chunk carries the
footer: the meta line, the cost line (`💰`), a budget or run-outlier alert,
the subscription usage line (`⚡`) and the resume line, in that order
([#770](https://github.com/littlebearapps/untether/issues/770)). The ~600
characters of headroom under the limit leave room for every footer line; in
the rare case one still wouldn't fit, it's sent as a short extra message
rather than risk Telegram rejecting the whole reply.

## Rendering

Agent markdown is rendered with a commonmark parser into Telegram entities, plus
a few Telegram-specific rewrites:

- **`<br>` tags** — a bare `<br>`, `<br/>` or `<br />` (any case, no
  attributes) becomes a line break, or a space inside a table row. Code spans
  and blocks are untouched; every other HTML tag stays escaped text
  ([#786](https://github.com/littlebearapps/untether/issues/786)).
- **Pipe tables** — commonmark has no table rule, so GFM pipe tables are kept
  readable instead: each row stays on its own line as `| a | b |` text, the
  delimiter row (`|---|`) is dropped and the header row is bolded. When a long
  reply is split, the header row is repeated in the chunk that continues the
  table ([#797](https://github.com/littlebearapps/untether/issues/797)).
- **Ordered list numbers** — a list that starts at a number other than 1
  (`42. …`, or the continuation chunk of a long numbered answer) keeps its
  numbers: the start is set on the list's first item (`<li value>`), which
  the HTML-to-entities converter honours, where it ignored `<ol start>` and
  renumbered from 1 ([#886](https://github.com/littlebearapps/untether/issues/886)).
- **Bare filenames** — `.md`, `.sh` and `.py` are also country-code TLDs, so a
  bare `CLAUDE.md`, `setup.sh` or `render.py:86` (paths and `:line[:col]`
  suffixes included) is rendered as inline code rather than auto-linked as a
  domain. Explicit link text and real URLs keep their links
  ([#788](https://github.com/littlebearapps/untether/issues/788)).
- **Line breaks** — CommonMark turns a single newline inside a paragraph into
  a space, which collapsed one-item-per-line digests into a single paragraph.
  A single newline is now kept as a line break when the next line is indented
  (2+ columns, kept as up to 8 non-breaking spaces), starts with a structural
  lead (emoji, bullet/arrow/status glyph, `#N`, `1.`, `(a)`, `**Key:**`, a
  checkbox or `Key: `), follows a line under 40 characters, starts with a word
  that would have fitted on the previous line (a wrapper never breaks early),
  or starts with a capital after a line ending in neither punctuation nor a
  small function word. Hard-wrapped prose still reflows into one paragraph;
  breaks inside link text and around pipe tables are left alone
  ([#870](https://github.com/littlebearapps/untether/issues/870)).
- **Agent text in code spans** — command titles, the long-running tail, verbose
  detail lines, `read:`/`glob:`/`ls:`/`grep:`/`find:` tool titles, changed-file
  paths and the Bash approval preview go through `markdown.inline_code`: the
  span's fence is longer than any backtick run inside the text (the #855
  diff-preview technique), so a command such as ``echo `date` `` or a heredoc
  PR body can't close the span early and run the following action lines
  together. A multi-line command in a Bash approval keeps its lines in a fenced
  `sh` block ([#871](https://github.com/littlebearapps/untether/issues/871)).

## File transfer and `/browse`

`/file put`, `/file get`, outbox delivery and `/browse` share one path check
(`check_path_access` / `deny_reason` in `telegram/files.py`), driven by
`[transports.telegram.files] deny_globs`. Default:

```toml
deny_globs = [
  ".git/**", ".env", "**/.env", "**/.env.*", ".envrc", "**/.envrc",
  "**/*.pem", "**/*.key", "**/id_rsa", "**/id_ed25519", "**/.ssh/**",
  "**/.netrc", "**/.npmrc", "**/.pypirc",
]
```

- **Matching** ([#831](https://github.com/littlebearapps/untether/issues/831)):
  `**` is recursive and also matches zero directories, so `**/*.pem` denies a
  project-root `key.pem`, and a trailing `/**` covers every depth below a
  matching directory. A bare pattern such as `.env` still matches that name at
  any depth. Any `.git` path component is denied case-insensitively (`.GIT/hooks`
  on macOS too). Since v0.35.5 a project-root `.env.example` matches `**/.env.*`;
  narrow the list if you need such files.
- **Symlinks** ([#390](https://github.com/littlebearapps/untether/issues/390)):
  the path is checked as requested and again after resolving symlinks, so an
  in-root link such as `cfg.txt → .env` or `docs/x → .git/hooks` cannot route
  `/file put` (including auto-put and media groups) or `/file get` past a deny
  glob, and a link pointing outside the project is refused. Benign in-root
  symlinks keep working. Denials log `file_transfer.path_denied`.
- **`/browse`** ([#389](https://github.com/littlebearapps/untether/issues/389))
  browses the active run's directory, else the chat's bound project, else
  `default_project`; with none of those it refuses and explains how to bind one
  (it no longer falls back to the process working directory). Listings,
  previews and direct paths apply the deny globs and also hide dot-paths except
  `.github` and `.gitignore`. Button ids are scoped per chat. Denials log
  `browse.path_denied`; escapes log `browse.path_escape_attempted`.
- **Outbox delivery** — after a run, files the agent left in `outbox_dir`
  (default `.untether-outbox`, up to `outbox_max_files` = 10) are sent as
  documents. Symlinks, deny-globbed and oversized files are skipped and, with
  `outbox_notify_skipped = true` (default), reported in the chat.
  `outbox_deliver_directories = "zip"` bundles a directory's deliverable
  members into one `.zip`, never following symlinks and pruning deny-globbed
  subdirectories.

See [File transfer](../../how-to/file-transfer.md) and
[Browse files](../../how-to/browse-files.md) for usage.

## Forum topics (workspace mode)

!!! info "Mode requirement"
    Forum topics are used by **workspace mode** only. Assistant and handoff modes don't use topics. See [Workflow modes](../modes.md) for the full comparison.

If you chose the **workspace** workflow during onboarding, topics are already enabled.
Topics bind Telegram forum threads to a project/branch and persist resume tokens per
topic, so replies keep the right context even after restarts.

Configuration (under `[transports.telegram]`):

=== "untether config"

    ```sh
    untether config set transports.telegram.topics.enabled true
    untether config set transports.telegram.topics.scope "auto"
    ```

=== "toml"

    ```toml
    [transports.telegram.topics]
    enabled = true
    scope = "auto" # auto | main | projects | all
    ```

Requirements:

- `main`: `chat_id` must be a forum-enabled supergroup (topics enabled).
- `projects`: each `projects.<alias>.chat_id` must point to a forum-enabled
  supergroup for that project.
- `all`: both the main chat and each project chat must be forum-enabled.
- `auto`: if any project chats are configured, uses `projects`; otherwise `main`.
- The bot needs the **Manage Topics** permission in the relevant chat(s).

Commands:

- `main`: `/topic <project> @branch` creates a topic in the main chat and binds it.
- `projects`: `/topic @branch` creates a topic in the project chat and binds it.
- `all`: use `/topic <project> @branch` in the main chat, or `/topic @branch` in
  project chats.
- `/ctx` shows the bound context and stored session engines inside topics.
  Outside topics, `/ctx set ...` and `/ctx clear` bind the chat context.
- `/new` inside a topic cancels that topic's running task (and its pending `/loop` entries) and clears stored resume tokens for that topic. Runs in other topics keep going; `/new` in General only cancels General's runs (General = no thread id = topic id 1). The `/cancel` no-reply fallback is scoped the same way. Non-forum groups stay chat-wide ([#826](https://github.com/littlebearapps/untether/issues/826)).

State is stored in `telegram_topics_state.json` alongside the config file.
Delete it to reset all topic bindings and stored sessions.

Note: main chat topics do not assume a default project; topics must be bound
before running without directives.

## Outbox model

- Single worker processes one op at a time.
- Each op is keyed; only one pending op per key.
- New ops with the same key overwrite the payload but **do not** reset
  `queued_at` (fairness).

Keys (include `chat_id` to avoid cross-chat collisions):

- `("edit", chat_id, message_id)` for edits (coalesced).
- `("delete", chat_id, message_id)` for deletes.
- `("send", chat_id, replace_message_id)` when replacing a progress message.
- Unique key for normal sends.

Scheduling:

- Ordered by `(priority, queued_at)`.
- Priorities: send=0, delete=1, edit=2.
- Within a priority tier, the oldest pending op runs first.

## Callback answering

Inline-keyboard button presses (Approve/Deny, plan-outline Pause, `/config`
toggles, `AskUserQuestion` options) produce Telegram `callback_query` updates.
The Bot API requires us to call `answerCallbackQuery` within **30 seconds** of
the press; if we don't, the *user's* Telegram client surfaces
`BotResponseTimeoutError`. The work Untether then performs (writing control
responses to Claude's PTY, editing feedback messages, etc.) happens
independently — answering just clears the spinner.

Callbacks are accepted only from allowed senders (`allowed_user_ids`, #377).
A Claude approval callback (`claude_control:…`) is also bound to the chat whose
message carries the button: the same callback data sent from another chat (a
modified client can attach any callback data to any bot message it can see)
reads as expired and answers nothing
([#388](https://github.com/littlebearapps/untether/issues/388)). Other callback
families already act only on the tapping chat.

Backends that want a visible toast ("Approved" / "Denied" / …) set
`answer_early = True` and provide `early_answer_toast(args_text) -> str | None`.
Dispatch hits `answerCallbackQuery` via that path **before** calling
`backend.handle(ctx)` so the Telegram ack never blocks on slow downstream
work. The invariant is covered by a regression test
(`tests/test_callback_dispatch.py::test_early_answer_fires_before_slow_handle`).

Every answer emits a structured INFO log for observability:

```
callback.answered command=<id> chat_id=<n>
  latency_ms=<ms>   # just the answerCallbackQuery HTTP round-trip
  total_ms=<ms>     # since the dispatcher entered handle_callback
  early=true|false  # whether the early-answer path fired
  has_toast=true|false
```

Use `journalctl --user -u untether --since ... | grep callback.answered` to
tell "we were fast, Telegram was slow" (high `latency_ms`) apart from "we were
slow before reaching Telegram" (high `total_ms` with low `latency_ms`).

Client-side `BotResponseTimeoutError` reports a round-trip that exceeded the
Bot API's 30 s window — they can still fire if Telegram itself is slow or if
the local network is congested, even when `latency_ms` is well under 30 s.
The ordering invariant above is what prevents *our* work from ever being
the cause.

## Rate limiting + backoff

- Per-chat pacing is computed from `private_chat_rps` and `group_chat_rps`.
  Defaults: 1.0 msg/s for private, 20/60 msg/s for groups (≈1 message every 3s).
  The group rate is configurable as `[progress] group_chat_rps` (> 0, ≤ 10);
  the private rate is fixed.
- Pacing is enforced per-chat via `_next_at[chat_id]`; each chat tracks its own
  earliest-allowed send time independently.
- The worker picks the highest-priority ready op whose chat is not blocked.
  On 429, `retry_at` blocks all chats globally until the retry window expires.
- On 429, `RetryAfter` is raised using `parameters.retry_after` when present;
  if missing, we fall back to a 5s delay. The outbox sets `retry_at` and
  requeues the op if no newer op for the same key has arrived.

## Error handling

- Network errors get at most one immediate retry (see [HTTP client](#http-client));
  after that they are logged at ERROR (`telegram.network_error`) and the call
  returns `None`.
- Non-429 HTTP errors are logged at ERROR (`telegram.http_error`) and dropped (no retry).
- Benign `editMessage*` / `deleteMessage` rejections ("message is not modified", "message to edit not found", "message to delete not found", "message can't be edited", "message can't be deleted") are classified by their `description` (case-insensitive substring; Telegram's `error_code` is 400 for all of them and documented as unstable) and logged at INFO as `telegram.benign_rejection` with a `reason_class` (`not_modified`, `target_gone`, `not_editable`, `not_deletable`), instead of ERROR. A missing, zero or negative `message_id`, `MESSAGE_ID_INVALID`, a benign-looking string on any other method, and every non-400 status stay at ERROR. When 5 rejections of one `(method, reason_class)` arrive within 60 s, one WARNING `telegram.benign_rejection.burst` (with `distinct_messages`, `message_ids`, `chat_ids`) is logged per window, so a wrong-id bug or a stuck edit loop still surfaces ([#746](https://github.com/littlebearapps/untether/issues/746)).
- The recorded failure reason (`transport.edit.failed error=`, `startup.orphan_cleanup.edit_failed reason=`) is Telegram's `description` when the response carries one, else `http <status>: <body>`.
- On `RetryAfter`, the op is retried unless a newer op superseded the same key.

## Replace progress messages

`send_message(replace_message_id=...)`:

- Drops any pending edit for that progress message.
- Enqueues the send at highest priority.
- If the send succeeds, enqueues a delete for the old progress message.

This keeps the final message first and avoids deleting progress if the send
fails.

## getUpdates

`get_updates` bypasses the outbox and retries on `RetryAfter` by sleeping
for the provided delay. Its HTTP timeout is the long-poll timeout plus 20 s.

## Close semantics

`TelegramClient.close()` shuts down the outbox and closes the HTTP client.
Pending ops are failed with `None` (best-effort).
