"""#770: footer lines (cost, budget alert, #702 outlier, usage) belong on the
LAST chunk of a split final message, before its resume line."""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.telegram_fakes import FakeTransport
from untether import runner_bridge as rb
from untether.model import ResumeToken, StartedEvent
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import Emit, Return, ScriptRunner
from untether.session_costs import SessionCostLedger, set_session_cost_ledger
from untether.settings import FooterSettings
from untether.telegram.bridge import TelegramPresenter
from untether.transport import RenderedMessage

pytestmark = pytest.mark.anyio

COST = "\U0001f4b0"  # 💰
OUTLIER = "\U0001f4b8 This run cost $9.00 (over the $5.00 alert)"
BUDGET = "⚠️ Budget: run cost $4.10 is 82% of the $5.00 limit"
USAGE = "⚡ 5h: 11% | 7d: 3%"
META = "\U0001f3f7"  # 🏷
RESUME = "↩️"  # ↩️
FOOTER_MARKS = (COST, "\U0001f4b8", "Budget:", "⚡")

# ~13k chars in ~100-word paragraphs → four chunks at 3500.
LONG_ANSWER = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(20))
USAGE_PAYLOAD = {"total_cost_usd": 1.03, "num_turns": 1, "duration_ms": 173_000}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    # Claude's cost delta ledger must not touch the real state file.
    set_session_cost_ledger(SessionCostLedger(path=None))

    async def _usage() -> dict[str, Any]:
        return {
            "five_hour": {"utilization": 11.0, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": 3.0, "resets_at": "2099-01-07T00:00:00Z"},
        }

    monkeypatch.setattr("untether.utils.usage_cache.fetch_claude_usage_cached", _usage)
    yield
    set_session_cost_ledger(None)


def _footer(monkeypatch: pytest.MonkeyPatch, **kw: bool) -> None:
    monkeypatch.setattr(rb, "_load_footer_settings", lambda: FooterSettings(**kw))


def _entity_text(text: str, entity: dict[str, Any]) -> str:
    raw = text.encode("utf-16-le")
    start = entity["offset"] * 2
    return raw[start : start + entity["length"] * 2].decode("utf-16-le")


def _resume_entity_ok(msg: RenderedMessage) -> bool:
    """The resume command's ``code`` entity still covers the command."""
    codes = [
        _entity_text(msg.text, e)
        for e in msg.extra.get("entities") or []
        if e.get("type") == "code"
    ]
    return any(c.startswith("claude resume sess-770") for c in codes)


async def _run(
    answer: str,
    *,
    presenter: TelegramPresenter | None = None,
    context_line: str | None = None,
) -> RenderedMessage:
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            # Late meta, as the Claude runner sends it — gives the final a
            # 🏷 meta line to order the footer lines against.
            Emit(
                StartedEvent(
                    engine="claude",
                    resume=ResumeToken(engine="claude", value="sess-770"),
                    meta={"model": "claude-opus-5-5"},
                )
            ),
            Return(answer=answer, usage=dict(USAGE_PAYLOAD)),
        ],
        engine="claude",
        resume_value="sess-770",
    )
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=presenter or TelegramPresenter(),
        final_notify=True,
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
        resume_token=None,
        context_line=context_line,
    )
    return transport.send_calls[-1]["message"]


def _chunks(final: RenderedMessage) -> list[RenderedMessage]:
    return [final, *(final.extra.get("followups") or [])]


async def test_split_final_puts_cost_outlier_and_usage_on_last_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _footer(monkeypatch, show_api_cost=True, show_subscription_usage=True)
    monkeypatch.setattr(rb, "_check_cost_budget", lambda usage: (None, None))
    monkeypatch.setattr(rb, "_check_run_cost_outlier", lambda usage: OUTLIER)

    chunks = _chunks(await _run(LONG_ANSWER))

    assert len(chunks) >= 3
    for chunk in chunks[:-1]:
        assert not any(mark in chunk.text for mark in FOOTER_MARKS), chunk.text
        assert RESUME not in chunk.text
    last = chunks[-1].text
    for line in (f"{COST}$1.03 · 1 tn · 2m 53s", OUTLIER, USAGE):
        assert last.count(line) == 1
    # Footer order: meta, cost, outlier, usage, then the resume line last.
    order = [last.index(m) for m in (META, COST, "\U0001f4b8", "⚡", RESUME)]
    assert order == sorted(order)
    assert _resume_entity_ok(chunks[-1])
    assert all(rb._utf16_len(c.text) <= rb._TELEGRAM_TEXT_LIMIT for c in chunks)


async def test_split_final_puts_standalone_budget_alert_on_last_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _footer(monkeypatch, show_api_cost=False, show_subscription_usage=False)
    monkeypatch.setattr(rb, "_check_cost_budget", lambda usage: (BUDGET, object()))
    # Suppressed by the budget alert (#702), even though it would fire.
    monkeypatch.setattr(rb, "_check_run_cost_outlier", lambda usage: OUTLIER)

    chunks = _chunks(await _run(LONG_ANSWER))

    assert len(chunks) >= 3
    assert all(BUDGET not in c.text for c in chunks[:-1])
    last = chunks[-1].text
    assert last.count(BUDGET) == 1
    assert OUTLIER not in last and COST not in last and "⚡" not in last
    assert last.index(META) < last.index(BUDGET) < last.index(RESUME)
    assert _resume_entity_ok(chunks[-1])


