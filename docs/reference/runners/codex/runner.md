# Codex runner

Untether runs Codex over `codex exec --json` (the default; see [untether-events.md](untether-events.md) and
[exec-json-cheatsheet.md](exec-json-cheatsheet.md)). The `codex app-server` transport lands with
[#962](https://github.com/littlebearapps/untether/issues/962), which adds the per-install `[engines.codex] transport`
key (not added yet, so every run uses exec);
it counts as native despite the CLI's Experimental label (stable-tier methods only, `experimentalApi` fields only
for plan mode, #965). This page gains the transport, permission modes and the native-vs-custom register with #962.

| | |
|---|---|
| Tested against | codex-cli **0.170.0** (exec drift suite green on 2026-11-18, [#1254](https://github.com/littlebearapps/untether/issues/1254); exec flag snapshot `PROBED_CLI_VERSION` in `tests/test_codex_cli_schema_drift.py`; app-server schema `PROBED_APP_SERVER_VERSION` in `src/untether/schemas/codex_app_server.py`) |

## Project trust ([#1240](https://github.com/littlebearapps/untether/issues/1240))

codex-cli 0.169.0+ disables an untrusted project's `.codex/` layer (config, MCP servers, hooks, rules). Exec reports
it on **stderr only**, before the JSON stream (`warning: Untrusted project-local configuration is disabled…`, checked
on 0.170.0, 2026-11-18). `CodexRunner.on_stderr_line` matches that prefix, logs `codex.project_config_untrusted` once
and emits one `⚠️` warning row right after `StartedEvent` (or before the final event if stderr arrives late). Native
signal surfaced as-is; Untether never grants trust (no `--trust`, refused in `extra_args` by #1040). App-server:
re-check with #962 (`config/read` layers carry `disabled_reason`). Docs:
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
- **Fixture.** `tests/fixtures/codex_app_server/schema-subset-0.169.0.json` (codex-cli 0.169.0, 2026-11-18), written
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

## App-server cancel and open items (draft, [#962](https://github.com/littlebearapps/untether/issues/962))

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

### Native-vs-custom register (draft; finalised in 962-6)

| Piece | Kind | Checked | Why custom |
|---|---|---|---|
| Cancel | native: `turn/interrupt` (stable) | codex-cli 0.170.0, 2026-11-19 | SIGTERM only as the fallback when the turn doesn't end within 3 s |
| Closing items Codex leaves open after an interrupt or a failed turn | custom | codex-cli 0.170.0, 2026-11-18 (Δ D19) | no native signal: an interrupted `commandExecution` never gets `item/completed`. A failed turn's open rows are closed (`ok: false`, `failed` / `cancelled`) before the error final; on `/cancel` the bridge has stopped reading, so they're only counted in the log |
