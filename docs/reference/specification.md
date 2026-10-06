# Untether Specification v0.36.0 [unreleased]

This document is **normative**. The words **MUST**, **SHOULD**, and **MAY** express requirements.

## 1. Scope

Untether v0.36.0 specifies:

- A **Telegram** bot bridge that runs an agent **Runner** and posts:
  - a throttled, edited **progress message**
  - a **final message** with the final answer and a resume line
- **Thread continuation** via a **resume command** embedded in chat messages
- **Parallel runs across different threads**
- **Serialisation within a thread** (no concurrent runs on the same thread)
- **Automatic runner selection** among multiple engines based on ResumeLine (with a configurable default for new threads)
- A Untether-owned **normalised event model** produced by runners and consumed by renderers/bridge

Out of scope:

- Non-Telegram clients (Slack/Discord/etc.)
- Token-by-token streaming of the assistant’s final answer
- Engines/runners that cannot provide **stable action IDs** within a run

## 2. Terminology

- **EngineId**: string identifier of an engine (e.g., `"claude"`, `"codex"`, `"opencode"`, `"pi"`; the deprecated `"gemini"` and `"amp"` still load but are deprecated and no longer supported, and may be removed in a future release).
- **Runner**: Untether adapter that executes an engine process and yields **Untether events**.
- **Thread**: a single engine-side conversation, identified in Untether by a **ResumeToken**.
- **ResumeToken**: Untether-owned thread identifier `{ engine: EngineId, value: str }`.
- **ResumeLine**: a runner-owned string embedded in chat that represents a ResumeToken.
- **Run**: a single invocation of `Runner.run(prompt, resume)`.
- **Live session**: a run whose engine process stays open after its first `completed` event so that later turns (follow-ups, background-task wake-ups) run in the same process (Claude Code only, [#776](https://github.com/littlebearapps/untether/issues/776)).
- **Turn**: one follow-up segment of a live session after the run's `completed` event, bracketed by `turn` events (§4.3.4).
- **UntetherEvent**: a normalised event emitted by a runner and consumed by renderers/bridge.
- **Progress message**: a Telegram message that is periodically edited during a run.
- **Final message**: a Telegram message that includes run status, final answer, and resume line.

## 3. Resume tokens and resume lines

### 3.1 Decision: canonical resume line is the engine CLI resume command

The canonical ResumeLine embedded in chat MUST be the engine’s CLI resume command, e.g.:

- `codex resume <id>`
- `claude --resume <id>`
- `opencode --session <id>`
- `pi --session <token>`
- `gemini --resume <id>` (deprecated engine)
- `amp threads continue <id>` (deprecated engine)

Runners MAY accept additional spellings when extracting (e.g. Claude's `claude -r <id>`, OpenCode's `opencode run --session <id>` / `-s <id>`), but MUST format the canonical form.

ResumeLine MUST resume the interactive session when the engine offers both interactive and headless modes. It MUST NOT point to a headless/batch command that requires a new prompt (e.g., a `run` subcommand that errors without a message).

Untether MUST treat the runner as authoritative for:

- formatting a ResumeToken into a ResumeLine
- extracting a ResumeToken from message text

### 3.2 ResumeToken schema (Untether-owned)

```python
@dataclass(frozen=True, slots=True)
class ResumeToken:
    engine: str  # EngineId
    value: str
    is_continue: bool = False
```

`is_continue=True` marks a `/continue` token: it resumes the engine's most recent CLI session for the working directory, so `value` MAY be empty until the run's `started` event names the real session (see §5.2).

### 3.3 Runner resume codec (MUST)

Each runner MUST implement:

* `format_resume(token: ResumeToken) -> str`
* `extract_resume(text: str | None) -> ResumeToken | None`
* `is_resume_line(line: str) -> bool`

Constraints:

* `format_resume()` MUST fail if `token.engine != runner.engine`.
* `extract_resume()` MUST return `None` if it cannot **confidently** parse a resume line for its engine.

### 3.4 Bridge resume resolution (MUST)

Given `text` (user message), optional `reply_text` (the message being replied to), and an ordered list of available runners `runners`:

1. The bridge MUST attempt to extract a resume token by polling all runners in order:
   1. for each `r` in `runners`, attempt `r.extract_resume(text)`
   2. choose the **first** runner that returns a non-`None` token and stop
2. If not found, it MUST repeat step (1) for `reply_text` if present.
3. If still not found, a transport MAY supply a stored ResumeToken for the conversation scope (non-normative: the Telegram transport keeps one per forum topic when topics are enabled and, with `session_mode = "chat"`, one per chat — per sender in groups, per topic in private chats, [#734](https://github.com/littlebearapps/untether/issues/734)).
4. Otherwise the run MUST start with `resume=None` (new thread) on the default runner (per §8, including chat-level overrides).

## 4. Normalised event model

### 4.1 Decision: events are trusted after normalisation

Runners are responsible for emitting well-formed Untether events. Consumers (renderer/bridge) SHOULD assume validity and MAY fail fast on invariant violations.

### 4.2 Supported event types (minimum set)

Untether MUST support:

* `started`
* `action`
* `completed`

Runners that keep a live session (§2) additionally emit:

* `turn`

Minimal runner mode is supported:

* A runner MAY emit only `started` and `completed`.
* If `action` events are emitted, `phase="completed"` alone is valid (no requirement to emit `started`/`updated` phases).

### 4.3 Event schemas

All events MUST include `engine: EngineId` and `type`.

#### 4.3.1 `started`

Required:

* `type: "started"`
* `engine: EngineId`
* `resume: ResumeToken`

Optional:

* `title: str`
* `meta: dict` — engine-specific metadata. All engines SHOULD populate `meta.model` with the model name when available. Claude Code also populates `meta.permissionMode` (plus `cwd`, `tools`, `mcp_servers` and similar `system/init` fields), and `meta.effort` when a reasoning level is set. The bridge MAY add `meta.trigger` (cron/webhook provenance) and `meta.complete` (the `✓ turn complete` marker). `model`, `effort`, `permissionMode`, `trigger` and `complete` feed the `🏷` footer line on final messages.

#### 4.3.2 `action`

Required:

* `type: "action"`
* `engine: EngineId`
* `action: Action`
* `phase: "started" | "updated" | "completed"`

Optional:

* `ok: bool` (typically on `phase="completed"`)
* `message: str`
* `level: "debug" | "info" | "warning" | "error"`

Notes:

* `phase="completed"` alone is valid.

#### 4.3.3 `completed`

Required:

* `type: "completed"`
* `engine: EngineId`
* `ok: bool`          (overall run success/failure)
* `answer: str`       (final assistant answer; MAY be empty)

Optional:

* `resume: ResumeToken`   (final token; new or existing, if known)
* `error: str | None`     (fatal error message, if any)
* `usage: dict`           (telemetry/usage if available)

`usage` is engine-defined. Non-normative: the reference bridge reads `total_cost_usd`, `duration_ms`, `duration_api_ms`, `num_turns` and a nested `usage` dict of token counts (`input_tokens`, `output_tokens`, …). Claude Code also supplies `session_cost_baseline` (per-run cost deltas for live sessions), `compaction` ([#819](https://github.com/littlebearapps/untether/issues/819)) and `safeguard` ([#814](https://github.com/littlebearapps/untether/issues/814)); the bridge adds `token_delta_source` when it converts session-cumulative token totals to per-run figures ([#419](https://github.com/littlebearapps/untether/issues/419)).

#### 4.3.4 `turn` (live sessions)

A runner that keeps its engine process live after the run's `completed` event (Claude Code in control-channel mode) brackets each later turn with `turn` events. A run is still exactly one `started → action* → completed`; each later turn is a `turn(phase="started") → action* → turn(phase="completed")` segment.

Required:

* `type: "turn"`
* `engine: EngineId`
* `phase: "started" | "completed"`
* `turn: int` (turn number within the live process; the run's own first turn is 1)

Optional:

* `reason: "task_finished" | "scheduled_wakeup" | "monitor_event" | "followup" | "hook_rewake" | "unknown"` (default `"unknown"`)
* `resume: ResumeToken`
* `ok: bool`, `answer: str`, `error: str`, `usage: dict` (on `phase="completed"`; `usage` is the raw result payload, session-cumulative)
* `command_uuid: str` (the injected follow-up the turn answers, when known)
* `detail: dict` (e.g. the background tasks a wake turn answers)
* `started_ago_s: float` (on `phase="started"`: how long before the event the turn really began, [#815](https://github.com/littlebearapps/untether/issues/815))

A consumer that stops iterating after `completed` ends the session; §5.5 applies.

### 4.4 Action schema (MUST; stable IDs)

Actions MUST have stable IDs within a run:

```python
@dataclass(frozen=True, slots=True)
class Action:
    id: str
    kind: str
    title: str
    detail: dict[str, Any]
```

Stability requirements:

* Within a single run, the same underlying action MUST keep the same `Action.id` across events.
* `Action.id` values MUST be unique within a run.
* IDs do **not** need to be stable across different runs/resumes.

Action kinds SHOULD come from an extensible stable set, e.g.:

* `command`, `tool`, `file_change`, `web_search`, `subagent`, `turn`, `warning`, `telemetry`, `note`

Unknown kinds MAY be rendered as `note`.

`telemetry` actions carry a value for the status line rather than a step (e.g. Claude's context-window use in `detail["context_pct"]`, [#819](https://github.com/littlebearapps/untether/issues/819)); renderers SHOULD NOT render them as action lines.

`detail` is freeform; no per-kind schema is required.

`ok` semantics are runner-defined.

User-visible warnings/errors SHOULD be surfaced as `action` events (typically `kind="warning"` or `kind="note"`, `phase="completed"`, `ok=False`) rather than introducing new event types.

## 5. Runner protocol and concurrency

### 5.1 Runner protocol (MUST)

```python
class Runner(Protocol):
    engine: str  # EngineId

    def is_resume_line(self, line: str) -> bool: ...
    def format_resume(self, token: ResumeToken) -> str: ...
    def extract_resume(self, text: str | None) -> ResumeToken | None: ...

    def run(
        self,
        prompt: str,
        resume: ResumeToken | None,
    ) -> AsyncIterator[UntetherEvent]: ...
```

### 5.2 Per-thread serialisation (MUST; core invariant)

Define:

* `ThreadKey(resume) := f"{resume.engine}:{resume.value}"`

Invariant:

* At most **one** active run may operate on the same `ThreadKey` at a time.

Rules:

* Runs for different ThreadKeys MAY run in parallel.
* Runs for the same ThreadKey MUST be queued and executed sequentially.
* This invariant MUST be enforced by the runner implementation even if used outside the Telegram bridge.

New thread rule (`resume is None`):

* When the runner learns the new thread’s ResumeToken, it MUST:

  * acquire the per-thread lock for that token
  * do so **before emitting** `started(resume=token)`

`/continue` rule (`resume.is_continue`):

* A `/continue` token does not name a session, so the runner MUST NOT lock it up front (every `/continue` for an engine would share one lock key). It MUST treat the run like a new thread and lock the real session id named by the first `started` event ([#817](https://github.com/littlebearapps/untether/issues/817)).

Non-normative: the reference `BaseRunner` keys locks as `ThreadKey` in a per-runner `WeakValueDictionary` of `anyio.Semaphore(1)`.

### 5.3 `started` emission and ordering

* If the runner obtains a ResumeToken for the run, it MUST emit exactly one `started` event containing that token. Live-session `turn` events (§4.3.4) are not additional `started` events.
* The runner MAY emit `action` events before `started` (e.g., pre-init warnings). Consumers MUST NOT assume `started` is the first event.

### 5.4 Completion

* If the run reaches `started`, and then terminates under the runner’s control (success or detected failure), the runner MUST emit exactly one `completed` event. It MUST be the last event, except that a live-session runner MAY follow it with `turn` segments (§4.3.4); no `started`, `completed` or out-of-segment `action` event may follow `completed`.
* If the runner never obtains a ResumeToken (e.g., fatal failure before session init), it MAY emit no `started` and no `completed`.

### 5.5 Event delivery semantics (MUST)

* Events MUST be yielded in the order produced by the runner.
* The runner MUST NOT spawn unbounded background tasks per event.
* If the consumer stops iterating early (cancel/break/exception), the runner MUST abort the run best-effort and release any held locks/resources.
* Wrapping generators MUST close the generators they iterate in the same task (non-normative: the reference `BaseRunner` wraps `run_impl` in `contextlib.aclosing`), so the engine's task group unwinds in the task that entered it ([#854](https://github.com/littlebearapps/untether/issues/854)).

### 5.6 Per-run state (MUST)

Runner instances are shared across chats, so per-run state (the stream state, the engine PID) MUST NOT be read from runner-instance attributes by the bridge. Non-normative: the reference implementation binds a per-run `RunStreamHandle` through a `ContextVar` before iterating the runner; the runner publishes its stream and PID into it (`publish_run_stream`), and the bridge reads only that handle ([#510](https://github.com/littlebearapps/untether/issues/510)). `JsonlSubprocessRunner.current_stream` / `last_pid` are diagnostics only.

## 6. Bridge (Telegram orchestration)

### 6.1 Responsibilities (MUST)

The bridge MUST:

* Receive Telegram updates
* Resolve resume token (per §3.4)
* Schedule runs per thread (per §6.2)
* Start runner execution with cancellation support
* Maintain a progress message while avoiding excessive edits
* Publish a final message containing status, answer, and resume line (when known)
* Support `/cancel` for in-flight runs

The bridge MUST NOT:

* parse engine-native streams/events
* embed engine-specific rules beyond calling runner resume extraction/formatting

Queue depth:

* There is no queue depth limit; all prompts are accepted.
* Non-normative: the reference base runner can refuse a spawn with an error `completed` when `[watchdog] max_concurrent_engine_runs` (default `0`, unlimited) is reached or free RAM is below the pre-spawn guard.

### 6.2 Scheduling (MUST)

Definitions:

* `Job := (chat_id, user_msg_id, text, resume: ResumeToken | None)`

Required behaviour:

* For `resume != None`, the bridge MUST enqueue jobs into `pending_by_thread[ThreadKey(resume)]`.
* For each ThreadKey, exactly one worker (or equivalent mechanism) MUST drain the queue sequentially.
* A worker MUST exit when its queue is empty; the bridge SHOULD avoid retaining state for inactive threads.
* The implementation MUST avoid spawning one long-lived task per queued job (bounded concurrency).

Runs that start as new threads:

* If a job starts with `resume=None` and later yields `started(resume=token)`, the bridge MUST treat that run as the in-flight job for `ThreadKey(token)` until it completes (for scheduling and cancellation routing).

Live sessions:

* While a live-session run is in flight, the bridge MAY deliver queued jobs for the same ThreadKey into the live engine process as follow-up turns instead of waiting for the run to end ([#776](https://github.com/littlebearapps/untether/issues/776)). A job taken off the queue MUST be either delivered or put back at the head of the queue — never lost or run twice. The engine process serialises the turns, so the one-active-run invariant (§5.2) still holds.
* Follow-up mode ([#775](https://github.com/littlebearapps/untether/issues/775)) decides what a message sent mid-run does: `queue` (default) waits for the running turn to end; `steer` writes it into the running session at once. Steering is Claude Code-only; other engines fall back to `queue`.

### 6.3 Progress message behaviour

* The bridge SHOULD send an initial progress message quickly (e.g., “Running…”).
* The bridge SHOULD avoid excessive edits and respect transport constraints (implementation-defined).
* The bridge SHOULD skip edits when rendered content is unchanged.
* Once `started` is observed, the progress view SHOULD include the canonical ResumeLine.

### 6.4 Final message requirements (MUST)

The final output MUST include:

* a status line (`done` / `error` / `cancelled`)

Each live-session turn (§4.3.4) SHOULD get its own final message with a status line and the turn's answer.
* the final `answer` (if any)
* the ResumeLine if known (and MUST include it if `started` was received)

### 6.5 Cancellation `/cancel` (MUST)

* The bridge MUST allow users to cancel a run in progress by sending `/cancel` in reply to the progress message (or by an equivalent mapping defined by the bridge).
* Cancellation MUST terminate the runner process via **SIGTERM**. Exception: a live session that is idle between turns (§2) SHOULD instead be closed gracefully by closing the engine's stdin — the CLI stops its background tasks and exits cleanly ([#776](https://github.com/littlebearapps/untether/issues/776)); an active turn is still terminated.
* After cancellation, the bridge MUST stop further progress edits and publish a “cancelled” status message.
* The bridge SHOULD include the ResumeLine if known.
* Any additional text after `/cancel` is ignored.

### 6.6 Telegram markdown + truncation (MUST)

The bridge MUST:

* escape/prepare Telegram markdown correctly
* enforce Telegram message length limits (including after escaping)
* avoid truncating away the ResumeLine (using `runner.is_resume_line()`)

If truncation is required:

* the bridge MUST keep the ResumeLine intact
* the bridge SHOULD preserve the beginning of the content and insert an ellipsis at the truncation point

### 6.7 Crash/error handling (MUST)

If the runner crashes or exits uncleanly:

* the bridge MUST publish an error status message
* if `started` was received, the bridge MUST include the ResumeLine in that error message

## 7. Renderer

Renderers MUST:

* be deterministic functions/state machines over Untether events + internal renderer state
* produce Telegram-ready markdown (or markdown + entities)
* tolerate `action` events that are “completed-only” (no prior `started`/`updated`)

Renderers MUST NOT:

* depend on engine-native event formats
* call Telegram APIs
* perform blocking I/O

Action update collapsing:

* If multiple `action` events share the same `Action.id`, renderers SHOULD treat later `started`/`updated` events as updates (replace the prior running line rather than appending).

## 8. Configuration and engine selection

Decision (v0.4.0):

* Untether MUST support configuring a **default engine** used to start new threads (`resume=None`).
  * If not configured, the default engine is implementation-defined (non-normative: the reference implementation defaults to `codex`).
* If no engine subcommand is provided, Untether MUST run in **auto-router** mode:
  * new threads use the configured default engine
  * resumed threads are routed based on ResumeLine extraction (per §3.4)
* If an engine subcommand is provided, Untether MUST still use the auto-router, but it overrides the configured default engine for new threads.
* Resume extraction MUST poll **all** available runners (per §3.4) and route to the first matching runner.
* New thread engine override (chat-level):
* Users MAY prefix the first non-empty line with `/{engine}` (e.g. `/claude`, `/codex`, or `/pi`) to select the engine for a **new** thread.
  * The bridge MUST strip that directive from the prompt before invoking the runner.
  * If a ResumeToken is resolved from the message or reply, it MUST take precedence and the `/{engine}` directive MUST be ignored.
* Bridges MAY persist default engine overrides per Telegram scope:
  * **Topic default**: forum topic (`chat_id + thread_id`)
  * **Chat default**: chat (`chat_id`)
* When no ResumeToken is resolved, engine selection MUST follow this precedence:
  1) explicit `/{engine}` directive
  2) topic default (if any)
  3) chat default (if any)
  4) project default engine (if configured for the resolved context)
  5) global default engine

### 8.1 Command menu (Telegram)

Untether SHOULD keep the bot’s slash-command menu in sync at startup by calling
`setMyCommands` with the canonical list of supported commands.

* The command list MUST include:
  * `cancel` — cancel the current run
  * one entry per available engine
  * one entry per configured project alias that is a valid Telegram command
* The command list SHOULD also include enabled command-backend plugins and the
  built-in chat commands (non-normative: `new`, `continue`, `ctx`, `agent`, `model`,
  `reasoning`, `listen`, plus `topic` when topics are enabled and `file` when file
  transfer is enabled).
* The command list MUST NOT include commands the bot does not support.
* Command descriptions SHOULD be terse and lowercase.
* The command list SHOULD be capped at 100 entries per Telegram's limit; if the
  config exceeds that limit, implementations SHOULD warn and truncate while
  still handling all commands at runtime.

## 9. Testing requirements (MUST)

Tests MUST cover:

1. **Runner contract**

   * If a token is obtained: exactly one `started`
   * Action schema validity (required fields; stable unique IDs within run)
   * Event ordering preserved
   * `completed` emitted and last for controlled termination after `started`
2. **Runner serialisation**

   * Concurrent runs for the same ResumeToken serialise
   * `resume=None` runs acquire the per-thread lock once token is known and before emitting `started`
3. **Bridge per-thread scheduling**

   * FIFO per ThreadKey
   * second job for same thread does not start until first completes
4. **Progress throttling**

   * edits not more frequent than configured interval
   * no edit when content unchanged
   * truncation preserves ResumeLine
5. **Cancellation**

   * `/cancel` terminates run and produces “cancelled”
   * ResumeLine included if known
6. **Renderer formatting**

   * completed-only actions render correctly
   * repeated events for same Action.id collapse as intended
7. **Auto-router engine selection**

   * resume lines for non-default engines are detected and routed correctly (poll all runners)
   * new threads use the configured default engine, with CLI subcommand overriding it

Test tooling SHOULD include event factories, deterministic/fake time, and a script/mock runner.

## 10. Lockfile (single-instance enforcement)

Untether MUST prevent multiple instances from racing `getUpdates` offsets for the same bot token.

### 10.1 Lock file location

The lock file MUST be stored next to the config file, with the config file's suffix replaced by `.lock`. For the default config path, this resolves to `~/.untether/untether.lock`.

### 10.2 Lock mechanism

The instance MUST hold an exclusive, non-blocking advisory lock (`flock(2)`) on the lock file for its whole lifetime. The kernel releases it when the process exits, so a crashed instance never leaves a lock that looks valid (this replaced PID-liveness checks in v0.35.4, [#459](https://github.com/littlebearapps/untether/issues/459)). The lock descriptor MUST NOT be inherited by engine subprocesses.

### 10.3 Lock file contents

After acquiring the lock, the instance SHOULD write JSON with:

* `pid: int` — the process ID holding the lock
* `token_fingerprint: str` — SHA256 hash of the bot token, truncated to 10 characters

These fields are diagnostic only and MUST NOT be used to decide whether the lock is held.

### 10.4 Lock acquisition and release

* If the `flock` is acquired, the instance proceeds.
* If another live process holds the `flock`, the instance MUST fail with an "already running" error naming the lock file.
* On shutdown the instance releases the `flock` and closes the descriptor. It SHOULD NOT delete the lock file (deleting it reopens an open-then-lock race); a leftover file with no live lock is harmless.

## 11. Progress persistence

### 11.1 Tracking active progress messages (MUST)

The bridge MUST track active progress messages in a persistent store (`active_progress.json` in the config directory). When a progress message is sent to Telegram, the bridge MUST register it with `(chat_id, message_id)`. When a run completes and the progress message is cleaned up, the bridge MUST unregister it.

### 11.2 Orphan cleanup on startup (MUST)

On startup, the bridge MUST load the active progress store and edit any orphan progress messages to indicate they were interrupted. Orphan messages MUST have their inline keyboards removed (no stale approval buttons). The bridge MUST clear the store after cleanup and before sending its startup message.

### 11.3 Persistence format

The store SHOULD be a JSON object keyed by `"<chat_id>:<message_id>"`, each value a `{chat_id, message_id}` entry. The bridge SHOULD tolerate a missing or corrupt store file by treating it as empty. Live-session background status messages ([#777](https://github.com/littlebearapps/untether/issues/777)) are tracked in the same store.

## 12. Outbox delivery

### 12.1 Agent-initiated file delivery (MAY)

Runners MAY write files to a designated outbox directory (default: `.untether-outbox/` relative to the project root) during a run. The bridge MUST scan the outbox after `CompletedEvent` and deliver any files as Telegram documents.

### 12.2 Constraints (MUST)

The bridge MUST enforce:

* **Deny globs** — files matching configured deny patterns (e.g. `*.env`, `.git/**`) MUST NOT be delivered
* **Max files** — at most `outbox_max_files` files per delivery (default: 10); files beyond the cap SHOULD be reported to the user rather than left for a later run
* **Size limit** — individual file size MUST NOT exceed the Telegram Bot API file upload limit (50 MB)
* **Containment** — the outbox directory MUST resolve inside the project root; an outbox that is absolute, contains `..`, or reaches outside the root (including through a symlinked path component) MUST NOT be scanned, delivered from or archived
* **Flat scan** — only files in the top-level outbox directory are delivered as files; subdirectories are skipped (and reported when `outbox_notify_skipped` is `true`, the default) unless `outbox_deliver_directories = "zip"` (default `"off"`), which sends each skipped directory as one zip after applying the same deny globs

### 12.3 Cleanup (SHOULD)

When `outbox_cleanup` is `true` (default), the bridge SHOULD delete delivered files from the outbox directory after successful delivery.

### 12.4 Freshness (SHOULD)

The bridge SHOULD deliver only entries that entered or changed in the outbox during the run (the later of the entry's modification and status-change times, against the run's start with a small grace). Older entries SHOULD NOT be sent; they SHOULD be quarantined once and reported in a single notice. A live session's run starts when the session started.

## 13. Changelog

### v0.36.0 (unreleased)

Developed and pre-released as 0.35.5rc1–rc20 and 0.36.0rc1–rc3; there is no 0.35.5 stable release.

- Add the `turn` event (§4.3.4) and live sessions: a live-session runner MAY follow `completed` with `turn` segments (§5.4); the bridge MAY inject queued jobs into the live process and supports follow-up mode `queue`/`steer` (§6.2).
- Document the `telemetry` action kind (§4.4) and the reference `usage` keys (§4.3.3).
- `/continue` runs lock the real session id from `started`, not the shared `<engine>:` key (§5.2).
- Require wrapping generators to close inner generators in the same task (§5.5) and per-run stream/PID binding (§5.6).
- Add `is_continue` to the `ResumeToken` schema (§3.2), transport-stored resume tokens (§3.4), and correct the OpenCode resume line to `opencode --session <id>` (§3.1).
- Align §10 (flock lock, v0.35.4), §11.3 (store format) and §12.2 (directory zip delivery, v0.35.4) with the implementation.
- Add outbox freshness (§12.4): deliver only entries written during the run; quarantine and report older ones. Files over `outbox_max_files` are reported, and the outbox must resolve inside the project root (§12.2).

### v0.35.0 (2026-03-18)

- Add progress persistence specification (§11): active progress messages MUST be tracked and orphans cleaned up on restart.
- Add outbox delivery specification (§12): runners MAY write files to an outbox directory; the bridge MUST scan, deliver, and enforce constraints.
- Bump version from v0.23.0 to v0.35.0 to align with the release.
- Clarify `ResumeToken` MAY include `is_continue: bool` for cross-environment resume.

### v0.22.1 (2026-02-10)

- No normative changes; align spec version with the v0.22.1 release.

### v0.22.0 (2026-02-10)

- No normative changes; align spec version with the v0.22.0 release.

### v0.21.5 (2026-02-08)

- No normative changes; align spec version with the v0.21.5 release.

### v0.21.4 (2026-01-22)

- No normative changes; align spec version with the v0.21.4 release.

### v0.21.3 (2026-01-21)

- No normative changes; align spec version with the v0.21.3 release.

### v0.21.2 (2026-01-20)

- No normative changes; align spec version with the v0.21.2 release.

### v0.21.1 (2026-01-18)

- No normative changes; align spec version with the v0.21.1 release.

### v0.21.0 (2026-01-16)

- No normative changes; align spec version with the v0.21.0 release.

### v0.20.0 (2026-01-15)

- No normative changes; align spec version with the v0.20.0 release.

### v0.19.0 (2026-01-15)

- No normative changes; align spec version with the v0.19.0 release.

### v0.18.0 (2026-01-13)

- No normative changes; align spec version with the v0.18.0 release.

### v0.17.1 (2026-01-12)

- No normative changes; align spec version with the v0.17.1 release.

### v0.17.0 (2026-01-12)

- No normative changes; align spec version with the v0.17.0 release.

### v0.16.0 (2026-01-12)

- No normative changes; align spec version with the v0.16.0 release.

### v0.15.0 (2026-01-11)

- No normative changes; align spec version with the v0.15.0 release.

### v0.14.1 (2026-01-10)

- No normative changes; align spec version with the v0.14.1 release.

### v0.14.0 (2026-01-10)

- No normative changes; align spec version with the v0.14.0 release.

### v0.13.0 (2026-01-09)

- No normative changes; align spec version with the v0.13.0 release.

### v0.12.0 (2026-01-09)

- No normative changes; align spec version with the v0.12.0 release.

### v0.11.0 (2026-01-08)

- No normative changes; align spec version with the v0.11.0 release.

### v0.10.0 (2026-01-08)

- Require Telegram command menus to include valid project aliases and warn/truncate when exceeding 100 commands.

### v0.9.0 (2026-01-07)

- No normative changes; align spec version with the v0.9.0 release.

### v0.8.0 (2026-01-05)

- Add `subagent` action kind for agent/task delegation tools.
- Add lockfile specification for single-instance enforcement (§10).

### v0.7.0 (2026-01-04)

- No normative changes; implementation migrated to structlog and msgspec schemas.

### v0.6.0 (2026-01-03)

- No normative changes; added interactive onboarding and lockfile implementation.

### v0.5.0 (2026-01-02)

- No normative changes; align spec version with the v0.5.0 release.

### v0.4.0 (2026-01-01)

- Add auto-router engine selection by polling all runners to decode resume lines; add configurable default engine for new threads (subcommand overrides default).

### v0.3.0 (2026-01-01)

- Require runners to implement explicit resume formatting/extraction/detection and treat runners as authoritative for resume tokens/lines.

### v0.2.0 (2025-12-31)

- Initial minimal Untether specification (Telegram bridge + runner protocol + normalised events + resume support).
