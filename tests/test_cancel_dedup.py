"""#525: triple-fire dedup for the inline Cancel button + text-reply paths.

Telegram delivered duplicate callbacks for the same tap when the user
double/triple-tapped the inline Cancel button before the keyboard could be
cleared. Repeat ``cancel.requested`` fires were benign today (cancel just
worked once and the duplicate set() was a no-op) but log noise + future
side-effectful cancel actions (telemetry, webhook, structured cancel hook)
would inherit the 3x fan-out. A 1-second TTL dedup keyed on
(chat_id, progress_message_id) prevents this without affecting legitimate
later retries.
"""

from __future__ import annotations

import time

from untether.telegram.commands.cancel import (
    _CANCEL_DEDUP_TTL_S,
    _RECENT_CANCELS,
    _claim_cancel,
)


def setup_function() -> None:
    _RECENT_CANCELS.clear()


def teardown_function() -> None:
    _RECENT_CANCELS.clear()


def test_first_claim_returns_true() -> None:
    assert _claim_cancel(-5235455627, 51263) is True


def test_immediate_second_claim_returns_false() -> None:
    assert _claim_cancel(-5235455627, 51263) is True
    assert _claim_cancel(-5235455627, 51263) is False


def test_triple_fire_within_one_second_dedupes_to_one() -> None:
    """Reproduces the #525 evidence: three cancels at +0ms, +493ms, +512ms."""
    chat_id = -5235455627
    msg_id = 51263
    fires = [_claim_cancel(chat_id, msg_id) for _ in range(3)]
    assert fires == [True, False, False]


def test_different_messages_in_same_chat_each_get_one_fire() -> None:
    chat_id = -5235455627
    assert _claim_cancel(chat_id, 51263) is True
    assert _claim_cancel(chat_id, 51264) is True  # different message
    assert _claim_cancel(chat_id, 51263) is False
    assert _claim_cancel(chat_id, 51264) is False


def test_same_message_in_different_chats_each_get_one_fire() -> None:
    msg_id = 51263
    assert _claim_cancel(-5235455627, msg_id) is True
    assert _claim_cancel(-5173025382, msg_id) is True  # different chat
    assert _claim_cancel(-5235455627, msg_id) is False


def test_claim_re_succeeds_after_ttl_expires(monkeypatch) -> None:
    """After the 1-second dedup window expires, a legitimate retry (e.g.
    user types ``/cancel`` after the keyboard already dismissed) succeeds.
    """
    real_monotonic = time.monotonic
    base = real_monotonic()

    times = iter(
        [
            base,
            base + 0.1,
            base + _CANCEL_DEDUP_TTL_S + 0.5,  # well past TTL
        ]
    )
    monkeypatch.setattr(
        "untether.telegram.commands.cancel.time.monotonic", lambda: next(times)
    )

    assert _claim_cancel(-5235455627, 51263) is True
    assert _claim_cancel(-5235455627, 51263) is False
    assert _claim_cancel(-5235455627, 51263) is True  # past TTL → new claim


def test_expired_entries_are_garbage_collected(monkeypatch) -> None:
    real_monotonic = time.monotonic
    base = real_monotonic()
    times = iter(
        [
            base,
            base + 0.1,
            base + 0.2,
            base + _CANCEL_DEDUP_TTL_S + 1.0,  # trigger GC
        ]
    )
    monkeypatch.setattr(
        "untether.telegram.commands.cancel.time.monotonic", lambda: next(times)
    )

    _claim_cancel(-1, 1)
    _claim_cancel(-2, 2)
    _claim_cancel(-3, 3)
    assert len(_RECENT_CANCELS) == 3
    _claim_cancel(-4, 4)  # third call sees all prior as expired, GCs them
    # After GC + insert of (-4, 4), only that survives.
    assert (-4, 4) in _RECENT_CANCELS
    assert len(_RECENT_CANCELS) == 1
