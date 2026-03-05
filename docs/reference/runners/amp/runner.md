Below is the implementation spec for the **AMP CLI (Sourcegraph)** runner shipped in Untether.

---

## Scope

### Goal

Provide the **`amp`** engine backend so Untether can:

* Run AMP non-interactively via the **AMP CLI** (`amp`).
* Stream progress by parsing **`--stream-json`** output. AMP uses a Claude Code-compatible JSONL protocol.
* Support resumable sessions via **`amp threads continue <thread-id>`**.

### Non-goals (v1)

* Interactive features via `--stream-json-input` — AMP supports stdin streaming for multi-turn conversations but this is not yet wired.
* Subagent tracking — `parent_tool_use_id` is present in the schema but not used for nested progress.
* Thread management commands — `amp threads list/search/share` etc. are not exposed via Telegram.

---

## UX and behavior

### Engine selection

* Default: use `default_engine` from config
* Override: `/amp <prompt>` in Telegram

### Resume UX (canonical line)

Untether appends a **single backticked** resume line at the end of the message:

```text
`amp threads continue T-2775dc92-90ed-4f85-8b73-8f9766029e83`
```

Notes:

* The resume token is the **thread ID** (format: `T-<uuid>`), captured from the `system(init)` event's `session_id` field.
* AMP calls sessions "threads" — `amp threads continue` resumes them.

### Non-interactive runs

The runner invokes:

```text
amp --dangerously-allow-all --mode <mode> --model <model> -x --stream-json <prompt>
```

Flags:

* `--dangerously-allow-all` — auto-approve all tool calls (default, configurable)
* `--mode <mode>` — optional (`deep|free|rush|smart`)
* `--model <model>` — optional, from config or `/config` override
* `-x` — execute mode (non-interactive)
* `--stream-json` — JSONL output

For resumed sessions:

```text
amp threads continue <thread-id> --dangerously-allow-all -x --stream-json <prompt>
```

---

## Config additions

=== "untether config"

    ```sh
    untether config set default_engine "amp"
    untether config set amp.model "claude-sonnet-4-6"
    untether config set amp.mode "smart"
    untether config set amp.dangerously_allow_all true
    ```

=== "toml"

    ```toml
    # ~/.untether/untether.toml

    default_engine = "amp"

    [amp]
    model = "claude-sonnet-4-6"       # optional; passed as --model
    mode = "smart"                     # optional; deep|free|rush|smart
    dangerously_allow_all = true       # default: true
    ```

Notes:

* `mode` controls model selection, system prompt, and tool availability within AMP.
* `dangerously_allow_all` defaults to `true` since Untether runs headless.

---

## Code changes (by file)

### `src/untether/runners/amp.py`

Exposes `BACKEND = EngineBackend(id="amp", build_runner=build_runner, install_cmd="npm install -g @sourcegraph/amp")`.

#### Runner invocation

```text
amp [threads continue <thread-id>] --dangerously-allow-all [--mode <mode>] [--model <model>] -x --stream-json <prompt>
```

#### Event translation

AMP uses a Claude Code-compatible JSONL protocol with a `type` discriminator. The runner translates:

* `system(subtype="init")` -> `StartedEvent` (captures session_id)
* `assistant` (tool_use blocks) -> `ActionEvent` (phase: started)
* `user` (tool_result blocks) -> `ActionEvent` (phase: completed)
* `assistant` (text blocks) -> text accumulation for final answer
* `result` -> `CompletedEvent` (with accumulated usage)

#### Usage accumulation

Unlike Gemini (which reports usage once in `result.stats`), AMP reports per-message `usage` in assistant messages. The runner accumulates `input_tokens` and `output_tokens` across all assistant messages and builds the final usage dict at completion.

---

## Installation and auth

Install the CLI globally:

```text
npm install -g @sourcegraph/amp
```

Run `amp login` to authenticate with Sourcegraph.

---

## Known pitfalls

* AMP uses `amp threads continue <thread-id>` for resume, not `--resume`.
* Thread IDs use the format `T-<uuid>` (e.g., `T-2775dc92-90ed-4f85-8b73-8f9766029e83`).
* `--stream-json-input` exists but is not yet wired — this could enable interactive features in the future.
* `parent_tool_use_id` in assistant/user messages tracks subagent nesting but is not used by the runner.
* AMP's `--model` flag may have no effect when using hosted models (model is controlled server-side by `--mode`).
