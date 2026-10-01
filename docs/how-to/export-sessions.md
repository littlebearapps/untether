# Export session transcripts

Untether records session events as they stream from the agent, so you can export a full transcript of any run directly from [Telegram](https://telegram.org) — review what your agent did while you were away, or share the results with your team.

## Export as markdown

Send `/export` in the chat where the run happened:

```
/export
```

Untether replies with a formatted transcript that includes:

- **Usage** — cost, turns and duration (or token counts when the engine reports no cost), labelled `last run` or, for Codex, `thread total` ([#859](https://github.com/littlebearapps/untether/issues/859))
- **Engine and model**
- **Action timeline** — each tool call with status and title
- **Final answer** — the agent's response text (up to 2,000 characters per run)

!!! untether "Untether"
    ```
    📄 Session export (14 events, md):

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

The usage line is the most recent run's figure; the transcript covers every run of the session. For Codex, which reports a running total for the whole thread, it is the thread total.

## Export as JSON

For structured data you can process programmatically, add `json`:

```
/export json
```

The JSON export contains the same information in a machine-readable format, suitable for logging, dashboards, or further analysis.

## What gets exported

Untether keeps the **20 most recently active sessions** in memory, across all chats; the least recently active is dropped first. History is lost on restart. The `/export` command exports the chat's most recently *active* session, so a resumed older session counts as the latest ([#417](https://github.com/littlebearapps/untether/issues/417)). In a forum group this is the most recent session in any topic of the group, not just the current topic.

Each session records:

- Start and completion events
- Every action (tool call) with its kind, title, and status
- The final answer text
- Usage and cost data (when reported by the engine)

## Long transcripts

The export is sent as a message, and only its first 3,000 characters are included so it fits in Telegram's 4,096-character limit. For runs with many actions or long answers the rest is cut off. The JSON export has the same cut-off.

Telegram can't give you the rest of a long transcript yet; sending the export as an attached file is tracked in [#418](https://github.com/littlebearapps/untether/issues/418).

## Related

- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Cost budgets](cost-budgets.md) — track and limit API costs
