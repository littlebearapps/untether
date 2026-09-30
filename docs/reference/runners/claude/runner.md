Below is a concrete implementation spec for the **Anthropic Claude Code (“claude” CLI / Agent SDK runtime)** runner shipped in Untether (v0.3.0).

---

## Scope

### Goal

Provide the **`claude`** engine backend so Untether can:

* Run Claude Code non-interactively via the **Agent SDK CLI** (`claude -p`). ([Claude Code][1])
* Run Claude Code interactively via permission mode (`--permission-mode plan --permission-prompt-tool stdio`) with a bidirectional control channel.
* Stream progress in Telegram by parsing **`--output-format stream-json --input-format stream-json --verbose`** (newline-delimited JSON). ([Claude Code][1])
* Support resumable sessions via **`--resume <session_id>`** (Untether emits a canonical resume line the user can reply with). ([Claude Code][1])

---

## UX and behavior

### Engine selection

* Default: `untether` (auto-router uses `default_engine` from config)
* Override: `untether claude`

Untether runs in auto-router mode by default; `untether claude` or `/claude` selects
Claude Code for new threads.

### Resume UX (canonical line)

Untether appends a **single backticked** resume line at the end of the message, like:

```text
`claude --resume 8b2d2b30-...`
```

Rationale:

* Claude Code supports resuming a specific conversation by session ID with `--resume`. ([Claude Code][1])
* The CLI reference also documents `--resume/-r` as the resume mechanism.

Untether should parse either:

* `claude --resume <id>`
* `claude -r <id>` (short form from docs)

**Note:** Claude Code session IDs should be treated as **opaque strings**. Do not assume UUID format.

### Permissions

Untether supports two modes:

**Non-interactive (`-p` mode):** Claude Code can require tool approvals but Untether cannot answer interactive prompts. Users must preconfigure permissions via `--allowedTools` or Claude Code settings. ([Claude Code][2])

**Interactive (permission mode):** When `permission_mode` is set (e.g. `plan` or `auto`), Untether uses `--permission-mode <mode> --permission-prompt-tool stdio` to establish a bidirectional control channel over stdin/stdout. Claude Code emits `control_request` events for tool approvals and plan mode exits; Untether responds with `control_response` (approve/deny with optional `denial_message`). This uses a PTY (`pty.openpty()`) to prevent stdin deadlock.

