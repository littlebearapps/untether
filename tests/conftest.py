from collections.abc import Callable

import pytest

from tests.telegram_fakes import FakeBot, FakeTransport
from tests.telegram_fakes import make_cfg as build_cfg
from untether.runners.mock import ScriptRunner
from untether.telegram.bridge import TelegramBridgeConfig


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def make_cfg() -> Callable[..., TelegramBridgeConfig]:
    def _factory(
        transport: FakeTransport, runner: ScriptRunner | None = None
    ) -> TelegramBridgeConfig:
        return build_cfg(transport, runner)

    return _factory


@pytest.fixture(autouse=True)
def _clear_cancel_dedup() -> None:
    """#525: ``_RECENT_CANCELS`` is module-level state that persists across
    tests. Without an explicit clear, two tests using the same
    ``(chat_id, progress_message_id)`` pair within ~1 second wall-clock
    would have the second see a "duplicate" and silently drop the
    cancel — confusing for the test author.

    Auto-clearing per-test keeps the tests independent. The dedup
    behaviour itself is exercised by ``tests/test_cancel_dedup.py``.
    """
    from untether.telegram.commands.cancel import _RECENT_CANCELS

    _RECENT_CANCELS.clear()
    yield
    _RECENT_CANCELS.clear()
