---
question: >-
  For #829 (live-session background hold), how often does a background agent emit
  `task_progress`, what signals a background Bash leaves, does closing stdin stop background
  agents, and is a session resumable after the process-group SIGINT Untether sends?
date: 2026-09-30
sources:
  - local probes against Claude Code CLI 2.1.285 (haiku), lba-1
  - docs/findings/2026-09-27-claude-live-session-probes.md
confidence: high
---

# Claude Code background agents: activity signals, stdin EOF and SIGINT resumability — probe findings

**Date:** 2026-09-30 · **CLI:** Claude Code 2.1.285 · **Host:** lba-1 · **Model:** haiku (parent and
subagents) · **Spawn:** Untether's control-channel shape — `--input-format stream-json
--output-format stream-json --verbose --include-hook-events --permission-mode default
--permission-prompt-tool stdio` with the `initialize` handshake and an auto-allow `can_use_tool`
responder, PIPE stdin, `start_new_session=True`, signals via `os.killpg` (as `signal_pid_group`).
Cost control: `--safe-mode --strict-mcp-config`, subagents forced to haiku
(`CLAUDE_CODE_SUBAGENT_MODEL`). Throwaway cwd with 30 three-line text files.

Grounds [#829](https://github.com/littlebearapps/untether/issues/829) (plan 21, P0-a…d — the hard
gate for its design branches). Extends
[`2026-09-27-claude-live-session-probes.md`](2026-09-27-claude-live-session-probes.md) (F1–F12) and
answers two of its "not yet probed" items. Probe scripts: `p0lib.py`, `p0a1_agent_cadence.py`,
`p0ab.py`, `p0c_bg_bash_ticks.py`, `p0c2_bash_signals.py` (kept with the gitignored rc15 plan pack,
`docs/plans/v0.35.5-rc15/probes/`). **Total spend: US$0.48** (9 sessions + 6 one-line resumes).
**Re-run them before building on any row below against a newer CLI.**

## Summary table

| # | Probe | Finding |
|---|---|---|
| G1 | P0-a cadence (`p0a1`, 1 agent, 15 × `sleep 12` + Read, 229 s) | `task_progress` is **per action, not a timer**: one frame at the start of every subagent tool call (30 frames for 30 tool calls). `usage.total_tokens` and `usage.tool_uses` rose on **every** frame (0 of 29 consecutive pairs flat). Largest gap between frames = **14.5 s** (the 12 s foreground sleep + one API round trip). |
| G2 | P0-d (`p0c`, agent runs one foreground `sleep 150`) | **No `task_progress` at all during a long foreground tool.** One frame at the tool's start, then silence for 150 s, then the agent's reply. |
| G3 | P0-c / P0-d (`p0c`, `p0a1`) | A subagent's foreground Bash that runs longer than ~3 s is registered as `task_started{owned_by_subagent:true, is_backgrounded:false, task_type:"local_bash"}` **~3 s after the tool starts**, and ends with `task_notification{status:"completed"}` — **no `task_updated`**. Its `tool_use_id` is the subagent's Bash call, whose `parent_tool_use_id` is the owning Agent call (Untether's `tool_parents` → `owner_tool_use_id`). Short tools (Read) register nothing. |
| G4 | P0-c (`p0c`, `p0c2`) | **`local_bash` never emits `task_progress`**, and `output_file` is **not** on `task_started` or in the `background_tasks_changed` snapshot (only on the final `task_notification`). But the **Bash tool_result** says `Output is being written to: <tmp>/claude-<uid>/<cwd-slug>/<sid>/tasks/<task_id>.output`, and that file **grows while the command prints** (7 → 223 bytes over 55 s for `echo tick; sleep 2`) and stays at **0 bytes** for a silent `sleep`. The Bash-tool shell's `fd 1` points at the same file. |
| G5 | P0-c (`p0c2`) | Bash-tool shells are direct CLI children, `zsh -c source …/shell-snapshots/snapshot-zsh-….sh …` (matches `_is_tool_shell`), each in its own process group. CPU ticks of the ticking shell tree: 1 → 9 over 55 s (≈1 tick / 5 s); the silent `sleep` tree stays flat. CPU works as a discriminator but is noisier than the output file. |
| G6 | P0-a EOF (`p0a2`, 2 agents, stdin closed 15 s after result #1) | **Background agents are NOT stopped by stdin EOF** (unlike background Bash, F3). Both agents kept working for **98 s after EOF** (124 subagent frames), finished normally (`task_updated{completed}`), the CLI even ran **both wake turns** (results #2 and #3) with stdin closed, then exited **rc 0**. Nothing is killed until the agents are done. The resume afterwards was normal. |
| G7 | P0-b (`p0b1`…`p0b6`, 6 runs) | Group **SIGINT → exit rc 0 in 0.2–0.6 s** in all 6 runs (stdin closed or open; agent mid-foreground-tool, mid-generation, mid-API-call, parent mid-wake-turn). Agent tasks get **no** `task_updated{killed}` frame; a running owned-foreground tool gets `task_notification{status:"stopped"}`. |
| G8 | P0-b structural check (zero-token) | Parent transcripts: **no unmatched `tool_use`** in any run; subagent transcripts (`<sid>/subagents/*.jsonl`) also clean. A trailing user line appeared **only** when SIGINT hit mid-wake-turn (`p0b3`: the `<task-notification>` line the interrupted turn was answering) — the resume replayed it harmlessly. |
| G9 | P0-b resume (`claude -p --resume <sid> "In one line: what were you doing?"`) | **6 / 6 resumable**: `subtype:"success"`, `is_error:false`, `num_turns:1`, a correct one-line account of the interrupted work. No `error_during_execution`, no 0-turn result. |

## P0-b run table

| Run | Moment of the signal | stdin | rc | exit after SIGINT | Structural | Resume |
|---|---|---|---|---|---|---|
| `p0b1` | EOF, then SIGINT 15 s later (Untether's close path); agent mid `sleep 8` loop | closed | 0 | 0.21 s | clean | ✅ on topic |
| `p0b2` | agent mid-generation (600-word story, API streaming), parent idle | open | 0 | 0.62 s | clean | ✅ |
| `p0b3` | 0.3 s after an agent's `task_notification` — parent **mid wake turn** | open | 0 | 0.52 s | trailing `<task-notification>` user line (turn interrupted) | ✅ (replayed, then answered) |
| `p0b4` | EOF, then SIGINT 5 s later; agent in back-to-back Read API calls | closed | 0 | 0.37 s | clean | ✅ |
| `p0b5` | 2 agents mid foreground `sleep 8`, parent idle | open | 0 | 0.52 s | clean | ✅ |
| `p0b6` | repeat of `p0b1` | closed | 0 | 0.21 s | clean | ✅ |

Five of the six runs are the shape #829's branch B2 covers (the parent's turn closed when the signal
arrives); all five are clean. `p0b3` is outside B2 by construction (a turn was open) and still resumed.

## Decisions for plan 21 (#829)

| Plan row | Outcome | Decision |
|---|---|---|
| a: progress gaps ≪ 30 min during normal agent work | max gap 14.5 s (G1) | (A) activity-based hold viable as designed |
| d: frames on a timer with flat usage? | no — per action, usage always rising (G1) | **D1 = any `task_progress` frame counts** |
| d/c: no frames during a long foreground tool, but owned-foreground task frames exist | yes (G2, G3) | **keep A.2**: a live subagent-owned foreground task marks its owning agent active; its start and end stamp the owner |
| a: agents stop on EOF in N ≤ 30 s? | no — they run to completion (G6) | not B1 |
| b: post-SIGINT resumable on ≥ 5 runs, structural check clean | yes (G7–G9) | **B2**: an Untether-initiated close of an idle-turn session that exits rc 0 on SIGINT is not quarantined (`stopped_clean=True`) |
| c: `task_progress` for `local_bash`? | no (G4) | a fallback is needed |
| c: `output_file` grows? | yes — path from the Bash tool_result (G4) | **output-file fallback**: at would-expire, a `local_bash` task whose output file was written after the hold started re-arms it (the file's mtime); a silent command still closes. Monitor tasks excluded. |

## Consequences beyond #829

- Because agents ignore EOF (G6), today's `/cancel` of an idle live session with a running agent
  always runs into the 15 s grace, SIGINT, and (before #829) the forced-teardown quarantine.
- A close with running agents lets their wake turns run after stdin is closed (G6) — a
  lifecycle that closed while idle can see a turn open during the grace.
- `closed` notices must not promise "reply to continue" before the exit outcome is known.