Key control channel features:
* Session registries (`_SESSION_STDIN`, `_REQUEST_TO_SESSION`) for concurrent session support
* Auto-approve for routine tools (Grep, Glob, Read, Bash, etc.)
* `ExitPlanMode` requests shown as Telegram inline buttons (Approve / Deny / Pause & Outline Plan) in `plan` mode; post-outline buttons add **Let's discuss** for plan discussion before approval
* `ExitPlanMode` requests silently auto-approved in `plan-auto` mode (no buttons shown)
* Text-based outline gate on ExitPlanMode after "Pause & Outline Plan" — retries without written outline text are auto-denied; the former time-based progressive cooldown was retired in [#570](https://github.com/littlebearapps/untether/issues/570) (upstream retry loop fixed in Claude Code ≥ 2.1.215)
* `📋 Plan (approved)` re-emit (#508): each `ExitPlanMode` control request is recorded per `request_id` (`ClaudeStreamState.exitplanmode_plans`) and settled only by a decision on that request. Its body becomes `last_exitplanmode_plan`, the prepend source, only when the request is approved: by Telegram Approve (`write_control_response`), by the `plan-auto` stamp, or by the post-outline `_DISCUSS_APPROVED` auto-approve. It is dropped on any denial or timeout. **The plan file is the source of truth, not `input.plan`.** On plan-file CLIs (seen on 2.1.284) Claude issues the plan-file `Write` and `ExitPlanMode` in the same message, and the CLI reads `input.plan` before the Write lands, so the input carries the *previous* plan. Untether tracks the parent's `Write`/`Edit`/`MultiEdit` calls to `<config dir>/plans/*.md` (`~/.claude` or `$CLAUDE_CONFIG_DIR`; subagent writes are ignored) in `plan_file_path` / `plan_file_content`. At decision time the body is the last full `Write`'s content if there is one, otherwise the file read from disk (at most 256 KiB, and only if it still resolves to a plan file, so a symlink can't point it elsewhere), otherwise `input.plan` as a fallback for CLIs without plan files. When the input disagrees with the file, Untether logs `claude.plan.stale_input` (INFO: `decision`, `plan_source`, `input_chars`, `file_chars`) and uses the file. On the input-only fallback, bodies you explicitly deny (❌ Deny) are remembered for the process, and an approved input identical to one of them logs `claude.plan.stale_input reason=matches_rejected` and prepends nothing. Pause & Outline and Let's discuss denials pass `rejects_plan=False`: they are procedural, not a verdict on the plan ([#793](https://github.com/littlebearapps/untether/issues/793))

**Safety note:** `-p/--print` skips the workspace trust dialog; only use this flag in trusted directories.

---

## Config additions

Untether config lives at `~/.untether/untether.toml`.

Add a new optional `[claude]` section.

Recommended v1 schema:

=== "untether config"

    ```sh
    untether config set default_engine "claude"
    untether config set claude.model "claude-sonnet-4-5-20250929"
    untether config set claude.allowed_tools '["Bash", "Read", "Edit", "Write"]'
    untether config set claude.extra_args '["--chrome"]'
    untether config set claude.dangerously_skip_permissions false
    untether config set claude.use_api_billing false
    ```

=== "toml"

    ```toml
    # ~/.untether/untether.toml

    default_engine = "claude"

    [claude]
    model = "claude-sonnet-4-5-20250929" # optional (Claude Code supports model override in settings too)
    permission_mode = "plan-auto"        # optional — see "Permission modes" below
    allowed_tools = ["Bash", "Read", "Edit", "Write"] # optional but strongly recommended for automation
    extra_args = ["--chrome"]           # optional: extra upstream CLI flags (e.g. --chrome opts into Claude-in-Chrome)
    dangerously_skip_permissions = false # optional (high risk; prefer sandbox use only)
    use_api_billing = false             # optional (keep ANTHROPIC_API_KEY for API billing)
    ```

Notes:

* `--allowedTools` exists specifically to auto-approve tools in programmatic runs. ([Claude Code][1])
* Claude Code tools (Bash/Edit/Write/WebSearch/etc.) and whether permission is required are documented. ([Claude Code][2])
* If `allowed_tools` is omitted, Untether defaults to `["Bash", "Read", "Edit", "Write"]`.
* Untether reads `model`, `permission_mode`, `allowed_tools`, `extra_args`, `dangerously_skip_permissions`, and `use_api_billing` from `[claude]`.
* `permission_mode` is validated at config load against the same allowlist crons use ([#742](https://github.com/littlebearapps/untether/issues/742)); an unknown value raises a `ConfigError` instead of failing at subprocess spawn. See "Permission modes" below.
* `extra_args` lets you pass additional upstream `claude` CLI flags that Untether doesn't expose directly — for example `["--chrome"]` opts into the Claude-in-Chrome extension (otherwise gated off by Claude Code 2.1.x), or `["--strict-mcp-config"]` / `["--mcp-config", "path"]` for MCP tweaks. Flags Untether manages internally (`-p`, `--print`, `--output-format`, `--input-format`, `--resume`/`-r`, `--continue`/`-c`, `--permission-mode`, `--permission-prompt-tool`) are rejected at config-load with a `ConfigError`. Mirrors `codex.extra_args` and `pi.extra_args`. `--include-hook-events` is **not** reserved: Untether adds it itself in control-channel mode (see "Async hooks" under Live sessions) and skips its own copy when `extra_args` already carries it, so configs that already pass it keep working ([#812](https://github.com/littlebearapps/untether/issues/812)).
* By default Untether strips `ANTHROPIC_API_KEY` from the subprocess environment so Claude Code uses subscription billing. Set `use_api_billing = true` to keep the key.

### Permission modes

Derived from **Claude Code CLI 2.1.228** (verified on lba-1, 2026-08-12). The
canonical set lives in `runners/run_options.py`
(`CLAUDE_CLI_PERMISSION_MODES`); `tests/test_claude_permission_modes.py`
re-derives it from the installed binary and fails on drift.

| `permission_mode` | Flag sent to the CLI | Meaning |
|---|---|---|
| `default` | `--permission-mode default` | Reads only; everything else prompts |
| `manual` | `--permission-mode manual` | Documented alias for `default` (CLI ≥ 2.1.200) |
| `acceptEdits` | `--permission-mode acceptEdits` | Reads, in-scope file edits, common filesystem commands |
| `plan` | `--permission-mode plan` | Research only; edits blocked until the plan is approved |
| **`plan-auto`** | `--permission-mode plan` | **Untether-only sugar** — plan mode with `ExitPlanMode` auto-approved |
| `auto` | `--permission-mode auto` | Claude Code's own classifier-gated auto mode |
| `dontAsk` | `--permission-mode dontAsk` | Auto-denies anything that would prompt; only pre-approved tools run |
| `bypassPermissions` | `--permission-mode bypassPermissions` | Skips all checks |

`plan-auto` is the **only** value Untether translates. Every other value
reaches the CLI verbatim.

> **Renamed in 0.35.5rc8 ([#741](https://github.com/littlebearapps/untether/issues/741)).**
> `plan-auto` was spelled `auto` until 0.35.5rc7, which shadowed the CLI's own
> `auto` mode and made it unreachable. Stored chat prefs are rewritten once,
> at first load, guarded by a `permission_mode_migrated` flag in
> `telegram_chat_prefs_state.json` — it must be one-shot, because after the
> rename `auto` is a value the user can legitimately *choose* from the UI, and
> a per-read rewrite would make the new mode permanently unreachable. A TOML
> `permission_mode = "auto"` is never rewritten: it now means the CLI's auto
> mode and logs a one-shot `claude.permission_mode.auto_semantics_changed`
> WARN.

**Interaction with `--permission-prompt-tool stdio`.** Untether passes the
prompt tool alongside *every* mode, and the two compose rather than conflict:

* Modes that prompt (`default`, `manual`, `plan`) raise a `control_request`
  for each gated tool, which becomes a Telegram approval.
* Modes that don't prompt for routine work (`acceptEdits`, `auto`) raise no
  `control_request` for the actions they auto-approve. `diff_preview` is
  therefore inert in those modes — it has been inert under `acceptEdits`
  since it shipped, and `auto` behaves the same way.
* **`AskUserQuestion` still raises a `can_use_tool` control_request in `auto`
  mode** (probed on 2.1.228 against Untether's exact argv), so ask-mode option
  buttons keep working. This was the load-bearing question for
  [#741](https://github.com/littlebearapps/untether/issues/741): adopting
  upstream `auto` does not cost Untether its interactivity.
* `ExitPlanMode` does not arise in `auto` — there is no plan gate — so the
  outline gate and `_DISCUSS_APPROVED` machinery apply to `plan` / `plan-auto`
  only.
* When auto mode's classifier blocks an action 3 times consecutively or 20
  times in total, Claude Code falls back to prompting; because Untether
  supplies a prompt tool, that fallback surfaces as a normal Telegram
  approval rather than a silently dropped action.

Auto mode requires a supported model (Opus 4.6+/Sonnet 4.6+/Fable 5) and an
organisation that has not set `permissions.disableAutoMode`. Where it is
unavailable the CLI rejects `--permission-mode auto` at startup with rc=1 and
a stderr message, which Untether surfaces through the normal fatal-error path.

#### The six-stage permission pipeline, and where Untether sits ([#749](https://github.com/littlebearapps/untether/issues/749))

A tool call passes through six stages inside Claude Code before it runs. Only
two of them are Untether's:

| Stage | Decided by | Untether's lever |
|---|---|---|
| 1 · hooks | `PreToolUse` hooks | — |
| 2 · deny rules | `permissions.deny` in settings | — |
| 3 · ask rules | `permissions.ask` in settings | — |
| 4 · mode logic | `--permission-mode` (incl. auto's classifier) | chooses the mode |
| **5 · allow rules** | `permissions.allow` + **`--allowedTools`** | **sends or omits the allowlist** |
| **6 · prompt** | `--permission-prompt-tool stdio` → `can_use_tool` | **approves, denies, or routes to Telegram** |

Reaching **stage 6 means every earlier stage declined to decide** — it is
unresolved permission work by definition. Until 0.35.5rc9 Untether
blanket-approved every stage-6 request except `ExitPlanMode` and
`AskUserQuestion`, in *all* modes. Combined with `--allowedTools
Bash,Read,Edit,Write` at stage 5, that made `default`, `manual` and
`acceptEdits` behave indistinguishably from `bypassPermissions`.

rc9 makes the stage-6 gate mode-derived (`is_claude_prompting_mode`, carried
onto the run as `ClaudeStreamState.prompting_mode`):

| Mode class | Modes | Stage 6 behaviour |
|---|---|---|
| **Prompting** | `default`, `manual`, `acceptEdits` | **Every** tool routes to a Telegram approval |
| **Autonomous** | `plan`, `plan-auto`, `auto`, `dontAsk`, `bypassPermissions` | Only `ExitPlanMode` / `AskUserQuestion` route; the rest are approved |

Autonomous modes keep the narrow set deliberately. `DEFAULT_ALLOWED_TOOLS`
only covers `Bash`/`Read`/`Edit`/`Write`, so `Glob`, `Grep`, `WebFetch` and
`Task` already arrive at stage 6 under `plan`. Gating them would raise an
approval button per tool in the fleet's most-used mode while buying no safety
— plan mode blocks writes internally, verified by probe G (a `Write` was never
created and never surfaced as a `can_use_tool`).

#### `--allowedTools` is mode-aware ([#749](https://github.com/littlebearapps/untether/issues/749))

Stage 5 runs *before* the prompt, so the allowlist is the stronger of the two
levers: whatever it covers never reaches a Telegram approval no matter what
stage 6 does. Since 0.35.5rc9 Untether sends it only where it doesn't
contradict the mode.

| Mode | Allowlist sent | Resulting gate |
|---|---|---|
| `default` | ✗ | every tool prompts |
| `manual` | ✗ | every tool prompts |
| `acceptEdits` | ✗ | in-scope edits auto-run in the CLI; out-of-scope writes prompt |
| `plan` | ✓ | reads pre-approved; writes blocked internally by plan mode |
| `plan-auto` | ✓ | as `plan`, plus `ExitPlanMode` rubber-stamped |
| `auto` | ✓ | classifier decides at stage 4; stage 6 is the fallback path |
| `dontAsk` | ✓ | **only** the allowlisted tools can run — see below |
| `bypassPermissions` | ✓ | no checks |
| *(unset)* | ✓ | legacy `-p` path, no control channel |

`DEFAULT_ALLOWED_TOOLS` itself is unchanged (`Bash`, `Read`, `Edit`, `Write`)
— rc9 changes *when* it is sent, not what it contains. An explicit
`[engines.claude] allowed_tools` always wins, in every mode; when it applies
in a prompting mode Untether logs
`claude.allowed_tools.prompting_mode_override` (INFO, once per mode per
process) so the interaction is discoverable rather than silent.

> **`dontAsk` is deliberately more than the CLI's `dontAsk`** (decisions.md
> D-6). Untether's `dontAsk` = the CLI's `dontAsk` **plus** `Bash`, `Read`,
> `Edit` and `Write` pre-approved. The CLI's own `dontAsk` auto-denies
> anything that would prompt, so without an allowlist it cannot run any tool
> at all (probe F). Those four are adopted as a defensible core surface for a
> locked-down agent — this is an owned product decision, not inherited
> plumbing. Narrowing it to `Read,Glob,Grep` would make the mode genuinely
> read-only and closer to its documented "locked-down CI" purpose; revisit if
> `dontAsk` acquires real usage.

> **Known gap, carried to v0.35.6.** An explicit `permissions.ask` rule reaches
> stage 6 *even under `bypassPermissions`* — the CLI deliberately overriding
> the mode to honour the user's highest-priority rule. Because autonomous modes
> retain both the two-tool gate and the stage-5 allowlist, such a rule is still
> pre-empted for the four allowlisted tools. Closing that requires a stage-5
> change, not a wider stage-6 gate.

**`--permission-prompt-tool` is a hidden flag** — it is absent from
`claude --help`, so its continued existence can only be established by
spawning the binary. `tests/test_claude_permission_modes.py::
test_750_permission_prompt_tool_flag_still_accepted` does that by passing the
flag with its argument missing and reading commander's error, which costs zero
tokens. That probe is rc9's entire mitigation for
[#750](https://github.com/littlebearapps/untether/issues/750): if Anthropic
ever drops the flag, CI fails there instead of five fleet hosts failing in
production.

---

## Environment variables

The Claude runner modifies the subprocess environment before spawning `claude`:

| Variable | Behaviour |
|----------|-----------|
| `UNTETHER_SESSION` | Set to `1`. Signals to Claude Code plugins (hooks, rules, agents) that the session is running via Untether/Telegram. Plugins can check `[ -n "${UNTETHER_SESSION:-}" ]` in shell hooks to adjust behaviour — e.g. skip blocking Stop hooks that would displace the user's requested content in Telegram's single-message output. See [PitchDocs](https://github.com/littlebearapps/lba-plugins) for a reference implementation. |
| `ANTHROPIC_API_KEY` | Stripped from the environment by default so Claude Code uses subscription billing. Set `use_api_billing = true` in `[claude]` config to keep the key and use API billing instead. |
| `CLAUDE_ENABLE_STREAM_WATCHDOG` | Set to `1` via `setdefault` ([#322](https://github.com/littlebearapps/untether/issues/322)). Enables the upstream stream watchdog so Claude Code aborts cleanly on SSE idle timeout instead of hanging. User overrides via shell env still win. |
| `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | Set to `300000` (5 min) via `setdefault` ([#342](https://github.com/littlebearapps/untether/issues/342)). Matches the undici idle-body timeout that motivated [#322](https://github.com/littlebearapps/untether/issues/322) and Untether's `[watchdog] stuck_after_tool_result_timeout` default. As of v0.35.3 ([#438](https://github.com/littlebearapps/untether/issues/438)) this default is user-configurable via `[watchdog] claude_stream_idle_timeout_ms` (range 30 s – 30 min) for deployments that hit upstream Anthropic API stalls on long opus 4.7 1M plan-mode generations. Shell-set values still win via `setdefault`. The earlier 60 s value tripped on `opus · max` legitimate chain-of-thought windows. |
| `MCP_TOOL_TIMEOUT` | Set to `120000` (2 min) via `setdefault` ([#322](https://github.com/littlebearapps/untether/issues/322)). |
| `MAX_MCP_OUTPUT_TOKENS` | Set to `12000` via `setdefault` ([#322](https://github.com/littlebearapps/untether/issues/322)). |

This is not a security mechanism — `UNTETHER_SESSION` is a simple presence flag. It carries no credentials and poses no risk if set outside Untether. See the [environment variables reference](../../env-vars.md) for all variables.

### Boundary: `env -i` wrap and runtime audit ([#361](https://github.com/littlebearapps/untether/issues/361))

After `[claude].env()` returns the filtered allowlist, the runner wraps the resolved cmd with `env -i KEY=VAL …` so the resolved environment at exec time is exactly that allowlist — even if upstream Claude Code, a wrapper script, or PAM `/etc/environment` would otherwise inject host vars after the parent's `env=` kwarg is honoured. The structured `subprocess.spawn` log redacts each KV value (`KEY=***`) so allowlisted provider keys aren't logged verbatim into journald.

A companion runtime audit (gated by `[security] env_audit = true`, default true) samples `/proc/<claude_pid>/environ` on first `system.init` and emits one `claude.env_audit.leaked_var` WARNING per non-allowlisted name observed (dedup per session per name). Linux-only; non-Linux silently no-ops.

**Known upstream limitation:** Claude Code itself can re-introduce host vars at the Bash-tool subprocess level (e.g. when Bash is invoked with `-l` or `-i` it sources `~/.profile` / `~/.bashrc`). The audit only covers Claude's own process env, not its descendants. See [security how-to](../../../how-to/security.md#known-upstream-limitation) for operator mitigation guidance.

### Reasoning levels (`--effort`)

`--effort` accepts `low`, `medium`, `high`, `xhigh`, `max`. The `xhigh` level was added in v0.35.2 ([#351](https://github.com/littlebearapps/untether/issues/351)) for Claude Code CLI v2.1.114+ — it sits between `high` and `max` and is exposed in `/config → 🧠 Effort`. Set per-chat via the inline menu or pin per-engine via `[engines.claude] reasoning = "xhigh"`.

### Post-result idle timeout + "✓ turn complete" hint ([#333](https://github.com/littlebearapps/untether/issues/333))

> **0.35.5rc11:** with live sessions on (the default) the post-result phase is owned by the live-session lifecycle below; this watchdog keeps only the pre-result silence cap ([#592](https://github.com/littlebearapps/untether/issues/592)). The text below describes `[watchdog] live_sessions = false`.

After Claude Code emits its final `result` event the bidirectional CLI can sit alive for up to ~36 min before exiting on its own, leaving Untether's progress message looking stuck. The runner now closes that gap two ways:

1. **Footer marker** — every successful `result` event arms a supplementary `StartedEvent` with `meta={"complete": "✓ turn complete"}`, which `markdown.format_meta_line` renders alongside model / effort / permission / trigger so the user sees the turn boundary immediately. Errored results don't emit the hint (no false "complete" tag on a failure). Live-session follow-up and wake turns get the same marker on their finals from the bridge ([#798](https://github.com/littlebearapps/untether/issues/798)).
2. **Server-side timer** — `_post_result_idle_watchdog` arms `result_received_at` and closes stdin (`this_proc_stdin.aclose()`) once the deadline passes, after which the CLI hits stdin EOF and exits cleanly (rc=0). Claude's auto-continue safety gate already excludes `last_event_type == "result"` so the clean exit will not phantom-resume the session.

Configure via `[watchdog]`:

* `post_result_idle_enabled` — default `true`. Explicit kill-switch.
* `post_result_idle_timeout` — seconds (default `600`, range 30–3600).

Approval-state guard: if `_REQUEST_TO_SESSION` or `_PENDING_ASK_REQUESTS` has live entries for the session the timer re-arms instead of closing — prevents orphaning a button-click `control_response` mid-flight.

Two structlog events for ops: `claude.post_result_idle.deferred` (approval guard fired) and `claude.post_result_idle.closing_stdin` (deadline passed cleanly). The per-poll `claude.post_result_idle.tick` (every ≤30 s) logs at INFO only while the timer is armed, or while an approval/ask is pending (`pending_requests`/`pending_asks` > 0 — the "waiting on the user" marker, [#696](https://github.com/littlebearapps/untether/issues/696)); an unarmed tick — every in-progress turn, and every tick of a live session, whose post-result idle `_live_session_lifecycle` owns — is DEBUG. Arming emits one INFO `claude.post_result_idle.armed` edge ([#799](https://github.com/littlebearapps/untether/issues/799)). With live sessions on, the watchdog still owns the #592 pre-result silence cap (each turn) and the #333 stdout-closed-but-alive subcountdown; only its post-result close branch stands down.

#### This watchdog is PERMANENT, not a transitional workaround ([#569](https://github.com/littlebearapps/untether/issues/569))

The post-result idle watchdog, its SIGTERM/SIGKILL subcountdown, and the stuck-after-`tool_result` detector ([#322](https://github.com/littlebearapps/untether/issues/322)) all exist because Claude Code's `--output-format stream-json` intermittently **stops producing output and never exits**. Upstream status, checked 2026-05-29 and unchanged as of 0.35.4rc8:

* [anthropics/claude-code#39700](https://github.com/anthropics/claude-code/issues/39700) — "stream-json intermittently hangs — process stops producing output and never exits" — **CLOSED / NOT_PLANNED**
* [anthropics/claude-code#30333](https://github.com/anthropics/claude-code/issues/30333) — "ResultMessage never emitted in headless SDK mode" — **CLOSED / NOT_PLANNED**

Because upstream declined to fix both, this machinery is a **permanent mitigation**. Do not mistake it for retire-able dead code during a future cleanup, and do not remove the post-result limbo SIGTERM timeout — it also bounds the MCP-child and memory leaks tracked in [#590](https://github.com/littlebearapps/untether/issues/590) / [#592](https://github.com/littlebearapps/untether/issues/592).

**Constraint on [#527](https://github.com/littlebearapps/untether/issues/527) (unified predicate-based stall detector):** that refactor changes *when Untether warns*. It must not drop the kill-the-hung-process path (`_post_result_idle_watchdog` → SIGTERM → SIGKILL). **The detector decides messaging; the watchdog decides survival. They must stay decoupled.**

Full audit: [`docs/audits/2026-05-29-claude-workaround-retirement-audit.md`](../../../audits/2026-05-29-claude-workaround-retirement-audit.md).

### `Stream idle timeout - partial response` classification ([#438](https://github.com/littlebearapps/untether/issues/438))

When Claude fails with `API Error: Stream idle timeout - partial response received`, the runner's `_extract_error` now appends a one-line classification to the user-visible message:

* **Type-A (mid-generation)** — `num_turns ≥ 1 && duration_api_ms > 0`. Suggests raising `[watchdog] claude_stream_idle_timeout_ms` to ride out longer SSE silences (typical for opus 4.7 1M plan-mode generations).
* **Type-B (cold-start zero-byte stall)** — `num_turns ≤ 1 && duration_api_ms == 0`. Tells the user explicitly that raising the timeout will **not** help — it's an upstream Anthropic API outage, not a local watchdog miscalibration.

Auto-retry on Type-A is deferred to v0.35.4 pending upstream Anthropic stabilisation.

### `rate_limit_event` surfacing ([#349](https://github.com/littlebearapps/untether/issues/349) / [#790](https://github.com/littlebearapps/untether/issues/790))

Claude Code's `rate_limit_event` is a **quota-status snapshot** (`status` = `allowed` / `allowed_warning` / `rejected`, plus `resetsAt`, `rateLimitType` and per-window `unifiedWindows` utilization), sent whenever an API response moves the rounded usage — not only when throttled. Only a `rejected` snapshot that paid extra usage isn't covering is treated as a throttle: the runner renders `⏳ Rate limited until 17:30 AEST (~30 min)` from the stream's own `resetsAt` and latches `rate_limit_wait_until` (clamped to 24 h) so the stall detector treats the gap as an expected wait. `allowed` heartbeats are stashed on `ClaudeStreamState.rate_limit_windows` and otherwise ignored; `allowed_warning` produces at most one `⚠️ … limit N% used` note per window. Before #790 the schema modelled a shape the CLI never sends, so every heartbeat decoded as "bare" and the [#657](https://github.com/littlebearapps/untether/issues/657) fallback showed a fake `~60s` throttle every few minutes; bare events now latch nothing. Full decision table: [stream-json cheatsheet](stream-json-cheatsheet.md#rate_limit_event).

`ClaudeStreamState.rate_limit_total_s` accumulates throttle time (extension-only for repeats against one deadline); structured `claude.rate_limit_event` logs `status`, `rate_limit_type`, `resets_at`, `retry_after_s`, `retry_after_source` (`resets_at` / `retry_after_ms` / `reset_ts` / `result_error` / `action_required` / `default` / `bare` / `stale` / `covered_by_overage`), `count` and `cumulative_s`.

### `system/api_retry` back-offs ([#792](https://github.com/littlebearapps/untether/issues/792))

When an API call fails with a retryable error (429/529/5xx/connection), Claude Code emits `system/api_retry` with `attempt`, `max_retries`, `retry_delay_ms`, `error_status` and an `error` category before backing off. The runner renders one updating note per retry sequence (`🔁 API error 529 (overloaded) — retrying in 8s (attempt 2/10)`) and latches `api_retry_wait_until` so the bridge's stall monitor treats the back-off as an expected wait (`awaiting_api_retry()`, reason `api_retry_waiting`) rather than a hang. `claude.api_retry` logs at INFO, WARN on the final attempt. Shapes in the [stream-json cheatsheet](stream-json-cheatsheet.md).

### Safeguard stops ([#814](https://github.com/littlebearapps/untether/issues/814))

Anthropic's safeguards can stop a response (HTTP 200, `stop_reason: "refusal"`). Claude Code then either re-runs it once on the same model (unless `CLAUDE_CODE_DISABLE_REFUSAL_RETRY` is set), switches to a fallback model (`switchModelsOnFlag`), or ends the turn. Before 0.35.5rc14 none of this was visible: the assistant frame's `stop_reason` wasn't decoded and `system/informational` was dropped. CLI facts: [`docs/findings/2026-09-29-claude-rc14-cli-surface.md`](../../../findings/2026-09-29-claude-rc14-cli-surface.md) §B.

**Signals** (shapes in the [stream-json cheatsheet](stream-json-cheatsheet.md#safeguard-stops-and-model-fallback-814)): a main-thread assistant frame with `message.stop_reason == "refusal"` (+ `stop_details.category`), deduped by message id; the `system/informational` notice `"<Model>'s safeguards stopped the response above · continuing once with that noted"` (outcome `retried`); `system/model_refusal_fallback` (outcome `switched`; `scope: "local"` = a subagent fell back and the session model is unchanged); `system/model_refusal_no_fallback` (outcome `not_retried`). A refused frame with no CLI reaction by the turn's result resolves to `retried` if more output followed, else `not_retried`. The refusal frame and the CLI's reaction to it feed one per-turn tally, so a paired stop counts once. System subtypes dispatch through `_SYSTEM_SUBTYPE_HANDLERS` in `runners/claude.py`.

**Rendering.** One updating progress row per turn (`claude.safeguard.<turn>`): `🛡️ <model> safeguards stopped a response · retried once | switched to <model> | not retried (×N)`, titled with the model named by the event, never a hard-coded one. The row is `ok=True`; a stop never marks the run as an error. The tally rides on `usage["safeguard"]`, so it reaches both the run's `CompletedEvent` and a live `TurnEvent(completed)`. The bridge adds a footer line (`🛡️ safeguards stopped N response(s) · <outcome>`, placed on the last chunk of a split final like the other footer lines, #770) and, the first time a session sees a stop, one `💡` hint link: [real-time cyber safeguards](https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude) for category `cyber`, else [automatic model fallback](https://code.claude.com/docs/en/model-config#automatic-model-fallback). A `not_retried` stop with no answer gets an explanatory body instead of an empty final. A stopped wake turn is never folded into the #777 status message.

**Related banners.** `system/model_fallback` (overloaded / model not found — not a safeguard stop) renders `↪️ Switched model <a> → <b> (<trigger>)` and logs `claude.model_fallback`. Other `informational` banners at level `warning` / `notice` render a generic `⚠️` / `ℹ️` row; `info` / `suggestion` are log-only (`claude.informational`, DEBUG) until the level mix has been audited.

Logs: `claude.safeguard_stop` (INFO, once per resolved stop: `model`, `source` = `stop_reason|informational|model_refusal_fallback|model_refusal_no_fallback`, `category`, `outcome`, `fallback_model`, `turn`, `turn_count`, `session_count`), `claude.model_fallback`, `claude.informational`. A notice that lands after the turn's result (live sessions) is still counted and logged, but no row is drawn. The live check is opportunistic only: never provoke a refusal on purpose.

### Per-session background-task tracking ([#346](https://github.com/littlebearapps/untether/issues/346) / [#347](https://github.com/littlebearapps/untether/issues/347) / [#776](https://github.com/littlebearapps/untether/issues/776))

Claude Code can arm long-running work and end its turn while the work continues: `Monitor`, `Bash run_in_background=true`, background `Agent`/`Task` (the default for subagents), `ScheduleWakeup`, `RemoteTrigger`.

**Native task map (0.35.5rc11+).** Claude Code reports its own background-task lifecycle on stream-json (`system/task_started`, `task_progress`, `task_updated`, `task_notification`, `background_tasks_changed`; shapes in the [stream-json cheatsheet](stream-json-cheatsheet.md)). `translate_claude_event` folds them into `ClaudeStreamState.tasks` (`ClaudeTask` by `task_id`). A task *holds the live session open* (`ClaudeTask.holds_session`) iff it is `is_backgrounded` and its status is `running`/`pending`. That includes a subagent's own backgrounded task (`owned_by_subagent`), which outlives the agent that started it; before [#801](https://github.com/littlebearapps/untether/issues/801) the idle close killed one mid-run. A subagent's foreground tools (`is_backgrounded=false`) don't count. `task_updated.patch.status` / `task_notification.status` end a task. The `background_tasks_changed` snapshot is the parent's own list, so it reconciles only top-level tasks (`is_live_background`: backgrounded, not `owned_by_subagent`), and wake-turn attribution (#785) likewise uses only top-level tasks. Registration and end are logged as `claude.task.registered` / `claude.task.ended` ([#662](https://github.com/littlebearapps/untether/issues/662)).

**Resumed tasks (0.35.5rc13+).** When Claude sends a finished background agent back to work (e.g. `SendMessage` — "send it back to re-check"), the CLI reuses the **same** `task_id` and emits a fresh snapshot listing it plus a new `task_started`. That task is *revived*: its status goes back to `running`, `ended_at` is cleared, `started_at` resets to the revival and `ClaudeTask.revived_count` increments, logged as `claude.task.revived` (INFO for background tasks; `prior_status`, `ended_ago_s`, `source`). `task_started` and a `task_updated` patch with a live status revive at once. A snapshot that lists an ended id revives it only if the task ended at least 5 s earlier; a listing inside that window overlaps the end events and is ignored (`claude.task.snapshot_revive_skipped`, DEBUG). Likewise, a snapshot that omits a task revived under 5 s ago predates the revival and does not end it (`claude.task.snapshot_end_deferred`, DEBUG); the resumed run's own `task_updated` ends it as usual. `task_progress` never revives a task, because it carries no status and a late one must not hold the session open. Revival also clears the task from the #785 announced set, and a turn completing while the task is live again does not mark it announced, so the resumed run's finish opens a normal `task_finished` wake turn. Before this fix the task stayed terminal, `live_tasks` read 0, and the idle close SIGINT'd the resumed agent partway through its work ([#801](https://github.com/littlebearapps/untether/issues/801)).

Once the CLI emits any task event, the native map is authoritative for Monitor / Bash-bg / Agent-bg liveness. The older tool_use handles (`live_monitors`, `live_bg_bashes`, `live_bg_agents` with their bounded age-outs, [#374](https://github.com/littlebearapps/untether/issues/374) / [#573](https://github.com/littlebearapps/untether/issues/573) / [#646](https://github.com/littlebearapps/untether/issues/646)) remain only as a fallback for CLIs that never emit task events. `ScheduleWakeup` and `RemoteTrigger` emit no task events, so they keep their handles, and a pending ScheduleWakeup is tracked from its confirmation text ("scheduled for … (in 94s)") plus a 60 s grace. `has_live_background_work()`, `session_live_bg_count()` and the `background_task_summary()` footer all read one counting function, so they can't disagree.

### Live sessions ([#776](https://github.com/littlebearapps/untether/issues/776))

In control-channel mode (a permission mode is set) the Claude CLI keeps running after a `result`: a finished background task, each Monitor line and a firing ScheduleWakeup each start a new turn by themselves, and a user line written to stdin while idle runs as another turn. Before 0.35.5rc11 Untether stopped reading at the first `result` and closed stdin, so those turns ran invisibly (or died — closing stdin stops background work), and SIGTERM/quarantine later sent the next follow-up to a fresh session. Probe evidence: [`docs/findings/2026-09-27-claude-live-session-probes.md`](../../../findings/2026-09-27-claude-live-session-probes.md).

**Stream.** The run is still `StartedEvent → ActionEvent* → CompletedEvent` (turn 1, the user's message). The runner keeps reading; every later turn is a `TurnEvent(started) → ActionEvent* → TurnEvent(completed)` segment with a `reason`:

| reason | trigger | Telegram header |
|---|---|---|
| `task_finished` | a `task_notification` preceded the turn | 🔔 Background task finished — <task> |
| `monitor_event` | a Monitor task is live, no notification | 📡 Monitor — <monitor> (sent silently) |
| `scheduled_wakeup` | `command_lifecycle(started)` with an unknown uuid | ⏰ Scheduled wake-up |
| `followup` | `command_lifecycle.command_uuid` matches a line Untether injected | none — a normal reply under the follow-up |
| `hook_rewake` | an `asyncRewake` hook exited 2 while idle (or the result's `origin.kind` is `task-notification` after one did) | 🪝 Hook feedback — <hook event> (always pushed, never folded) |

Background subagent events (tagged `parent_tool_use_id`) arriving while the parent is idle do not open a turn. Only a top-level background task's `task_notification` attributes a turn — a subagent's own task (`owned_by_subagent`, or not `is_backgrounded`) is ignored for labels (`claude.turn.notification_ignored`), and the registered task description is preferred over the notification summary. The CLI often starts the wake turn on a background agent's result *before* any task event names it, so it opens `unknown`: if the task ends during that turn, the turn completes as `task_finished` for it (`claude.turn.retro_attributed`; the bridge delivers the real header); if it ends within 30 s after, it is paired with that turn (`claude.turn.task_end_paired`). Either way the task's own notification turn that follows carries `detail.already_announced` and is delivered without a push — one buzz per finish ([#785](https://github.com/littlebearapps/untether/issues/785)).

**Delivery.** `FollowupTurnRouter` (bridge) gives each turn a fresh tracker, a progress message only if the turn runs >5 s, uses a tool or raises an approval (approval/plan/question keyboards attach to it), then a new final with the header, replying to the message whose turn launched the task it reports on — each `ClaudeTask` records its `origin_turn`, a wake turn's `TurnEvent.detail` carries `task_ids` + `origin_turn`, and the router maps turns to the message each answered (the run's prompt, or an injected follow-up's own message; unknown → the run's prompt; a retro-attributed `unknown` turn is re-anchored before its final is sent) ([#795](https://github.com/littlebearapps/untether/issues/795)); outbox files are delivered per turn; turn messages are aliases of the live run in `running_tasks` so replies and `/cancel` reach it. `/cancel` (or `/new`) of an in-flight follow-up or wake turn renders it like a first-turn cancel (`cancelled · claude · Ns`, log `live_turn.cancelled turn= reason=cancel`), not as an error; a turn whose result carries `terminal_reason` `aborted_streaming` / `aborted_tools` is rendered the same way, with its cost delta, budget check and `runner.completed` still accounted first. Genuine process loss (crash, cap, close grace, lifecycle-initiated closes) keeps the error final, with `close_reason=` on `live_turn.interrupted` ([#806](https://github.com/littlebearapps/untether/issues/806)).

**Background status ([#777](https://github.com/littlebearapps/untether/issues/777)).** Rendered by `background_status.py` over the native task map — no extra event parsing. Pre-result, the run's `ProgressEdits.background_provider` appends a `⏳ background (N)` block (`live_shown()`: every live task that holds the session open — #801 `holds_session` — except a subagent's own task while the Agent that spawned it is listed, linked via the subagent tool_use's `parent_tool_use_id` → `ClaudeTask.owner_tool_use_id`; once that agent ends, or the owner is unknown, the task gets its own row, so the panel never shows nothing while the session is held; agent rows carry elapsed · `task_progress` tokens · tool uses · current step from the progress `description`; shell/Monitor rows carry elapsed; descriptions are markdown-escaped) and the heartbeat tick repaints it. Post-result, `BackgroundStatusManager.after_turn()` runs after the run's answer and after each later turn: with live background tasks and no active status message it sends one (`notify=False`, plain text, registered in `active_progress.json`), which a 1 s poll edits at most every 30 s — sooner (≥2 s spacing) when the task set or a status changes — and finalises when the set empties. The run's end (`/cancel`, `/new`, idle/max-hold/cap close, drain, process death) finalises it with the close reason; rows still live become ⏹️ stopped. `[progress] show_background_tasks` / `background_tasks_max_rows`. Logs: `background_status.opened|finalised`.

**Wake-ack consolidation ([#785](https://github.com/littlebearapps/untether/issues/785) part 2).** With `[progress] consolidate_wake_turns` (default on) and a status message present, `_deliver_final` asks `wake_fold_decision()` before sending a wake turn's final: reasons `task_finished` / `unknown` / `scheduled_wakeup` / `monitor_event`, `ok`, no non-note action in the turn (tools, approvals, questions; thinking notes don't count and don't open a progress message), answer ≤ `FOLD_MAX_CHARS` (300) → the answer is folded, one line, in full, under the attributed task's row (`TurnEvent.detail.task_ids`) or as a 💬 note, and the status message is edited instead (accounting — cost delta, `runner.completed`, stats — still runs; a budget/outlier notice forces a normal delivery). Content decides first because the report turn often opens `unknown` before the last task's end lands; at completion a turn that left no background work running is `last_task` and breaks out (pushed) when it is a new `task_finished` (not already announced), or a `task_finished`/`unknown` turn in a batch that hasn't pushed yet; a restatement or an unnamed `unknown` no-op after the batch has pushed ("nothing new since the report") folds into the finalised status message instead, as does a late `scheduled_wakeup` no-op. When the report itself raced the task's end and folded, the restatement carries the batch's push and the earlier note is filed under the task's row. If every wake turn of a batch folded, then once its status message finalises and the session has idled 5 s (or the run ends normally), one short pushed notice (`✅ all N background tasks done`) replies to the launching prompt (`background_status.quiet_notice`). Any wake turn that is delivered as its own message while its batch has not pushed yet always pushes (`live_turn.push_promoted`), even when flagged `already_announced` — that flag can come from a finish that was only folded, or from pairing the task's end with an unrelated `unknown` ack turn that completed within 30 s before it. Up to three read-only result-collection calls (`Read` / `Glob` / `Grep`, legacy `TaskOutput`; `COLLECTION_FOLD_MAX`) don't make an ack "tools" — since CLI 2.1.277 the model `Read`s a finished task's `output_file` instead of calling `TaskOutput`. An unattributed ack note is keyed by its turn and claimed only by the task whose end was paired with that turn, never by "the latest note" ([#813](https://github.com/littlebearapps/untether/issues/813)). Off → rc12 per-turn delivery. Logs: `live_turn.fold_decision` (`decision=fold|tools|long_answer|last_task|error|not_wake`, `folded`), `background_status.folded`.

**Follow-ups.** A queued resume job for a session whose process is live is written into that process (`live_followup.inject_live_followup` → `inject_when_idle` → `write_user_message`), queue semantics: it waits until the current turn has ended (a mid-turn write would be folded into the running turn — that is steer, [#775](https://github.com/littlebearapps/untether/issues/775)). `ThreadScheduler` offers queued jobs to the injector while the live run is in flight. No live process, a closing one, or another engine → the unchanged `--resume` path.

**Steer ([#775](https://github.com/littlebearapps/untether/issues/775)).** With follow-up mode `steer` (`/steer <text>`, bare `/steer`, `/config` → Follow-up, `[transports.telegram] followup_mode`), the Telegram loop writes a plain-text/voice prompt into the live session *immediately* (`telegram/steer.maybe_steer` → `steer_into_session` → `write_user_message`, fresh `command_uuid`, reply anchor registered) instead of queueing it. Probed on CLI 2.1.284: the CLI emits `command_lifecycle{queued}` at the write; mid-tool, `command_lifecycle{started}` follows the running tool's result **while the turn is still open** and the line is folded into that turn (one result) — `translate_claude_event` turns that into a `↪️ steer received: …` note (`detail.absorbed_command_uuid`, log `claude.live_session.injected_absorbed`), clears the line's awaiting marker and the bridge drops its anchor; after the turn's last tool call, `started` arrives after the `result` and the line runs as the next turn (`reason=followup`, replying to the steer). `--replay-user-messages` is **not** needed (and not passed): `command_lifecycle` carries our uuid either way. **Race guard:** `steer_into_session` checks and writes under `LiveSession.lock`; the window is closed by `close_live_session` (`closing`) and by `close_steer_window` (`steer_closed`, set by the bridge the moment `/cancel`/`/new` hits an active turn), so a steer either lands before the close or falls back to the queue path — never into a pipe that is closing. An idle session whose chat options changed also falls back (the queue path restarts it with the new options). Files, media groups, forwards and commands always queue; a pending AskUserQuestion wins; other engines, legacy mode and `live_sessions = false` fall back with a one-line notice (`steer.fallback`). Logs: `steer.written`, `steer.fallback`, `claude.live_session.steered`, `claude.live_session.steer_window_closed`, `live_followup.anchor_absorbed`.

**Async hooks ([#812](https://github.com/littlebearapps/untether/issues/812), 0.35.5rc14).** A command hook with `async: true` or `asyncRewake: true` runs in the background after the turn's result. An `asyncRewake` hook that exits 2 wakes the model with its stderr (for example the security-guidance plugin's commit review), but the CLI **drops** a rewake that fires after stdin has closed, so an idle close used to lose those findings silently. CLI facts: [`docs/findings/2026-09-29-claude-rc14-cli-surface.md`](../../../findings/2026-09-29-claude-rc14-cli-surface.md) §A.

- *Seeing hooks.* With `--include-hook-events` (see "Subprocess invocation") the CLI emits `system/hook_started` / `hook_progress` / `hook_response`. The runner pairs `hook_started` with `hook_response` by `hook_id` (at most 256 pending; `SessionStart` / `Setup` never hold). Hook frames produce no Untether events and never touch progress rows or stall timers. In `runner.py` they don't overwrite `last_event_type` (so the #470 post-result check and auto-continue still see `result`), but they do count as liveness; their ring label is `hook:<subtype>`. `session.summary` carries `hooks_started`.
- *The hold.* `has_pending_async_hooks()` is a sibling predicate OR'd into the lifecycle's live-work check. It is deliberately not part of the task map, so hooks never appear in footers, the #777 panel, the #592 cap or `_is_clean_idle`. While an idle session has a hook still pending, stdin stays open (`claude.hook.pending_hold`, once per hold). The hold is bounded by `[watchdog] async_hook_max_hold` (default 630 s = the CLI's 600 s `asyncRewake` timeout + its 30 s exit wait; range 0–3600), counted from the *newest* unpaired hook's `hook_started` — Untether can't tell which unpaired hook is the running one, so whichever it is gets its full bound. Past that every unpaired hook expires together with one `claude.hook.hold_expired` WARN per hold (`live_hook_processes` = hook processes still alive, `pending_hooks` = unpaired candidates, distinct `hook_events`, `held_s`, `max_hold_s`), and the hold ends even if a hook process is still alive.
- *Finished hooks release early.* The CLI withholds a plain `async` hook's `hook_response` until the next turn or stdin close, even though the process exited long ago, so an unpaired `hook_started` isn't proof the hook is running. On each idle tick the lifecycle counts the CLI's live **hook processes** (`proc_diag.cli_children()` → `hook_evidence_children()`): any direct child of the CLI except Bash-tool shells; children that started more than 5 s (`HOOK_START_SLACK_S`) before the oldest unpaired hook's `hook_started` (the CLI emits that frame and only then spawns the hook, so no unpaired hook's process can be older; start times and hook frames are compared on `hook_clock()`, which keeps counting through system sleep); and children **in the CLI's own process group** that are in the session baseline (the CLI's children right after `system/init`, recorded once by `capture_cli_baseline()` as pid + start time — the MCP servers) or whose argv looks like an MCP or language server. The process-group test matters because the CLI spawns every command hook `detached` (its own process group; `test_claude_cli_schema_drift.py` checks this): a `UserPromptSubmit` hook still running at `system/init`, a baselined PID reused by a later process, or a hook whose argv happens to say `mcp` is never exempt, while an MCP server behind a non-exec'ing `sh -c` is. A `/hooks/` argv token counts, and so does a child whose argv, start time or group can't be read. The `-c` wrapper alone isn't enough: every command hook is spawned as `/bin/sh -c <command>`, but bash (macOS `/bin/sh`) and zsh exec a single command, so the security-guidance hook runs on a Mac as `bash …/sg-python.sh …` with no shell left (Linux dash keeps it). Zombies are skipped. On Linux the children come from `/proc/<pid>/task/*/children`, with a /proc parent-pid scan when the kernel has no such file; the macOS backend reads one `ps -axo pid=,ppid=,pgid=,stat=,etime=,lstart=,command=` listing. A shell or `env` wrapper that forks the CLI instead of exec'ing it is resolved to the CLI (Untether's own `env -i` wrapper execs). The decision is all-or-nothing: while **any** hook process is alive, every unpaired hook stays pending. Hook frames carry no pid, and several hooks start in the same millisecond, so a process can't be tied to a particular hook (0.35.5rc14's first per-hook binding released a still-running `asyncRewake` hook this way, and its rewake was lost). Once no hook process has been alive for 1 s, every unpaired hook has finished and is released together: `claude.hook.hold_released reason=no_hook_process`. A released hook's late response still pairs. An unreadable process table keeps the bounded hold. `close_live_session` re-checks the same way, so `/cancel` doesn't report finished hooks as killed.
- *The rewake turn.* A `hook_response` with `outcome: "error"`, `exit_code: 2` while idle arms a 10 s hint; the next turn opens as `hook_rewake` (`claude.turn.hook_rewake attributed=open`). A turn already open when the signal lands is confirmed at its result by `origin.kind == "task-notification"` (`attributed=result`). The bridge delivers it as its own **pushed** message headed `🪝 Hook feedback — <event>` (e.g. `Stop`); it is never folded into the status message, because its content is usually security findings.
- *When the bound is hit.* A close with a background hook still running gets a 35 s close grace instead of 15 s (the CLI's own 30 s `asyncRewake` exit wait + 5 s). The evidence is any of: unpaired hooks with a live hook process, a raw-argv `/hooks/` descendant scan (when hook events aren't available), or a live hook process alone (`claude.live_session.hook_process_at_close`) — inline hook commands have no `/hooks/` path. The close logs `claude.live_session.async_hook_killed` (WARN for automatic closes, INFO for `/cancel`, `/new`, drain and changed options) with `hook_count` = the live hook processes (capped by the candidates; the candidate count only when the process table is unreadable), `live_hook_processes`, the distinct `hook_events`, and the unpaired candidates' `hook_names` / `hook_ids` (marked as candidates in `note`). With no live hook process at the close nothing is logged or announced. Automatic closes also tell the user, never naming more hooks than were running: `⏳ Closing session — a background hook (Stop or UserPromptSubmit) was still running; its feedback wasn't delivered.` (two or more: `N background hooks (…) were still running; their feedback wasn't delivered.`).

Kill switch: `[watchdog] hold_for_async_hooks = false` passes no flag and holds nothing (pre-rc14 behaviour).

**Plan approvals are turn-scoped ([#383](https://github.com/littlebearapps/untether/issues/383), 0.35.5rc15).** Approving `ExitPlanMode` (or a plan-gated diff-preview tool) adds the session to `_PLAN_EXIT_APPROVED`, which skips the opt-in diff-preview gate for the rest of that reply (#283/#369). Until rc15 it was cleared only at process end, so under live sessions one approval covered every follow-up and wake turn for up to 4 h. `_open_followup_turn` now clears it at **every** turn open, whatever the reason (`claude.plan_approval.cleared reason=turn_boundary`); a mid-turn steer fold (`_absorb_injected`) is not a boundary. An unconsumed post-outline approval (`_DISCUSS_APPROVED`, the `da:` **✅ Approve Plan** button) survives exactly one boundary — `_DISCUSS_CARRY` records the first one (`claude.plan_approval.carried`) and the second clears it — so "outline → Approve Plan → go ahead" needs one tap; consuming it discards both. The ExitPlanMode approval shows **✅ Approve Plan** and, in plan / plan-auto chats, the caption *Approving lets Claude carry out this plan without further prompts.* (prompting-mode chats: *Approving ends planning; Claude still asks before each action.*). The *Plan mode resumes when this reply ends.* clause is added only when it is true — with live sessions off, every message respawns with `--permission-mode plan`.

**Lifecycle** (`_live_session_lifecycle`, per run). While idle: nothing live for `post_result_limbo_grace` (60 s) → close stdin; background work still live `post_result_bg_max_hold` (1800 s, re-armed each turn) after the last turn → notice + close; `live_session_max_s` (4 h) from spawn → notice + close; a pending approval/ask or an injected line not yet started pauses the timers. Closing stdin makes the CLI stop its tasks and exit rc=0 (graceful — nothing quarantined); only if it hasn't exited 15 s later (35 s when a background hook is evident, #812) does Untether log a process snapshot (`claude.live_session.close_grace_expired`: state, wchan, CPU across the grace, FDs, TCP, children with redacted cmdlines) and escalate — SIGINT (the CLI's Ctrl-C path), then SIGTERM/SIGKILL 5 s later. The session is quarantined (`forced_teardown_after_result`) only when the close was not clean: a close of an idle turn with no live background work leaves a complete transcript, so it is not quarantined even if the CLI needed a signal ([#791](https://github.com/littlebearapps/untether/issues/791)); #631 empty-resume recovery remains the backstop. `/cancel`, `/new` and drain/restart close idle live sessions the same way with a notice naming the stopped tasks; a turn in progress is still killed by `/cancel`.

**Resume guard.** On `--resume` of a session whose previous process ended with background work still live, the CLI replays `task_notification{stopped}` and answers it with a 0-turn result before running the real turn. That result is absorbed (`claude.resume_guard.absorbed`), not delivered — no empty-resume quarantine or resend.

**Cost.** `total_cost_usd` is cumulative per session (also across `--resume`); the bridge records per-turn deltas via `session_costs.json` ([#778](https://github.com/littlebearapps/untether/issues/778)). A turn's delta includes spend by background subagents during it.

**Kill switch:** `[watchdog] live_sessions = false` restores the pre-rc11 "stop at the first result" behaviour. Legacy `-p` mode (no permission mode) is always single-result.

Logs: `claude.turn.started|completed`, `live_turn.started|interrupted`, `claude.live_session.stdin_closed` (reason `idle_no_tasks|max_hold|abs_cap|cancel|drain`), `claude.live_session.injected|inject_unavailable|injected_turn_timeout|followup_not_run|close_grace_expired|exited_after_sigint|forced_teardown`, `cost.turn_delta`, `cost.baseline_unknown`; `claude.hook.pending_hold|hold_released|hold_expired|rewake_signal|cancelled`, `claude.turn.hook_rewake`, `claude.live_session.async_hook_killed`, `live_turn.cancelled`; `session.summary` carries `followup_turns`, `hooks_started` and `peak_live_idle_seconds` (live-idle holds are kept out of `peak_idle_seconds`, and the stall monitor stays silent while live-idle — [#787](https://github.com/littlebearapps/untether/issues/787)). `peak_live_idle_seconds` measures only the idle gaps between turns: the run feeds each `TurnEvent` boundary to its `ProgressEdits`, so an active follow-up or wake turn no longer counts as idle ([#811](https://github.com/littlebearapps/untether/issues/811)).

---

## Code changes (by file)

### 1) New file: `src/untether/runners/claude.py`

#### Backend export

Expose a module-level `BACKEND = EngineBackend(...)` (from `untether.backends`).
Untether auto-discovers runners by importing `untether.runners.*` and looking for
`BACKEND`.

`BACKEND` should provide:

* Engine id: `"claude"`
* `install_cmd`:
  * Install command for `claude` (used by onboarding when missing on PATH).
  * Error message should include official install options and “run `claude` once to authenticate”.

    * Install methods include install scripts, Homebrew, and npm. ([Claude Code][4])
    * Agent SDK / CLI can use Claude Code authentication from running `claude`, or API key auth. ([Claude Code][5])

* `build_runner()` should parse `[claude]` config and instantiate `ClaudeRunner`.

#### Runner implementation

Implement a new `Runner`:

#### Public API

* `engine: EngineId = "claude"`
* `format_resume(token) -> str`: returns `` `claude --resume {token}` ``
* `extract_resume(text) -> ResumeToken | None`: parse last match of `--resume/-r`
* `is_resume_line(line) -> bool`: matches the above patterns
* `run(prompt, resume)` async generator of `UntetherEvent`

#### Subprocess invocation

Core invocation (non-interactive):

* `claude -p --output-format stream-json --input-format stream-json --verbose` ([Claude Code][1])
  * `--verbose` overrides config and is required for full stream-json output.
  * `--input-format stream-json` enables JSON input on stdin.

Core invocation (permission mode):

* `claude --output-format stream-json --input-format stream-json --verbose --permission-mode <mode> --permission-prompt-tool stdio`
  * No `-p` flag — prompt is sent via stdin as a JSON user message.
  * `--permission-prompt-tool stdio` enables the bidirectional control channel.
* `--include-hook-events` (0.35.5rc14+, [#812](https://github.com/littlebearapps/untether/issues/812)) is added when `[watchdog] hold_for_async_hooks` is on **and** the installed CLI lists it in `claude --help`. The help probe runs once per binary path + mtime (a CLI upgrade re-probes) and is logged as `claude.hook_events.probe` (`supported`, `probe_ok`); an unresolvable binary or a failed probe means the flag is not passed.

Resume:

* add `--resume <session_id>` if resuming. ([Claude Code][1])

Model:

* add `--model <name>` if configured. ([Claude Code][1])

Effort (reasoning depth):

* add `--effort <level>` if a reasoning override is set (low/medium/high/xhigh/max).

Permissions:

* add `--allowedTools "<rules>"` if configured. ([Claude Code][1])
* add `--dangerously-skip-permissions` only if explicitly enabled (high risk; document clearly).

Prompt passing:

* Pass the prompt as the final positional argument after `--` (CLI expects `prompt` as an argument). This also protects prompts that begin with `-`. ([Claude Code][1])

Other flags:

* Claude Code exposes more CLI flags, but Untether does not surface them in config.

#### Stream parsing

In stream-json mode, Claude Code emits newline-delimited JSON objects. ([Claude Code][1])

Per the official Agent SDK TypeScript reference, message types include:

* `system` with `subtype: 'init'` and fields like `session_id`, `cwd`, `tools`, `model`, `permissionMode`, `output_style`. ([Claude Code][3])
* `assistant` / `user` messages with Anthropic SDK message objects. ([Claude Code][3])
* final `result` message with:

  * `subtype: 'success'` or `'error'`,
  * `is_error`, `result` (string on success),
  * `usage`, `total_cost_usd`,
  * `duration_ms`, `duration_api_ms`, `num_turns`,
  * `structured_output` (optional). ([Claude Code][3])

  Note: upstream Claude Code CLI may also emit `error`, `permission_denials`, and
  `modelUsage` fields, but these are **not captured** by Untether's
  `StreamResultMessage` schema (msgspec silently ignores unknown fields).

Untether should:

* Parse each line as JSON; on decode error emit a warning ActionEvent (like CodexRunner does) and continue.
* Prefer stdout for JSON; log stderr separately (do not merge).
* Treat unknown top-level fields (e.g., `parent_tool_use_id`) as optional metadata and ignore them unless needed.

#### Mapping to Untether events

**StartedEvent**

* Emit upon first `system/init` message:

  * `resume = ResumeToken(engine="claude", value=session_id)`
    (treat `session_id` as opaque; do not validate as UUID)
  * `title = model` (or user-specified config title; default `"claude"`)
  * `meta` should include `cwd`, `model`, `tools`, `permissionMode`, `output_style` for debugging. `model` and `permissionMode` are used for the `🏷` footer line on final messages.

**Action events (progress)**
The core useful progress comes from tool usage.

Claude Code tools list is documented (Bash/Edit/Write/WebSearch/WebFetch/TodoWrite/Task/etc.). ([Claude Code][2])

Strategy:

* When you see an **assistant message** with a content block `type: "tool_use"`:

  * Emit `ActionEvent(phase="started")` with:

    * `action.id = tool_use.id`
    * `action.kind` based on tool name (complete mapping):

      * `Bash` → `command`
      * `Edit`/`Write`/`NotebookEdit` → `file_change` (best-effort path extraction)
      * `Read` → `tool`
      * `Glob`/`Grep` → `tool`
      * `WebSearch`/`WebFetch` → `web_search`
      * `TodoWrite`/`TodoRead` → `note`
      * `AskUserQuestion` → `note`
      * `Task`/`Agent` → `tool`
      * `KillShell` → `command`
      * otherwise → `tool`
    * `action.title`:

      * Bash: use `input.command` if present
      * Read/Write/Edit/NotebookEdit: use file path (best-effort; field may be `file_path` or `path`)
      * Glob/Grep: use pattern
      * WebSearch: use query
      * WebFetch: use URL
      * TodoWrite/TodoRead: short summary (e.g., “update todos”)
      * AskUserQuestion: short summary (e.g., “ask user”)
      * otherwise: tool name
    * `detail` includes a compacted copy of input (or a safe summary).

* When you see a **user message** with a content block `type: "tool_result"`:

  * Emit `ActionEvent(phase="completed")` for `tool_use_id`
  * `ok = not is_error`
  * `content` may be a string or an array of content blocks; normalize to a string for summaries
  * `detail` includes a small summary (char count / first line / “(truncated)”)

This mirrors CodexRunner’s “started → completed” item tracking and renders well in existing `UntetherProgressRenderer`.

**CompletedEvent**

* Emit on `result` message:

  * `ok = (is_error == false)` (treat `is_error` as authoritative; `subtype` is informational)
  * `answer = result` on success; on error, a concise message using `errors` and/or denials
  * `usage` attach:

    * `total_cost_usd`, `usage`, `modelUsage`, `duration_ms`, `duration_api_ms`, `num_turns` ([Claude Code][3])
  * Always include `resume` (same session_id).
* Emit exactly one completed event per run. After emitting it, ignore any
  trailing JSON lines (do not emit a second completion).
* We do not use an idle-timeout completion; completion is driven by Claude Code’s
  `result` event or process exit handling.

**Permission denials**
Because result includes `permission_denials`, optionally emit warning ActionEvent(s) *before* CompletedEvent (CompletedEvent must be final):

* kind: `warning`
* title: “permission denied: <tool_name>”
  This preserves the “warnings before started/completed” ordering principle Untether already tests for CodexRunner.

#### Session serialization / locks

Must match Untether runner contract:

* Lock key: `claude:<session_id>` (string) in a `WeakValueDictionary` of `anyio.Lock`.
* When resuming:

  * acquire lock before spawning subprocess.
* When starting a new session:

  * you don’t know session_id until `system/init`, so:

    * spawn process,
    * wait until the **first** `system/init`,
    * acquire lock for that session id **before** yielding StartedEvent,
    * then continue yielding.

This mirrors CodexRunner’s correct behavior and ensures “new run + resume run” serialize once the session is known.
Assumption: Claude Code emits a single `system/init` per run. If multiple `init`
events arrive, ignore the subsequent ones (do not attempt to re-lock).

#### Cancellation / termination

Reuse the existing subprocess lifecycle pattern (like `CodexRunner.manage_subprocess`):

* Kill the process group on cancellation
* Drain stderr concurrently (log-only)
* Ensure locks release in `finally`

## Documentation updates

### README

Add a “Claude Code engine” section that covers:

* Installation (install script / brew / npm). ([Claude Code][4])
* Authentication:

  * run `claude` once and follow prompts, or use API key auth (Agent SDK docs mention `ANTHROPIC_API_KEY`). ([Claude Code][5])
* Non-interactive permission caveat + how to configure:

  * settings allow/deny rules,
  * or `--allowedTools` / `[claude].allowed_tools`. ([Claude Code][2])
* Resume format: `` `claude --resume <id>` ``.

### `docs/developing.md`

Extend “Adding a Runner” with:

* “ClaudeRunner parses Agent SDK stream-json output”
* Mention key message types and the init/result messages.

---

## Test plan

Mirror the existing `CodexRunner` tests patterns.

### New tests: `tests/test_claude_runner.py`

1. **Contract & locking**

* `test_run_serializes_same_session` (stub `run_impl` like Codex tests)
* `test_run_allows_parallel_new_sessions`
* `test_run_serializes_new_session_after_session_is_known`:

  * Provide a fake `claude` executable in tmp_path that:

    * prints system/init with session_id,
    * then waits on a file gate,
    * a second invocation with `--resume` writes a marker file and exits,
    * assert the resume invocation doesn’t run until gate opens.

2. **Resume parsing**

* `format_resume` returns `claude --resume <id>`
* `extract_resume` handles both `--resume` and `-r`

3. **Translation / event ordering**

* Fake `claude` outputs:

  * system/init
  * assistant tool_use (Bash)
  * user tool_result
  * result success with `result: "ok"`
* Assert Untether yields:

  * StartedEvent
  * ActionEvent started
  * ActionEvent completed
  * CompletedEvent(ok=True, answer="ok")

4. **Failure modes**

* `result` subtype error with `errors: [...]`:

  * CompletedEvent(ok=False)
* permission_denials exist:

  * warning ActionEvent(s) emitted before CompletedEvent

5. **Cancellation**

* Stub `claude` that sleeps; ensure cancellation kills it (pattern already used for codex subprocess cancellation tests).

---

## Implementation checklist (v0.3.0)

* [x] Export `BACKEND = EngineBackend(...)` from `src/untether/runners/claude.py`.
* [x] Add `src/untether/runners/claude.py` implementing the `Runner` protocol.
* [x] Add tests + stub executable fixtures.
* [x] Update README and developing docs.
* [ ] Run full test suite before release.

---

If you want, I can also propose the exact **event-to-action mapping table** (tool → kind/title/detail rules) you should start with, based on Claude Code’s documented tool list (Bash/Edit/Write/WebSearch/etc.). ([Claude Code][2])

---

## Interactive enhancements (v0.4.0+)

### AskUserQuestion support

When Claude Code calls `AskUserQuestion`, the control request is intercepted and shown in Telegram. The question text is extracted from the tool input (supports both `{"question": "..."}` and `{"questions": [{"question": "..."}]}` formats).

Flow:
1. Claude Code emits `control_request` with `tool_name: "AskUserQuestion"`
2. Runner registers in `_PENDING_ASK_REQUESTS[request_id] = question_text`
3. Telegram shows the question with Approve/Deny buttons
4. User replies with text → `telegram/loop.py` intercepts via `get_pending_ask_request()`
5. `answer_ask_question()` sends `control_response(approved=False, denial_message="The user answered...")` — the answer is in the denial message so Claude Code reads it and continues

### Diff preview in tool approvals

When a tool requiring approval (Edit/Write/Bash) goes through the control request path, `_format_diff_preview()` generates a compact preview:
- **Edit**: shows removed (`-`) and added (`+`) lines (up to 4 each, truncated to 60 chars)
- **Write**: shows first 8 lines of new content
- **Bash**: shows the command prefixed with `$`

The preview is appended to the `warning_text` in the progress message. Only applies to tools that go through `ControlRequest` (not auto-approved tools).

### Cost tracking and budget

`runner_bridge.py` calls `_check_cost_budget()` after each `CompletedEvent` to compare run cost against configured budgets (`[cost_budget]` in `untether.toml`). Budget alerts are shown in the progress footer.

`cost_tracker.py` provides:
- `CostBudget` — per-run and daily budget thresholds with configurable warning percentage
- `CostAlert` — alert levels: info, warning, critical, exceeded
- `record_run_cost()` / `get_daily_cost()` — daily accumulation with midnight reset

### Session export

`commands/export.py` records session events during runs via `record_session_event()` and `record_session_usage()`. Up to 20 sessions are retained. `/export` outputs markdown; `/export json` outputs structured JSON.

[1]: https://code.claude.com/docs/en/headless "Run Claude Code programmatically - Claude Code Docs"
[2]: https://code.claude.com/docs/en/settings "Claude Code settings - Claude Code Docs"
[3]: https://code.claude.com/docs/en/sdk/sdk-typescript "Agent SDK reference - TypeScript - Claude Docs"
[4]: https://code.claude.com/docs/en/quickstart "Quickstart - Claude Code Docs"
[5]: https://platform.claude.com/docs/en/agent-sdk/quickstart "Quickstart - Claude Docs"

## See also

- [Error Reference](../../errors.md) — actionable hints for common engine errors
