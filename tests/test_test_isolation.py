"""#808: the unit suite's host-isolation guard tests itself.

``tests/conftest.py`` points every path-less config loader at a per-test tmp
path and blocks non-loopback httpx requests. These tests pin that behaviour
so a refactor of the fixtures (or a new ``HOME_CONFIG_PATH`` binding in
``src/``) can't quietly re-expose the host's staging config or the live
Anthropic usage API.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from tests.conftest import HOME_CONFIG_PATH_MODULES
from tests.telegram_fakes import FakeTransport
from untether import settings as settings_mod
from untether.markdown import MarkdownPresenter
from untether.model import CompletedEvent, ResumeToken, StartedEvent
from untether.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from untether.runners.mock import Emit, ScriptRunner
from untether.settings import FooterSettings, _resolve_config_path

pytest_plugins = ["pytester"]

_SRC = Path(__file__).resolve().parents[1] / "src" / "untether"
_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def test_config_resolves_under_tmp(tmp_path: Path) -> None:
    resolved = _resolve_config_path(None)
    assert resolved.is_relative_to(tmp_path)
    assert resolved != Path.home() / ".untether" / "untether.toml"


def test_explicit_home_patch_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test's own ``HOME_CONFIG_PATH`` patch runs after the autouse fixture
    and must override it (the onboarding/CLI tests rely on this)."""
    own = tmp_path / "own" / "untether.toml"
    monkeypatch.setattr("untether.settings.HOME_CONFIG_PATH", own)
    assert _resolve_config_path(None) == own


def test_env_var_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / "env" / "untether.toml"
    monkeypatch.setenv("UNTETHER_CONFIG_PATH", str(env_path))
    assert _resolve_config_path(None) == env_path


def test_every_home_config_binding_is_patched() -> None:
    """Every ``src/`` module that imports ``HOME_CONFIG_PATH`` by name must be
    listed in the conftest fixture — otherwise that binding still points at
    the host's real config."""
    bound: set[str] = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if (
            re.search(r"^HOME_CONFIG_PATH\s*=", text, re.MULTILINE)
            or re.search(r"from\s+\S+\s+import\s+\([^)]*\bHOME_CONFIG_PATH\b", text)
            or re.search(r"from\s+\S+\s+import\s+[^(\n]*\bHOME_CONFIG_PATH\b", text)
        ):
            rel = path.relative_to(_SRC.parent).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            bound.add(".".join(parts))
    assert bound, "the scan found no bindings — the regex has rotted"
    assert bound <= set(HOME_CONFIG_PATH_MODULES)


def test_live_http_blocked() -> None:
    with pytest.raises(httpx.ConnectError, match="#808"):
        httpx.get("https://example.com")


@pytest.mark.anyio
async def test_live_http_blocked_async() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError, match="#808"):
            await client.get("https://api.anthropic.com/api/oauth/usage")


def test_mock_transport_unaffected() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="mocked"))
    with httpx.Client(transport=transport) as client:
        resp = client.get("https://example.com")
    assert resp.text == "mocked"


def test_loopback_allowed() -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"loopback-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_port}/", timeout=5)
    finally:
        server.shutdown()
        server.server_close()
    assert resp.text == "loopback-ok"


@pytest.mark.anyio
async def test_usage_footer_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Claude final with ``show_subscription_usage = true`` reaches the
    usage fetch, which is refused at the transport — no outbound call, and
    the final carries no usage line."""
    from untether.telegram.commands import usage
    from untether.utils import usage_cache

    # Give the fetch a token so it gets as far as httpx (the fixture's tmp
    # credentials path starts empty).
    creds = usage._DEFAULT_CREDENTIALS_PATH
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 2**62}})
    )
    monkeypatch.setattr(
        "untether.runner_bridge._load_footer_settings",
        lambda: FooterSettings(show_subscription_usage=True),
    )

    attempted: list[str] = []
    guarded = httpx.AsyncHTTPTransport.handle_async_request

    async def _record(self: httpx.AsyncHTTPTransport, req: httpx.Request):
        attempted.append(req.url.host)
        return await guarded(self, req)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _record)

    token = ResumeToken(engine="claude", value="sess-808")
    transport = FakeTransport()
    runner = ScriptRunner(
        [
            Emit(StartedEvent(engine="claude", resume=token)),
            Emit(CompletedEvent(engine="claude", resume=token, ok=True, answer="DONE")),
        ],
        engine="claude",
        resume_value=token.value,
    )
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=runner,
        incoming=IncomingMessage(channel_id=1, message_id=10, text="go"),
        resume_token=None,
    )

    assert attempted == ["api.anthropic.com"]
    stats = usage_cache.get_cache_stats()
    assert stats.last_error_kind == "ConnectError"
    assert "#808" in (stats.last_error_message or "")
    finals = [c["message"].text for c in transport.send_calls]
    finals += [c["message"].text for c in transport.edit_calls]
    final = next(t for t in finals if "DONE" in t)
    assert "⚡" not in final


_INNER_CACHE_TESTS = """
from untether import settings

CFG = (
    'transport = "telegram"\\n[transports.telegram]\\n'
    'bot_token = "1:test"\\nchat_id = 1\\nallow_any_user = true\\n'
)


def test_a_populates_cache():
    path = settings.HOME_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CFG)
    assert settings.load_settings_if_exists() is not None
    settings._bound_settings_class(path)
    assert settings._SETTINGS_CACHE


def test_b_sees_empty_cache():
    assert settings._SETTINGS_CACHE == {}
    assert settings._bound_settings_class.cache_info().currsize == 0
"""


def test_settings_cache_cleared_between_tests(pytester: pytest.Pytester) -> None:
    """#506: ``_isolated_config`` clears the process-wide settings parse
    cache per test. Order-independent: the two inner tests run in a fresh
    pytester session under a copy of the real conftest."""
    pytester.makeconftest(_CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_inner_cache=_INNER_CACHE_TESTS)
    result = pytester.runpytest("-p", "no:cacheprovider", "-p", "no:randomly")
    result.assert_outcomes(passed=2)


def test_settings_cache_clear_helper(tmp_path: Path) -> None:
    """Same-process complement: the helper the fixture calls empties both
    the entry cache and the bound-class cache."""
    cfg = tmp_path / "u.toml"
    cfg.write_text(
        'transport = "telegram"\n[transports.telegram]\n'
        'bot_token = "1:test"\nchat_id = 1\nallow_any_user = true\n'
    )
    assert settings_mod.load_settings_if_exists(cfg) is not None
    settings_mod._bound_settings_class(cfg)
    assert settings_mod._SETTINGS_CACHE
    settings_mod.clear_settings_cache()
    assert settings_mod._SETTINGS_CACHE == {}
    assert settings_mod._bound_settings_class.cache_info().currsize == 0
