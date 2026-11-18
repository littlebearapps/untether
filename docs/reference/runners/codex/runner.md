# Codex runner

Untether runs Codex over `codex exec --json` (the default; see [untether-events.md](untether-events.md) and
[exec-json-cheatsheet.md](exec-json-cheatsheet.md)). The `codex app-server` transport lands with
[#962](https://github.com/littlebearapps/untether/issues/962), which adds the per-install `[engines.codex] transport`
key (not added yet, so every run uses exec);
it counts as native despite the CLI's Experimental label (stable-tier methods only, `experimentalApi` fields only
for plan mode, #965). This page gains the transport, permission modes and the native-vs-custom register with #962.

| | |
|---|---|
| Tested against | codex-cli **0.169.0** (exec flag snapshot `PROBED_CLI_VERSION` in `tests/test_codex_cli_schema_drift.py`; app-server schema `PROBED_APP_SERVER_VERSION` in `src/untether/schemas/codex_app_server.py`) |

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
