from untether.model import ResumeToken
from untether.router import AutoRouter, RunnerEntry
from untether.runners.claude import ClaudeRunner
from untether.runners.codex import CodexRunner


def _router() -> tuple[AutoRouter, ClaudeRunner, CodexRunner]:
    codex = CodexRunner(codex_cmd="codex", extra_args=[])
    claude = ClaudeRunner(claude_cmd="claude")
    router = AutoRouter(
        entries=[
            RunnerEntry(engine=claude.engine, runner=claude),
            RunnerEntry(engine=codex.engine, runner=codex),
        ],
        default_engine=codex.engine,
    )
    return router, claude, codex


def test_router_resolves_text_before_reply() -> None:
    router, _claude, _codex = _router()
    token = router.resolve_resume("`codex resume abc`", "`claude --resume def`")

    assert token == ResumeToken(engine="codex", value="abc")


def test_router_poll_order_selects_first_matching_runner() -> None:
    router, _claude, _codex = _router()
    text = "`codex resume abc`\n`claude --resume def`"

    token = router.resolve_resume(text, None)

    assert token == ResumeToken(engine="claude", value="def")


def test_router_resolves_reply_text_when_text_missing() -> None:
    router, _claude, _codex = _router()

    token = router.resolve_resume(None, "`codex resume xyz`")

    assert token == ResumeToken(engine="codex", value="xyz")


def test_router_resolves_reply_with_resume_emoji_prefix() -> None:
    """Reply text containing ↩️ prefix (as rendered in final messages)."""
    router, _claude, _codex = _router()

    token = router.resolve_resume(
        "follow up task", "\u21a9\ufe0f `claude --resume abc123`"
    )

    assert token == ResumeToken(engine="claude", value="abc123")


def test_router_resolves_reply_emoji_without_variation_selector() -> None:
    """↩ (U+21A9) without the variation selector U+FE0F."""
    router, _claude, _codex = _router()

    token = router.resolve_resume(None, "\u21a9 `codex resume thread-42`")

    assert token == ResumeToken(engine="codex", value="thread-42")


def test_router_resolves_reply_resume_in_full_final_message() -> None:
    """Resume token embedded in a full final message with answer + footer."""
    router, _claude, _codex = _router()

    reply_text = (
        "I've completed the task.\n"
        "\N{LABEL} sonnet | plan\n"
        "\n"
        "\u21a9\ufe0f `claude --resume 8b2d2b30-abcd-1234-5678-deadbeef0000`"
    )
    token = router.resolve_resume("do the next step", reply_text)

    assert token == ResumeToken(
        engine="claude", value="8b2d2b30-abcd-1234-5678-deadbeef0000"
    )


def test_router_resolves_reply_resume_without_emoji_still_works() -> None:
    """Regression: reply text without emoji prefix still extracts correctly."""
    router, _claude, _codex = _router()

    token = router.resolve_resume(None, "`codex resume xyz`")

    assert token == ResumeToken(engine="codex", value="xyz")


def test_router_is_resume_line_union() -> None:
    router, _claude, _codex = _router()

    assert router.is_resume_line("`codex resume abc`")
    assert router.is_resume_line("claude --resume def")
