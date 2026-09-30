---
question: >-
  For rc15 Track C (#209, #416, #417, #419, #819), what does Codex CLI 0.157.1
  actually accept on its argv and emit on `codex exec --json`, and what does the
  Codex SDK contract say?
date: 2026-09-30
sources:
  - openai/codex @ rust-v0.157.1 (git tree API; files cited inline as path@L)
  - https://github.com/openai/codex/issues/3559, /issues/5002 (minimal + web_search 400)
  - openai/codex commits 942af8447 (#39630), ddfa69175 (#19308), 2edad72de (#33454), f8c6026c3 (#46319), 45f68843b (#15424)
  - npm @openai/codex-sdk (dist-tags), PyPI openai-codex / openai-codex-cli-bin
  - binary: /usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex (strings)
  - zero-token probes on lba-1 (`--help`, clap/config validation via `codex … features list`, `codex app-server generate-json-schema`)
  - local read-only state: ~/.codex/sessions rollouts (cli_version 0.157.1, originator codex_exec), ~/.codex/models_cache.json, ~/.codex/state_5.sqlite schema
confidence: high (flags, usage semantics, compaction dropped, SDK types); med (web_search duplicate-id wire shape, which compaction path OpenAI uses, rollout side-channel stability)
---

# Codex CLI 0.157.1 `exec --json` surface for rc15 Track C

**Date:** 2026-09-30 · **CLI:** codex-cli 0.157.1 (`codex --version`) · **Tag:** `rust-v0.157.1` ·
**SDKs:** npm `@openai/codex-sdk` latest **0.159.2** (alpha 0.161.0-alpha.3); PyPI **`openai-codex` 0.159.2**
(Python SDK, import `openai_codex`) + runtime wheel `openai-codex-cli-bin` 0.159.2 · **Host:** lba-1 ·
**Native binary:** `codex` is a node shim (`/usr/lib/node_modules/@openai/codex/bin/codex.js`) wrapping the
musl binary above (285 MB).

No paid runs. Every claim below is from source at the tag, `--help`/clap/config validation that exits
before any model call, the schema the installed binary generates, or existing local rollout files.

## Headline findings (read these first)

1. **Codex "safe" mode is broken on 0.157.1 (new bug, not yet filed).** Untether sends
   `--ask-for-approval untrusted` (`src/untether/runners/codex.py:501-502`). Upstream retired
   `untrusted` in #39630 (2026-08-20). The CLI now fails at argv parse with **rc=2 before any JSONL**:
   `error: invalid value 'untrusted' for '--ask-for-approval <APPROVAL_POLICY>' [possible values: on-request, never]`.
   The config route is also closed: `-c approval_policy="untrusted"` →
   `approval_policy = "untrusted" is no longer supported; remove this setting`.
   Separately, the root `-a` flag is **never inherited by `exec`**, and exec forces approval `Never`, so
   the `never` branch has been a silent no-op all along.
2. **`turn.completed.usage` is the thread's cumulative total, not per-turn.** On `exec resume` it
   includes every earlier run in the thread. Summing it across runs over-counts, and it cannot be used
   as a context-size signal (#417, #419, #819).
3. **Compaction is dropped by `exec --json`.** The window size is also dropped, and so is the per-request
   `last` usage. Nothing in exec JSON gives Codex context % or a compaction row. The data exists in the
   app-server protocol (`thread/tokenUsage/updated`, `contextCompaction` item) and in the on-disk
   rollout (`event_msg/token_count`).

---

## Q1 (#209) — bypass / dangerous flags on 0.157.1

### Finding

| Flag | Where accepted | Inherited into `exec` when placed before `exec`? | Notes |
|---|---|---|---|
| `--dangerously-bypass-approvals-and-sandbox` (alias **`--yolo`**, hidden) | top-level, `exec`, `exec resume` (global) | yes | Sets approval never + `danger-full-access`. |
| `--approve-for-me` (alias **`--not-so-yolo`**, hidden) | top-level, `exec` | yes | Auto-review reviewer on workspace-write. Conflicts with `--sandbox` and `--yolo`. |
| `--dangerously-bypass-hook-trust` | top-level, `exec`, `exec resume` (global) | yes | **New.** Runs unvetted hooks. Also written as `bypass_hook_trust` thread config. |
| `-s/--sandbox read-only\|workspace-write\|danger-full-access` | top-level, `exec` | yes | |
| `--add-dir <DIR>` | top-level, `exec` | yes | Extra writable roots. |
| `-C/--cd <DIR>` | top-level, `exec` | yes | Moves the workspace root. Untether controls cwd. |
| `--worktree` | top-level, `exec`, `exec resume` | yes | **New.** Runs in a new managed git worktree. |
| `--oss`, `--local-provider` | top-level, `exec` | yes | Swaps the provider. |
| `-p/--profile <NAME>` | top-level, `exec` | yes | **Semantics changed ("profile v2"):** layers `$CODEX_HOME/<name>.config.toml` instead of selecting `[profiles.<name>]`. Untether's `codex.profile` passes `--profile` (`codex.py:771`). |
| `--ignore-rules` | `exec`, `exec resume` only | n/a | Skips execpolicy `.rules`. Dangerous. |
| `--ignore-user-config` | `exec`, `exec resume` only | n/a | Skips `config.toml` (auth still uses `CODEX_HOME`). |
| `--ephemeral` | `exec`, `exec resume` only | n/a | No session files, so Untether resume breaks. |
| `--skip-git-repo-check`, `--json` (alias `--experimental-json`), `--color`, `-o/--output-last-message`, `--output-schema`, `--thread-source`, `--strict-config` | `exec` (most are global to its subcommands) | n/a | |
| `--enable/--disable <FEATURE>` | everywhere | via `-c features.*` | Equivalent to `-c features.<name>=…`. |
| `-c key=value` | everywhere | **yes** (root overrides are prepended to exec's) | Relevant keys: `sandbox_mode`, `approval_policy` (overridden to `never` in exec anyway), `shell_environment_policy.*`, `sandbox_workspace_write.network_access`/`writable_roots`, `web_search`, `features.*`, `bypass_hook_trust`. |
| `-a/--ask-for-approval on-request\|never` | **top-level only** | **no** (silently ignored by exec) | `untrusted` and `on-failure` are rejected by clap. |
| `--search` | top-level only | **no** | Exec needs `-c web_search="live"`. |
| `--remote`, `--remote-auth-token-env` | top-level only | rejected for `exec` | |
| `--full-auto` | **removed** | — | `error: unexpected argument '--full-auto' found` |

### Evidence

- `codex exec --help` / `codex --help` / `codex exec resume --help` (0.157.1), captured 2026-09-30.
- `codex-rs/utils/cli/src/shared_options.rs@L35-76` holds the shared options. `approve-for-me` has
  `alias = "not-so-yolo"` and `conflicts_with_all`. The dangerous bypass flag has `alias = "yolo"`.
  `inherit_exec_root_options` at L95-126 copies model/oss/profile/sandbox/auto_review/yolo/hook-trust/cwd/worktree/add_dir.
- `codex-rs/cli/src/main.rs@L1134-1148`: `Subcommand::Exec` calls `inherit_exec_root_options(&interactive.shared)`
  and `prepend_config_flags(root -c)`. It never reads `interactive.approval_policy` or `web_search`.
- `codex-rs/utils/cli/src/approval_mode_cli_arg.rs@L9-16`: `ApprovalModeCliArg { OnRequest, Never }` only.
  Commit 942af8447 "Retire the untrusted approval policy (#39630)", 2026-08-20.
- `codex-rs/protocol/src/protocol.rs@L983-1006`: `AskForApproval::UnlessTrusted` still exists
  (`serde rename "untrusted"`) but the config loader rejects it (probe below).
- `codex-rs/exec/src/lib.rs@L571-577`: `approval_policy: Some(AskForApproval::Never)` ("Default to never
  ask for approvals in headless mode"). The only exception is the AutoReview reviewer rebuild at L755-790.
  L2014-2035: `CommandExecutionRequestApproval` / `FileChangeRequestApproval` are rejected with
  "… approval is not supported in exec mode".
- `codex-rs/exec/src/cli.rs@L21-74` (exec-only globals), L140-147 (`mark_exec_global_args`: model, yolo,
  bypass_hook_trust, worktree made global).
- Probes (all exit before any model call):
  ```
  $ codex -c 'notify=[]' --ask-for-approval untrusted exec --json --skip-git-repo-check --color=never - </dev/null
  error: invalid value 'untrusted' for '--ask-for-approval <APPROVAL_POLICY>'
    [possible values: on-request, never]
  rc=2
  $ codex -c 'approval_policy="untrusted"' features list
  Error: failed to load configuration
  Caused by: approval_policy = "untrusted" is no longer supported; remove this setting
  $ codex -c 'approval_policy="on-failure"' features list   → OK (serde alias of on-request)
  $ codex exec --full-auto --help → error: unexpected argument '--full-auto' found
  $ codex exec -a never --help    → error: unexpected argument '-a' found
  $ codex exec --search --help    → error: unexpected argument '--search' found
  $ codex exec --yolo --help      → prints help (hidden alias accepted)
  ```

### Untether today

- `src/untether/runners/codex.py:43-50` `_EXEC_ONLY_FLAGS = {"--ask-for-approval", "--skip-git-repo-check",
  "--json", "--output-schema", "--output-last-message", "--color", "-o"}`; `:52-56` prefixes
  `--output-schema=`, `--output-last-message=`, `--color=`; `:59` `find_exec_only_flag`; `:747-758`
  raises `ConfigError` on a hit. `:732` defaults `extra_args = ["-c", "notify=[]"]`.
- `:490-512` `build_args`: `extra_args` → `--model` → `-c model_reasoning_effort=…` →
  `--ask-for-approval untrusted|never` (top-level, **ignored by exec / fatal for untrusted**) →
  `exec --json --skip-git-repo-check --color=never` → `resume <id> -` / `-`.
- Gaps in the reserved set: `-a` (short form) and `--ask-for-approval=…` (no prefix), `--experimental-json`,
  every dangerous flag in the table (`--yolo`, `--dangerously-bypass-*`, `--sandbox danger-full-access` /
  `-s`, `-c sandbox_mode=…`, `-c approval_policy=…`, `--approve-for-me`/`--not-so-yolo`, `--add-dir`,
  `-C/--cd`, `--worktree`, `--ignore-rules`, `--ignore-user-config`, `--ephemeral`, `--oss`,
  `--dangerously-bypass-hook-trust`). `-m/--model` and `-p` also shadow Untether-managed values.
  `--ask-for-approval` is misclassified as exec-only: it is actually top-level only.

### Implication (#209 + new safe-mode bug)

- **File a new bug (severity major):** "Codex safe mode fails at argv parse on CLI ≥ the #39630 release".
  The fix needs a new meaning for `safe`. Exec has no interactive approval path (requests are rejected,
  policy is forced `never`), so the realistic mapping is a sandbox tier. For example `safe` →
  `--sandbox read-only` or `workspace-write` placed **after** `exec`, and full-auto → the current
  default. Drop the `-a` flag entirely. `/config` copy (`telegram/commands/config.py:119,775`) promises
  "untrusted tools blocked", which needs rewording.
- #209: add a Codex deny-list, separate from "managed" flags. Match both `--flag` and `--flag=` forms and
  short aliases. Cover the dangerous set above plus `-c` keys whose name starts with `sandbox_mode`,
  `approval_policy`, `bypass_hook_trust` or `shell_environment_policy`. Rename/split `_EXEC_ONLY_FLAGS`
  into "managed" (reject) and "dangerous" (reject, or warn behind an explicit opt-in).
- `codex.profile` users with legacy `[profiles.x]` tables: verify the v2 semantics (open unknown U6).

**Confidence:** high.

---

## Q2 (#416) — reasoning `minimal` + web_search

### Finding

- **The 400 comes from the server, not the client.** Wording, as seen in #416 and upstream #3559/#10706:
  `{"error":{"message":"The following tools cannot be used with reasoning.effort 'minimal': web_search.","type":"invalid_request_error","param":"tools","code":null}}`.
  There is no client-side guard: nothing in the tag's source pairs `Minimal` with web search. The
  upstream issues were closed NOT_PLANNED (#3559) or as stale (#5002).
- **0.157.1 still accepts `minimal` and does not validate effort at all.** The `ReasoningEffort` enum has
  `None, Minimal, Low, Medium, High, XHigh, Max, Ultra, Persistent, Custom(String)`, and
  `-c model_reasoning_effort="bogus_effort"` loads fine. The value is sent verbatim.
  `resolve_reasoning_effort` only remaps `Ultra`/`Persistent`. Clamping to the model's supported levels
  happens only on a mid-session model switch (`with_model`).
- **Per-model effort lists exist** in model metadata (`ModelInfo.supported_reasoning_levels`,
  `default_reasoning_level`), cached locally in `~/.codex/models_cache.json`. **No current model lists
  `minimal`** (cache fetched 2026-09-30, client 0.157.1):
  `gpt-5.5: low…xhigh`; `gpt-5.6-*`/`gpt-6-*`: `low…max` (+`ultra` on some); default `medium`
  (`gpt-5.6-sol`: `low`).
- **web_search is on by default, not a resume artefact.** The default `WebSearchMode` is `Cached`, and
  `Cached` still sends the hosted `web_search` tool (with `external_web_access: false`). Only `Disabled`
  omits it. So `minimal` fails on fresh runs too (the #416 title's "resumed session" is incidental).
  Workaround: `-c web_search="disabled"`, which is accepted. The valid values are
  `disabled|cached|indexed|live`.
- **How exec surfaces a failed turn:** app-server `Error` notification → `{"type":"error","message":"<msg> (<additional_details>)"}`
  (stored as `last_critical_error`), then `TurnCompleted{status: Failed}` →
  `{"type":"turn.failed","error":{"message":…}}`, using the turn's structured error, else the last
  critical error, else `"turn failed"`. The exact 400 body text inside `message` needs a live capture (U4).

### Evidence

- `codex-rs/protocol/src/openai_models.rs@L59-72` (enum), L414-415 (`default_reasoning_level`,
  `supported_reasoning_levels`); `codex-rs/protocol/src/openai_models/reasoning_effort.rs@L10-40`;
  `codex-rs/core/src/session/turn_context.rs@L639-659` (clamp only in `with_model`).
- `codex-rs/protocol/src/config_types.rs@L376-382` (`#[default] Cached`);
  `codex-rs/core/src/tools/hosted_spec.rs@L15-20` (`Cached => (false, None)`, `Disabled | None => return None`).
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs@L447-457` (Error → `error` event), L538-557 (Failed → `turn.failed`).
- Probes: `codex -c 'model_reasoning_effort="minimal"' features list` → OK;
  `-c 'model_reasoning_effort="bogus_effort"'` → OK; `-c 'web_search="bogus"'` →
  ``unknown variant `bogus`, expected one of `disabled`, `cached`, `indexed`, `live` in `web_search` ``.
- Untether: `src/untether/telegram/engine_overrides.py:15` `"codex": ("minimal", "low", "medium", "high", "xhigh")`;
  `src/untether/error_hints.py:208-211` maps `invalid_request_error` → "Try updating the engine CLI".

### Implication (#416)

- Add a specific hint pattern **ahead of** the generic `invalid_request_error`:
  `cannot be used with reasoning.effort` → "This model/tool combination doesn't support that reasoning
  level — pick Low or higher (or disable web search)." Precedent: the end-of-life ordering in `error_hints.py`.
- Pre-empt it. When `reasoning == "minimal"` for Codex, either append `-c web_search="disabled"` or
  (better, given no current model lists `minimal`) drop `minimal` from the Codex level list. Optionally
  derive levels from `~/.codex/models_cache.json` `supported_reasoning_levels` for the selected model,
  which is a local file with zero network. Also consider adding `max`, which current models support
  and Untether's Codex tuple lacks.

**Confidence:** high (client behaviour, defaults); med (exact current server wording on gpt-5.5, which
may instead reject `minimal` as an unsupported effort; see U4).

---

## Q3 (#419 / #417) — `Usage` and `WebSearchItem`

### Finding: `Usage`

`Usage` has 5 fields: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`
(`#[serde(default)]`, added #33454, 2026-07-16), `output_tokens`, `reasoning_output_tokens` (#19308,
2026-04-24). All are `i64` and required on the wire.

**It is the thread's cumulative total.** Since exec moved onto the in-process app-server (#15424),
`EventProcessorWithJsonOutput` stores each `thread/tokenUsage/updated` payload and on
`TurnCompleted{Completed}` emits `usage_from_last_total()`, which copies `usage.total.*`. It drops
`usage.last` (the latest model request) and `model_context_window`.

- **Within a run:** `total` = the sum over every model request in the turn (tool loops re-send the whole
  context each request). So `input_tokens` is already far larger than the context size. Local rollout
  example: `total.input_tokens = 1,087,735` vs `last.input_tokens = 49,869`, window 258,400.
- **On `exec resume`:** core seeds `token_info` from the rollout on resume/fork ("Seed usage info from
  the recorded rollout so UIs can show token counts immediately on resume/fork"). The first update in
  the new turn therefore reports `total = previous thread total + this run`. Exec resumes with
  `exclude_turns: true`, which only suppresses the *restored* notification. The seeded state still
  carries into the next update.
- **Consequence for Untether:** each Codex `CompletedEvent.usage` is a running thread total. Summing it
  per run (budget, daily totals, `/usage`) counts run 1 once, run 1 again inside run 2, and so on
  (triangular over-count). The correct per-run figure is `usage(run N) − usage(run N−1)` for the same
  thread_id. This is the same shape as the Claude `total_cost_usd` fix in #778 (`session_costs.json`),
  and needs a Codex token ledger keyed by thread_id. The "167508 in" in #417's `/export` is this cumulative figure.
- Edge: if a turn completes with no token update (e.g. no model call), `usage` is all zeros
  (`Usage::default()`). A failed or interrupted turn emits no usage at all.

### Finding: `WebSearchItem` (changed 2026-09-17, #46319)

`{ id, query, action, results? }`, where `action` is `WebSearchAction`, serialised with
`"type"` = `search` (`query?`, `queries?`) | `open_page` (`url?`) | `find_in_page` (`url?`, `pattern?`)
| `other`, and `results` is an optional array of opaque JSON (e.g. `{"url","content"}` or `{"url","error":{…}}`).

- **`item.started` is now emitted for web_search** (it is not on the `map_started_item` skip list, which
  skips only AgentMessage/Reasoning). It carries `query: ""` and `action: {"type":"other"}` (the
  upstream test `web_search_start_and_completion_reuse_item_id`). **`item.completed`** carries the real
  query, action and results under the **same** item id. Untether's cheatsheet ("only `item.completed`")
  is stale.
- **The wire shape has a duplicate `id` key.** `ThreadItem { id, #[serde(flatten)] details }` and
  `WebSearchItem` has its own `id` (the raw app-server id). `serde_json::to_string` writes
  `{"id":"item_0","type":"web_search","id":"search-1",…}`. Upstream tests use `to_value`, which keeps
  the last key (`"search-1"`). Checked locally: Untether's msgspec decoder also keeps the last key
  (`WebSearchItem(id='ws_abc', …)`). Started and completed share the raw id, so action correlation
  still works. Wire confirmation is still owed (U1).

### Evidence

- `codex-rs/exec/src/exec_events.rs@L59-73` (`Usage`, doc comment still says "during the turn"),
  L296-306 (`WebSearchItem`).
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs@L64` (`last_total_token_usage`), L118-129
  (`usage_from_last_total` copies `usage.total.*`), L303-322 (web search mapping), L343-351
  (`map_started_item`), L509-512 (`ThreadTokenUsageUpdated` stored, no event), L526-536 (`turn.completed`).
- `codex-rs/exec/tests/event_processor_with_json_output.rs` `token_usage_update_is_emitted_on_turn_completion`
  (L1302-1370), `web_search_start_and_completion_reuse_item_id` (L468-540),
  `web_search_page_actions_and_results_survive_json_output` (L412-466).
- `codex-rs/core/src/session/mod.rs@L1599-1606` (resume seeds token info) and L1627-1634 (fork);
  `codex-rs/protocol/src/protocol.rs@L2269-2307` (`TokenUsageInfo`, `append_last_usage` adds into total);
  `codex-rs/exec/src/lib.rs@L1409` (`exclude_turns: true`);
  `codex-rs/app-server/tests/suite/v2/thread_resume.rs@L3850-3897` (restored total 150 vs last 90).
- Local rollout (exec 0.157.1, originator `codex_exec`) `event_msg/token_count`:
  `{"info":{"total_token_usage":{…},"last_token_usage":{…},"model_context_window":258400},"rate_limits":{…}}`.
- Untether: `src/untether/schemas/codex.py:36-39` `Usage` has 3 fields (extra fields are silently
  ignored, so no decode error); `:130-132` `WebSearchItem(id, query)`; `runners/codex.py:339-360`
  uses `query` as the title for started and completed (started ⇒ empty title); `:610-617` passes
  `msgspec.to_builtins(usage)` as `CompletedEvent.usage`.

### Implication (#419, #417)

- #419: extend `Usage` with `cache_write_input_tokens: int = 0` and `reasoning_output_tokens: int = 0`
  (defaults keep old fixtures decoding). Extend `WebSearchItem` with `action: WebSearchAction | None`
  (tagged union with an `other` fallback) and `results: list[Any] | None = None`. Render the title on
  `completed`: `search` → the query (or the joined `queries`), `open_page` → `open <url>`,
  `find_in_page` → `find "<pattern>" in <url>`. On `started`, fall back to "web search" when `query == ""`.
- #417 / cost: treat Codex usage as cumulative per thread. Store the last total per thread_id and report
  deltas (mirror #778). Fresh runs: delta = total. Resumed runs: delta = total − stored.
  Unknown baseline (e.g. a thread started from the CLI, then `/continue`d): show the total with a
  "thread total" label, or omit the per-run figure.

**Confidence:** high (source + upstream test + resume test); med on the duplicate-`id` wire bytes (U1).

---

## Q4 (#819) — compaction and context window

### Finding

- **exec JSON drops compaction.** Core emits a `TurnItem::ContextCompaction` → app-server item
  `{"type":"contextCompaction","id":…}` (id only: no trigger, no pre/post tokens), plus the deprecated
  `thread/compacted` (`ContextCompactedNotification {threadId, turnId}`). In `map_item_with_id`,
  `ContextCompaction` falls to `_ => None`. `ServerNotification::ContextCompacted` falls to
  `_ => CodexStatus::Running`. The binary's `context_compaction` / `compaction_trigger` strings belong
  to analytics/rollout/compaction internals (`codex_analytics::CompactionTrigger`), not the exec stream.
- **One indirect exec signal, local compaction only.** `compact.rs` (the `RemoteCompactionSupport::Unsupported`
  path) emits a `Warning` after compacting:
  `Heads up: Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible to keep threads small and targeted.`
  Exec maps Warning → `item.completed` `{"type":"error","message":…}` (Untether already decodes `ErrorItem`).
  **The remote-v2 path (`compact_remote_v2.rs`) emits the item but no warning.** Which path the default
  OpenAI/ChatGPT provider uses is `provider.capabilities().remote_compaction`, likely V2 for OpenAI and
  therefore silent (U3).
- **Context window source.** `TokenUsageInfo.model_context_window` = `turn_context.model_context_window()`,
  which is the model's *usable* window: `context_window × effective_context_window_percent / 100`
  (default 95%). For all current models that is 272,000 × 0.95 = **258,400**, matching the rollout.
  Config overrides: `model_context_window`, `model_auto_compact_token_limit` (both accepted by
  `-c`). The auto-compact default is `min(config, 90% × context_window)`.
  **None of this appears in exec JSON.** `Usage` has no window field, and `last` is dropped.
- **How Codex computes context % itself:** `TokenUsage::percent_of_context_window_remaining(window)` uses
  `last.total_tokens` with a `BASELINE_TOKENS = 12000` offset:
  `remaining = (window − 12000 − max(last.total_tokens − 12000, 0)) / (window − 12000)`.
- **The app-server protocol exposes everything:** `thread/tokenUsage/updated` →
  `ThreadTokenUsage { total, last, modelContextWindow: number|null }`, where each `TokenUsageBreakdown`
  has `totalTokens, inputTokens, cachedInputTokens, cacheWriteInputTokens, outputTokens, reasoningOutputTokens`,
  emitted per model request. The `contextCompaction` item arrives via `item/started`/`item/completed`.
  There is also a `thread/compact/start` request. Verified against the **installed** binary via
  `codex app-server generate-json-schema --out /tmp/…` (314 schema files; `ThreadTokenUsage` required
  `last`,`total`; `contextCompaction` present in `ServerNotification.json`).
- **Rollout side-channel (native, no migration).** Exec still writes a rollout in `paginated` history
  mode. `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` gets `event_msg` `token_count`
  lines carrying `info.total_token_usage`, `info.last_token_usage`, `info.model_context_window` and
  `rate_limits` (the last is relevant to #411). `~/.codex/state_5.sqlite` table `threads(id, rollout_path, …)`
  maps thread_id → path. This is an **internal, unversioned format**: treat it as best-effort with a
  drift probe.

### Evidence

- `codex-rs/exec/src/event_processor_with_jsonl_output.rs@L323` (`_ => None`), L442-446 (Warning → error
  item), L598 (`_ => CodexStatus::Running`); upstream test `unsupported_items_do_not_consume_synthetic_ids` (L280).
- `codex-rs/app-server-protocol/src/protocol/v2/item.rs@L422-424` (`ContextCompaction { id }`),
  L1053-1054 (core → v2 mapping); `…/protocol/common.rs@L1943` (`thread/tokenUsage/updated`), L1994
  (`thread/compacted`); `schema/typescript/v2/{ThreadTokenUsage,TokenUsageBreakdown,ContextCompactedNotification}.ts`.
- `codex-rs/core/src/compact.rs@L404-409` (warning after local compaction), L257 (compaction item);
  `codex-rs/core/src/session/turn.rs@L1465-1497` (V2 remote vs local selection);
  `codex-rs/core/src/compact_remote_v2.rs@L374-376` (item, no warning).
- `codex-rs/protocol/src/openai_models.rs@L389-391` (95% default), L452-460, L514-534;
  `codex-rs/protocol/src/protocol.rs@L2412-2457` (`BASELINE_TOKENS`, percent formula).
- `~/.codex/models_cache.json`: every model has `context_window 272000`, `effective_context_window_percent 95`.
- Python SDK `sdk/python/src/openai_codex/_run.py@L78-79,114-115`: `TurnResult.usage` comes from
  `ThreadTokenUsageUpdatedNotification`; `api.py@L675,784` `thread.compact()`.

### Implication / recommendation (#819)

**Native in rc15 (exec JSON only):** no Codex `% ctx` from the stream. `turn.completed.usage` is
cumulative, so using it would show values far above 100%. Recommended rc15 scope for Codex:

1. **Context %, final header only, via the rollout side-channel.** After `turn.completed`, resolve
   `rollout_path` from `state_5.sqlite` (read-only, `mode=ro`) or glob `$CODEX_HOME/sessions/**/rollout-*-<thread_id>.jsonl`.
   Read the last `event_msg/token_count` and compute Codex's own formula from
   `last_token_usage.total_tokens` and `model_context_window`. Omit silently on any miss, as #819
   requires. Tailing the rollout during the run for live updates is possible but is more coupling.
   Defer it unless the final-only version proves stable.
2. **Compaction row:** upstream-blocked for exec JSON on the remote-v2 path. Optionally surface the
   local-path `Heads up: … multiple compactions …` warning item as a `🗜️ Context compacted` row
   (string match; drift-pinned). Record the rest in #819 as "exec drops ContextCompaction (exec
   `_ => None`); needs app-server".
3. Do **not** use a static per-model window table. The metadata already exists locally
   (`models_cache.json`: `context_window × effective_context_window_percent`) if the rollout is unavailable.

**Future (not rc15):** migrate the Codex runner to `codex app-server` (stdio JSON-RPC v2), or use the
official Python SDK `openai-codex`, which is an app-server client (pydantic, Python ≥ 3.10) and pins
its own `openai-codex-cli-bin`. That yields per-request `last` usage, `modelContextWindow`, a real
`contextCompaction` item, approval requests (which would bring back a genuine safe mode), and
`thread/compact/start`. It is a larger migration: JSON-RPC lifecycle, approvals, resume semantics.

**Confidence:** high (drops, window derivation, app-server surface); med (remote vs local compaction path
on OpenAI; rollout format stability).

---

## Q5 — Codex SDK contract vs Untether's msgspec schema

### SDK packages (verified 2026-09-30)

- TypeScript: `sdk/typescript`, npm **`@openai/codex-sdk`** (repo `version: 0.0.0-dev`; npm `latest` 0.159.2).
  It wraps `codex exec --experimental-json` (`src/exec.ts@L92`) and passes options as `--config`:
  `model_reasoning_effort`, `sandbox_workspace_write.network_access`, `web_search="…"`,
  `approval_policy="…"`, plus `--sandbox`, `--cd`, `--add-dir`, `--skip-git-repo-check`,
  `--output-schema`, `--thread-source`, `resume <id>`, `--image`.
- Python: **exists now.** `sdk/python`, PyPI **`openai-codex`** 0.159.2 (`from openai_codex import Codex`).
  It is **not** an exec wrapper: it drives the app-server (`generated/v2_all.py`, `notification_registry.py`),
  with runtime `openai-codex-cli-bin` (pinned `==0.153.4` at this tag; PyPI 0.159.2).

### TS types (verbatim excerpts, `sdk/typescript/src/events.ts`, `items.ts` @ rust-v0.157.1)

```ts
/** Describes the usage of tokens during a turn. */
export type Usage = {
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
};
export type TurnCompletedEvent = { type: "turn.completed"; usage: Usage; };
export type TurnFailedEvent = { type: "turn.failed"; error: ThreadError; };
export type ThreadErrorEvent = { type: "error"; message: string; };
export type ThreadEvent = ThreadStartedEvent | TurnStartedEvent | TurnCompletedEvent | TurnFailedEvent
  | ItemStartedEvent | ItemUpdatedEvent | ItemCompletedEvent | ThreadErrorEvent;

export type WebSearchItem = { id: string; type: "web_search"; query: string; };
export type CommandExecutionStatus = "in_progress" | "completed" | "failed";
export type ThreadItem = AgentMessageItem | ReasoningItem | CommandExecutionItem | FileChangeItem
  | McpToolCallItem | WebSearchItem | TodoListItem | ErrorItem;
```

**The TS SDK lags the Rust source.** It has no `collab_tool_call`, no `WebSearchItem.action/results`, and
no `declined` status. Its `ApprovalMode` still offers `"untrusted"` (fatal on 0.157.1), and its `Usage`
comment says "during a turn" although the value is the thread total. **Use `exec_events.rs` as the
contract, not the TS SDK.**

### Diff: Untether `schemas/codex.py` (header: "derived from tag rust-v0.77.0") vs `exec_events.rs` @ rust-v0.157.1

| Area | Upstream 0.157.1 | Untether | Action |
|---|---|---|---|
| `Usage` | 5 fields incl. `cache_write_input_tokens`, `reasoning_output_tokens` | 3 fields | add both (default 0) — #419 |
| `WebSearchItem` | `id, query, action (search/open_page/find_in_page/other), results?`; started + completed | `id, query` | add `action`, `results` — #419 |
| `McpToolCallItemResult` | `content, _meta?, structured_content` (Option) | `content, structured_content` (required) | add `meta: Any = None` (`name="_meta"`); make `structured_content` default `None` |
| `McpToolCallItem.arguments` | `#[serde(default)]` | required | default `None` |
| `CollabToolCallItem.tool` | enum `spawn_agent/send_input/wait/close_agent` (other tools → item dropped) | `str \| None` | fine (lenient) |
| `CollabAgentState.status` | enum `pending_init/running/interrupted/completed/errored/shutdown/not_found` | `str` | fine |
| `CommandExecutionStatus` | `in_progress/completed/failed/declined` | same | ok |
| `PatchApplyStatus` | `in_progress/completed/failed` (declined → failed) | same | ok |
| `AgentMessageItem` | `text` only (the app-server `phase` is not forwarded) | `text, phase?` | harmless; `phase` is always `None` now |
| `ReasoningItem` | emitted only on completed, empty summaries skipped | same | ok |
| Top-level events | 8 variants | 8 variants | ok |
| Not in exec JSON | `contextCompaction`, `plan`, `imageView`, `sleep`, `imageGeneration`, review-mode, `subAgentActivity`, hooks, `turn/diff`, token usage updates | — | upstream drop; see Q4 |
| Warnings/deprecations/config warnings/model reroute | surfaced as `item.completed` `error` items (`"model rerouted: a -> b (…)"`) | `ErrorItem` | ok; consider distinct rendering |

Decode check (local, `uv run python`, no writes): the 0.157.1 wire lines for web_search (duplicate id),
5-field usage, `collab_tool_call` and `declined` all decode today. Extra fields are dropped silently,
so nothing breaks, but the data is lost.

**Confidence:** high.

---

## Drift probes to add (`tests/test_codex_cli_schema_drift.py`, zero-token)

Mirror `tests/test_claude_cli_schema_drift.py`: skip when `codex` is absent; resolve the native binary
via `readlink -f $(which codex)` → `…/node_modules/@openai/codex-*/vendor/*/bin/codex`; run each
subprocess with `timeout` and a temp `CODEX_HOME` where the command allows it.

| # | Probe | Pins |
|---|---|---|
| D1 | `codex -a untrusted features list` → rc≠0, stderr contains `invalid value 'untrusted'` and `[possible values: on-request, never]` | safe-mode argv (the new bug); fails loudly if the value set changes |
| D2 | `codex -c 'approval_policy="untrusted"' features list` → stderr `is no longer supported` | config route closed |
| D3 | `codex exec --help` contains `--json`, `--skip-git-repo-check`, `--color`, `--ephemeral`, `--ignore-rules`, `--ignore-user-config`, `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust`, `--approve-for-me`, `--add-dir`, `--worktree`, `-s, --sandbox`, `--output-schema`, `-o, --output-last-message` | #209 deny-list completeness (new flags show up as a diff) |
| D4 | `codex exec --yolo --help` rc=0; `codex exec --not-so-yolo --help` rc=0; `codex exec --full-auto --help` → `unexpected argument` | hidden aliases the deny-list must match |
| D5 | `codex --help` lists `-a, --ask-for-approval` and `--search`; `codex exec -a never --help` → `unexpected argument '-a'` | top-level-only flags (not inherited by exec) |
| D6 | `codex -c 'web_search="bogus"' features list` → ``expected one of `disabled`, `cached`, `indexed`, `live` `` | #416 workaround value |
| D7 | `codex -c 'model_reasoning_effort="minimal"' features list` rc=0 | client still passes `minimal` through |
| D8 | Binary `grep -c -F` (fixed strings are fast; avoid regex over 285 MB): `reasoning_output_tokens`, `cache_write_input_tokens`, `thread/tokenUsage/updated`, `contextCompaction`, `Heads up: Long threads and multiple compactions`, `is no longer supported; remove this setting` | usage fields, compaction warning text for the Q4 option-2 matcher |
| D9 | `codex app-server generate-json-schema --out <tmp>` → `v2/ThreadTokenUsageUpdatedNotification.json` definitions `ThreadTokenUsage.required == ["last","total"]` and has `modelContextWindow`; `ServerNotification.json` contains `"contextCompaction"` | app-server contract for the future migration and #819 |
| D10 | (optional, reads host state, so opt-in) `~/.codex/models_cache.json` models: `context_window`, `effective_context_window_percent`, `supported_reasoning_levels[].effort` keys present | window derivation and per-model effort lists |
| D11 | Fixture test: a recorded 0.157.1 `web_search` line **with a duplicate `id`** decodes to the raw id, and `turn.completed` with 5 usage keys round-trips all 5 | schema regression once #419 lands |

## Open unknowns / live capture needed (paid — do not run without approval)

Run these in a scratch git dir with `CODEX_HOME` untouched, `--skip-git-repo-check`, and output teed to `/tmp`.

- **U1 — web_search wire bytes (duplicate `id`) and started/completed pairing:**
  `printf 'Use web search to find the codex-cli latest version, answer in one line' | codex -c 'web_search="live"' exec --json --skip-git-repo-check --color=never - | tee /tmp/u1.jsonl`
  Then `grep web_search /tmp/u1.jsonl`.
  **Result (2026-10-01, codex-cli 0.157.1, `-m gpt-5.6-luna`, #419 step 0):** confirmed. Both
  `item.started` and `item.completed` carry the **duplicate `id`** (`"id":"item_2",…,"id":"exec-8c5b…"`),
  the raw id is shared across the two phases, `item.started` has `query:""` + `action:{"type":"other"}`,
  and `item.completed` carries the real query in both `query` and `action.query` plus `results`
  (`text_result` objects with `title`/`url`/`snippet`). A second search completed with `query:""`,
  `action:{"type":"other"}` and one `"Internal Error"` result — the placeholder title covers it.
  Trimmed fixture: `tests/fixtures/codex_0157_web_search.jsonl`.
- **U2 — cumulative usage across resume:**
  `echo 'say hi' | codex exec --json --skip-git-repo-check - | tee /tmp/u2a.jsonl`, take the `thread_id`, then
  `echo 'say hi again' | codex exec --json --skip-git-repo-check resume <thread_id> - | tee /tmp/u2b.jsonl`.
  Compare `turn.completed.usage` in u2a/u2b with the rollout `token_count` total/last
  (`sqlite3 'file:~/.codex/state_5.sqlite?mode=ro' "select rollout_path from threads where id='<thread_id>'"`).
  Also confirms whether resume appends to the original rollout file (for the side-channel).
  **Result (2026-10-01, codex-cli 0.157.1, `-m gpt-5.6-luna`, #419 step 0) — GO:** usage is the
  thread's running total. Three runs on one thread (fresh, `resume <id>`, `resume --last`) reported
  `input_tokens 10656 → 21328 → 32017`, `cached 5888 → 11776 → 21760`, `output 6 → 13 → 21`
  (every field non-decreasing). The rollout's `token_count` agrees: `total` = the exec figure,
  `last` ≈ 10.7k per run. `thread.started` is re-emitted with the **same** `thread_id` on both
  `resume <id>` and `resume --last`, and resume appends to the original rollout file. Fixture:
  `tests/fixtures/codex_0157_resume_usage.jsonl`. Cost: ≈104k input tokens (≈60 % cached) + ≈220
  output on the ChatGPT-subscription auth (no metered spend).
- **U3 — compaction in exec JSON + rollout:**
  a multi-turn session with `-c model_auto_compact_token_limit=20000` (or a `/compact`-equivalent via
  the app-server). Check exec JSON for an `error` item "Heads up: …", and the rollout for `compacted` /
  `context_compacted` lines. This tells us whether the OpenAI provider takes the remote-v2 (silent) path.
- **U4 — current 400 wording:**
  `echo hi | codex exec --json --skip-git-repo-check -m gpt-5.5 -c 'model_reasoning_effort="minimal"' -`
  (default web_search=cached), then the same with `-c 'web_search="disabled"'`. The server may now
  reject `minimal` itself on gpt-5.5, with different wording. Capture the exact `error` and `turn.failed`
  lines and any `Reconnecting… n/m` retries.
- **U5 — safe-mode replacement semantics:** with `--sandbox read-only` after `exec`, confirm that a
  write attempt produces a failed `command_execution`/`file_change` item and not a hang. Exec rejects
  approval requests, so this is expected but unverified.
- **U6 — profile v2 back-compat:** does `--profile foo` still honour a legacy `[profiles.foo]` table in
  `config.toml`, or only `$CODEX_HOME/foo.config.toml`? This is zero-token (`codex exec -p foo` with an
  empty stdin exits "No prompt provided via stdin" before loading the profile, and `features list`
  rejects `--profile` with "--profile only applies to runtime commands …". Candidate: `codex -p foo
  debug prompt-input` (listed as profile-aware; confirm it is local-only first) in a temp `CODEX_HOME`
  holding both a legacy `[profiles.foo]` table and a `foo.config.toml`). It was not run here, to avoid
  touching `~/.codex`.
