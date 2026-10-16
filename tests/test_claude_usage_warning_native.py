"""#1150: with the subscription-usage footer off, a Claude final's near-limit
warning comes from Claude Code's own ``rate_limit_event`` snapshot — no read
of the stored login token and no call to the usage endpoint. The opt-in
footer (engine-wide or the chat's override) still fetches, as before."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests.telegram_fakes import FakeTransport
from untether import runner_bridge as rb
from untether.events import EventFactory
from untether.markdown import MarkdownPresenter
from untether.model import CompletedEvent, StartedEvent
from untether.runner import JsonlStreamState, publish_run_stream
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.claude import ClaudeStreamState, translate_claude_event
from untether.runners.mock import MockRunner, _resume_token
from untether.runners.run_options import EngineRunOptions, apply_run_options
from untether.schemas import claude as claude_schema
from untether.settings import FooterSettings
from untether.transport import RenderedMessage

pytestmark = pytest.mark.anyio


def _rate_limit_state(
    *,
    five_hour: float,
    seven_day: float | None = None,
    status: str = "allowed",
    five_reset_in_s: float = 3 * 3600 + 20 * 60,
) -> ClaudeStreamState:
    """A ``ClaudeStreamState`` fed one real-shaped ``rate_limit_event``
    (CLI 2.1.283 payload, see the stream-json cheatsheet) through the
    runner's own translation."""
    now = time.time()
    windows: dict[str, Any] = {
        "five_hour": {"utilization": five_hour, "resetsAt": int(now + five_reset_in_s)}
    }
    if seven_day is not None:
        windows["seven_day"] = {
            "utilization": seven_day,
            "resetsAt": int(now + 4 * 86400),
        }
    line = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": int(now + five_reset_in_s),
                "rateLimitType": "five_hour",
                "overageStatus": "rejected",
                "isUsingOverage": False,
                "unifiedWindows": windows,
            },
            "uuid": "u-1150",
            "session_id": "sid-1150",
        }
    )
    state = ClaudeStreamState()
    translate_claude_event(
        claude_schema.decode_stream_json_line(line),
        title="claude",
        state=state,
        factory=EventFactory("claude"),
    )
    return state


