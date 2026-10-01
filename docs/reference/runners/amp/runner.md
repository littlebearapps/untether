!!! warning "Deprecated"

    The `amp` engine is **deprecated in v0.35.5**: it still loads but is
    unsupported, and removal is targeted for **0.36.0**. The integration is
    unmaintained, and AMP remotely refuses out-of-date clients with `426` and
    exits 1. The AMP-only `/threads` command is deprecated with it
    ([#458](https://github.com/littlebearapps/untether/issues/458)).

Below is the implementation spec for the **AMP CLI (Sourcegraph)** runner shipped in Untether.

---

## Scope

### Goal

Provide the **`amp`** engine backend so Untether can:

* Run AMP non-interactively via the **AMP CLI** (`amp`).
* Stream progress by parsing **`--stream-json`** output. AMP uses a Claude Code-compatible JSONL protocol.
* Support resumable sessions via **`amp threads continue <thread-id>`**.

### Non-goals (v1)

* Full thread management — only the deprecated AMP-only `/threads` command (list, search, view, resume, archive) is exposed via Telegram; `share` and the rest are not.

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
amp [--dangerously-allow-all] [--mode <mode>] --stream-json [--stream-json-input] -x <prompt>
```

Flags:

* `--dangerously-allow-all` — auto-approve all of AMP's tool calls. **Default flipped to `false` in v0.35.3** ([#206](https://github.com/littlebearapps/untether/issues/206)); set `[amp] dangerously_allow_all = true` to enable.
* `--mode <mode>` — optional (`deep|free|rush|smart`), from `[amp] mode`; a `/config` model override is passed as `--mode`
* No `--model` is passed: `[amp] model` only labels the `🏷` footer when no mode is set
* `-x` — execute mode (non-interactive)
* `--stream-json` — JSONL output
* `--stream-json-input` — optional; enables stdin streaming (preliminary support, configurable)

Prompts starting with `-` are space-prefixed via `sanitize_prompt()` (base runner method) to prevent the CLI from interpreting the prompt as a flag.

For resumed sessions:

```text
amp threads continue <thread-id> [--dangerously-allow-all] [--mode <mode>] --stream-json [--stream-json-input] -x <prompt>
```

---

## Config additions

=== "untether config"

    ```sh
    untether config set default_engine "amp"
    untether config set amp.model "claude-sonnet-4-6"
    untether config set amp.mode "smart"
    untether config set amp.dangerously_allow_all false
    ```

=== "toml"

    ```toml
    # ~/.untether/untether.toml

    default_engine = "amp"

    [amp]
    model = "claude-sonnet-4-6"       # optional; footer label only (not passed to amp)
    mode = "smart"                     # optional; deep|free|rush|smart
    dangerously_allow_all = false      # default: false (changed in v0.35.3 #206)
    stream_json_input = false          # default: false; passes --stream-json-input
    ```

Notes:

* `mode` controls model selection, system prompt, and tool availability within AMP.
* `dangerously_allow_all` defaults to `false` as of v0.35.3 ([#206](https://github.com/littlebearapps/untether/issues/206)) — opt in only if you specifically want AMP runs without its built-in permission system. Untether's own permission layer remains the primary control.
* `stream_json_input` enables `--stream-json-input` for stdin streaming. Nothing reads it back: approvals, plan mode, AskUserQuestion, live sessions and steer are Claude Code-only.

---

## Code changes (by file)

### `src/untether/runners/amp.py`

Exposes `BACKEND = EngineBackend(id="amp", build_runner=build_runner, install_cmd="npm install -g @sourcegraph/amp")`.

#### Runner invocation

```text
amp [threads continue <thread-id>] [--dangerously-allow-all] [--mode <mode>] --stream-json [--stream-json-input] -x <prompt>
```

#### Event translation

AMP uses a Claude Code-compatible JSONL protocol with a `type` discriminator. The runner translates:

* `system(subtype="init")` -> `StartedEvent` (captures session_id)
* `assistant` (tool_use blocks) -> `ActionEvent` (phase: started)
* `user` (tool_result blocks) -> `ActionEvent` (phase: completed)
* `assistant` (text blocks) -> text accumulation for final answer
* `result` -> `CompletedEvent` (with accumulated usage)

#### Subagent tracking

`parent_tool_use_id` from assistant/user messages is stored in `action.detail["parent_tool_use_id"]` when present. This tracks which tool calls belong to subagent invocations.

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
* `--stream-json-input` is passed when `stream_json_input = true` in config. No approve/deny flow is wired for AMP; interactive features are Claude Code-only.
* Untether does not pass `--model`; model selection is controlled server-side by `--mode`.

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
