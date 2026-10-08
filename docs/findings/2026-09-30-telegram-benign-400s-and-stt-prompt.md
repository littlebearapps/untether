---
question: >-
  (D, #746) What exact `description` strings does the Telegram Bot API return
  for benign edit/delete failures, and how does Untether log them today?
  (E, #789) What are the transcription `prompt` semantics and limits for
  Whisper (OpenAI + Groq) and gpt-4o-*-transcribe, what does the current
  default cost in tokens, and what should the revised default be?
date: 2026-09-30
sources:
  - https://core.telegram.org/bots/api (Bot API 10.3, 2026-08-24; "Making requests" + deleteMessage)
  - https://github.com/tdlib/telegram-bot-api/blob/e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1/telegram-bot-api/Client.cpp (master, 2026-08-25)
  - https://github.com/tdlib/td/blob/42e6a5259551178d1dab54a22ad96d14bd906e20/td/telegram/MessagesManager.cpp (master, 2026-09-25)
  - https://github.com/aiogram/aiogram/blob/v2.25.2/aiogram/utils/exceptions.py
  - https://github.com/aiogram/aiogram/blob/v3.31.0/aiogram/exceptions.py
  - https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/error.py
  - https://github.com/grammyjs/grammY/blob/v1.46.0/src/core/error.ts
  - https://developers.openai.com/api/docs/guides/speech-to-text (accessed 2026-09-30)
  - https://developers.openai.com/cookbook/examples/whisper_prompting_guide (accessed 2026-09-30)
  - https://console.groq.com/docs/speech-to-text (accessed 2026-09-30)
  - https://github.com/openai/whisper/blob/86098128c0b4f24f0e2aa2994de830614b474227/whisper/decoding.py (L596-L609)
  - https://github.com/openai/whisper/blob/86098128c0b4f24f0e2aa2994de830614b474227/whisper/transcribe.py (L238-L242)
  - https://github.com/openai/whisper/tree/86098128c0b4f24f0e2aa2994de830614b474227/whisper/assets (multilingual.tiktoken, gpt2.tiktoken)
  - repo HEAD 2f198d2 (feature/v0.35.5rc14), issues #746, #789 (incl. 2026-09-30 relevance-sweep comments)
confidence: high (D strings, D code map, E token counts, E truncation mechanics); med (E term-ordering effect — undocumented upstream)
---

# Telegram benign 400s (#746) and the STT vocabulary prompt (#789)

Research for `docs/plans/v0.35.5-rc15/HANDOVER.md` §4 Tracks D and E. Planning
only: no code changed, no Bot API or transcription API calls made.

---

## Track D: benign edit/delete 400s (#746)

### Finding

1. **The exact strings.** The official Bot API server builds these descriptions
   itself; they are not free-form MTProto errors. All are `error_code: 400` and
   come back with **HTTP status 400**:

   | Method | `description` (exact) | Origin in the server source |
   |---|---|---|
   | `editMessageText` (and other `editMessage*`) | `Bad Request: message to edit not found` | `check_message(..., "message to edit", ...)` → `PSLICE() << message_type << " not found"` (`Client.cpp` L9084-L9091, L7399; call sites L14695, L14751, L14801, L14851, L14889, L14932) |
   | `editMessage*` | `Bad Request: message can't be edited` | TDLib `edit_message_text` → `promise.set_error(400, "Message can't be edited")` (`MessagesManager.cpp` L23484, also L23524/L23559/L23763). The Bot API lower-cases the first letter (`Client.cpp` L190-L199) |
   | `editMessage*` | `Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message` | MTProto `MESSAGE_NOT_MODIFIED` rewritten at `Client.cpp` L110-L113 |
   | `editMessage*` (rare) | `Bad Request: message not found` | the edit succeeded in TDLib but the message is missing from the server's cache (`Client.cpp` L7013-L7016) |
   | `deleteMessage` | `Bad Request: message to delete not found` | `check_message(..., "message to delete", ...)` (`Client.cpp` L14992) |
   | `deleteMessage` | `Bad Request: message can't be deleted` | TDLib `"Message can't be deleted"` (`MessagesManager.cpp` L8553) or MTProto `MESSAGE_DELETE_FORBIDDEN` (`Client.cpp` L151-L153) |
   | `deleteMessage` | `Bad Request: message can't be deleted for everyone` | TDLib, bots only, when revoking isn't allowed (`MessagesManager.cpp` L8556). This is the usual failure for a message older than 48 hours |
   | any, passthrough | `Bad Request: MESSAGE_ID_INVALID` | Raw MTProto code. The Bot API keeps upper case when the second character is `_` or upper case (`Client.cpp` L190-L193). It is not produced by the edit/delete paths above, which pre-check with `getMessage`, so it's unlikely there. aiogram 2 still matched it |

   Notes:
   - The "not found" check also fires when `message_id <= 0` or the bot has no
     access to the chat's messages (`Client.cpp` L9089-L9091). "not found"
     therefore covers "deleted", "never existed", and "can't see it".
   - The Bot API docs promise nothing about these strings: *"In case of an
     unsuccessful request, 'ok' equals False and the error is explained in the
     'description'. An Integer 'error_code' field is also returned, but its
     contents are subject to change in the future."* (core.telegram.org/bots/api,
     Bot API 10.3). The description text is the only way to tell these errors
     apart, and it is not a stable contract.
   - `deleteMessage` has documented limits: *"A message can only be deleted if it
     was sent less than 48 hours ago"*, plus rules on bot and admin rights
     (same page).

2. **What the libraries do.** Nobody has a stable error-code table. They match
   on substrings:
   - **aiogram 2.25.2** (`aiogram/utils/exceptions.py`) is the canonical table.
     `MessageNotModified` = `'message is not modified'`, `MessageToEditNotFound`
     = `'message to edit not found'`, `MessageCantBeEdited` = `"message can't be
     edited"`, `MessageToDeleteNotFound` = `'message to delete not found'`,
     `MessageCantBeDeleted` = `"message can't be deleted"`, `InvalidMessageId`
     = `'message_id_invalid'`. Matching is a case-insensitive substring test:
     `detect()` lower-cases the description, then `cls.match.lower() in
     message` (L122-L141).
   - **aiogram 3.31.0** dropped the per-string classes and now has only a
     generic `TelegramBadRequest`.
   - **python-telegram-bot v22.8** has only a generic `BadRequest`, no
     per-string subclasses. The community idiom is `"message is not modified" in
     str(e)`.
   - **grammY v1.46.0** has a generic `GrammyError` with `error_code` and
     `description`. Callers match on `description`.

3. **How Untether handles them today (HEAD 2f198d2).** The live path is
   **`telegram.http_error` at ERROR, not `telegram.api_error`:**
   - `src/untether/telegram/client_api.py:268-311`. Telegram returns HTTP 400,
     so `resp.raise_for_status()` raises first. Every non-429 status is logged
     as **`logger.error("telegram.http_error", method, status, url, error,
     body, message_id)`** (L294-L307). The reason is recorded as
     `f"http {status}: {body[:200]}"` (L308-L310) and `None` is returned. No
     status or description is classified.
   - `client_api.py:204-226`. `telegram.api_error` (ERROR) runs only for an
     **HTTP 200 with `ok:false`**. The official server never sends that for
     these errors. The #598 unit test (`tests/test_telegram_client_api.py:285-302`)
     exercises this unreachable envelope path, not the HTTP-400 path production
     takes.
   - The mac evidence in #746 confirms the shape: `telegram.http_error ...
     status=400 body='{"ok":false,"error_code":400,"description":"Bad Request:
     message to edit not found"}' method=editMessageText`.
   - `src/untether/telegram/bridge.py:398-426`, `TelegramTransport.edit`. The
     only benign special case is here. The reason is popped with
     `pop_edit_error` (`client.py:247-253`). If `"message is not modified"` is
     in it, the bridge logs **`transport.edit.noop` at INFO** (L408-L418).
     Anything else logs **`transport.edit.failed` at WARNING** with
     `error=<reason>` (L419-L426). The substring still matches the
     `http 400: {…}` reason because the description sits well inside the first
     200 characters. **But the ERROR `telegram.http_error` line has already
     been emitted**, so even a "not modified" no-op leaves an ERROR in the log.
     "message to edit not found" and "can't be edited" get ERROR plus WARNING.
   - `bridge.py:459-473`, `TelegramTransport.delete`. `transport.delete.failed`
     (WARNING) fires only on an exception. `HttpBotClient.delete_message`
     (`client_api.py:550-565`) returns `False` on a 400 and never raises. A
     benign delete 400 therefore shows up only as the ERROR
     `telegram.http_error` line, plus `telegram.message.deleted success=False`
     at DEBUG.
   - `src/untether/telegram/outbox.py:201-222`. `outbox.op.failed` (ERROR)
     fires only when an op raises. A 400 returns `None`, so benign 400s never
     reach it. This matters because `outbox.op.failed` is in the watcher's
     `BUG_EVENTS`.
   - `src/untether/telegram/loop.py:554-593`, `_cleanup_orphan_progress` (the
     #746 path). It calls `cfg.bot.edit_message_text(...)`, which goes through
     the outbox and returns `None` on failure. It never checks the return
     value. So:
     - (a) the `except` → `startup.orphan_cleanup.edit_failed` DEBUG branch
       (L586-L592) is dead for API errors;
     - (b) **`startup.orphan_cleanup.edited` (DEBUG) is logged even when the
       edit failed** (L581-L585);
     - (c) the recorded reason is never popped. It lingers in
       `_last_api_errors` until the 64-entry bound evicts it;
     - (d) the only visible trace is the ERROR `telegram.http_error`.
     `progress_persistence.py` is pure file I/O (load, save, register,
     unregister, clear) and does no Telegram calls.
   - `.claude/skills/telegram-bot-api/SKILL.md:105-107` documents "Non-429
     errors: logged and dropped (no retry)". It says nothing about benign
     classes.

4. **Issue watcher and /monitor.** The claim in #746 that the issue watcher
   files ERROR lines is **not accurate for the watcher**. It uses an
   allowlist: `~/.local/bin/untether-issue-watcher` (mtime 2026-07-20) files
   only the events in `BUG_EVENTS` and `WARNING_EVENTS` (L81-L138).
   `telegram.http_error`, `telegram.api_error` and `transport.edit.failed` are
   not in either set, and the docstring says it skips "Telegram API errors"
   (L12). It **is** accurate for `/monitor`. That uses a generic warn/error
   grep whose `[signals.error_grep] exclude_regex` in
   `~/.config/monitor/_shared.toml` excludes only `catalog_staleness|catalog\.refresh`
   (sl adds `missing_on_path`). A benign 400 is therefore a `/monitor`
   filing candidate on every restart that has a deleted orphan.

5. **How often it happens.** Over the last 14 days of the `untether` journal on
   nsd and channelo, the only `telegram.http_error` lines are `getUpdates` Bad
   Gateway (2 and 5). There are no `transport.edit.noop` or `.failed` lines
   and no edit/delete 400s. lba-1's journal starts on 2026-09-29 and has none.
   The benign 400s are rare and bunched around restarts (the #746 orphan path
   on mac). That fits a small, low-risk fix.

### Evidence
- Bot API server `Client.cpp` @ e3e9dd8 (2026-08-25): L80-L207
  (`fail_query_with_error` translation and prefixing), L6998-L7019, L7377-L7400,
  L9084-L9116, L14695, L14992-L15010. Retrieved with `gh api` on 2026-09-30.
- TDLib `MessagesManager.cpp` @ 42e6a52 (2026-09-25): L8553, L8556, L23481-L23484.
- aiogram v2.25.2 `exceptions.py` L110-L230, aiogram v3.31.0 `exceptions.py`,
  PTB v22.8 `error.py` L158, grammY v1.46.0 `error.ts` L16-L36.
- Bot API docs, Bot API 10.3 (2026-08-24): "Making requests", deleteMessage.
- Untether code map above, at HEAD 2f198d2. Fleet journal tallies from
  2026-09-30, read-only.

### Implication for Untether (#746 plan)
- **Classify in `client_api._request`'s HTTP-error branch, not in the envelope
  path.** On `status == 400`, try `resp.json()`, take `description`, and match
  it case-insensitively as a substring (aiogram-style) against a small benign
  set, scoped to `editMessage*` and `deleteMessage`:
  - `message is not modified`
  - `message to edit not found`
  - `message can't be edited`
  - `message to delete not found`
  - `message can't be deleted` (this also covers "... for everyone")
  - `message_id_invalid`

  Log a match at INFO (or DEBUG) instead of ERROR. Everything else stays at
  ERROR, so if Telegram changes the wording the failure is loud, exactly as
  today. Keep recording the reason so `bridge.py`'s `transport.edit.noop` and
  `.failed` split keeps working. Consider recording the parsed `description`
  instead of `http 400: {body[:200]}`.
- **Event naming is a `/monitor` contract decision.** Either keep
  `telegram.http_error` and lower its level, or add a distinct event such as
  `telegram.edit_target_gone` or `telegram.benign_rejection`. A distinct
  event is easier to grep and exclude, and needs no change to the watcher
  allowlist. Either way it removes ERROR noise from `/monitor`.
- **Orphan cleanup (`loop.py:575-592`).** Check the return value. Log
  `startup.orphan_cleanup.edited` only on success, log a failure with the
  popped reason, and stop leaving reasons in `_last_api_errors`. This fixes
  the misleading "edited" DEBUG line.
- **Tests.** The existing #598 test feeds a 200 with an `ok:false` envelope.
  New tests should drive an `httpx.MockTransport` returning **HTTP 400** with
  the exact bodies above. Assert the level with `structlog.testing.capture_logs()`,
  per the testing-conventions trap. Include one non-benign 400, for example
  `Bad Request: can't parse entities`, to prove it still logs at ERROR.
- **Correct the issue body.** Out of scope for code, but the plan should state
  that the issue watcher does not file `telegram.http_error` today; only
  `/monitor` does.

### Confidence
**High** for the strings, which come straight from the server source at a
pinned SHA and match aiogram 2's table and the mac log. **High** for the code
map. **Medium** for long-term stability of the strings: they are undocumented
and could be reworded upstream, which is why the fallback must stay at ERROR.

---

## Track E: speech-to-text `prompt` (#789)

### Finding

1. **Token limit and truncation.**
   - **OpenAI `whisper-1`:** *"Whisper doesn't follow instructions like a
     general-purpose text model and accepts prompts of up to 224 tokens."* and
     *"For whisper-1, prompts have a 224-token limit and provide less control
     than the recommended transcription model."* (OpenAI speech-to-text guide,
     accessed 2026-09-30).
   - The Whisper prompting cookbook states the truncation direction: *"If the
     prompt is longer than 224 tokens, only the final 224 tokens of the prompt
     will be considered; all prior tokens will be silently ignored."*
   - The mechanism is in open-source Whisper. `decoding.py` L602-L609 builds
     `[sot_prev] + prompt_tokens[-(n_ctx // 2 - 1):]`. With `n_text_ctx = 448`,
     that is the **last 223 prompt tokens** plus `<|startofprev|>`, which makes
     224. `transcribe.py` L238 has the same budget
     (`remaining_prompt_length = n_text_ctx // 2 - 1`). The prompt is fed as
     *previous-segment transcript*, not as an instruction.
   - **Groq** (`whisper-large-v3`, `whisper-large-v3-turbo`): *"prompt …
     Prompt to guide the model's style or specify how to spell unfamiliar
     words. (limited to 224 tokens)"* and *"The prompt parameter (max 224
     tokens) helps provide context and maintain a consistent output style.
     Unlike chat completion prompts, these prompts only guide style and
     context, not specific actions."* Its best practices say to use the
     audio's language, denote proper spellings, and keep the prompt concise.
     The docs don't say whether Groq truncates or rejects a prompt over 224
     tokens. **Unverified**, because this task allowed no API calls.
   - **`gpt-4o-transcribe` and `gpt-4o-mini-transcribe`:** these support
     `prompt` (*"Existing gpt-4o-transcribe and gpt-4o-mini-transcribe
     integrations also support prompting"*). The guide attaches the 224-token
     limit and the "less control" caveat to `whisper-1` only. No limit is
     documented for the 4o models, so treat 224 as the conservative common
     floor. Untether's **config default model is `gpt-4o-mini-transcribe`**
     (`settings.py:158`), but the fleet (nsd) runs Groq
     `whisper-large-v3-turbo`, so the Whisper limits are the ones that bind.

2. **How it biases spelling and casing.** From the cookbook:
   - *"Pass names in the prompt to prevent misspellings"*. A glossary or list
     works, e.g. `"Glossary: Aimee, Shawn, BBQ, …"`.
   - Whisper *"follows the style of the prompt, rather than any instructions"*.
     Casing and punctuation in the prompt get reproduced (the lower-case
     "president biden" example).
   - *"when prompts are short, Whisper may be less reliable at following their
     style"*, and *"The prompt will not override the model's comprehension of
     the audio."*

   Two consequences follow:
   - **Casing matters.** `CLAUDE.md` in upper case biases the output towards
     `CLAUDE.md`, which is what we want for the filename.
   - **Tokenisation explains the #789 residue.** Measured with Whisper's
     `multilingual.tiktoken`, which large-v3 uses:
     - ` Claude` → `[' Cla', 'ude']`
     - ` CLAUDE.md` → `[' C', 'LAU', 'DE', '.', 'm', 'd']`
     - ` Clawde` → `[' C', 'law', 'de']`

     The all-caps filename shares **no** tokens with the spoken-word form
     `Claude`. That is why having only `CLAUDE.md` and `Claude Code` in the
     prompt didn't stop "Clawde" and "Clawed". `Claude Code` does contain
     `' Cla','ude'`, but only ever followed by ` Code`. A **bare `Claude`**
     term is the direct fix, which matches the nsd observation in the
     2026-09-28 comment.

3. **Term ordering.**
   - The only documented positional effect is **tail-keeping truncation**. Once
     a prompt goes over the limit, the earliest terms are the ones silently
     dropped.
   - Nothing upstream says earlier terms are weighted more. The monitor
     comment's line "Whisper weights earlier prompt tokens more" is
     **unsupported**. If anything, the mechanism (prompt as the text that
     immediately precedes the transcript) would favour the tail.
   - For a prompt far inside 224 tokens, ordering is a weak and undocumented
     effect. It's worth one A/B on a recorded clip, not a design constraint.

4. **Current default cost.**
   - `src/untether/telegram/voice.py:37-40` is `"Untether, Telegram, Claude
     Code, Codex, OpenCode, Gemini, Amp, Pi, MCP, CLI, repo, changelog, PyPI,
     CLAUDE.md, AGENTS.md"`: **120 characters, 52 tokens** with Whisper's
     multilingual tokenizer (43 with GPT-2, which is English-only Whisper).
     Tokens were counted exactly with the Whisper vocab files at 86098128 via
     `tiktoken` in a throwaway `uv run --no-project --with tiktoken` env, using
     the `" " + prompt.strip()` encoding from `transcribe.py`.
   - That is about 23% of the window, so **truncation never touches the
     default**.
   - The measured density for this comma-list style is about **2.3 characters
     per token**. The `settings.py:244-268` validator allows **up to 1000
     characters**, which is roughly 430 tokens. A maximum-length operator
     override would therefore silently lose its **first** ~half on Whisper.
     The validator docstring and `docs/reference/transports/telegram.md:72-76`
     say "~224 tokens" but don't warn about that head loss.

5. **Relevance sweep (authoritative, 2026-09-30).** *"`telegram/voice.py:~37-40`
   default prompt still has no bare "Claude". Put "Claude" first, and drop
   "Gemini, Amp" (deprecated) and "Pi" (out of scope) to save prompt budget.
   Update the prompt test; live-check with a voice note."*

### Proposed revised default (#789)

```
Claude, Claude Code, CLAUDE.md, AGENTS.md, Codex, OpenCode, Untether, Telegram, MCP, CLI, repo, changelog, PyPI
```

- 111 characters, **47 multilingual tokens** (39 GPT-2), about 21% of the
  window. Tail truncation can't apply.
- The sweep's instruction is honoured: bare `Claude` comes first, and
  `Gemini`, `Amp` and `Pi` are dropped. Grouping the three Claude forms
  together lets the stem ` Cla|ude` sit next to the filename. Every term stays
  product-generic, so the #703 rule holds.
- **Alternative for the A/B clip** (same terms, same 47 tokens, tail-weighted):
  `Untether, Telegram, MCP, CLI, repo, changelog, PyPI, Codex, OpenCode,
  AGENTS.md, Claude Code, CLAUDE.md, Claude`. Use it only if the recorded-clip
  A/B shows that placing a term last beats placing it first. Neither order is
  documented to matter at this length.
- **Don't add `Claude.md`** in mixed case. It would share the stem, but it
  biases the output towards a non-canonical filename.
- **Caveat on dropping `Pi`.** Pi is still a supported engine in the runner
  set. "Pi" is a common word, though, and it's out of v0.35.5 scope per the
  sweep, so dropping it costs little. Operators who dictate "run it on Pi" can
  set an override.

### Implication for Untether (#789 plan)
- **One-line constant change** at `voice.py:37-40`, plus the comment block
  (L24-L36). Add a #789 note that bare `Claude` is needed because `CLAUDE.md`
  tokenises differently.
- **Test** (`tests/test_telegram_voice.py:440-460`):
  - Split the default on `", "` and assert `terms[0] == "Claude"` (or just
    `"Claude" in terms`).
  - Assert `"Gemini"`, `"Amp"` and `"Pi"` are not in `terms`. Use membership
    in the split list, not a substring test: `"Pi"` is case-sensitively
    absent from `"PyPI"`, but a split is safer.
  - Keep the `CLAUDE.md` and `AGENTS.md` assertions.
  - Tighten the budget proxy from `<= 1000` to something like `<= 300` chars.
    At the measured ~2.3 characters per token, 300 chars is about 130 tokens,
    comfortably inside 224. Tiktoken is not a dependency, so a character proxy
    is the pragmatic test.
- **Docs and FAQ.** `docs/reference/config.md:92` and
  `docs/reference/transports/telegram.md:72-80` describe the shipped default,
  so refresh the term list. Optionally add one sentence: "Whisper keeps only
  the **last** ~224 tokens (roughly 500 characters of a comma list), so put
  your most important terms at the end of a long override." Check the FAQ
  (Q9, voice) under the release-discipline FAQ rule. It shows an example
  override, not the default, so it probably needs no change.
- **Optional follow-up, not #789.** The 1000-character cap allows prompts
  whose head Whisper silently drops. Consider a WARN above about 500
  characters, or doc-only guidance. Record it as a separate enhancement.
- **Live check.** RC12-10 in `docs/reference/integration-testing.md:297`
  already covers voice vocabulary. Extend it with a clip that says "Thanks,
  Claude … update CLAUDE.md and AGENTS.md", sent to `@untether_dev_bot` over a
  Groq `whisper-large-v3-turbo` config.

### Confidence
- **High:** the 224-token window, tail-keeping truncation (docs and source
  agree), token counts (exact tokenizer), and the case and style mirroring
  (cookbook).
- **Medium:** that a bare `Claude` fixes "Clawde". The mechanism is sound, but
  it needs the live clip.
- **Low to medium:** any term-ordering effect inside the window, which is
  undocumented.
- **Unknown:** how Groq behaves above 224 tokens, and the prompt limit for
  gpt-4o-*-transcribe.
