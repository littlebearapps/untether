"""Webhook HTTP server (aiohttp-based, runs as an anyio task)."""

from __future__ import annotations

import asyncio
import json

import anyio
from aiohttp import streams, web
from aiohttp.multipart import MultipartReader

from ..logging import get_logger
from .actions import _deny_reason, _resolve_file_path
from .auth import verify_auth
from .dispatcher import TriggerDispatcher
from .rate_limit import TokenBucketLimiter
from .settings import TriggersSettings, WebhookConfig
from .templating import render_prompt

logger = get_logger(__name__)

_SAFE_FILENAME_RE = __import__("re").compile(r"^[a-zA-Z0-9._-]+$")


class _MultipartError(Exception):
    """Raised during multipart parsing to return an HTTP error."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _multipart_reader_from_bytes(
    raw_body: bytes,
    content_type: str,
) -> MultipartReader:
    """Build a MultipartReader that streams from an in-memory body.

    The request body is pre-read by ``_process_webhook`` for size check and
    auth verification, so we can't use ``request.multipart()`` (that stream
    is already exhausted).  Instead, feed the bytes into a fresh
    :class:`aiohttp.streams.StreamReader` and construct the reader manually.
    """
    loop = asyncio.get_event_loop()
    stream = streams.StreamReader(
        _NullProtocol(),  # type: ignore[arg-type]
        limit=2**16,
        loop=loop,
    )
    stream.feed_data(raw_body)
    stream.feed_eof()
    return MultipartReader({"Content-Type": content_type}, stream)


class _NullProtocol:
    """Minimal stand-in for a transport protocol used by StreamReader.

    StreamReader only needs ``_reading_paused`` bookkeeping to be callable;
    it never flushes to a real transport when we feed bytes directly.
    """

    def __init__(self) -> None:
        self._reading_paused = False

    def pause_reading(self) -> None:  # pragma: no cover - no-op
        self._reading_paused = True

    def resume_reading(self) -> None:  # pragma: no cover - no-op
        self._reading_paused = False


async def _parse_multipart(
    raw_body: bytes,
    content_type: str,
    webhook: WebhookConfig,
) -> tuple[dict, str | None]:
    """Parse a multipart/form-data request.

    Returns ``(form_fields_dict, saved_file_path_or_none)``.
    Raises ``_MultipartError`` on validation failure.
    """
    import tempfile
    from pathlib import Path

    from .templating import render_template_fields

    form_fields: dict[str, str] = {}
    saved_path: str | None = None

    reader = _multipart_reader_from_bytes(raw_body, content_type)

    async for part in reader:
        if part.filename:
            # File part — sanitise filename and save.
            raw_name = part.filename or "upload.bin"
            safe_name = raw_name.replace("/", "_").replace("\\", "_")
            if not _SAFE_FILENAME_RE.match(safe_name):
                safe_name = "upload.bin"

            # Read file content with size limit.
            max_file = webhook.max_file_size_bytes
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await part.read_chunk(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_file:
                    raise _MultipartError(413, "file too large")
                chunks.append(chunk)
            file_data = b"".join(chunks)

            # Build destination path.
            form_fields["file"] = {"filename": safe_name}
            if webhook.file_destination:
                dest_template = webhook.file_destination
                template_ctx = {**form_fields, "file": {"filename": safe_name}}
                dest_str = render_template_fields(dest_template, template_ctx)
            else:
                dest_str = f"/tmp/untether-uploads/{safe_name}"

            target = _resolve_file_path(dest_str)
            if target is None:
                raise _MultipartError(400, "invalid file destination path")

            reason = _deny_reason(target)
            if reason is not None:
                raise _MultipartError(
                    400, f"file destination blocked by deny glob: {reason}"
                )

            # Atomic write.
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=target.parent,
                prefix=".untether-upload-",
            ) as handle:
                handle.write(file_data)
                temp_name = handle.name
            Path(temp_name).replace(target)
            saved_path = str(target)

            logger.info(
                "triggers.multipart.file_saved",
                webhook_id=webhook.id,
                filename=safe_name,
                path=saved_path,
                size=len(file_data),
            )
        else:
            # Form field.
            name = part.name or "_unnamed"
            value = (await part.read()).decode("utf-8", errors="replace")
            form_fields[name] = value

    return form_fields, saved_path


def build_webhook_app(
    settings: TriggersSettings,
    dispatcher: TriggerDispatcher,
) -> web.Application:
    """Build the aiohttp application for webhook handling."""
    routes_by_path: dict[str, WebhookConfig] = {wh.path: wh for wh in settings.webhooks}
    rate_limiter = TokenBucketLimiter(
        rate=settings.server.rate_limit,
        window=60.0,
    )
    max_body = settings.server.max_body_bytes

    # Strong references to in-flight dispatch tasks (#281).  Without this,
    # asyncio can garbage-collect the task mid-flight and the dispatch is
    # silently dropped.  Tasks remove themselves on completion.
    _dispatch_tasks: set[asyncio.Task[None]] = set()

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

        # Parse payload — multipart or JSON.
        payload: dict = {}
        file_saved_path: str | None = None

        content_type = request.content_type or ""
        if webhook.accept_multipart and content_type.startswith("multipart/"):
            # Pass the full header value (including the ``boundary=`` param)
            # so MultipartReader can locate the delimiter.
            full_ct = request.headers.get("Content-Type", content_type)
            try:
                payload, file_saved_path = await _parse_multipart(
                    raw_body, full_ct, webhook
                )
            except _MultipartError as exc:
                return web.Response(status=exc.status, text=exc.message)
            except ValueError as exc:
                logger.warning(
                    "triggers.webhook.multipart_parse_failed",
                    webhook_id=webhook.id,
                    error=str(exc),
                )
                return web.Response(status=400, text="invalid multipart body")
        elif raw_body:
            try:
                payload = json.loads(raw_body)
                if not isinstance(payload, dict):
                    payload = {"_body": payload}
            except json.JSONDecodeError:
                return web.Response(status=400, text="invalid json")

        if file_saved_path is not None:
            payload["file"] = {
                "saved_path": file_saved_path,
                "filename": payload.get("file", {}).get("filename", ""),
            }

        # Event filter (e.g. GitHub X-GitHub-Event header)
        if webhook.event_filter:
            event_type = request.headers.get(
                "X-GitHub-Event", ""
            ) or request.headers.get("X-Event-Type", "")
            if event_type != webhook.event_filter:
                return web.Response(status=200, text="filtered")

        # Route by action type — fire-and-forget so HTTP response (and
        # therefore the rate limiter, #281) isn't gated on slow downstream
        # work like Telegram outbox pacing or http_forward network calls.
        if webhook.action == "agent_run":
            prompt = render_prompt(webhook.prompt_template, payload)

            async def _run_agent() -> None:
                try:
                    await dispatcher.dispatch_webhook(webhook, prompt)
                except Exception:
                    logger.exception(
                        "triggers.webhook.dispatch_failed",
                        webhook_id=webhook.id,
                    )

            task = asyncio.create_task(_run_agent())
            _dispatch_tasks.add(task)
            task.add_done_callback(_dispatch_tasks.discard)
            return web.Response(status=202, text="accepted")

        # Non-agent actions.
        async def _run_action() -> None:
            try:
                await dispatcher.dispatch_action(webhook, payload, raw_body)
            except Exception:
                logger.exception(
                    "triggers.webhook.dispatch_failed",
                    webhook_id=webhook.id,
                )

        task = asyncio.create_task(_run_action())
        _dispatch_tasks.add(task)
        task.add_done_callback(_dispatch_tasks.discard)
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
