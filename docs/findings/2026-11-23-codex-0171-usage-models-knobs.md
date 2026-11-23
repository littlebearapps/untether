---
question: >-
  On codex-cli 0.171.0, what does Codex ship natively for subscription usage (#966), the per-model reasoning
  catalogue and default (#846, #1360) and the web search knob (#423), how do those reads behave when logged out,
  and does DEC-12 (web search only; --ephemeral / --ignore-* won't-do) still hold?
date: 2026-11-23
sources:
  - "installed codex-cli 0.171.0 on lba-1: `codex app-server generate-json-schema` / `generate-ts` (stable + experimental), `--help` of codex, exec, app-server, debug, debug models"
  - "zero-token app-server probes on lba-1, 2026-11-23: docs/plans/v0.36.2/codex/probes/live-zero-token-{reads,loggedout,thread-usage}-0.171.0.jsonl"
  - "zero-token exec probe (logged-out CODEX_HOME): docs/plans/v0.36.2/codex/probes/exec-remote-0.171.0.txt"
  - https://github.com/openai/codex/releases/tag/rust-v0.171.0 (retrieved 2026-11-23)
  - "openai/codex PRs #61706, #61707, #61841, #62066, #62144, #62145, #61801, #61195, #61934, #61382 (retrieved 2026-11-23; probes/upstream-prs-0.171.0.txt)"
  - "openai/codex codex-rs/protocol/src/openai_models/reasoning_effort.rs at rust-v0.171.0 (retrieved 2026-11-23)"
  - https://developers.openai.com/codex/app-server (serves learn.chatgpt.com/docs/app-server; retrieved 2026-11-23, maxAge 0)
  - https://learn.chatgpt.com/docs/config-file/config-reference (retrieved 2026-11-23, maxAge 0)
confidence: high (schema, probes, release notes); med (exec footer push behaviour and `ultra` runtime cost, both need a token-spending turn)
---

# Codex 0.171.0: usage, model catalogue and knobs (#966, #846, #423)

**Date:** 2026-11-23 · **CLI:** codex-cli 0.171.0 (tag `rust-v0.171.0`, published 2026-11-20) · **Host:** lba-1 ·
**Login:** ChatGPT Pro · **Untether:** `dev` at `bfb565d0` (0.36.2rc4). No model tokens were spent. The protocol delta
for the whole surface is in `docs/plans/v0.36.2/codex/RESEARCH-DELTA-0.171.md`.

## Answers

### 1. Subscription usage (#966): fully native, stable tier

- `account/rateLimits/read` (stable) returned in 0.49 s (0.25 s with `excludeResetCreditDetails: true`) on Pro:
  `ordinaryUsageAllowed: true`; one bucket `codex` with `primary {usedPercent: 8, windowDurationMins: 10080, resetsAt}`
  (weekly), `secondary: null`; `credits {hasCredits: true, unlimited: false, balance: "62500.0000000000"}`;
  `planType: "pro"`; `rateLimitResetCredits {availableCount: 3, credits: [3 rows "Full reset"]}`;
  `rateLimitUpsell: null` [probe reads].
- Schema notes that matter for the UX [SCH]:
  - `ordinaryUsageAllowed`: "Backend permission for ordinary included usage … Null means unavailable; **clients must
    not infer recovery from percentages or reset times**." So "blocked / allowed" comes from this field, not from
    `usedPercent >= 100`.
  - `account/rateLimits/updated` is a "Sparse rolling rate-limit update. Clients should merge available values into
    the most recent `account/rateLimits/read` response or refetch … Nullable account metadata … does not clear a
    previously observed value."
  - `RateLimitResetCreditsSummary.availableCount` is authoritative; `credits` can be capped or `null` (docs agree).
  - `PlanType` has 20 values (`free` … `pro`, `prolite`, `promax`, `team`, `business`, `enterprise`, `edu`, …,
    `unknown`): render it as text, never switch on it.
- Docs agree with the schema and add: "Fetch `account/rateLimits/read` after consuming a reset instead of inferring
  updated windows" [DOC]. Untether never consumes a reset (display only).
- **Logged out** (empty `CODEX_HOME`): `account/read` → `{account: null, requiresOpenaiAuth: true}`;
  `account/rateLimits/read` → JSON-RPC `-32600 "codex account authentication required to read rate limits"`;
  `account/usage/read` → `-32600 "… to read token usage"` [probe logged-out].
- **API key**: docs say `account/usage/read` doesn't work for "API-key-only and Bedrock auth" [DOC]; the rate-limit
  read on an API key login is unverified (owed, zero tokens, needs an API key in a scratch `CODEX_HOME`).
- **USD (DEC-10):** `account/usage/read {threadId}` on three recent Untether app-server threads (each had a prompt)
  returned `threadUsage: null` on Pro [probe thread-usage]. Native dollar cost isn't available on this plan, so
  Untether shows no USD for Codex. `account/usage/read` without a thread id gives lifetime/streak totals and daily
  token buckets (not planned for `/usage`).
- `codex exec --json` has no rate-limit event (exec schema unchanged since 0.170; `thread.started` carries only
  `thread_id`), so exec runs can't refresh the footer from their own stream.

### 2. Reasoning catalogue and default (#846, #1360): native, stable tier

- `model/list` (stable, 11 ms after `initialize`) on Pro lists 10 visible models, plus `gpt-reserve` and
  `codex-auto-review` with `includeHidden: true` [probe reads]:

  | Model | `isDefault` | default effort | efforts |
  |---|---|---|---|
  | `gpt-6.2-sol` (new in 0.171.0, RN #62106) | **true** | low | low, medium, high, xhigh, max, ultra |
  | `gpt-6.2-astra` (new) | | low | low … ultra |
  | `gpt-6.1-terra`, `gpt-6.1-sol`, `gpt-5.6-sol` | | low | low … ultra |
  | `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-terra` | | medium | low … ultra |
  | `gpt-6-luna`, `gpt-5.6-luna` | | medium | low … max (no `ultra`) |

- The catalogue is account-specific: the logged-out list has 11 models including `gpt-5.5` and no hidden ones, so a
  cache key needs the account (auth mode + plan) as well as the CLI version.
- Each effort carries Codex's own description. `ultra` = "Maximum reasoning with automatic task delegation"; `max` =
  "Maximum reasoning depth for the hardest problems" [probe reads]. In source, `ultra` resolves to the model's
  `multi_agent_reasoning_effort` (if supported), else `max`, else the highest non-ultra level; unknown strings pass
  through verbatim [SRC `reasoning_effort.rs`]. So `ultra` means sub-agents, more quota and time.
- `config/read {cwd}` (stable) returns `config.model` (`gpt-6.1-sol`, Nathan's `~/.codex/config.toml`) and
  `config.model_reasoning_effort` (`low`); `thread/start` echoes `model` and `reasoningEffort` [probe reads]. With no
  `config.model`, Codex uses the catalogue's `isDefault` model, which is now `gpt-6.2-sol`, not `gpt-6.1-sol`.
- Config reference: `model_reasoning_effort` = "Reasoning effort advertised by the selected model, such as `low`,
  `medium`, `high`, `xhigh`, `max`, or `ultra`. Available levels depend on the model and client."
  `plan_mode_reasoning_effort` exists (for #965) [CFG]. `ReasoningEffort` is a free `string` in the schema.
- `codex debug models` dumps the same catalogue as JSON (debug tooling, not a documented contract; used only by the
  existing drift test with `--bundled`).
- `model/rerouted {fromModel, toModel, reason: "highRiskCyberActivity"}` (stable notification) means the model that
  ran can differ from the one asked for. Rare; a footer fed from `thread/start` alone can be wrong after a reroute.

### 3. Web search (#423): native config key, validated on app-server

- Config reference: `web_search = disabled | cached | indexed | live`, **default `"cached"`** ("an OpenAI-maintained
  index without external web access"); with `--yolo` or another full-access sandbox it defaults to `"live"`;
  `"disabled"` removes the tool [CFG]. So "unset" isn't "off".
- `thread/start.config.web_search` is validated: `"disabled"` accepted; `"bogus"` → `-32600 "failed to load
  configuration: unknown variant 'bogus', expected one of 'disabled', 'cached', 'indexed', 'live' in 'web_search'"`
  [probe reads]. `thread/resume` takes the same free-form `config`.
- `modelProvider/capabilities/read` (stable) → `{imageGeneration: true, webSearch: true}`; `configRequirements/read`
  (stable) → `requirements.allowedWebSearchModes` when managed policy restricts it (`requirements: null` on lba-1).
- `codex exec` has no `--search` of its own (root `--search` = live); `-c web_search="…"` before `exec` works on both
  transports' argv shapes.
- 0.171.0 pins a thread's search provider (Search MCP vs native) across resumes (#62144), so switching the toggle on
  a resumed thread may not change which search tool that thread uses.

### 4. DEC-12 on 0.171.0: still holds

- `--ephemeral`, `--ignore-user-config` ("auth still uses `CODEX_HOME`"), `--ignore-rules` are still exec-only;
  `codex app-server --help` has none of them [HELP]. App-server's `thread/start.ephemeral` gives `path: null` (no
  rollout), so resume and `/continue` can't work on such a thread [probe reads].

### 5. New in 0.171.0 that changes Untether's flag guard: `codex exec --remote`

- "Added `codex exec --remote <ADDR>` to start or resume noninteractive sessions on an existing app server" (RN
  #61707). The PR: "Accept remote options before or after `exec`, and reject conflicting endpoints. Reject
  unsupported agent configuration overrides and active threads."
- Probed (logged-out `CODEX_HOME`, zero tokens): `codex --remote ws://127.0.0.1:9 exec --json …` and the
  `exec --remote` form both try to connect (`failed to connect to remote app server …`); adding `--sandbox read-only`,
  `-c …` or `--model` fails with `--remote currently supports only prompt, resume by UUID, and output options;
  configure the agent on the server` [probe exec-remote].
- Untether's #209 snapshot classifies `--remote` / `--remote-auth-token-env` as `root-only` and doesn't block them, so
  on 0.171.0 an `extra_args` entry would send exec runs to another app server with that server's config, sandbox and
  credentials (only when no Untether `-c` / sandbox / model flag is present; the default `-c notify=[]` already makes
  it fail). The flag snapshot test stays green because the flag name was already classified.

## Method

1. `codex app-server generate-json-schema [--experimental]` and `generate-ts [--experimental]` into `/tmp`; method
   lists extracted from the JSON bundles; TS diffed against `probes/schema-0.169.0/` (0.169→0.170 was already
   recorded as identical lists plus E2/E7).
2. One stdio `codex app-server` in an empty `/tmp` cwd: `initialize` (stable), `account/read`,
   `account/rateLimits/read` (with and without `excludeResetCreditDetails`), `model/list` (with and without
   `includeHidden`), `config/read`, `configRequirements/read`, `modelProvider/capabilities/read`, `account/usage/read`,
   two ephemeral read-only `thread/start`s (no turn). Same reads with an empty `CODEX_HOME`. Third run: `thread/list`
   then `account/usage/read {threadId}` ×3. Scripts: `probes/probe171*.py`; transcripts sanitised with
   `probes/sanitise.py` and trimmed (config reduced to the relevant keys, home path replaced).
3. `uv run pytest tests/test_codex_cli_schema_drift.py tests/test_codex_app_server_drift.py`: 48 passed on 0.171.0.

## Owed (token-spending, need Nathan's go)

See the plan's "Owed probes" section: `ultra` on both transports (T2), `account/rateLimits/updated` shape after a
0.171.0 turn (T1, optional), and the API-key read (T6, zero tokens but needs a key).
