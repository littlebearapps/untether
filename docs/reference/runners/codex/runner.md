# Codex runner

Untether runs Codex over one of two transports, chosen per install by `transport` in the `[codex]` (or
`[engines.codex]`) table ([#962](https://github.com/littlebearapps/untether/issues/962)):

- **`exec`** (the default): one `codex exec --json` per message. See [untether-events.md](untether-events.md) and
  [exec-json-cheatsheet.md](exec-json-cheatsheet.md).
- **`app-server`** (opt-in): one private `codex app-server --listen stdio://` per message, spoken to over JSON-RPC
  (stable-tier methods only, `experimentalApi: false`). It counts as native despite the CLI's Experimental label
  (Nathan, 2026-11-18, DEC-1); `experimentalApi` fields are reserved for plan mode (#965). See
  [Transports](#transports) below and the app-server section of [untether-events.md](untether-events.md#app-server-transport-962).

The engine id stays `codex` on both, resume lines (`codex resume <thread uuid>`) work across them, and a chat can
switch transport without losing its session.

| | |
|---|---|
| Tested against | codex-cli **0.171.0** (exec and app-server drift suites green on 2026-11-23; earlier [#1254](https://github.com/littlebearapps/untether/issues/1254); exec flag snapshot `PROBED_CLI_VERSION` in `tests/test_codex_cli_schema_drift.py`; app-server schema `PROBED_APP_SERVER_VERSION` in `src/untether/schemas/codex_app_server.py`) |

## Transports

| | `exec` | `app-server` |
|---|---|---|
| Process | `codex [extra_args] exec --json … -` per message | `codex [extra_args] app-server --listen stdio://` per message (DEC-4; live sessions are #964) |
| `extra_args` | the #209 guard (bypass and managed flags refused) | only `-c`, `--enable`, `--disable`, `--strict-config` (the #209 `-c` rule still applies) |
| `profile` | `--profile <name>` | refused at load (`codex app-server` has no `--profile`, DEC-19) |
| Full auto | no sandbox flag (Codex's own setting) | `sandbox` omitted only when Codex's selected `[projects]` entry (from `config/read`: the first match for the cwd, then its git repo root, no parent walk, #1408) has a `trust_level`, else `read-only` with a ⚠️ row when the user sandbox is writable (P1: a writable sandbox in an unlisted project makes Codex save it as trusted) |
| Safe | `--sandbox read-only` | `sandbox: "read-only"` on `thread/start` / `thread/resume` |
| Ask me / Ask before writes / Auto-review ([#421](https://github.com/littlebearapps/untether/issues/421)) | refused before spawn (`codex_mode_needs_app_server`, session kept): exec can't ask | `on-request` with `workspace-write` (read-only + ⚠️ row in a project Codex doesn't list, D1) / `read-only` / `workspace-write` + `auto_review`; see [Approvals and tiers](#approvals-and-tiers-963-421) |
| Approvals, diff preview ([#963](https://github.com/littlebearapps/untether/issues/963)) | none (`codex exec` rejects every approval request) | Telegram cards for command, file-change and permission requests; Diff preview applies |
| Unknown `permission_mode` | runs full auto, logs `codex.permission_mode.unknown` | runs read-only, logs `codex.app_server.unknown_mode_safe` once (DEC-5, fail closed) |
| Final answer | `_select_final_answer` over `agent_message` items | `agentMessage.phase = "final_answer"` |
| `/cancel` | SIGTERM | `turn/interrupt`, SIGTERM after 3 s |
| `% ctx` / 🗜️ row | none | `thread/tokenUsage/updated` / `contextCompaction` item (#832) |
| Before spawning | nothing | the #961 support gate (below) |

Every `thread/start` and `thread/resume` sends the tier's `approvalPolicy` and `approvalsReviewer` (`never` + `user`
for Full auto, Safe and every cron or webhook run), the cwd, the sandbox (when set) and the chat's model (`thread/resume` adds `excludeTurns: true`: Untether never reads the
history, and full-history hydration draws a deprecation warning on 0.170.0). A resumed thread restores its saved
model, so a resume with no `/model` override sends Codex's own configured model from `config/read` (`config.model`)
instead ([#1369](https://github.com/littlebearapps/untether/issues/1369)): `/model clear` then takes effect on the
next message rather than only after `/new`. Untether then compares the echo: a mismatch logs `codex.permission_mode.mismatch` and
adds a ⚠️ row. Approval requests become cards (below); any other server request (user input, MCP elicitation, …) is
declined with one ⚠️ row.
`/continue` lists the project's most recent thread (`thread/list`, source kinds `cli`, `vscode`, `exec`, `appServer`)
and resumes it, or starts a new one with a note.

**Switching.** Set `transport = "app-server"` under `[codex]`; with `watch_config = true` the engine table is rebuilt
and the next message uses it (a run already working keeps its transport), otherwise restart. Set `"exec"` (or remove
the key) to go back. Logs: `codex.app_server.ready version=…` once per binary, then `codex.app_server.*` and
`codex.rpc.*` per run.

**Context and compaction (#832, app-server only).** Each `thread/tokenUsage/updated` gives the header
`N% ctx` = `last.totalTokens / modelContextWindow` of that update (half up, clamped to 100; native, so no `~`), sent
only when it changes and never without a window. `total` is thread-cumulative and becomes the footer's five token
keys (exec's meaning; the backend declares `usage_scope = {"tokens": "thread_cumulative"}`, #850). A
`contextCompaction` item opens `🗜️ Compacting context…` and closes as `🗜️ Context compacted`; the count goes to
`runner.completed compactions=N`. P8 (0.170.0) found that a compaction turn reports `last` as the compaction
request's size with the breakdown zeroed, so the value after a compaction is that size until the next request
updates it. Exec gets neither: #832's exec half (reading Codex's rollout files) is won't-do under the native-first
rule (DEC-13).

**Session compaction count ([#1182](https://github.com/littlebearapps/untether/issues/1182), app-server only).** The `contextCompaction` `item/completed` row carries
`detail["compaction"] = True` (never the started row), and the bridge counts it per thread in `session_costs.json`:
the header reads `N% ctx (1)`, and the count carries over to later runs of the same thread. The deprecated
`thread/compacted` notification is not read (0.171.0 schema: "Use `ContextCompaction` item type instead"). A thread
Untether hasn't counted yet starts at 0; seeding it from `thread/resume`'s `thread.turns[].items[]` (a native source)
is a possible follow-up, not built. **Exec gap:** `codex exec --json` has no compaction event (checked on 0.170.0;
0.171.0's binary has no `context_compaction` item string, inferred), so exec chats show no count.

## Project trust ([#1240](https://github.com/littlebearapps/untether/issues/1240))

codex-cli 0.169.0+ disables an untrusted project's `.codex/` layer (config, MCP servers, hooks, rules). Exec reports
it on **stderr only**, before the JSON stream (`warning: Untrusted project-local configuration is disabled…`, checked
on 0.170.0, 2026-11-18). `CodexRunner.on_stderr_line` matches that prefix, logs `codex.project_config_untrusted` once
and emits one `⚠️` warning row right after `StartedEvent` (or before the final event if stderr arrives late). Native
signal surfaced as-is; Untether never grants trust (no `--trust`, refused in `extra_args` by #1040). On app-server
the same notice arrives as a `configWarning` and gives the same row and footer. Docs:
[project config](https://developers.openai.com/codex/config-advanced),
[`projects.<path>.trust_level`](https://developers.openai.com/codex/config-reference).

## App-server drift probe and support gate ([#961](https://github.com/littlebearapps/untether/issues/961))

Codex can print its own protocol for the installed version at no token cost:
`codex app-server generate-json-schema --out DIR` (stable tier) and the same with `--experimental`. Untether uses
that, never a version comparison, to decide whether a CLI can run the app-server transport.

- **Contract.** `src/untether/schemas/codex_app_server.py` lists what the transport depends on:
  `CODEX_APP_SERVER_USED` (client requests, notifications and server requests, each with the tier it relies on),
  `CODEX_APP_SERVER_TYPES` (payload types and `ThreadItem` variants it reads) and `CODEX_APP_SERVER_SENDS` (enum
  values it sends: approval policies, sandbox modes, reviewers, declines, `ModeKind`). Later phases add their own.
- **Subset.** `runners/codex_app_server.schema_subset()` reduces both bundles to `{key: {tier, names}}`: a method's
  params (`params.*`) and response (`result.*`) property names, a type's properties, an enum's values. Anything in
  the stable bundle is `S`; anything only in the experimental bundle is `E`.
- **Fixture.** `tests/fixtures/codex_app_server/schema-subset-0.171.0.json` (codex-cli 0.171.0, 2026-11-23), written
  by `scripts/codex_app_server_schema_subset.py`. `tests/test_codex_app_server_schema.py` (CI) checks the contract
  against it; `tests/test_codex_app_server_drift.py` (lba-1 only) regenerates both bundles from the installed CLI and
  fails when a fixture method, field, variant or enum value is removed or changes tier. The same file runs one
  JSON-RPC handshake: `initialize` succeeds, `collaborationMode/list` is refused without `experimentalApi`, and the
  process exits within 5 s of stdin closing. It never calls `thread/start` or starts a turn.
- **Gate.** Before the first app-server spawn per binary (path + mtime, so a Codex update re-checks),
  `codex_app_server_support()` generates both bundles in a temp dir (~0.2 s each, removed afterwards) and checks every
  `CODEX_APP_SERVER_USED` method is present at its tier. A missing method, a stable one now experimental-only, or a
  CLI that can't generate the schema refuses the run before spawn with one `completed_error` carrying
  `usage[PRESPAWN_BLOCKED_KEY] = "codex_app_server_unsupported"` (the saved session is kept). The message names the
  installed version, the tested minimum (codex-cli 0.169.0) and the fallback `transport = "exec"`. A probe that
  can't run (timeout, OS error) isn't cached. Logs: `codex.app_server.ready version=…` once per binary,
  `codex.app_server.unsupported`, `codex.app_server.probe_failed`. The exec transport never runs the gate.

After a Codex upgrade on lba-1: `uv run pytest tests/test_codex_app_server_drift.py tests/test_codex_cli_schema_drift.py -v -rs`
(a skip on lba-1 counts as a fail). On drift, re-derive the fixture with the script, bump `PROBED_APP_SERVER_VERSION`
and review the transport before the next rc.

## Spawn environment ([#1241](https://github.com/littlebearapps/untether/issues/1241))

Since v0.36.2, `CodexRunner.env()` spawns `codex exec` with the [env allowlist](../../env-vars.md#env-allowlist-claudepi)
(`filtered_env()`), the `[security] env_extra_allow` / `env_extra_prefix_allow` extras, and Codex's own extras:
prefix `CODEX_` (`CODEX_HOME`, `CODEX_SQLITE_HOME`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `CODEX_CA_CERTIFICATE`),
`RUST_LOG`, `DBUS_SESSION_BUS_ADDRESS` (keyring credential store), the OpenAI workload-identity names and the shared
proxy/CA tuple `env_policy.NETWORK_ENV_NAMES`. The ChatGPT login is found through `HOME` (`~/.codex`). The #962
app-server child must reuse `_CODEX_ENV_EXTRAS` / `_CODEX_ENV_PREFIXES`, never a second list. The first spawn per
process logs `env_policy.dropped engine=codex names=[…]` (names only).

Names Codex reads under a user-chosen name (`model_providers.<id>.env_key`, MCP `env_vars`, `bearer_token_env_var`,
`env_http_headers`) can't be listed, so they need `env_extra_allow`. Codex already starts stdio MCP servers with a
minimal env of its own, so MCP needs no Untether work.

Native-first record (codex-cli 0.170.0, 2026-11-18): Codex's `shell_environment_policy` (`inherit`, `filters`, `set`,
`ignore_default_excludes`) is **partial** for this job. It is the user's own config, filters only what Codex's shell
commands see (not the Codex process, hooks or `notify`), and its default `inherit = "all"` keeps KEY/SECRET/TOKEN
names. Untether doesn't override it with `-c`; it stays the user's second layer, and Untether's filter only limits what
Untether hands to Codex, as for Claude, Pi and Antigravity. The spawn environment is Untether's own cross-engine
surface. Not covered: the `codex --version`, schema-probe and `/auth codex` calls still inherit the full env (they run
no model or tools; see [#375](https://github.com/littlebearapps/untether/issues/375)).

## Approvals and tiers (#963, #421)

App-server only. Checked against codex-cli 0.171.0 (schema, source, and the token probes P-A and P-B, 2026-11-23).

| Tier (stored) | `approvalPolicy` | `sandbox` | `approvalsReviewer` |
|---|---|---|---|
| Full auto (`auto` / none) | `never` | omitted if Codex lists the project, else `read-only` | `user` |
| Safe (`safe`) | `never` | `read-only` | `user` |
| Ask me (`ask`) | `on-request` | `workspace-write` if Codex lists the project, else `read-only` + ⚠️ row | `user` |
| Ask before writes (`ask-writes`) | `on-request` | `read-only` | `user` |
| Auto-review (`review`) | `on-request` | as Ask me | `auto_review` |
| any tier, cron or webhook run | `never` | per tier | `user` |

- **Requests.** `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` and
  `item/permissions/requestApproval` are deferred by the peer (`codex_rpc.DEFER`) and posted as a card
  (`runners/codex_approvals.py`): "Permission Request [codex] - Run command", "Network access to `host`", "Send input
  to a running command", "Edit files" or "Extra permissions", with "(sub-agent)" when the request's `threadId` isn't
  the run's. A command card always shows `params.command`, the string that runs (redacted); Codex's `commandActions`
  parse is only an extra "Codex reads it as" line. File cards take the diff from the `fileChange` item
  (`item/started`, refreshed by `item/fileChange/patchUpdated`), cached by item id from any thread before the
  sub-agent filter; P-B saw `item/started` about 5 ms before the request. Diff preview off shows the paths only.
- **Buttons.** ✅ Approve = `accept`, 🔁 Allow for session = `acceptForSession`, 📌 Always allow `<prefix>` / `<host>`
  (saved on this host) = `acceptWithExecpolicyAmendment` / `applyNetworkPolicyAmendment` (Codex writes
  `$CODEX_HOME/rules/default.rules`), ❌ Deny = `decline` (always offered, D2), 🛑 Stop = `cancel` (the turn ends and
  the final says to tell Codex what to do differently). Which buttons show comes from `availableDecisions` when Codex
  sends it, else from Codex's own default set. Permissions: Approve and session grant only what the card shows; Deny
  grants nothing.
- **Answers.** `CodexResponder` (engine `codex`, `ia:` prefix) writes the answer from the tap through the peer's
  locked write. `serverRequest/resolved` closes the card with the answer, or withdraws it ("⏹️ Withdrawn by Codex")
  when Codex dropped it unanswered. Cards still open at `turn/completed` are retired, and the run's end drops the
  session, so a late tap writes nothing. There's no deadline: Codex has none for these requests.
- **Auto-review.** `item/autoApprovalReview/started|completed` give 🛡️ rows (allowed, denied with the rationale,
  timed out, stopped); no card, no push. `guardianWarning` gives a ⚠️ row. `codexErrorInfo: "tooManyDenials"` (with
  `auto_review.circuit_break_action = "strict"`) gets a hint naming Ask me.
- **Unattended.** Cron and webhook runs send `never`. A request that still arrives is declined by the peer, with one
  🔒 row, `permission.unattended_deny` and `usage["unattended"]` for the footer (#835 posture).
- **P-A (0.171.0).** `on-request` + `workspace-write`, trusted scratch repo: a `curl` retry outside the sandbox asked
  with `kind: "command"`, a `reason`, `proposedExecpolicyAmendment` and `availableDecisions: ["accept",
  {acceptWithExecpolicyAmendment}, "cancel"]` on a stable connection, and no `networkApprovalContext`. The unlisted
  `decline` was accepted: `serverRequest/resolved` came at once, the item completed `declined`, and the turn went on
  to `completed`. `cancel` wasn't exercised (the model didn't ask again), so Stop's end-of-turn behaviour rests on
  the schema text ("the turn will also be immediately interrupted") until CX-12.
- **P-B (0.171.0).** `on-request` + `read-only`: `acceptForSession` on the first `apply_patch` let the second edit to
  the same file through without a request. No `patchUpdated` was sent. `add` diffs are the file content; `update`
  diffs are a unified hunk.

## App-server cancel and resume failures

`/cancel` on an app-server run sends `turn/interrupt {threadId, turnId}` and keeps reading the same buffered stdout
reader until `turn/completed` (any status: `interrupted`, or a finish that won the race; an error answer such as
`no active turn` is ignored), then closes stdin so Codex exits by itself. It runs shielded and bounded at 3 s; if the
bound runs out, the base teardown's SIGTERM is the fallback. A cancel before the turn id is known (during the
handshake) is a plain teardown. Logged as `codex.app_server.interrupt ok=… elapsed_s=… open_items_closed=…`. The base
`run_impl` closes the reader in the run's own task (`aclosing`), so a cancel that lands in the bridge rather than in
the read (a stalled Telegram send, #614) interrupts the same way.

A failed `thread/resume` ends the run with Codex's own text (`no rollout found for thread id …`, `invalid session id:
…`) inside `codex app-server thread/resume failed: …`, which `_resume_failure_clears_session` matches. Resume tokens are
the thread UUIDs on both transports, so a chat can switch transport and keep its session.

## Native-vs-custom register

What the Codex runner builds itself, and why (native-first rule, step 4). Checked against codex-cli 0.170.0; the #963
/ #421 rows and the usage, catalogue and web-search rows against 0.171.0.

| Piece | Kind | Checked | Why custom / notes |
|---|---|---|---|
| App-server transport | native by exception | 0.170.0, 2026-11-18 (DEC-1) | The CLI still labels `codex app-server` Experimental (fails the "not marked experimental" test); Nathan accepted it. Stable-tier methods only, `experimentalApi: false`; experimental fields only for plan mode (#965) |
| One app-server process per run | custom (process model) | 0.170.0, 2026-11-18 (DEC-4) | Native would be a long-lived session; per-session processes come with live sessions (#964). Stdio exit on stdin close is about 0.1 s |
| Cancel | native: `turn/interrupt` (stable) | 0.170.0, 2026-11-19 | SIGTERM only as the fallback when the turn doesn't end within 3 s |
| Closing items Codex leaves open after an interrupt or a failed turn | custom | 0.170.0, 2026-11-18 (Δ D19) | No native signal: an interrupted `commandExecution` never gets `item/completed`. A failed turn's open rows are closed (`ok: false`, `failed` / `cancelled`) before the error final; on `/cancel` the bridge has stopped reading, so they're only counted in the log |
| Writable tiers read-only in an untrusted project (Full auto, Ask me, Auto-review) | custom (policy) | 0.170.0, 2026-11-19 (P1, DEC-7); 0.171.0, 2026-11-23 (D1) | Codex saves `trust_level = "trusted"` for an unlisted project when a thread starts there with a writable sandbox, sent or inherited. Untether never grants trust, so it sends `read-only` unless the entry Codex selects has a `trust_level`; unknown or missing config fails closed. `config/read` and `thread/start` have no trust field (0.171.0 schema), so Untether mirrors Codex's lookup (`selected_project_entry`, #1408): `ProjectTrustLookup::from_paths` order (canonical cwd, cwd, canonical repo root, repo root), first key present wins even with no `trust_level`, no parent walk. The repo root mirrors Codex's own filesystem walk (`git-utils/src/trust.rs` `resolve_root`: nearest `.git`, skipping a `.git` directory without `HEAD`; no `git` call, so an invalid nested repo that `git` would ignore still counts, as it does for Codex), and only a real `.git` directory yields a root, so linked worktrees, submodules, `--separate-git-dir` checkouts and symlinked `.git`/`HEAD` fail closed (Codex's worktree validation is #1413). The native signal (`config/read`) decides; only the fallback is custom. `turn/start.sandboxPolicy` may avoid the trust write (P-C, not run) |
| Unknown `permission_mode` runs read-only | custom (policy) | 0.170.0, 2026-11-18 (DEC-5) | No native notion of Untether's mode names. Fail closed (exec keeps its old fail-open; an app-server-only tier on exec is refused before spawn) |
| Tier names and labels (Full auto, Safe, Ask me, Ask before writes, Auto-review) | custom (labels) | 0.171.0, 2026-11-23 (#421, DEC-5) | Codex has no named presets on app-server (the TUI's are client-side). Each tier is only a native (`approvalPolicy`, `sandbox`, `approvalsReviewer`) triple; `untrusted` is retired in the docs and `granular` is experimental, so neither is offered |
| Preamble | custom (prompt prefix) | 0.170.0, 2026-11-18 (DEC-15, PLAN Q2) | Native `thread/start.developerInstructions` exists, so this fails "no instructions prepended when a system-prompt option exists". The MVP keeps the bridge's prefix, as for every engine, because moving one engine needs a bridge seam; owed as a follow-up |
| Moderated answers | native passthrough | 0.170.0, 2026-11-18 (E2, PLAN Q6) | `agentMessage.moderation` is untyped (its `display` shape changed in one release); the text is shown as sent and `codex.app_server.moderated` logged once per run |
| Rows for items exec never emits | custom (rendering) | 0.170.0, 2026-11-19 | `collabAgentToolCall` (sub-agent row), `imageView`, and one generic row per unknown item type (logged once). Untether's own progress surface, fed from native items |
| Approval cards | native + rendering | 0.171.0, 2026-11-23 (#963) | The three stable approval requests, `serverRequest/resolved` and Codex's own decisions; the card text and buttons are Untether's surface. User input (#965) and MCP elicitation (#1243) are still declined with one ⚠️ row |
| `availableDecisions` read from an experimental field | native by exception (read-only) | 0.171.0, 2026-11-23 (findings H2, P-A) | The schema marks it experimental, yet the server sends it on stable connections (only `additionalPermissions` is stripped). Untether reads it as a hint and never asks for experimental fields |
| Decision-list fallback | custom | 0.171.0, 2026-11-23 (#963) | The stable schema has no decision list; when `availableDecisions` is absent Untether mirrors Codex's own `default_available_decisions` (approvals.rs L379) so the buttons match what Codex would offer |
| Deny always offered (`decline`) | custom (policy) | 0.171.0, 2026-11-23 (D2, P-A) | Codex's plain-command default set has no `decline`; it is in the decision enum, and P-A shows Codex accepts it unlisted and carries on. Stop (`cancel`) shows where Codex lists it |
| Permission grants limited to what the card shows | custom (policy) | 0.171.0, 2026-11-23 (#963) | Codex takes a granted subset (`GrantedPermissionProfile`); Untether grants only the network flag, paths, entries and glob depth it rendered, and names the rest as not granted |
| Unattended decline fallback | custom | 0.171.0, 2026-11-23 (#963, D4) | Cron and webhook runs send `never`, so none is expected; one that arrives is declined fail closed with the #835 🔒 row, log and footer, as for Claude |
| Auto-review rows | native + rendering | 0.171.0, 2026-11-23 (#963) | `item/autoApprovalReview/*` are stable methods with an `[UNSTABLE]` payload, read as dicts (a missing key gives a generic row). Overriding a denial (`thread/approveGuardianDeniedAction`) is #1245 |
| `/usage` quota and plan limits ([#966](https://github.com/littlebearapps/untether/issues/966), was the [#1370](https://github.com/littlebearapps/untether/issues/1370) gap) | native read, custom spawn | 0.171.0, 2026-11-23 (DEC-11) | `account/read` + `account/rateLimits/read` (stable) give plan, every bucket and window, `ordinaryUsageAllowed`, credits and the reset-credit count. Untether's `/usage` and exec runs have no Codex process to ask, so `runners/codex_reads.py` spawns `codex app-server` for one batch of reads (zero tokens, never `thread/start`; empty temp cwd; app-server allow-list of `extra_args`; pre-spawn guard). Both transports. Reset credits are shown as a count and never consumed (DEC-29) |
| Read caches | custom | 0.171.0, 2026-11-23 | Usage 60 s (failures 30 s), catalogue 1 h per cwd (failures 5 min), one lock per key. `/usage` must not spawn per tap and a final must never wait on a spawn. Keys hold the codex command and CLI version; values keep the auth kind and plan only, never email, user or account ids |
| Quota footer | native push (app-server) + custom cache peek (exec) | 0.171.0, 2026-11-23 | App-server merges `account/rateLimits/updated` (sparse; null never clears) into the cache. `codex exec --json` has no rate-limit event and no process after the run, so the exec footer only peeks the cache (≤ 15 min) filled by `/usage`, app-server pushes and the warm-up. Never spawns |
| Background warm-up | custom | 0.171.0, 2026-11-23 (DEC-27) | On the first Codex run per Untether process and cwd, then at most hourly, the catalogue and usage are read in the background once the run's process has spawned (never awaited). Without it an exec chat that never opened `/usage` or `/config` has no footer model or quota |
| Cost in dollars | native field, null on Pro | 0.171.0, 2026-11-23 (DEC-10) | `account/usage/read {threadId}.threadUsage` is `null` on a ChatGPT Pro login (three threads probed), so Untether shows no USD for Codex. Revisit if a plan reports it |
| Reasoning levels per model ([#846](https://github.com/littlebearapps/untether/issues/846)) | native: `model/list` + `config/read` (stable) | 0.171.0, 2026-11-23 | The Reasoning page lists the effective model's `supportedReasoningEfforts` with Codex's descriptions and default (`config.model_reasoning_effort` if the model lists it, else `defaultReasoningEffort`). The run-time drop of a level the model lacks is custom: Codex doesn't validate (unknown strings pass verbatim, `ultra` is clamped), and `drop_unsupported_reasoning` is pure, so it only peeks the cached catalogue (vocabulary when nothing is cached) |
| `ultra` | native level | 0.171.0, 2026-11-23 (DEC-26, probe T2) | Offered only where `model/list` lists it. T2: `-c model_reasoning_effort=ultra` (exec) and `turn/start.effort = "ultra"` (app-server) both accepted and ran (≈ 28k input tokens each, mostly cached, on a trivial prompt); no sub-agent started (`multiAgentMode: "explicitRequestOnly"` on the thread), so the sub-agent cost is still unmeasured. The page adds Untether's one note: Codex may start sub-agents |
| Footer model on exec ([#1360](https://github.com/littlebearapps/untether/issues/1360)) | custom (cached read or none) | 0.171.0, 2026-11-23 | `exec --json` never names the model (`thread.started` has only `thread_id`). The footer shows the chat's override, else the model Codex's own `config/read` resolves to for the run's cwd (else `model/list`'s `isDefault`), from the cache; a supplementary `started` event carries it when the warm-up lands mid-run. Unknown, or a `--profile` the read can't apply: no model shown, never a guess. App-server uses the `thread/start` echo and `model/rerouted` |
| Web search ([#423](https://github.com/littlebearapps/untether/issues/423)) | native: `web_search` config key | 0.171.0, 2026-11-23 (DEC-25) | Per-chat Codex default / Live / Off: exec `-c web_search="live"\|"disabled"` before `exec`; app-server `config.web_search` on `thread/start` and `thread/resume` (validated by Codex). `modelProvider/capabilities/read.webSearch` hides the setting; `configRequirements/read.allowedWebSearchModes` limits it. A resumed thread may keep its search provider (upstream #62144). `--ephemeral`, `--ignore-user-config`, `--ignore-rules` stay won't-do (DEC-12, re-verified on 0.171.0) |
| Model after `/model clear` on a resumed thread ([#1369](https://github.com/littlebearapps/untether/issues/1369)) | native: `config/read` + `thread/resume.model` (stable) | 0.170.0, 2026-11-22 | Probed: a thread started on `gpt-nonexistent` (its one turn failed `modelNotFound`, no tokens) resumed on `gpt-nonexistent` with no `model` and on the configured `gpt-6.1-sol` when `thread/resume` carried `config/read`'s `config.model`. With no model in Codex's config the thread keeps its saved model (`shortcut:`; `model/list`'s `isDefault` would cover it), so the `modelNotFound` hint also names `/new` |
| `% ctx`, compaction row | native | 0.170.0, 2026-11-19 (P8) | `thread/tokenUsage/updated` and the `contextCompaction` item (#832); no rollout-file reading (DEC-13) |
| Session compaction count | native (app-server); exec: none | 0.171.0, 2026-11-26 | the `contextCompaction` completion, counted by the bridge ([#1182](https://github.com/littlebearapps/untether/issues/1182)); exec has no compaction event, so no count |
| Steer into a running turn ([#964](https://github.com/littlebearapps/untether/issues/964)) | native `turn/steer` (stable), custom fallback | 0.171.0, 2026-11-26 (964-0 P-S1, P6) | Codex takes the text into the running turn (no new `turn/started`) and echoes it as a `userMessage` whose `clientId` is the `clientUserMessageId` Untether sent; that echo is the `↪️ steer received` row. Codex's refusals (no active turn, id mismatch, compact or review turn, output schema) fall back to Untether's queue. Custom: a steer sent but never answered (timeout, the turn ended first, the process exited) is reported steered and never queued again; if Codex never ran it, the run-end sweep says so. App-server only; a steer-only target on the per-run process until live sessions (964-3) |
| Turn state machine for live sessions ([#964](https://github.com/littlebearapps/untether/issues/964) phase 964-3a; off until 964-3b) | native `turn/start` / `turn/started` / `turn/completed` / `turn/interrupt` (stable), custom attribution | 0.171.0, 2026-11-26 (P-S1, P9) | Codex has no stable field linking a follow-up's `turn/start` to the `turn/started` it causes before the answer (P9 saw `turn/started` first), and on a busy thread `turn/start` steers instead (`start_or_steer_turn`). So Untether writes one follow-up at a time (registered before the write) and attributes the next `turn/started` to it only once its line is written; an earlier one is Codex's own, an answer naming a turn already running for another owner is an absorption, and an answer naming another turn than the attributed one re-labels it. A late answer to a start whose waiter was dropped is ignored. A sent but unanswered start is never written twice. `/cancel` interrupts the open turn by its id (waiting for a just-written follow-up's turn); an earlier turn's late `turn/completed` is ignored; approval cards belong to the turn they arrived in. The thread stays owned until the process has exited: stdin closed, then the exit grace, SIGTERM, a bounded wait, SIGKILL and a bounded reap; if the exit still can't be confirmed the thread stays owned (`codex.app_server.thread_kept`) |
| Live sessions never quarantine ([#964](https://github.com/littlebearapps/untether/issues/964) phase 964-3b) | custom (policy) | 0.171.0, 2026-11-27 (SRC thread processor) | Codex rollouts are durable, an interrupted turn too ("even an interrupted turn is durable now"), so a closed or dead live session is always resumable: every `closed` / `died` notice reports `quarantined: False`, and Claude's #631 quarantine is never used for Codex |
| Handoff wait before a resume ([#964](https://github.com/littlebearapps/untether/issues/964) phase 964-3b) | custom (known edge) | 0.171.0, 2026-11-27 | The bridge's #633 wait now applies to Codex: a resume waits until no process owns the thread (`_RUNS_BY_THREAD`, released only once the process has exited). It only waits after a close the process hasn't finished yet (e.g. an options change); on a timeout the message starts a fresh thread with the existing ⚠️ notice, and a process whose exit can't be confirmed (`thread_kept`) keeps its thread owned, so the resume goes fresh. Rare; logged as `session.handoff` |
| Trust re-checked before each live follow-up ([#964](https://github.com/littlebearapps/untether/issues/964) §16 item 6) | native `config/read` (stable), custom comparison | 0.171.0, 2026-11-27 | A live thread keeps the sandbox its spawn decided for up to 4 h, and Codex has no event for a trust or config change. For writable tiers Untether re-reads `config/read` before every follow-up or idle steer and compares the selected `[projects]` entry's `trust_level` (the #1408 lookup, repo root re-resolved), `sandbox_mode` and `model` with the spawn's. Any change, or no readable answer, closes the session (`trust_changed`, fail closed) and the message resumes through a fresh spawn that decides again. Read-only tiers never decided, so they aren't re-read |
| Follow-up whose turn never starts ([#964](https://github.com/littlebearapps/untether/issues/964) phase 964-3b) | custom (bound) | 0.171.0, 2026-11-27 | No native signal says a written `turn/start` will never open a turn (answer lost, or answered and no `turn/started`). After 120 s (`FOLLOWUP_TURN_TIMEOUT_S`, Claude's injected-turn bound) it stops counting as pending (`codex.live_session.followup_turn_timeout`), so the session can take the next follow-up and close when idle; a turn that starts later anyway is Codex's own (`unknown`) |
| `sleep` item (`clock.sleep`) | native item + rendering | 0.171.0, 2026-11-26 (964-0) | The model waiting on a running command or on the user; it carries `durationMs`. Shown as `⏳ sleeping …`; its deadline feeds the stall monitor's expected-wait check (`live_wakeups`), so a long sleep isn't reported as a stall |
| Waiting flags for the stall watchdogs ([#1412](https://github.com/littlebearapps/untether/issues/1412) item 1) | native `thread/status/changed` (stable) | 0.171.0, 2026-11-26 (probe 1412-1) | `active{activeFlags: [waitingOnApproval]}` arrives just before an approval request and `active[]` once it is answered; any thread's flags (sub-agents too) make `awaiting_user_approval()` true, so a long approval is an expected wait, not a stall. The card fallback stays. On a stable connection `request_user_input` isn't offered, so `waitingOnUserInput` wasn't seen (the model asked in text and waited in a `sleep` item). `systemError` gives one ⚠️ row |
| `/compact` on app-server ([#964](https://github.com/littlebearapps/untether/issues/964)) | native `thread/compact/start` (stable), custom interception | 0.171.0, 2026-11-26 (964-0 P-C) | Codex's `/compact` is a TUI command, so over app-server the text would reach the model; Untether intercepts a bare `/compact` (with text it's a prompt) and sends the native request on the resumed thread. No session: refused before spawn. The final names no token figures: a resume with `excludeTurns: true` replays no `thread/tokenUsage/updated` (probed; without it one arrives), and the compact turn's own `last` is the compaction request, not the context left. Exec is unchanged (text) |
