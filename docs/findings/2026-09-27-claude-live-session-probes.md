# Claude Code live-session behaviour (stream-json) — probe findings

**Date:** 2026-09-27 · **CLI:** Claude Code 2.1.283 · **Host:** lba-1 · **Model:** haiku ·
**Mode:** `--input-format stream-json --output-format stream-json --verbose --permission-mode bypassPermissions`

Grounds [#776](https://github.com/littlebearapps/untether/issues/776) (rc11 live-session model) and
[#775](https://github.com/littlebearapps/untether/issues/775) / [#777](https://github.com/littlebearapps/untether/issues/777)
(rc12). Probe scripts: `docs/plans/v0.35.5-rc11/probes/` (local, gitignored). Each costs a few cents.
**Re-run them before building on any row below against a newer CLI.**

## Summary table

| # | Probe | Finding |
|---|---|---|
| F1 | `bg_bash_wake.py` | Background Bash completes after result #1. The CLI emits `task_updated{status:completed}`, `task_notification`, `system/init`, then runs **a new turn on its own** → result #2. Needs the process alive **and stdin open**. |
| F2 | `bg_agent_wake.py` | Same for a background `Agent`. Subagent events are interleaved on stdout (carry `parent_tool_use_id`). |
| F3 | `bg_bash_stdin_close_kills.py`, `resume_guard_probe.py` leg 1 | Closing stdin after result #1 → ~5s later `task_updated{status:killed}` + `task_notification{status:stopped}`, then clean exit **rc=0**. Closing stdin is a *graceful* teardown (the transcript records the stop), unlike SIGTERM. |
| F4 | `replay_probe.py` | Closing stdin with **no** background tasks → exit rc=0 in **0.6s**. |
| F5 | `steer_mid_tool.py` | A `user` line written **during a tool call** is folded into the **same turn** at the next tool boundary (one result). ⇒ writing mid-turn is *steer*, not *queue*. |
| F6 | `steer_post_last_tool.py`, `replay_probe.py` | A `user` line written after the turn's last tool call, or while idle, becomes a **new turn in the same process** (new result). |
| F7 | `replay_probe.py` | **`command_lifecycle`** (new top-level `type`) is emitted per input command: `{"type":"command_lifecycle","command_uuid":<uuid>,"state":"queued"\|"started"\|"completed"}`. `command_uuid` **equals the `uuid` field we put on the injected `user` line**. `completed` for command N arrives lazily (seen when command N+1 was queued). |
| F8 | `replay_probe.py` | With `--replay-user-messages`, each consumed user line is echoed as `{"type":"user","uuid":<our uuid>,"isReplay":true,...}` just before its turn's assistant output. |
| F9 | `wake_probe.py` | **`ScheduleWakeup` works in stream-json mode** and emits **no `task_*` event**. The wake is announced by `command_lifecycle{state:started}` (unknown `command_uuid`), then `system/init`, a turn, result #2. Requires stdin open. The tool_result text states the fire time ("scheduled for 19:15:00 (in 94s)"). |
| F10 | `monitor_probe.py` | `Monitor` registers as `task_started{task_type:"local_bash", is_backgrounded:true}`. **Every streamed line triggers its own wake turn and result** (3 ticks → results #2–#4) **with no per-tick `task_*` event** (just `system/init` + turn). The end of the stream emits `task_updated{completed}` + `task_notification{summary:"Monitor … stream ended"}`. Monitor has `timeout_ms` expiry. |
| F11 | `resume_guard_probe.py` leg 2 | **Path B reproduced.** `--resume` of a session whose bg task was killed: `task_notification{status:stopped, output_file:""}` **before** `system/init`, then a **0-turn result** (`num_turns=0`, `duration_api_ms=0`, `result:""`), then `system/init` and the **real** turn → result #2 with the real answer. |
| F12 | `resume_guard_probe.py`, staging journal | **`total_cost_usd` is cumulative per *session*, carried across `--resume`**, not per process. The path-B 0-turn result reported the previous process's total (0.0611); the real answer's result reported 0.0686. |

## F12 has a pre-existing consequence (not only a #776 concern)

`runner_bridge` records `result.total_cost_usd` as *the run's cost* (`record_run_cost`, budget, footer,
`cost.run_outlier`). Because the value is session-cumulative, every resumed run over-counts. Evidence,
staging `untether.service` journal, 2026-09-22..27, `runner.completed` for one session
(`7ff32810…`, project `untether`) on successive messages: `total_cost_usd` = 3.23 → 3.48 → 3.71 →
11.08 → 11.71 → 12.77. The daily total therefore sums running totals. Another session (`e68746e1…`):
18.81 then 28.15 for a follow-up that actually cost ~9.3.

## Event shapes (verbatim, uuid/session_id stripped)

```
system/background_tasks_changed {"tasks":[{"task_id","task_type":"local_bash"|"local_agent","description"}]}   # full snapshot; [] when empty
system/task_started  {"task_id","tool_use_id","description","is_backgrounded":true,"task_type":"local_bash"}
system/task_started  {"task_id","tool_use_id","description","subagent_type","is_backgrounded":true,"spawn_depth":1,"task_type":"local_agent","prompt"}
system/task_started  {"task_id","owned_by_subagent":true,"is_backgrounded":false,"task_type":"local_bash",...}   # subagent's own foreground tool — NOT background work
system/task_progress {"task_id","tool_use_id","description","subagent_type","usage":{"total_tokens","tool_uses","duration_ms"},"last_tool_name"}   # agents only (so far)
system/task_updated  {"task_id","patch":{"status":"completed"|"killed","end_time":<epoch_ms>}}
system/task_notification {"task_id","tool_use_id","status":"completed"|"stopped","output_file","summary","usage"?}
command_lifecycle    {"command_uuid","state":"queued"|"started"|"completed","uuid","session_id"}
user (replay)        {"type":"user","uuid":<injected uuid>,"isReplay":true,"message":{...},"timestamp"}
```

## Not yet probed

- `RemoteTrigger` (assumed like ScheduleWakeup: no task events).
- Whether `task_progress` ever fires for `local_bash`.
- Whether a `user` line written *while a wake turn is running* behaves like F5 (assumed yes).
- Concurrent wake + injected message ordering (CLI queue order vs Untether attribution — F7's
  `command_uuid` should make attribution exact regardless).
