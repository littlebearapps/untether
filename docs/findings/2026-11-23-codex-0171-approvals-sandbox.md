---
question: >-
  For #963 (Codex approvals, diff preview, auto-review) and #421 (sandbox tiers) on the app-server transport,
  what does codex-cli 0.171.0 natively provide (server requests, decisions, notifications, sandbox and approval
  enums, trust behaviour, exec), and what changed since the 0.170.0 research the v0.36.2 plan was written against?
date: 2026-11-23
sources:
  - installed codex-cli 0.171.0 on lba-1 (`codex --version`; npm package installed 2026-11-23 04:04)
  - "`codex app-server generate-json-schema [--experimental]` and `generate-ts [--experimental]` → /tmp/cx963-{schema,schema-exp,ts,ts-exp} (2026-11-23), diffed against docs/plans/v0.36.2/codex/probes/schema-0.169.0/ and ts-diff-0.169.0-to-0.170.0.diff"
  - "`codex exec --help`, `codex app-server --help` (0.171.0) vs probes/cli-help-0.169.0.txt and cli-help-app-server-0.170.0.txt"
  - "zero-token probe: `initialize` + four `thread/start` calls (no turn) with a throwaway CODEX_HOME in a scratch git repo under /tmp, 2026-11-23"
  - "`uv run pytest tests/test_codex_app_server_drift.py tests/test_codex_cli_schema_drift.py` on 0.171.0: 48 passed (2026-11-23)"
  - https://github.com/openai/codex/releases/tag/rust-v0.171.0 (published 2026-11-20T00:44Z; accessed 2026-11-23)
  - openai/codex @ rust-v0.171.0 (gh api contents, accessed 2026-11-23) — codex-rs/app-server-protocol/src/protocol/v2/item.rs, codex-rs/app-server/src/transport.rs, codex-rs/app-server/src/bespoke_event_handling.rs, codex-rs/protocol/src/approvals.rs, codex-rs/app-server/src/request_processors/{thread_processor,turn_processor}.rs, codex-rs/exec/src/lib.rs, codex-rs/core/src/exec_policy.rs, codex-rs/tui/src/bottom_pane/approval_overlay.rs, codex-rs/app-server/README.md
  - https://developers.openai.com/codex/agent-approvals-security (firecrawl, maxAge 0, accessed 2026-11-23)
  - https://learn.chatgpt.com/docs/sandboxing/auto-review (firecrawl, maxAge 0, accessed 2026-11-23)
  - docs/plans/v0.36.2/codex/probes/doc-app-server-2026-11-17.md (app-server docs snapshot, §Approvals)
confidence: high (schema, enums, source-level semantics, docs); med (wire behaviour on a stable connection, which is read from source and not yet seen live — see "Owed live checks")
---

# Codex 0.171.0: approvals, auto-review and sandbox tiers on app-server

**Date:** 2026-11-23 · **Host:** lba-1 · **CLI:** codex-cli 0.171.0 · **Untether:** `dev` at `bfb565d0`
(0.36.2rc4) · **Baseline:** `docs/plans/v0.36.2/codex/RESEARCH-DELTA-0.169.md` (Δ Dn) and
`RESEARCH-DELTA-0.170.md` (E n).

No model tokens spent. Everything below is from the generated schema, `--help`, upstream source at the tag, official
docs, and `thread/start` calls that start no turn.

## Headline

1. **Nothing in 0.170 → 0.171 touches the approval surface.** Every approval-related type is byte-identical to
   0.169.0 in the stable bundle: the three approval server requests and their params/responses, both decision
   enums, `ServerRequestResolvedNotification`, `ThreadStatus`/`ThreadActiveFlag`, `AskForApproval`, `SandboxMode`,
   `ApprovalsReviewer`, `PermissionGrantScope`, the network/execpolicy amendment types, `FileUpdateChange`,
   `PatchChangeKind`, `turn/diff/updated`, `item/fileChange/patchUpdated`, `item/autoApprovalReview/*`,
   `guardianWarning`, `autoApprovalReview/strictReviewRequired`, `thread/approveGuardianDeniedAction` (stable).
   The #961 drift suite is green on 0.171.0.