@pytest.fixture
def token_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record (and refuse) every route to the login token / usage endpoint."""
    calls: list[str] = []

    def _recorder(name: str, *, is_async: bool):
        if is_async:

            async def _async(*_a: Any, **_kw: Any) -> Any:
                calls.append(name)
                return {
                    "five_hour": {
                        "utilization": 11.0,
                        "resets_at": "2099-01-01T00:00:00+00:00",
                    },
                    "seven_day": {
                        "utilization": 3.0,
                        "resets_at": "2099-01-07T00:00:00+00:00",
                    },
                }

            return _async

        def _sync(*_a: Any, **_kw: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not run")

        return _sync

    monkeypatch.setattr(
        "untether.utils.usage_cache.fetch_claude_usage_cached",
        _recorder("fetch_claude_usage_cached", is_async=True),
    )
    monkeypatch.setattr(
        "untether.telegram.commands.usage.fetch_claude_usage",
        _recorder("fetch_claude_usage", is_async=True),
    )
    for name in (
        "_read_credentials_raw",
        "_read_access_token",
        "_read_access_token_with_expiry",
    ):
        monkeypatch.setattr(
            f"untether.telegram.commands.usage.{name}",
            _recorder(name, is_async=False),
        )
    return calls


def _footer(monkeypatch: pytest.MonkeyPatch, *, show: bool) -> None:
    monkeypatch.setattr(
        rb,
        "_load_footer_settings",
        lambda: FooterSettings(show_api_cost=False, show_subscription_usage=show),
    )


# ---------------------------------------------------------------------------
# The warning line itself (footer off)
# ---------------------------------------------------------------------------


async def test_warning_from_rate_limit_event(token_reads: list[str]) -> None:
    state = _rate_limit_state(five_hour=0.75, seven_day=0.30)
    out = await rb._maybe_append_usage_footer(
        RenderedMessage(text="Done.", extra={}),
        always_show=False,
        engine_state=state,
    )
    assert out.text.startswith("Done.\n⚡5h: 75% (3h ")
    assert out.text.endswith(") | 7d: 30%")
    assert token_reads == []


async def test_critical_warning_from_rate_limit_event(token_reads: list[str]) -> None:
    state = _rate_limit_state(five_hour=0.92, seven_day=0.41, status="allowed_warning")
    out = await rb._maybe_append_usage_footer(
        RenderedMessage(text="Done.", extra={}),
        always_show=False,
        engine_state=state,
    )
    assert "\n⚠️ 5h: 92% (3h " in out.text
    assert out.text.endswith(" | 7d: 41%")
    assert token_reads == []


async def test_limit_hit_from_rate_limit_event(token_reads: list[str]) -> None:
    state = _rate_limit_state(five_hour=1.0, status="rejected")
    out = await rb._maybe_append_usage_footer(
        RenderedMessage(text="Done.", extra={}),
        always_show=False,
        engine_state=state,
    )
    assert "\n\U0001f6d1 5h limit hit — resets in 3h " in out.text
    assert token_reads == []


async def test_no_7d_part_when_event_has_no_seven_day(token_reads: list[str]) -> None:
    state = _rate_limit_state(five_hour=0.8)
    out = await rb._maybe_append_usage_footer(
        RenderedMessage(text="Done.", extra={}),
        always_show=False,
        engine_state=state,
    )
    assert "⚡5h: 80% (" in out.text
    assert "7d" not in out.text


@pytest.mark.parametrize(
    "state",
    [
        None,  # no stream state (non-JSONL runner, no event seen)
        ClaudeStreamState(),  # Claude ran but sent no rate_limit_event
        "below",
        "stale",
    ],
)
async def test_no_warning_without_a_fresh_near_limit_snapshot(
    token_reads: list[str], state: Any
) -> None:
    if state == "below":
        state = _rate_limit_state(five_hour=0.69, seven_day=0.95)
    elif state == "stale":
        # The 5h window has reset since the snapshot — it no longer applies.
        state = _rate_limit_state(five_hour=0.95, five_reset_in_s=-60)
    out = await rb._maybe_append_usage_footer(
        RenderedMessage(text="Done.", extra={}),
        always_show=False,
        engine_state=state,
    )
    assert out.text == "Done."
    assert token_reads == []


async def test_footer_on_still_fetches(token_reads: list[str]) -> None:
    state = _rate_limit_state(five_hour=0.95)
    out = await rb._maybe_append_usage_footer(
        RenderedMessage(text="Done.", extra={}),
        always_show=True,
        engine_state=state,
    )
    assert token_reads == ["fetch_claude_usage_cached"]
    assert "⚡ 5h: 11%" in out.text


# ---------------------------------------------------------------------------
# Whole Claude runs through handle_message
# ---------------------------------------------------------------------------


class _RateLimitedClaudeRunner(MockRunner):
    """Publishes a stream whose engine state carries a ``rate_limit_event``
    snapshot, like ``ClaudeRunner`` does, then answers (or fails)."""

    def __init__(self, engine_state: Any, *, error: bool = False) -> None:
        super().__init__(events=[], engine="claude", resume_value="sid-1150")
        self._stream = JsonlStreamState(expected_session=None)
        self._stream.engine_state = engine_state
        self._error = error

    async def run(self, prompt, resume):
        publish_run_stream(self._stream, None)
        token = _resume_token(self.engine, self._resume_value)
        async with self.lock_for(token):
            yield StartedEvent(engine=self.engine, resume=token, title=self.title)
            if self._error:
                raise RuntimeError("boom")
            yield CompletedEvent(
                engine=self.engine, resume=token, ok=True, answer="done"
            )


async def _run(runner: MockRunner) -> FakeTransport:
    transport = FakeTransport()
    await handle_message(
        ExecBridgeConfig(
            transport=transport, presenter=MarkdownPresenter(), final_notify=True
        ),
        runner=runner,
        incoming=IncomingMessage(channel_id=123, message_id=10, text="go"),
        resume_token=None,
    )
    return transport


def _final_text(transport: FakeTransport, *, error: bool) -> str:
    calls = transport.edit_calls if error else transport.send_calls
    return calls[-1]["message"].text


@pytest.mark.parametrize("error", [False, True])
async def test_footer_off_run_reads_no_token_and_warns_from_event(
    monkeypatch: pytest.MonkeyPatch, token_reads: list[str], error: bool
) -> None:
    _footer(monkeypatch, show=False)
    state = _rate_limit_state(five_hour=0.85, seven_day=0.40)
    transport = await _run(_RateLimitedClaudeRunner(state, error=error))
    text = _final_text(transport, error=error)
    assert "⚡5h: 85% (" in text
    assert "| 7d: 40%" in text
    assert token_reads == []


async def test_footer_on_run_fetches(
    monkeypatch: pytest.MonkeyPatch, token_reads: list[str]
) -> None:
    _footer(monkeypatch, show=True)
    state = _rate_limit_state(five_hour=0.85)
    transport = await _run(_RateLimitedClaudeRunner(state))
    assert token_reads == ["fetch_claude_usage_cached"]
    assert "⚡ 5h: 11%" in _final_text(transport, error=False)


async def test_chat_override_on_fetches_with_engine_wide_off(
    monkeypatch: pytest.MonkeyPatch, token_reads: list[str]
) -> None:
    _footer(monkeypatch, show=False)
    with apply_run_options(EngineRunOptions(show_subscription_usage=True)):
        await _run(_RateLimitedClaudeRunner(ClaudeStreamState()))
    assert token_reads == ["fetch_claude_usage_cached"]


async def test_chat_override_off_with_engine_wide_on_reads_no_token(
    monkeypatch: pytest.MonkeyPatch, token_reads: list[str]
) -> None:
    _footer(monkeypatch, show=True)
    with apply_run_options(EngineRunOptions(show_subscription_usage=False)):
        await _run(_RateLimitedClaudeRunner(_rate_limit_state(five_hour=0.2)))
    assert token_reads == []