async def test_single_chunk_final_keeps_footer_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _footer(monkeypatch, show_api_cost=True, show_subscription_usage=True)
    monkeypatch.setattr(rb, "_check_cost_budget", lambda usage: (None, None))
    monkeypatch.setattr(rb, "_check_run_cost_outlier", lambda usage: None)

    final = await _run("All done!")

    assert "followups" not in final.extra
    text = final.text
    assert text.index("All done!") < text.index(COST) < text.index("⚡")
    assert text.index("⚡") < text.index(RESUME)
    # The inserted lines shift the resume command's code entity with them.
    assert _resume_entity_ok(final)


async def test_trim_mode_final_keeps_footer_on_the_single_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _footer(monkeypatch, show_api_cost=True, show_subscription_usage=True)
    monkeypatch.setattr(rb, "_check_cost_budget", lambda usage: (None, None))
    monkeypatch.setattr(rb, "_check_run_cost_outlier", lambda usage: OUTLIER)

    final = await _run(
        LONG_ANSWER, presenter=TelegramPresenter(message_overflow="trim")
    )

    assert "followups" not in final.extra
    text = final.text
    assert text.index(COST) < text.index(OUTLIER) < text.index("⚡")
    assert text.index("⚡") < text.index(RESUME)
    assert _resume_entity_ok(final)
    assert rb._utf16_len(text) <= rb._TELEGRAM_TEXT_LIMIT


async def test_near_limit_last_chunk_stays_under_telegram_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A last chunk carrying a full ~3500-char body still has room for every
    footer line: MAX_BODY_CHARS leaves ~600 chars of headroom."""
    _footer(monkeypatch, show_api_cost=True, show_subscription_usage=True)
    monkeypatch.setattr(rb, "_check_cost_budget", lambda usage: (None, None))
    monkeypatch.setattr(rb, "_check_run_cost_outlier", lambda usage: OUTLIER)
    answer = ("x" * 3400 + "\n\n") * 2 + "y" * 3499

    with capture_logs() as logs:
        chunks = _chunks(
            await _run(answer, context_line="`dir: some-project` @ `feature/branch`")
        )

    assert len(chunks) == 3
    last = chunks[-1]
    assert "y" * 3499 in last.text
    assert all(m in last.text for m in (COST, "\U0001f4b8", "⚡"))
    assert rb._utf16_len(last.text) <= rb._TELEGRAM_TEXT_LIMIT
    assert not [e for e in logs if e.get("event") == "final.footer_overflow"]
    assert _resume_entity_ok(last)


# ── _insert_footer_line unit cases ──────────────────────────────────────────


def _msg(text: str, entities: list[dict[str, Any]] | None = None) -> RenderedMessage:
    return RenderedMessage(
        text=text, extra={"entities": entities or [], "reply_markup": {"k": 1}}
    )


def test_insert_shifts_entities_after_the_insertion_by_utf16_length() -> None:
    text = "**Done** body\n\n\U0001f3f7 opus\n\n↩️ claude resume abc"
    resume_off = rb._utf16_len(text[: text.index("claude resume")])
    bold = {"type": "bold", "offset": 0, "length": 8}
    code = {"type": "code", "offset": resume_off, "length": 17}
    msg = _msg(text, [bold, code])

    out = rb._insert_footer_line(msg, "\n\U0001f4b0$1.00")

    assert out.text.index("\U0001f4b0") < out.text.index("↩")
    bold_out, code_out = out.extra["entities"]
    assert bold_out == bold  # before the insertion: untouched
    assert _entity_text(out.text, code_out) == "claude resume abc"
    assert out.extra["reply_markup"] == {"k": 1}
    assert msg.extra["entities"][1]["offset"] == resume_off  # input not mutated


def test_insert_targets_last_followup_and_leaves_input_untouched() -> None:
    first = _msg("chunk one")
    middle = _msg("chunk two")
    last = _msg("chunk three\n\n↩️ claude resume abc")
    msg = RenderedMessage(
        text=first.text, extra={**first.extra, "followups": [middle, last]}
    )

    out = rb._insert_footer_line(msg, "\n⚡ 5h: 1%")

    assert out.text == "chunk one"
    assert out.extra["followups"][0] is middle
    assert out.extra["followups"][1].text == (
        "chunk three\n⚡ 5h: 1%\n\n↩️ claude resume abc"
    )
    assert msg.extra["followups"][1] is last  # original list not mutated


def test_insert_that_would_overflow_becomes_its_own_message() -> None:
    body = "z" * (rb._TELEGRAM_TEXT_LIMIT - 5)
    msg = RenderedMessage(
        text="chunk one", extra={"entities": [], "followups": [_msg(body)]}
    )

    with capture_logs() as logs:
        out = rb._insert_footer_line(msg, "\n\U0001f4b0$1.00 · 1 tn")
        out = rb._insert_footer_line(out, "\n⚡ 5h: 1%")

    followups = out.extra["followups"]
    assert followups[0].text == body  # the full chunk is left alone
    # The overflow line becomes a trailing message; the next line joins it.
    assert followups[1].text == "\U0001f4b0$1.00 · 1 tn\n⚡ 5h: 1%"
    assert len(followups) == 2
    overflow = [e for e in logs if e.get("event") == "final.footer_overflow"]
    assert len(overflow) == 1