2. **`availableDecisions` is an experimental field, but the server sends it on stable connections anyway.** The
   schema puts it only in the `--experimental` bundle (`#[experimental("item/commandExecution/requestApproval.availableDecisions")]`,
   item.rs L1749), yet `strip_experimental_fields()` (item.rs L1756, called from transport.rs L208 when
   `experimentalApi` is off) only clears `additionalPermissions`. `bespoke_event_handling.rs` always fills it with
   `effective_available_decisions()`. **Docs vs schema disagree:** the docs list it as a plain optional field; the
   schema marks it experimental. Per the plan's rule (prefer the schema), treat it as an optional hint and keep a
   stable-only fallback that reproduces Codex's own default set (below).
3. **Codex's own default decision set (approvals.rs L379–L411)**, used whenever `availableDecisions` is absent:
   - network prompt (`networkApprovalContext` set): `accept`, `acceptForSession`, `applyNetworkPolicyAmendment`
     (first `allow` amendment, if any), `cancel`;
   - extra permissions on the command (experimental field): `accept`, `cancel`;
   - plain command: `accept`, `acceptWithExecpolicyAmendment` (only when `proposedExecpolicyAmendment` is set),
     `cancel`.
   **`decline` and `acceptForSession` are not in the plain-command default set**, though the decision enum accepts
   both. File changes have no `availableDecisions`; OpenAI's TUI always offers `accept` / `acceptForSession` /
   `cancel` (approval_overlay.rs `patch_options`).
4. **`decline` vs `cancel` (TUI labels, approval_overlay.rs):** `decline` = "No, continue without running it" (the
   turn goes on); `cancel` = "No, and tell Codex what to do differently" (the turn stops; the user's next message is
   the feedback). That is Codex's native "deny with feedback". `acceptForSession` = "don't ask again for this command
   in this session" (commands), "for these files" (patches), "allow this host for this conversation" (network).
5. **`acceptWithExecpolicyAmendment` persists host-wide.** It appends the prefix rule to
   `$CODEX_HOME/rules/default.rules` (exec_policy.rs L63, L965), so it applies to every later Codex session on the
   host, the operator's own terminal included. `applyNetworkPolicyAmendment` with `allow` likewise saves a host rule.
