"""Webhook HTTP server (aiohttp-based, runs as an anyio task)."""

from __future__ import annotations

import json

import anyio
from aiohttp import web

from ..logging import get_logger
from .auth import verify_auth
from .dispatcher import TriggerDispatcher
from .rate_limit import TokenBucketLimiter
from .settings import TriggersSettings, WebhookConfig
from .templating import render_prompt

logger = get_logger(__name__)


def build_webhook_app(
    settings: TriggersSettings,
    dispatcher: TriggerDispatcher,
) -> web.Application:
    """Build the aiohttp application for webhook handling."""
    routes_by_path: dict[str, WebhookConfig] = {
        wh.path: wh for wh in settings.webhooks
    }
    rate_limiter = TokenBucketLimiter(
        rate=settings.server.rate_limit,
        window=60.0,
    )
    max_body = settings.server.max_body_bytes

    # Warn about unauthenticated webhooks at build time.
    for wh in settings.webhooks:
        if wh.auth == "none":
            logger.warning(
                "triggers.webhook.no_auth",
                webhook_id=wh.id,
                path=wh.path,
            )

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps({"status": "ok", "webhooks": len(routes_by_path)}),
            content_type="application/json",
        )

    async def handle_webhook(request: web.Request) -> web.Response:
        path = request.path
        webhook = routes_by_path.get(path)
        if webhook is None:
            return web.Response(status=404, text="not found")

        try:
            return await _process_webhook(request, webhook, path)
        except Exception:
            logger.exception(
                "triggers.webhook.internal_error",
                webhook_id=webhook.id,
                path=path,
            )
            return web.Response(status=500, text="internal error")

    async def _process_webhook(
        request: web.Request, webhook: WebhookConfig, path: str
    ) -> web.Response:
        # Size check
        if request.content_length and request.content_length > max_body:
            return web.Response(status=413, text="payload too large")

        raw_body = await request.read()
        if len(raw_body) > max_body:
            return web.Response(status=413, text="payload too large")

        # Auth
        if not verify_auth(webhook, request.headers, raw_body):
            logger.warning(
                "triggers.webhook.auth_failed",
                webhook_id=webhook.id,
                path=path,
            )
            return web.Response(status=401, text="unauthorized")

        # Rate limit (per-webhook + global)
        if not rate_limiter.allow(webhook.id) or not rate_limiter.allow("__global__"):
            return web.Response(status=429, text="rate limited")

        # Parse payload
        if raw_body:
            try:
                payload = json.loads(raw_body)
                if not isinstance(payload, dict):
                    payload = {"_body": payload}
            except json.JSONDecodeError:
                return web.Response(status=400, text="invalid json")
        else:
            payload = {}

        # Event filter (e.g. GitHub X-GitHub-Event header)
        if webhook.event_filter:
            event_type = (
                request.headers.get("X-GitHub-Event", "")
                or request.headers.get("X-Event-Type", "")
            )
            if event_type != webhook.event_filter:
                return web.Response(status=200, text="filtered")

        # Template and dispatch
        prompt = render_prompt(webhook.prompt_template, payload)
        await dispatcher.dispatch_webhook(webhook, prompt)

        return web.Response(status=202, text="accepted")

    app = web.Application(client_max_size=max_body)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/{path:.*}", handle_webhook)
    return app


async def run_webhook_server(
    settings: TriggersSettings,
    dispatcher: TriggerDispatcher,
) -> None:
    """Run the webhook HTTP server until cancelled."""
    app = build_webhook_app(settings, dispatcher)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(
            runner,
            settings.server.host,
            settings.server.port,
        )
        await site.start()
        logger.info(
            "triggers.server.started",
            host=settings.server.host,
            port=settings.server.port,
            webhooks=len(settings.webhooks),
        )
        # Block until cancelled by structured concurrency.
        await anyio.sleep_forever()
    finally:
        await runner.cleanup()
