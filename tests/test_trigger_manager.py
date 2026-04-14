"""Tests for TriggerManager — mutable trigger config holder for hot-reload."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from untether.transport import MessageRef
from untether.triggers.manager import TriggerManager
from untether.triggers.server import build_webhook_app
from untether.triggers.settings import TriggersSettings, parse_trigger_config

# ── Helpers ──────────────────────────────────────────────────────────


def _settings(**overrides: Any) -> TriggersSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(overrides)
    return parse_trigger_config(base)


def _webhook(
    wh_id: str = "wh1",
    path: str = "/hooks/test",
    secret: str = "tok_123",
    **kw: Any,
) -> dict[str, Any]:
    return {
        "id": wh_id,
        "path": path,
        "auth": "bearer",
        "secret": secret,
        "prompt_template": "Event: {{text}}",
        **kw,
    }


def _cron(
    cron_id: str = "cr1",
    schedule: str = "0 9 * * *",
    prompt: str = "hello",
    **kw: Any,
) -> dict[str, Any]:
    return {"id": cron_id, "schedule": schedule, "prompt": prompt, **kw}


@dataclass
class FakeTransport:
    sent: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1

    async def send(self, *, channel_id, message, options=None):
        ref = MessageRef(channel_id=channel_id, message_id=self._next_id)
        self._next_id += 1
        self.sent.append({"channel_id": channel_id, "text": message.text})
        return ref

    async def edit(self, *, ref, message, wait=True):
        return ref

    async def delete(self, *, ref):
        return True

    async def close(self):
        pass


@dataclass
class FakeTaskGroup:
    tasks: list = field(default_factory=list)

    def start_soon(self, fn, *args):
        self.tasks.append((fn, args))


@dataclass
class RunJobCapture:
    calls: list = field(default_factory=list)

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)


def _make_dispatcher(transport=None, run_job=None):
    from untether.triggers.dispatcher import TriggerDispatcher

    transport = transport or FakeTransport()
    run_job = run_job or RunJobCapture()
    return TriggerDispatcher(
        run_job=run_job,
        transport=transport,
        default_chat_id=100,
        task_group=FakeTaskGroup(),  # type: ignore[arg-type]
    )


# ── TriggerManager unit tests ───────────────────────────────────────


class TestTriggerManagerInit:
    def test_empty_init(self):
        mgr = TriggerManager()
        assert mgr.crons == []
        assert mgr.webhook_for_path("/any") is None
        assert mgr.webhook_count == 0
        assert mgr.default_timezone is None

    def test_init_with_settings(self):
        s = _settings(
            webhooks=[_webhook()],
            crons=[_cron()],
            default_timezone="Australia/Melbourne",
        )
        mgr = TriggerManager(s)
        assert len(mgr.crons) == 1
        assert mgr.crons[0].id == "cr1"
        assert mgr.webhook_for_path("/hooks/test") is not None
        assert mgr.webhook_count == 1
        assert mgr.default_timezone == "Australia/Melbourne"


class TestTriggerManagerUpdate:
    def test_update_replaces_crons(self):
        mgr = TriggerManager(_settings(crons=[_cron("a")]))
        assert len(mgr.crons) == 1
        assert mgr.crons[0].id == "a"

        mgr.update(_settings(crons=[_cron("b"), _cron("c")]))
        assert len(mgr.crons) == 2
        ids = {c.id for c in mgr.crons}
        assert ids == {"b", "c"}

    def test_update_replaces_webhooks(self):
        mgr = TriggerManager(_settings(webhooks=[_webhook("wh1", "/hooks/one")]))
        assert mgr.webhook_for_path("/hooks/one") is not None
        assert mgr.webhook_for_path("/hooks/two") is None

        mgr.update(_settings(webhooks=[_webhook("wh2", "/hooks/two")]))
        assert mgr.webhook_for_path("/hooks/one") is None
        assert mgr.webhook_for_path("/hooks/two") is not None

    def test_update_clears_when_empty(self):
        mgr = TriggerManager(
            _settings(
                webhooks=[_webhook()],
                crons=[_cron()],
            )
        )
        assert mgr.webhook_count == 1
        assert len(mgr.crons) == 1

        mgr.update(TriggersSettings())
        assert mgr.webhook_count == 0
        assert mgr.crons == []

    def test_update_timezone(self):
        mgr = TriggerManager(_settings(default_timezone="America/New_York"))
        assert mgr.default_timezone == "America/New_York"

        mgr.update(_settings(default_timezone="Australia/Melbourne"))
        assert mgr.default_timezone == "Australia/Melbourne"

    def test_old_cron_list_unaffected_by_update(self):
        """In-flight iteration safety: old list ref stays valid after update."""
        mgr = TriggerManager(_settings(crons=[_cron("a")]))
        old_crons = mgr.crons  # grab reference
        mgr.update(_settings(crons=[_cron("b")]))
        # Old reference should still have the old data.
        assert len(old_crons) == 1
        assert old_crons[0].id == "a"
        # New data via property.
        assert mgr.crons[0].id == "b"


# ── Webhook server with TriggerManager ──────────────────────────────


class TestWebhookServerWithManager:
    @pytest.mark.anyio
    async def test_health_reflects_manager_count(self):
        settings = _settings(webhooks=[_webhook()])
        mgr = TriggerManager(settings)
        dispatcher = _make_dispatcher()
        app = build_webhook_app(settings, dispatcher, manager=mgr)

        async with TestClient(TestServer(app)) as cl:
            resp = await cl.get("/health")
            data = await resp.json()
            assert data["webhooks"] == 1

            # Hot-reload: add a second webhook.
            mgr.update(
                _settings(
                    webhooks=[
                        _webhook("wh1", "/hooks/one"),
                        _webhook("wh2", "/hooks/two"),
                    ]
                )
            )
            resp = await cl.get("/health")
            data = await resp.json()
            assert data["webhooks"] == 2

    @pytest.mark.anyio
    async def test_new_webhook_accessible_after_update(self):
        settings = _settings(webhooks=[_webhook("wh1", "/hooks/one")])
        mgr = TriggerManager(settings)
        dispatcher = _make_dispatcher()
        app = build_webhook_app(settings, dispatcher, manager=mgr)

        async with TestClient(TestServer(app)) as cl:
            # /hooks/two doesn't exist yet.
            resp = await cl.post(
                "/hooks/two",
                headers={"Authorization": "Bearer tok_456"},
                json={"text": "hi"},
            )
            assert resp.status == 404

            # Hot-reload: add /hooks/two.
            mgr.update(
                _settings(
                    webhooks=[
                        _webhook("wh1", "/hooks/one"),
                        _webhook("wh2", "/hooks/two", secret="tok_456"),
                    ]
                )
            )

            resp = await cl.post(
                "/hooks/two",
                headers={"Authorization": "Bearer tok_456"},
                json={"text": "hi"},
            )
            assert resp.status == 202

    @pytest.mark.anyio
    async def test_removed_webhook_returns_404(self):
        settings = _settings(
            webhooks=[
                _webhook("wh1", "/hooks/one"),
                _webhook("wh2", "/hooks/two"),
            ]
        )
        mgr = TriggerManager(settings)
        dispatcher = _make_dispatcher()
        app = build_webhook_app(settings, dispatcher, manager=mgr)

        async with TestClient(TestServer(app)) as cl:
            # Both exist.
            resp = await cl.post(
                "/hooks/one",
                headers={"Authorization": "Bearer tok_123"},
                json={"text": "hi"},
            )
            assert resp.status == 202

            # Hot-reload: remove /hooks/one.
            mgr.update(_settings(webhooks=[_webhook("wh2", "/hooks/two")]))

            resp = await cl.post(
                "/hooks/one",
                headers={"Authorization": "Bearer tok_123"},
                json={"text": "hi"},
            )
            assert resp.status == 404

    @pytest.mark.anyio
    async def test_webhook_secret_update_takes_effect(self):
        settings = _settings(
            webhooks=[_webhook("wh1", "/hooks/test", secret="old_secret")]
        )
        mgr = TriggerManager(settings)
        dispatcher = _make_dispatcher()
        app = build_webhook_app(settings, dispatcher, manager=mgr)

        async with TestClient(TestServer(app)) as cl:
            # Old secret works.
            resp = await cl.post(
                "/hooks/test",
                headers={"Authorization": "Bearer old_secret"},
                json={"text": "hi"},
            )
            assert resp.status == 202

            # Hot-reload: change secret.
            mgr.update(
                _settings(
                    webhooks=[_webhook("wh1", "/hooks/test", secret="new_secret")]
                )
            )

            # Old secret fails.
            resp = await cl.post(
                "/hooks/test",
                headers={"Authorization": "Bearer old_secret"},
                json={"text": "hi"},
            )
            assert resp.status == 401

            # New secret works.
            resp = await cl.post(
                "/hooks/test",
                headers={"Authorization": "Bearer new_secret"},
                json={"text": "hi"},
            )
            assert resp.status == 202


# ── Cron scheduler with TriggerManager ──────────────────────────────


class TestCronSchedulerWithManager:
    def test_manager_crons_readable(self):
        """Cron scheduler reads manager.crons each tick."""
        mgr = TriggerManager(_settings(crons=[_cron("a"), _cron("b")]))
        assert len(mgr.crons) == 2

        mgr.update(_settings(crons=[_cron("c")]))
        assert len(mgr.crons) == 1
        assert mgr.crons[0].id == "c"

    def test_manager_default_timezone_readable(self):
        mgr = TriggerManager(_settings(default_timezone="America/New_York"))
        assert mgr.default_timezone == "America/New_York"

        mgr.update(_settings(default_timezone="Australia/Melbourne"))
        assert mgr.default_timezone == "Australia/Melbourne"

        mgr.update(_settings())
        assert mgr.default_timezone is None


# ── Helper methods added for rc4: id lists, per-chat filters, remove_cron ──


class TestTriggerManagerHelpers:
    def test_cron_ids_and_webhook_ids_snapshots(self):
        mgr = TriggerManager(
            _settings(
                crons=[_cron("a"), _cron("b")],
                webhooks=[_webhook("h1"), _webhook("h2", path="/hooks/other")],
            )
        )
        assert sorted(mgr.cron_ids()) == ["a", "b"]
        assert sorted(mgr.webhook_ids()) == ["h1", "h2"]

    def test_cron_ids_empty_when_no_crons(self):
        mgr = TriggerManager(_settings())
        assert mgr.cron_ids() == []
        assert mgr.webhook_ids() == []

    def test_crons_for_chat_uses_cron_chat_id(self):
        mgr = TriggerManager(
            _settings(
                crons=[
                    _cron("a", chat_id=111),
                    _cron("b", chat_id=222),
                    _cron("c", chat_id=111),
                ]
            )
        )
        matching = mgr.crons_for_chat(111)
        assert sorted(c.id for c in matching) == ["a", "c"]

    def test_crons_for_chat_falls_back_to_default(self):
        mgr = TriggerManager(_settings(crons=[_cron("a"), _cron("b", chat_id=999)]))
        # Default chat catches crons without chat_id.
        matching = mgr.crons_for_chat(555, default_chat_id=555)
        assert [c.id for c in matching] == ["a"]
        # Non-default chat only sees its explicit match.
        matching = mgr.crons_for_chat(999, default_chat_id=555)
        assert [c.id for c in matching] == ["b"]

    def test_crons_for_chat_no_default_excludes_unset(self):
        """When no default_chat_id is passed, crons with chat_id=None are excluded."""
        mgr = TriggerManager(_settings(crons=[_cron("a"), _cron("b", chat_id=555)]))
        matching = mgr.crons_for_chat(555)
        assert [c.id for c in matching] == ["b"]

    def test_webhooks_for_chat_filters_by_chat_id(self):
        mgr = TriggerManager(
            _settings(
                webhooks=[
                    _webhook("h1", chat_id=111),
                    _webhook("h2", path="/hooks/other", chat_id=222),
                    _webhook("h3", path="/hooks/third", chat_id=111),
                ]
            )
        )
        matching = mgr.webhooks_for_chat(111)
        assert sorted(wh.id for wh in matching) == ["h1", "h3"]

    def test_remove_cron_removes_and_returns_true(self):
        mgr = TriggerManager(_settings(crons=[_cron("a"), _cron("b"), _cron("c")]))
        assert mgr.remove_cron("b") is True
        assert [c.id for c in mgr.crons] == ["a", "c"]

    def test_remove_cron_missing_returns_false(self):
        mgr = TriggerManager(_settings(crons=[_cron("a")]))
        assert mgr.remove_cron("missing") is False
        assert [c.id for c in mgr.crons] == ["a"]

    def test_remove_cron_atomic_during_iteration(self):
        """Iterators over the old list keep all entries even after a remove_cron."""
        mgr = TriggerManager(_settings(crons=[_cron("a"), _cron("b"), _cron("c")]))
        snapshot = mgr.crons  # iterator captures this reference
        assert mgr.remove_cron("b") is True
        # Old snapshot still shows all three — list replacement is safe.
        assert [c.id for c in snapshot] == ["a", "b", "c"]
        # New reference reflects the removal.
        assert [c.id for c in mgr.crons] == ["a", "c"]

    def test_remove_cron_then_update_rehydrates(self):
        """Config reload re-adds run_once crons that were previously removed."""
        mgr = TriggerManager(_settings(crons=[_cron("a", run_once=True)]))
        assert mgr.remove_cron("a") is True
        assert mgr.cron_ids() == []
        # Simulate a config reload with the same cron still in TOML.
        mgr.update(_settings(crons=[_cron("a", run_once=True)]))
        assert mgr.cron_ids() == ["a"]
