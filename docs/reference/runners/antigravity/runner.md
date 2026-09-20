Below is the implementation spec for the **Antigravity CLI** (`agy`) runner shipped in Untether.

---

## Scope

### Goal

Provide the **`antigravity`** engine backend so Untether can:

* Run Antigravity non-interactively via the **Antigravity CLI** (`agy`).
* Stream progress by parsing **`--output-format stream-json`** (newline-delimited JSON). Each line is a JSON object with an `event` field.
* Support resumable sessions via **`--conversation <conversation_id>`** and cross-environment resume via **`--continue`**.
* Control permissions via **`--dangerously-skip-permissions`** (default for headless execution) and **`--mode <accept-edits|plan>`**.
* Control reasoning effort via **`--effort <low|medium|high>`**.

---

## UX and behaviour

### Engine selection

* Default: use `default_engine` from config
* Override: `/antigravity <prompt>` in Telegram

### Resume UX (canonical line)

Untether appends a **single backticked** resume line at the end of the message:

```text
`agy --conversation <conversation_id>`
```

Notes:

* The resume token is the **conversation ID** (UUID string, e.g., `fe2e5cfc-e5e4-4b67-8575-61ca39ba3ecb`), captured from the `init` event's `conversation_id` field.
* `agy --continue` is used for cross-environment resume of the most recent session.

### Non-interactive runs

The runner invokes:

```text
agy --output-format stream-json --dangerously-skip-permissions [--conversation <id>] [--model <model>] [--effort <low|medium|high>] [--mode <accept-edits|plan>] --prompt=<prompt>
```

Flags:

* `--output-format stream-json` — NDJSON stream output
* `--dangerously-skip-permissions` — passed by default for headless runs (auto-approves tool permission prompts)
* `--prompt=<value>` — prompt bound directly to flag (prevents flag injection when prompt starts with `-`)
* `--conversation <id>` — resumes conversation by ID
* `-c`, `--continue` — continues most recent conversation
* `--model <model>` — model selection
* `--effort <low|medium|high>` — reasoning effort
* `--mode <accept-edits|plan>` — execution mode

---

## Config additions

=== "untether config"

    ```sh
    untether config set default_engine "antigravity"
    untether config set antigravity.model "gemini-3.8-flash-high"
    ```

=== "toml"

    ```toml
    # ~/.untether/untether.toml

    default_engine = "antigravity"

    [antigravity]
    # cmd = "~/.local/bin/agy"             # optional; default ~/.local/bin/agy (or agy on PATH)
    model = "gemini-3.8-flash-high"        # optional; passed as --model
    dangerously_skip_permissions = true    # optional; default true
    ```

---

## Code structure

### `src/untether/runners/antigravity.py`

Exposes `BACKEND = EngineBackend(id="antigravity", cli_cmd=default_antigravity_cmd(), build_runner=build_runner, install_cmd="curl -fsSL https://antigravity.google/install.sh | bash")`.

#### Event translation

Antigravity NDJSON output uses an `event` discriminator field. The runner translates:

* `init` -> `StartedEvent` (captures conversation_id and model)
* `step_update` (step_type=tool) -> `ActionEvent` (phase: started / completed)
* `step_update` (step_type=agent_response) -> text accumulation for final answer
* `result` -> `CompletedEvent` (with usage from result payload)
* `error` -> `CompletedEvent` (ok=false)

---

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
