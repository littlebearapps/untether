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
def _isolated_quarantine_store(tmp_path):
    """#631/#634: ``handle_message`` resolves the QuarantineStore singleton
    via ``get_quarantine_store()`` on every entry. Without this fixture, any
    of the ~220 unfixtured tests that reach ``handle_message`` would lazily
    materialise the real ``~/.untether/session_quarantine.json`` — and
    ``QuarantineStore.load()``'s prune-then-flush can silently REWRITE that
    production file from a pytest run once fleet markers exist and age past
    the 7-day prune window. Inject a fresh, isolated store for every test so
    the singleton never touches disk outside ``tmp_path``.

    Tests with their own local ``quarantine_store`` fixture (test_exec_
    bridge.py, test_loop_coverage.py, test_claude_runner.py) call
    ``set_quarantine_store(...)`` themselves within the test body / their
    own fixture; that call simply overrides this one for the duration of
    the test — fixture setup order (autouse fixtures of the same scope run
    before explicitly-requested ones) guarantees the local override always
    wins, and a redundant ``set_quarantine_store(None)`` in this fixture's
    teardown after a local fixture already reset it is harmless.
    """
    from untether.session_quarantine import QuarantineStore, set_quarantine_store

    store = QuarantineStore(path=tmp_path / "session_quarantine.json")
    set_quarantine_store(store)
    yield store
    set_quarantine_store(None)


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
