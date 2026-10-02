# Export session transcripts

Untether records session events as they stream from the agent, so you can export a full transcript of any run directly from [Telegram](https://telegram.org) — review what your agent did while you were away, or share the results with your team.

## Export as markdown

Send `/export` in the chat where the run happened:

```
/export
```

Untether replies with the full transcript attached as a Markdown file (`untether-export-<engine>-<session>-<YYYYmmdd-HHMM>.md`), with a one-line summary as the caption ([#418](https://github.com/littlebearapps/untether/issues/418)). The file includes:

- **Usage** — cost, turns and duration (or token counts when the engine reports no cost), labelled `last run` or, for Codex, `thread total` ([#859](https://github.com/littlebearapps/untether/issues/859))
- **Engine and model**
- **Action timeline** — each tool call with status and title
- **Final answer** — the agent's full response text for each run
- **Later turns** — in a Claude live session, each follow-up you sent into it and each 🔔 background wake turn, under its own `## Turn N (follow-up)` / `## Turn N (background task finished)` heading with its actions and answer

!!! untether "Untether"
    📎 `untether-export-claude-1f0c9a2e-…-20261001-0915.md`

    📄 Session export — claude · 14 events · Markdown<br>
    Session: 1f0c9a2e-…

Opened, the file looks like this:

```markdown
# Session Export: 1f0c9a2e-…
Exported: 2026-10-01 09:15 UTC

**Usage:** $0.4200 · 5 turns · 42.3s · last run

---

## Session Started (claude)
Model: claude-opus-5-5

- ✓ 🔧 Read src/main.py
- ✓ 📝 src/main.py
- ✓ `uv run pytest`

## ✓ Completed

Fixed the import order in main.py and all tests pass.
```

The usage line is the most recent run's figure (for a Claude live session, its latest turn's running total for that process); the transcript covers every run and every turn of the session. For Codex, which reports a running total for the whole thread, it is the thread total.

## Export as JSON

For structured data you can process programmatically, add `json`:

```
/export json
```

You get a `.json` file with the same information in a machine-readable format, suitable for logging, dashboards, or further analysis.

## What gets exported

Untether keeps the **20 most recently active sessions** in memory, across all chats; the least recently active is dropped first. History is lost on restart. The `/export` command exports the chat's most recently *active* session, so a resumed older session counts as the latest ([#417](https://github.com/littlebearapps/untether/issues/417)). It's the most recent session **in the chat**: in a forum group that means the most recent session in any topic of the group, not just the current topic (topic-scoped export is tracked in [#849](https://github.com/littlebearapps/untether/issues/849)).

Each session records:

- Start and completion events, plus a boundary for each later turn of a live session (JSON: `{"type": "turn", "phase": "started", "turn": N, "reason": …}`, and that turn's `completed` event carries `turn` and `reason`)
- Every action (tool call) with its kind, title, and status
- The final answer text
- Usage and cost data (when reported by the engine)

## Long transcripts

The export file is never truncated: long answers and long action timelines are included in full. The file holds everything the transcript shows, including the full answers, so share it with the same care as the chat itself.

Untether caps an export file at 10 MB (Telegram allows 50 MB, but a large upload would hold up other messages from the bot while it sends). If the file is over that cap, or the upload fails, Untether sends the old inline preview instead: the first 3,000 characters of the transcript, with a note that the file couldn't be attached. A slow upload that times out after reaching Telegram can occasionally deliver both the file and the preview.

## Related

- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Cost budgets](cost-budgets.md) — track and limit API costs
