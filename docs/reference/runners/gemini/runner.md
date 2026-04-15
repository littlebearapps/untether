Below is the implementation spec for the **Gemini CLI** runner shipped in Untether.

---

## Scope

### Goal

Provide the **`gemini`** engine backend so Untether can:

* Run Gemini non-interactively via the **Gemini CLI** (`gemini`).
* Stream progress by parsing **`--output-format stream-json`** (newline-delimited JSON). Each line is a JSON object with a `type` field.
* Support resumable sessions via **`--resume <session_id>`**.

### Non-goals (v1)

* Plan mode interaction — Gemini supports `enter_plan_mode`/`exit_plan_mode` tools but these require interactive stdin.

---

## UX and behavior

### Engine selection

* Default: use `default_engine` from config
* Override: `/gemini <prompt>` in Telegram

### Resume UX (canonical line)

Untether appends a **single backticked** resume line at the end of the message:

```text
`gemini --resume abc123def`
```

Notes:

* The resume token is the **session id** (short alphanumeric string, e.g., `abc123def`), captured from the `init` event's `session_id` field.
* `--resume latest` is also valid in the CLI but Untether always uses explicit session IDs.

### Non-interactive runs

The runner invokes:

```text
gemini -p --output-format stream-json --model <model> --prompt=<prompt>
```

Flags:

* `-p` — non-interactive (print mode)
* `--output-format stream-json` — JSONL output
* `--model <model>` — optional, from config or `/config` override
* `--prompt=<value>` — prompt bound directly to flag (prevents injection when prompt starts with `-`)
* `--resume <session_id>` — when resuming a session
* `--approval-mode <mode>` — defaults to `yolo` (full access) when no override is set; configurable via `/config` or `permission_mode` run option

---

## Config additions

=== "untether config"

    ```sh
    untether config set default_engine "gemini"
    untether config set gemini.model "gemini-2.5-pro"
    ```

=== "toml"

    ```toml
    # ~/.untether/untether.toml

    default_engine = "gemini"

    [gemini]
    model = "gemini-2.5-pro"   # optional; passed as --model
    ```

Notes:

* Gemini auto-routes between Pro (planning) and Flash (implementation) when no model is specified.
* Authentication is handled by the Gemini CLI itself (Google AI Studio or Vertex AI).

---

## Code changes (by file)

### `src/untether/runners/gemini.py`

Exposes `BACKEND = EngineBackend(id="gemini", build_runner=build_runner, install_cmd="npm install -g @google/gemini-cli")`.

#### Runner invocation

```text
gemini -p --output-format stream-json [--resume <session_id>] [--model <model>] [--approval-mode <mode>] --prompt=<prompt>
```

#### Event translation

Gemini JSONL output uses a `type` discriminator field. The runner translates:

* `init` -> `StartedEvent` (captures session_id and model)
* `tool_use` -> `ActionEvent` (phase: started)
* `tool_result` -> `ActionEvent` (phase: completed)
* `message` (role=assistant) -> text accumulation for final answer
* `result` -> `CompletedEvent` (with usage from `stats`)
* `error` -> `CompletedEvent` (ok=false)

#### Tool name normalisation

Gemini uses snake_case tool names. The runner normalises them via `_TOOL_NAME_MAP`:

| Gemini tool | Normalised |
|---|---|
| `read_file` | `read` |
| `edit_file` | `edit` |
| `write_file` | `write` |
| `web_search` | `websearch` |
| `web_fetch` | `webfetch` |
| `list_dir` | `ls` |
| `find_files` | `glob` |
| `search_files` | `grep` |

---

## Installation and auth

Install the CLI globally:

```text
npm install -g @google/gemini-cli
```

Run `gemini` once interactively to authenticate with Google AI Studio or Vertex AI.

---

## Known pitfalls

* Gemini has no `--stream-json-input` mode, so interactive features (approve/deny, plan mode toggle) are not possible in headless mode.
* `--approval-mode` controls tool access in headless mode. Untether defaults to `yolo` (full access — all tools auto-approved) when no override is set, since headless mode has no interactive approval path. Without this default, Gemini's CLI read-only mode disables write tools (`run_shell_command`, `write_file`, `edit_file`), causing most tasks to stall as the agent cascades through sub-agents. Users can restrict via `/config` → Approval mode: edit files (`auto_edit`, blocks shell but allows file operations) or read-only (denies most tool calls).
* Tool names are snake_case (e.g., `read_file`) unlike Claude Code's PascalCase — the runner normalises these.

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
