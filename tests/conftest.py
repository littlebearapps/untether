import hashlib
import inspect
import os
from collections.abc import Callable, Iterator
from pathlib import Path

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
def _isolated_session_cost_ledger():
    """#778: in-memory ledger per test so the bridge never reads or writes
    the real ``~/.untether/session_costs.json``."""
    from untether.session_costs import SessionCostLedger, set_session_cost_ledger

    ledger = SessionCostLedger(path=None)
    set_session_cost_ledger(ledger)
    yield ledger
    set_session_cost_ledger(None)


@pytest.fixture(autouse=True)
def _isolated_daily_cost() -> Iterator[None]:
    """#898: the daily cost total is module state persisted to
    ``daily_cost.json`` once ``init_daily_cost`` runs (startup, /health).
    Reset it per test so no test inherits another's total or file path."""
    import untether.cost_tracker as cost_tracker

    cost_tracker._daily_cost = ("", 0.0)
    cost_tracker._daily_cost_path = None
    yield
    cost_tracker._daily_cost = ("", 0.0)
    cost_tracker._daily_cost_path = None


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


@pytest.fixture(autouse=True)
def _clear_request_channel_bindings() -> Iterator[None]:
    """#388: ``_REQUEST_TO_CHANNEL`` binds request ids to chats. Tests reuse
    ids (``req-1``…) across files; a binding left by one test would make
    another test's tap from a different chat read ``channel_mismatch``."""
    from untether.runners.claude import _REQUEST_TO_CHANNEL

    _REQUEST_TO_CHANNEL.clear()
    yield
    _REQUEST_TO_CHANNEL.clear()


# ---------------------------------------------------------------------------
# #808: host isolation — config file and live network
# ---------------------------------------------------------------------------

# Every module that binds ``HOME_CONFIG_PATH`` by name. The constant is frozen
# at import (``config.py``), so patching ``HOME`` does nothing; each binding
# has to be swapped. ``test_test_isolation.py`` re-greps ``src/`` and fails
# when a new binding appears that this tuple doesn't cover.
HOME_CONFIG_PATH_MODULES: tuple[str, ...] = (
    "untether.config",
    "untether.settings",
    "untether.api",
    "untether.cli",
    "untether.cli.config",
    "untether.telegram.onboarding",
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_NETWORK_DISABLED = "network disabled in unit tests (#808)"


def _real_host_config_path() -> Path:
    # Deliberately not ``HOME_CONFIG_PATH``: by the time teardown runs that
    # constant is patched, and it was frozen at import anyway.
    return Path(os.path.expanduser("~/.untether/untether.toml"))


def _config_fingerprint(path: Path) -> tuple[str, int] | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


@pytest.fixture(autouse=True, scope="session")
def _host_config_untouched() -> Iterator[None]:
    """#808: the real ``~/.untether/untether.toml`` is the staging config on
    lba-1. Fingerprint it before the session and fail the run if any test
    read-migrated or rewrote it. (A deliberate hand edit of the staging
    config mid-run would trip this too — rerun if so.)"""
    path = _real_host_config_path()
    before = _config_fingerprint(path)
    yield
    after = _config_fingerprint(path)
    assert after == before, (
        f"#808: the unit suite modified the host config {path} "
        f"(before={before}, after={after})"
    )


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """#808: point every path-less config loader at a per-test tmp path.

    ``load_settings_if_exists()`` (called from the bridge and the Claude
    runner on every run) resolves ``UNTETHER_CONFIG_PATH`` then
    ``HOME_CONFIG_PATH``, and ``migrate_config_file`` *writes*. Without this,
    a test run reads — and can rewrite — the host's staging config.

    The bindings are patched rather than the env var being set, because the
    env var is checked first: setting it here would override the tests that
    patch ``HOME_CONFIG_PATH`` themselves. Those tests' own patches, and any
    test that ``setenv``s ``UNTETHER_CONFIG_PATH``, still win (D-11).

    The tmp file is never created, so path-less loaders see "no config" and
    fall back to defaults.

    #506: the process-wide settings parse cache is cleared before and after
    every test, so no test can be served another test's parsed config.
    """
    import importlib

    from untether.settings import clear_settings_cache

    config_path = tmp_path / ".untether" / "untether.toml"
    monkeypatch.delenv("UNTETHER_CONFIG_PATH", raising=False)
    for name in HOME_CONFIG_PATH_MODULES:
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "HOME_CONFIG_PATH", config_path)

    # The usage command reads OAuth credentials from ~/.claude; the path is
    # also bound as a default argument, so swap those defaults too.
    from untether.telegram.commands import usage

    real_creds = usage._DEFAULT_CREDENTIALS_PATH
    fake_creds = tmp_path / ".claude" / ".credentials.json"
    monkeypatch.setattr(usage, "_DEFAULT_CREDENTIALS_PATH", fake_creds)
    for obj in vars(usage).values():
        defaults = getattr(obj, "__defaults__", None)
        if inspect.isfunction(obj) and defaults and real_creds in defaults:
            monkeypatch.setattr(
                obj,
                "__defaults__",
                tuple(fake_creds if d == real_creds else d for d in defaults),
            )
    clear_settings_cache()
    yield config_path
    clear_settings_cache()


@pytest.fixture(autouse=True)
def _no_live_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """#808: block every non-loopback httpx request.

    Every Claude final runs ``_maybe_append_usage_footer``, which fetched
    ``https://api.anthropic.com/api/oauth/usage`` with the host's real OAuth
    token (and could 429 the live bots). Patching the real transports means
    ``httpx.MockTransport`` and loopback servers (trigger webhook tests) keep
    working; anything else raises ``httpx.ConnectError``, which the usage
    path already swallows. Opt out with ``@pytest.mark.allow_network``.

    The usage cache is module-level, so it is reset per test as well — a
    stale entry from one test must not answer the next test's fetch.
    """
    import httpx

    from untether.utils import usage_cache

    usage_cache.reset_cache()
    if request.node.get_closest_marker("allow_network") is None:
        real_sync = httpx.HTTPTransport.handle_request
        real_async = httpx.AsyncHTTPTransport.handle_async_request

        def _guarded_sync(self: httpx.HTTPTransport, req: httpx.Request):
            if req.url.host not in _LOOPBACK_HOSTS:
                raise httpx.ConnectError(_NETWORK_DISABLED, request=req)
            return real_sync(self, req)

        async def _guarded_async(self: httpx.AsyncHTTPTransport, req: httpx.Request):
            if req.url.host not in _LOOPBACK_HOSTS:
                raise httpx.ConnectError(_NETWORK_DISABLED, request=req)
            return await real_async(self, req)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _guarded_sync)
        monkeypatch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", _guarded_async
        )
    yield
    usage_cache.reset_cache()


@pytest.fixture(autouse=True)
def _no_cli_help_probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """#812: ``_build_args`` asks ``claude --help`` (once per binary) whether
    ``--include-hook-events`` exists. Unit tests must never spawn the host's
    real CLI for that, so the probe reports "unknown" (→ flag omitted, the
    pre-rc14 argv) unless a test stubs ``_probe_cli_help`` itself."""
    from untether.runners import claude as claude_runner

    monkeypatch.setattr(claude_runner, "_probe_cli_help", lambda path: None)
    claude_runner._HOOK_EVENTS_SUPPORT.clear()
    yield
    claude_runner._HOOK_EVENTS_SUPPORT.clear()
