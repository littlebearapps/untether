# Export session transcripts

Untether records session events as they stream from the agent, so you can export a full transcript of any run directly from Telegram — available on iPhone, iPad, Android, Mac, Windows, Linux, and Telegram Web.

## Export as markdown

Send `/export` in the chat where the run happened:

```
/export
```

Untether replies with a formatted transcript that includes:

- **Model** and engine used
- **API usage** (input/output tokens, cost)
- **Action timeline** — each tool call with status and title
- **Final answer** — the agent's response text

!!! untether "Untether"
    **Session export (markdown)**

    **Model:** claude-opus-4-6
    **Tokens:** 12,450 in / 3,200 out
    **Cost:** $0.42

    **Actions:**
    1. Read src/main.py
    2. Edit src/main.py
    3. Bash: uv run pytest

    **Answer:**
    Fixed the import order in main.py and all tests pass.

## Export as JSON

For structured data you can process programmatically, add `json`:

```
/export json
```

The JSON export contains the same information in a machine-readable format, suitable for logging, dashboards, or further analysis.

## What gets exported

Untether keeps up to **20 sessions** in memory per chat. The `/export` command exports the most recent session for the current chat (or topic, if you're in a forum thread).

Each session records:

- Start and completion events
- Every action (tool call) with its kind, title, and status
- The final answer text
- Usage and cost data (when reported by the engine)

## Long transcripts

Telegram messages are limited to approximately 3,500 characters. For runs with many actions or long answers, the export may be truncated to fit within Telegram's limits. The JSON format is generally more compact and fits longer sessions.

For very long sessions, consider using the JSON export and processing it outside Telegram.

## Related

- [Commands & directives](../reference/commands-and-directives.md) — full command reference
- [Cost budgets](cost-budgets.md) — track and limit API costs
