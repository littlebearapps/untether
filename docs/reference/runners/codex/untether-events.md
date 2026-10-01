# Codex -> Untether event mapping

This document describes how Codex exec --json events are translated to Untether's normalized event model.

> **Authoritative source:** The schema definitions are in `src/untether/schemas/codex.py` and the translation logic is in `src/untether/runners/codex.py`. When in doubt, refer to the code.

## Invocation

Untether runs Codex non-interactively; it never stops to ask for approval, and approvals, plan mode, AskUserQuestion, live sessions and steer are Claude Code-only. The argv (prompt on stdin) is:

```text
codex [extra_args] [--profile <p>] [--model <m>] [-c model_reasoning_effort=<level>] \
  exec --json --skip-git-repo-check --color=never [--sandbox read-only] \
  [resume <thread_id> | resume --last] -
```

* **`extra_args`** default to `["-c", "notify=[]"]` and sit at the root, before `exec`. A deny-list rejects managed flags (`--json`, `--skip-git-repo-check`, `--color`, `--output-schema`, `--output-last-message`, `--ask-for-approval`/`-a`, exec-only `--ignore-rules`/`--ignore-user-config`), bypass flags (`--yolo`, `--dangerously-bypass-approvals-and-sandbox`, `--approve-for-me`, `--not-so-yolo`, `--dangerously-bypass-hook-trust`, `--sandbox danger-full-access`, `-c` values mentioning `danger-full-access`, `:danger`, `bypass` or `dangerously`), workspace flags (`--cd`/`-C`, `--worktree`) and a bare `--`, in every spelling. A blocked flag stops the Codex engine loading, and the error names the flag ([#209](https://github.com/littlebearapps/untether/issues/209), see [Engine CLI flags](../../../how-to/security.md#engine-cli-flags-extra_args)). Root `--sandbox read-only|workspace-write` stays allowed.
* **Approval policy** (`/config` → Approval policy): **full auto** (default) adds no sandbox flag, so Codex uses its own sandbox setting. **safe** adds `--sandbox read-only` at the `exec` level (before `resume`, which has no `--sandbox`), so it outranks root-level sandbox flags and `config.toml`. Untether never passes `--ask-for-approval`: `codex exec` ignores it and forces approval to `never` ([#830](https://github.com/littlebearapps/untether/issues/830)). Any other `permission_mode` value logs `codex.permission_mode.unknown` once per value and runs full auto.
* **Reasoning** (`/config` → Reasoning): `low`, `medium`, `high` or `xhigh`, passed as `-c model_reasoning_effort=<level>`. `minimal` is not offered ([#416](https://github.com/littlebearapps/untether/issues/416)).
* **Exit code 2** with a clap argv error on stderr (`error: unexpected argument …` and similar) also logs `codex.argv.rejected` with the argv, to flag upstream flag drift.

## The 3-event Untether schema

The Untether event model uses 3 event types. The `action` event includes a `phase` field to represent started/updated/completed lifecycles.

### 1) `started`

Emitted once **as soon as you know the resume token** (Codex: `thread.started.thread_id`).

```json
{
  "type": "started",
  "engine": "codex",
  "resume": { "engine": "codex", "value": "0199..." },
  "title": "Codex",               // optional
  "meta": { "model": "o3", "permissionMode": "safe" }  // optional: used for 🏷 footer
}
```

Note: Codex JSONL does not include model or permission info in its event stream. The runner populates `meta.model` from the `/config` model override (falling back to `codex-mini-latest` for display), `meta.effort` when a reasoning level is set, and `meta.permissionMode` only when the approval policy is `safe` (non-default). See [Invocation](#invocation) for what each setting does to argv.

### 2) `action`

Emitted for **everything that is progress / updates / warnings / per-item lifecycle**.

```json
{
  "type": "action",
  "engine": "codex",
  "action": {
    "id": "item_5",
    "kind": "tool",               // command | tool | file_change | web_search | subagent | note | turn | warning | telemetry
    "title": "docs.search",       // short label for renderer
    "detail": { ... }             // structured payload (freeform)
  },
  "phase": "started",             // started | updated | completed
  "ok": true,                     // optional; present when phase=completed (or warnings)
  "message": "optional text",     // optional; logs/warnings can use this
  "level": "info"                 // optional: debug|info|warning|error
}
```

### 3) `completed`

Emitted once at end-of-run with the **final answer** (from `agent_message`) and final status.

```json
{
  "type": "completed",
  "engine": "codex",
  "resume": { "engine": "codex", "value": "0199..." },  // if known
  "ok": true,
  "answer": "Done. I updated the docs...",
  "error": null,
  "usage": { "input_tokens": 24763, "cached_input_tokens": 24448, "cache_write_input_tokens": 0,
             "output_tokens": 122, "reasoning_output_tokens": 64 }  // optional; THREAD running total
}
```

The runner forwards `turn.completed.usage` unchanged: all five fields, as the thread's running total. The bridge then rewrites the five flat fields to **this run's delta** using the per-thread ledger in `session_costs.json`, and adds `thread_total_usage` (the running total) and `token_delta_source` ([#419](https://github.com/littlebearapps/untether/issues/419); details in the [cheatsheet](exec-json-cheatsheet.md)). `/usage` in a Codex chat shows the token totals of the chat's last Codex session, with cached input and reasoning output shown as subsets ([#417](https://github.com/littlebearapps/untether/issues/417)). Codex has no subscription-quota data.

Why this fits Untether cleanly:

* Your `started` corresponds to the old “session.started” concept (runner learns resume token; bridge can now safely serialize per thread). 
* Your `action` is “everything that would have been action.started/action.completed/log/error” collapsed into one stream. 
* Your `completed` corresponds to final `RunResult` + status, using Codex’s `agent_message` as the answer source.  

---

## How everything fits together (end-to-end)

From the bridge/runner point of view:

1. **Bridge receives Telegram prompt**
2. Bridge tries to extract a resume line (`codex resume <uuid>`) from the message/reply (runner-owned parsing). 
3. Bridge calls `runner.run(prompt, resumeTokenOrNone)`
4. Codex runner spawns `codex exec --json ...` and reads JSONL line-by-line. 
5. The *first moment the runner can know thread identity* is:

   * `thread.started` → contains `thread_id` (this is your resume value)
6. Runner must (per Untether’s concurrency invariant) **acquire the per-thread lock as soon as the new thread token is known**, before emitting `started`. 
7. Runner translates subsequent Codex JSONL lines into `action` events for progress rendering.
8. Runner captures the final answer from `item.completed` where `item.type="agent_message"`. 
9. Runner emits exactly one `completed` event when the run ends (`turn.completed` or failure), including the captured final answer.

---

## Direct translation: every Codex `exec --json` line → your 3-event schema

Codex emits two categories: **top-level lines** and **item lines**. 

### A) Top-level lines

#### `thread.started`

Codex:

```json
{"type":"thread.started","thread_id":"0199..."}
```

→ Untether:

* emit **`started`**:

  * `resume.value = thread_id`

This is exactly the “learn resume tag” moment you described. 

---

#### `turn.started`

Codex:

```json
{"type":"turn.started"}
```

→ Untether (recommended):

* emit **`action`** with a synthetic action id, e.g. `"turn_0"`

  * `kind="turn"`, `phase="started"`, `title="turn started"`

You *can* also drop it if your UI doesn’t care, but if you want “every codex type translates”, this maps cleanly into `action`.

---

#### `turn.completed`

Codex includes usage:

```json
{"type":"turn.completed","usage":{...}}
```

→ Untether:

* emit **`completed`**

  * `ok=true`
  * `answer = last seen agent_message text` (or `""` if none)
  * `usage = usage` (optional)

This is your authoritative “run succeeded” boundary. 

---

#### `turn.failed`

Codex:

```json
{"type":"turn.failed","error":{"message":"..."}}
```

→ Untether:

* emit **`completed`**

  * `ok=false`
  * `error = error.message`
  * `answer = last seen agent_message` (if any; usually empty)

This is “run ended, but failed”. 

---

#### Top-level `error` (stream error)

Codex:

```json
{"type":"error","message":"stream error: broken pipe"}
```

Cheatsheet meaning: this is a **fatal stream failure** (not just a tool failure).
However, Codex may also emit transient reconnect notices as `type="error"` with
messages like `"Reconnecting... 1/5"` while it retries a dropped stream. Treat
those as non-fatal progress updates (do **not** end the run).

→ Untether (does **not** end the run on its own):

* `Reconnecting... N/M` → **`action`** with id `codex.reconnect`, `kind="note"`, `level="info"`, `detail={ attempt, max }`; `phase="started"` for attempt 1, `"updated"` after, so the row updates in place
* any other message → a warning **`action`** (`kind="warning"`, `phase="completed"`, `ok=false`, `level="warning"`, `message=message`)

The run ends via `turn.completed` / `turn.failed`, or when the process exits: a non-zero exit code emits a warning plus `completed` with `ok=false` (`codex exec failed (rc=…)` with a stderr excerpt); a clean exit with no `turn.*` emits `completed` with `ok=true` and the captured answer, or `ok=false` if no `thread_id` was ever seen.

---

### B) Item lines: `item.started`, `item.updated`, `item.completed`

All item lines include `item.id` and it is stable across updates/completion. 
That means your `action.action.id` should just be `item.id` — perfect match to “stable within a run”.

#### General rule (for any item.* line)

* `action.action.id = item.id`
* `action.phase = started | updated | completed`
* `action.action.kind` derived from `item.type`
* `action.action.detail` contains the relevant item fields (possibly trimmed)

Now, map each `item.type`:

---

## Item-type mapping: `item.type` → `action.kind/title/detail/ok`

Below is a “complete coverage” mapping for all item types listed in the cheatsheet. 

### 1) `agent_message`

Codex:

```json
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"...","phase":"final_answer"}}
```

`item.phase` is optional (`commentary`, `final_answer`, or absent).

→ Untether:

* `phase="commentary"` → **`action`** with `kind="note"`, `title=item.text`, `detail={ phase: "commentary" }` on started/updated/completed (`ok=true` on completion)
* any other `agent_message`: **no `action`**; on `item.completed` the text is stored for the turn
* the final answer is the turn's last `final_answer` message, else its last message with no phase; it is delivered by the eventual `completed` event (a new `turn.started` resets it)

---

### 2) `reasoning` (usually only `item.completed`, if enabled)

Codex gives a text breadcrumb. 

→ Untether `action`:

* `kind="note"`
* `title=item.text`
* `phase` maps 1:1 to started/updated/completed; `ok=true` on completion

---

### 3) `command_execution` (`item.started` and `item.completed`)

Codex fields include `command`, `status`, `aggregated_output` (often noisy), and
`exit_code` (null or omitted until completion). 

→ Untether `action`:

* `kind="command"`
* `title=item.command` with the run directory prefix stripped from paths
* `phase` maps 1:1 to started/updated/completed
* on completion: `detail={ exit_code, status }` and `ok = (item.status == "completed")` (and `exit_code == 0` when it is an int); `aggregated_output` is never copied

Note: “failed” command becomes `ok=false` but it’s still just an `action` completion — the overall run might still succeed later, depending on agent behavior.

---

### 4) `file_change` (only `item.completed`)

Codex contains `changes[]` and `status`. 

→ Untether `action`:

* `kind="file_change"`
* `title` = the changed paths joined with `, ` (or `N files` when none carry a path)
* `detail={ changes: [{ path, kind }], status, error: null }`
* `phase="completed"` (started/updated lines are ignored)
* `ok = (item.status == "completed")`

---

### 5) `mcp_tool_call` (`item.started` and `item.completed`)

Codex contains server/tool/arguments/status and may include result/error on
completion. Result can be large; may include base64 in content blocks. 

→ Untether `action`:

* `kind="tool"`
* `title=f"{item.server}.{item.tool}"`
* `detail={ server, tool, arguments, status }`
* on completion, include *summary* of result:

  * e.g. `detail.result_summary = { content_blocks: N, has_structured: bool }`
  * include `detail.error_message` if failed
* `phase` maps 1:1 to started/updated/completed
* `ok = (item.status == "completed" and item.error is None)`

Full `result.content` is never copied into `detail` (it can hold large base64 blobs); only the summary is kept.

---

### 6) `web_search` (`item.started` and `item.completed`)

Codex includes `query` (empty on `item.started`), an untyped `action`
(`{"type": "search"|"open_page"|"find_in_page"|"other", …}`) and, on
completion, opaque `results`. The schema keeps `action`/`results` as `Any` so an
unknown future action type never drops the line (#419).

→ Untether `action` (same raw id on both phases, so the started row completes in place):

* `kind="web_search"`
* `phase="started"` / `"completed"`, `ok=true` on completion
* `title` from `runners/codex.py:_web_search_title()`:

  | `action.type` | title | `detail.action_type` | rendered prefix |
  |---|---|---|---|
  | `search` | `action.query` → `query` → first 3 `queries` joined with ` · ` (+ ` (+N more)`) | `search` | `searched: ` |
  | `search` with no query at all | `web search` | `other` | none |
  | `open_page` | `action.url` → `query` → `page` | `open_page` | `opened: ` |
  | `find_in_page` | `"<pattern>" in <url>`, `"<pattern>"`, or `url` → `query` → `page` | `find_in_page` | `find in page: ` |
  | `other` / absent / unknown, `query` non-empty | `query` | `search` | `searched: ` |
  | `other` / absent / unknown, no `query` | `web search` | `other` | none |

* `detail={ query, action_type, url?, result_count? }` — raw `results` are never
  copied (only their count).
* Claude's `WebSearch` sets no `action_type` and keeps the `searched: ` prefix.

---

### 7) `todo_list` (`item.started`, `item.updated`, `item.completed`)

Codex includes checklist items with `completed` booleans. 

→ Untether `action`:

* `kind="note"`
* `title="todo <done>/<total>: <first unfinished item>"` (`…: done` when all are complete; `todo` when the list is empty)
* `detail={ done, total }`
* `phase` maps 1:1 to started/updated/completed
* `ok=true` when phase completed

This is the one case where `item.updated` is common; your unified `action` event is exactly the right shape for it.

---

### 8) Item `error` (non-fatal warning as an item; only `item.completed`)

Codex:

```json
{"type":"item.completed","item":{"id":"item_9","type":"error","message":"command output truncated"}}
```

Cheatsheet: this is a **non-fatal warning** (different from top-level fatal `error`). 

→ Untether `action`:

* `kind="warning"`
* `title=item.message`, `detail={ message }`, `message=item.message`
* `level="warning"`
* `phase="completed"` (started/updated lines are ignored)
* `ok=false` (rendered as a warning; it does not end the run)

---

### 9) `collab_tool_call` and unknown item types

`collab_tool_call` (sub-agent coordination) decodes but emits **no action**. An `item.*` line whose `item.type` Untether doesn't know decodes as `unknown_item` (instead of being dropped as invalid JSONL) and also emits nothing.

---

## Suggested “single-pass” translator logic (pseudocode)

This shows how to implement it without needing more than one pass or complicated buffering:

```python
final_answer = None
resume = None
did_emit_started = False
did_emit_completed = False
turn_index = 0

def emit(evt): yield evt  # emit to the output event stream

for line in codex_jsonl_stream:
    t = line["type"]

    if t == "thread.started":
        resume = {"engine": "codex", "value": line["thread_id"]}
        # acquire per-thread lock here (for new sessions) before emitting started
        emit({"type":"started","engine":"codex","resume":resume,"title":"Codex"})
        did_emit_started = True
        continue

    if t == "turn.started":
        emit({"type":"action","engine":"codex",
              "action":{"id":f"turn_{turn_index}","kind":"turn","title":"turn started","detail":{}},
              "phase":"started"})
        continue

    if t == "item.started" or t == "item.updated" or t == "item.completed":
        item = line["item"]
        item_type = item["type"]
        item_id = item["id"]

        if t == "item.completed" and item_type == "agent_message":
            final_answer = item.get("text","")
            continue

        # map item_type -> kind/title/detail/ok
        action_evt = map_item_to_action(item, phase=t.split(".")[1])
        emit(action_evt)
        continue

    if t == "turn.completed":
        emit({"type":"completed","engine":"codex","resume":resume,
              "ok":True,"answer":final_answer or "",
              "error":None,"usage":line.get("usage")})
        did_emit_completed = True
        continue

    if t == "turn.failed":
        emit({"type":"completed","engine":"codex","resume":resume,
              "ok":False,"answer":final_answer or "",
              "error":line["error"]["message"]})
        did_emit_completed = True
        continue

    if t == "error":  # stream error: progress/warning only, never ends the run
        emit(reconnect_note_or_warning(line.get("message")))
        continue

# If the stream ends without turn.completed/failed: rc != 0 -> completed ok=False;
# rc == 0 -> completed ok=True with final_answer (ok=False if no thread_id was seen)
```

This design preserves the Untether ordering/serialization principles: `started` happens as soon as resume token is known, actions stream in order, and exactly one `completed` closes the run. 

---

## One practical note: what “completed” should mean

Even though you *learn* the final answer at `agent_message`, you generally want `completed` to be emitted at the **turn boundary** (`turn.completed` / `turn.failed`), because:

* you can attach usage (`turn.completed.usage`) only there, 
* you guarantee `completed` is truly the last event,
* you still use `agent_message` as the authoritative answer payload.

That still matches your intent (“completed is when we get final answer”) because the answer comes from `agent_message`; you just *publish* it at the terminal boundary.
