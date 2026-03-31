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