6. **Trust is persisted by `thread/start` only.** thread_processor.rs L1515–L1575: trust is written when the request
   carries `cwd`, the project has no `trust_level`, the config layer stack isn't projectless, and the effective
   permissions can write the cwd. `thread/resume` and `turn/start` have no trust code (one `set_project_trust_level`
   call in the whole app-server, none in turn_processor.rs). README 0.171 adds: no trust is persisted where discovery
   finds no project-root marker, Git checkout or project `.codex` dir (narrows P1's non-git finding on 0.170).
   So a writable sandbox sent per turn (`turn/start.sandboxPolicy`, stable, "this turn and subsequent turns") after a
   read-only `thread/start` should not persist trust — **source reading only, owed a live check (P-C)**.
7. **Approval-policy values on a stable connection (probe, 0.171.0, no turn):**
   `untrusted` is still accepted and echoed, but the docs say it is **retired** ("Codex and ChatGPT Work no longer
   support `approval_policy = "untrusted"` … can prevent either client from starting"), so never send it.
   `granular` → `-32600 askForApproval.granular requires experimentalApi capability` (experimental: out of scope by
   DEC-1). `approvalsReviewer: "guardian_subagent"` echoes as `auto_review` (alias). `on-request` + `read-only` +
   `auto_review` echoes as sent.
8. **Auto-review (docs):** applies only with interactive approvals (`on-request`); with `never` there is nothing to
   review. On a denial Codex tells the agent to find a materially safer path or stop and ask. The CLI interrupts the
   turn after 3 consecutive denials or 10 of the last 50 (first denial for cyber models); with
   `auto_review.circuit_break_action = "strict"` the turn error carries `codexErrorInfo: "tooManyDenials"` (README
   0.171). User override = the TUI's `/approve`, i.e. `thread/approveGuardianDeniedAction {threadId, event}` with a
   serialised `GuardianAssessmentEvent` (stable method, `[UNSTABLE]` notification payload). Extra model calls → extra
   Codex usage.
9. **`codex exec` still rejects every approval request** (exec/src/lib.rs L2313–L2336: "command execution approval is
   not supported in exec mode") and forces `approval_policy = never` unless the resolved reviewer is `auto_review`
   (L638–L642, L856–L880). 0.171 moved exec onto the shared app-server client (#61706) without changing that.
10. **No native deadline** for command / file / permission approvals: a request stays pending until answered or until
    turn start/completion/interrupt clears it (then `serverRequest/resolved` is sent for the cleanup too). Only
    `requestUserInput` has `autoResolutionMs` (deprecated in favour of `isBlocking`).

## Delta 0.170.0 → 0.171.0 (what matters)

| # | Area | Kind | Change | Evidence | Affects |
|---|---|---|---|---|---|
| F1 | Approval surface | unchanged | All approval types, enums and notifications identical (headline 1) | TS diff vs 0.169 minus the known E2 delta | none: the plan's protocol design stands |
| F2 | `currentTime/read` | removed (E server request) | The one experimental server request is gone ("Make clock reads synchronous and remove client clock requests", #61382) | experimental `ServerRequest.ts` diff | none (Untether never handled it; the peer errors on unknown requests anyway) |
| F3 | `CodexErrorInfo` | new value (S) | `bioPolicy` (#62176) | `v2/CodexErrorInfo.ts` | error hint table: unknown codes already fall back; add a hint row when convenient |
| F4 | `modelProvider/default/set` | new (S client request) | sets the default model provider | `ClientRequest.ts` | none |
| F5 | `thread/search/partial` | new (S notification) | streaming search results | `ServerNotification.ts` | none (ignored notification) |
| F6 | `environmentId` | new fields (S) | on `ThreadItem` (mcpToolCall), MCP status/elicitation params | TS diffs | none (lenient decode) |
| F7 | `Project.position` | removed (S) | project list item | `v2/Project.ts` | none |
| F8 | `ThreadListParams.includedThreadIds` | new (S) | filter `thread/list` by id | `v2/ThreadListParams.ts` | none (maybe #964) |
| F9 | `AccountUpdatedNotification.reason` | new (S) | | | none |
| F10 | `codex exec --remote <ADDR>`, `--remote-auth-token-env` | **new on exec** | "start or resume noninteractive sessions on an existing app server" (#61707). Classified `root-only` in `tests/test_codex_cli_schema_drift.py` and **not** in `_CODEX_BLOCKED`, so `extra_args = ["--remote", …]` would now point an exec run at another app server (a shared daemon or another host), bypassing the per-run process (DEC-4) and the #1241 env filter | `codex exec --help` 0.171.0 | **new issue proposed** (not #963): block both flags in `extra_args`, update the #209 snapshot classification |
| F11 | Exec internals | changed | exec now uses the shared app-server client (#61706) | release notes | re-run Tier 1 on the exec transport at the rc5 cut (behaviour change risk, no protocol change seen) |
| F12 | Late approval cancellation | fix | "Prevented late completion events from cancelling approvals for a newer turn" (#61801); "Close late command approval items when turns are interrupted" (#61841) | release notes | #963 late-tap handling; re-check whether Δ D19 item closing still needs Untether's custom close for approval items |
| F13 | Project trust | narrowed | no trust persisted in dirs with no project marker / git / `.codex` | README 0.171 §Project trust | DEC-7 unchanged; P1's non-git result is now obsolete |
| F14 | Permission profiles | internal | "Preserve permission profiles across working directory updates" (#61195), legacy sandbox accessors removed (#61395) | release notes | none on the stable `sandbox` field; `activePermissionProfile` stays experimental-only |
| F15 | Guardian v3 | internal | housekeeping, evidence retrieval, per-agent reviewer conversations, nested-approval context (#61795–#62136, #61946) | release notes | auto-review payload is still `[UNSTABLE]`: render loosely |

## Native capability table (for the plan)

| Need | Native (tier) | Notes |
|---|---|---|
| Command approval | `item/commandExecution/requestApproval` (S) → `{decision}` | `kind: command \| writeStdin \| humanCommand`; `approvalId` disambiguates sub-callbacks; `networkApprovalContext {host, protocol}` = network prompt (render the host, not `command`) |
| File-change approval | `item/fileChange/requestApproval` (S) → `{decision}` | carries no diff; the `fileChange` item from `item/started` (and `item/fileChange/patchUpdated`) has `changes[{path, kind:{type}, diff}]` |
| Extra-permission request | `item/permissions/requestApproval` (S) → `{permissions: <granted subset>, scope: turn\|session, strictAutoReview?}` | deny = grant nothing; TUI offers turn / turn + strict review / session / deny |
| MCP elicitation | `mcpServer/elicitation/request` (S) | out of scope (#1243): keep declining |
| Request withdrawn / answered | `serverRequest/resolved {threadId, requestId}` (S) | also sent after Untether's own answer |
| Waiting on a human | `thread/status/changed {activeFlags}` (S) | `waitingOnApproval` / `waitingOnUserInput` |
| Turn diff | `turn/diff/updated {diff}` (S) | aggregated unified diff |
| Approval policy | `approvalPolicy` on `thread/start` / `thread/resume` / `turn/start` (S): `never`, `on-request` | `untrusted` retired (docs); `granular` experimental |
| Sandbox | `sandbox` on `thread/start`/`resume` (S, `SandboxMode` kebab-case); `sandboxPolicy` on `turn/start` (S, object) | writable `sandbox` on `thread/start` in an unlisted project persists trust |
| Reviewer | `approvalsReviewer` (S): `user`, `auto_review` (`guardian_subagent` = alias) | config key `approvals_reviewer`; `auto_review` needs `on-request` |
| Auto-review outcome | `item/autoApprovalReview/started|completed` (S method, `[UNSTABLE]` payload: `review{status, riskLevel, userAuthorization, rationale}`, `action{type,…}`), `guardianWarning {message}` (S), `autoApprovalReview/strictReviewRequired` (S) | |
| Override a denial | `thread/approveGuardianDeniedAction {threadId, event}` (S) | `event` = serialised `GuardianAssessmentEvent` (unstable shape) → #1245 |
| Circuit breaker | turn interrupted + warning; `codexErrorInfo: "tooManyDenials"` only with `auto_review.circuit_break_action = "strict"` | |

## Owed live checks (token-spending; need Nathan's go)

Each is one tiny turn on the stable connection (`gpt-6.1-sol` or the installed default, effort `low`) in a scratch
git repo under `/tmp` with a throwaway project entry; `~/.codex/config.toml` and `~/.codex/rules/` backed up and
restored. P8 (0.170.0) measured ≈13 k tokens for a one-line turn, mostly cached prompt.

| # | Check | Turn | Est. cost |
|---|---|---|---|
| P-A | Command approval on a stable connection: is `availableDecisions` present, which values, `networkApprovalContext` shape; answer `decline` (unlisted) → item `declined` and the turn continues; second request answered `cancel` → turn `interrupted` | `on-request` + `workspace-write` (trusted scratch project), prompt "run `curl -sI https://example.com`, then `git status`" | ≈ 20–30 k |
| P-B | File change: `item/started` → `patchUpdated`? → request; `acceptForSession` then a second write to the same file doesn't prompt | `on-request` + `read-only`, "create notes.txt containing hello, then append world" | ≈ 20 k |
| P-C | Untrusted project: `thread/start {sandbox:"read-only"}` then `turn/start {sandboxPolicy:{type:"workspaceWrite"}}` → no `trust_level` written, the write succeeds | same prompt as P-B, `never` | ≈ 15 k |
| P-D | Auto-review: `item/autoApprovalReview/*` payload on a stable connection, no server request reaches the client, denial row text, extra calls | `on-request` + `workspace-write` + `auto_review`, "delete /tmp/cx-probe-outside (outside the project)" | ≈ 30–50 k (reviewer calls) |
| P-E | Sub-agent approval: request `threadId` = child; child `item/started` (fileChange) arrives on the same connection | `on-request` + `read-only`, "spawn a sub-agent to create child.txt" | ≈ 40–60 k |
| P-F | Does `acceptForSession` survive a new process (`thread/resume` in a fresh `codex app-server`)? (Δ D21 says network approvals are restored on resume) | P-A's thread resumed in a new process, same command | ≈ 15 k |

Total ≈ 140–200 k tokens of Codex quota (Pro: quota, not dollars). P-A, P-B and P-C are the ones the design depends
on; P-D…P-F can ride on the dev-bot integration run instead.
