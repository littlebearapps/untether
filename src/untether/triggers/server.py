"""Webhook HTTP server (aiohttp-based, runs as an anyio task)."""

from __future__ import annotations

import asyncio
import ipaddress
import json

import anyio
from aiohttp import streams, web
from aiohttp.multipart import MultipartReader

from ..logging import get_logger
from .actions import _deny_reason, _resolve_file_path
from .auth import verify_auth
from .dispatcher import TriggerDispatcher
from .manager import TriggerManager
from .rate_limit import TokenBucketLimiter
from .settings import TriggersSettings, WebhookConfig
from .templating import render_prompt

logger = get_logger(__name__)

_SAFE_FILENAME_RE = __import__("re").compile(r"^[a-zA-Z0-9._-]+$")

# #906: most addresses the failed-auth limiter tracks at once. Its keys carry
# the caller's address, so without a cap an attacker picks how big it gets.
_AUTH_FAILED_MAX_KEYS = 10_000

# #906: peers whose address says nothing about who sent the request. Loopback
# is a reverse proxy or tunnel on this host (the default deployment: bind
# 127.0.0.1, proxy in front) or a local process; "unknown" is a connection
# with no peer address. Everyone behind such a peer shares one failed-auth
# bucket, so emptying it must never refuse a correct credential.
_LOOPBACK_PEER = "loopback"
_UNKNOWN_PEER = "unknown"
_SHARED_PEERS = frozenset({_LOOPBACK_PEER, _UNKNOWN_PEER})

# RFC 6052 well-known NAT64 prefix: the IPv4 sender is the low 32 bits.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def _client_address(request: web.Request) -> str:
    """The address a failed-auth bucket is scoped to (#906).

    This is the TCP peer aiohttp saw, never a forwarded header: a header is
    caller-supplied, so trusting one would let anyone choose their own bucket
    (or someone else's). Behind a reverse proxy every request therefore
    carries the proxy's address.

    Every loopback address is reported as ``"loopback"`` and a missing peer
    as ``"unknown"`` (see ``_SHARED_PEERS``). IPv6 peers are grouped by /64 —
    one subscriber usually holds the whole prefix and could otherwise take a
    fresh bucket per request — except behind the well-known NAT64 prefix,
    where a /64 would be every IPv4 sender at once. IPv4-mapped addresses
    (dual-stack sockets) count as their IPv4 address.
    """
    remote = request.remote
    if not remote:
        return _UNKNOWN_PEER
    try:
        addr = ipaddress.ip_address(remote.split("%", 1)[0])
    except ValueError:
        return remote
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return _LOOPBACK_PEER
    if isinstance(addr, ipaddress.IPv6Address) and addr not in _NAT64_WELL_KNOWN:
        return str(ipaddress.ip_network((addr, 64), strict=False))
    return str(addr)


def is_loopback_host(host: str) -> bool:
    """#382: whether a bind host is loopback-only (safe for unauthenticated
    webhooks).

    ``127.0.0.0/8`` and ``::1`` are loopback. The wildcard binds
    (``0.0.0.0`` / ``::``) reach external interfaces, so they are explicitly
    NON-loopback. ``host`` may be a hostname string rather than an IP literal:
    ``"localhost"`` is treated as loopback; any other unparseable/hostname value
    is treated conservatively as NON-loopback (we cannot prove it's local).
    """
    import ipaddress

    candidate = (host or "").strip()
    if candidate.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        # Hostname (or empty) — can't prove it's local; be conservative.
        return False
    return addr.is_loopback


def _unauthenticated_on_public_bind(settings: TriggersSettings) -> list[str]:
    """Return the ids of ``auth="none"`` webhooks that would be exposed on a
    non-loopback bind without the explicit opt-in. Empty list = safe to bind."""
    if settings.allow_unauthenticated_webhooks:
        return []
    if is_loopback_host(settings.server.host):
        return []
    return [wh.id for wh in settings.webhooks if wh.auth == "none"]


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
    Both methods accept arbitrary arguments because aiohttp's calling
    convention changes across minor versions (3.14 added
    ``resume_reading(resume_parser=...)``).
    """

    def __init__(self) -> None:
        self._reading_paused = False

    def pause_reading(
        self, *args: object, **kwargs: object
    ) -> None:  # pragma: no cover - no-op
        self._reading_paused = True

    def resume_reading(
        self, *args: object, **kwargs: object
    ) -> None:  # pragma: no cover - no-op
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
                # No destination configured — use the platform temp dir rather
                # than a hardcoded /tmp (portable across macOS/Linux; avoids
                # bandit B108 on predictable locations).
                dest_str = str(
                    Path(tempfile.gettempdir()) / "untether-uploads" / safe_name
                )

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
    manager: TriggerManager | None = None,
) -> web.Application:
    """Build the aiohttp application for webhook handling.

    If *manager* is provided, webhook lookups use ``manager.webhook_for_path()``
    so that config hot-reloads take effect on the next request.  When *manager*
    is ``None`` (backwards compat / tests), a static lookup table is used.
    """
    # Static fallback when no manager is provided.
    _static_routes: dict[str, WebhookConfig] | None = (
        None if manager is not None else {wh.path: wh for wh in settings.webhooks}
    )

    def _lookup(path: str) -> WebhookConfig | None:
        if manager is not None:
            return manager.webhook_for_path(path)
        assert _static_routes is not None
        return _static_routes.get(path)

    def _webhook_count() -> int:
        if manager is not None:
            return manager.webhook_count
        assert _static_routes is not None
        return len(_static_routes)

    rate_limiter = TokenBucketLimiter(
        rate=settings.server.rate_limit,
        window=60.0,
    )
    # #906: failed auth is counted apart from accepted requests, in its own
    # limiter — its keys include the caller's address, so it is capped, and
    # filling it must not be able to crowd out the request buckets above.
    auth_failed_limiter = TokenBucketLimiter(
        rate=settings.server.rate_limit,
        window=60.0,
        max_keys=_AUTH_FAILED_MAX_KEYS,
    )
    # #906: at most ``rate_limit`` ``auth_failed`` warnings a minute for each
    # webhook, whatever the number of addresses they come from. Keyed by
    # webhook id only, so it needs no cap.
    auth_warn_limiter = TokenBucketLimiter(
        rate=settings.server.rate_limit,
        window=60.0,
    )
    max_body = settings.server.max_body_bytes

    # Strong references to in-flight dispatch tasks (#281).  Without this,
    # asyncio can garbage-collect the task mid-flight and the dispatch is
    # silently dropped.  Tasks remove themselves on completion.
    _dispatch_tasks: set[asyncio.Task[None]] = set()

    # Warn about unauthenticated webhooks at build time (only when no manager;
    # TriggerManager.update() handles this for hot-reload).
    if manager is None:
        for wh in settings.webhooks:
            if wh.auth == "none":
                logger.warning(
                    "triggers.webhook.no_auth",
                    webhook_id=wh.id,
                    path=wh.path,
                )

    async def handle_health(request: web.Request) -> web.Response:
        # #294: surface paused state on the health endpoint so external
        # monitors can tell paused-but-up apart from healthy-and-active.
        paused = manager is not None and manager.is_paused
        return web.Response(
            text=json.dumps(
                {
                    "status": "paused" if paused else "ok",
                    "webhooks": _webhook_count(),
                    "paused": paused,
                }
            ),
            content_type="application/json",
        )

    async def handle_webhook(request: web.Request) -> web.Response:
        path = request.path
        webhook = _lookup(path)
        if webhook is None:
            return web.Response(status=404, text="not found")

        # #294: master pause — return 503 (not 404) so callers can
        # distinguish "route exists but is paused" from "route does not exist".
        if manager is not None and manager.is_paused:
            logger.info(
                "triggers.webhook.paused_skipped",
                webhook_id=webhook.id,
                path=path,
            )
            return web.Response(
                status=503,
                text="triggers paused",
                headers={"Retry-After": "60"},
            )

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
        # #906: failed auth spends a bucket of its own, keyed by webhook and
        # client address. A caller that empties its bucket is locked out: that
        # address gets 429 before any credential is checked — even a correct
        # one — so one address can't keep guessing past the limit, and it
        # locks out nobody else.
        #
        # That only holds while an address means one caller. A loopback peer
        # is a reverse proxy (or a local process) carrying everyone's
        # requests, so there the bucket is shared, and an empty one must not
        # refuse a correct credential — anyone could then shut the webhook to
        # its real senders with a few bad requests a minute. The same goes
        # for an address the full key table has no room to track: it has done
        # nothing itself. In both cases the credential is still checked;
        # only a failed attempt is refused (429 instead of 401).
        client = _client_address(request)
        auth_failed_key = f"{webhook.id}|{client}"
        # ``auth = "none"`` can't fail auth, so it has nothing to budget.
        budgeted = webhook.auth != "none"

        def _has_budget() -> bool:
            return not budgeted or auth_failed_limiter.available(auth_failed_key)

        def _locked_out() -> web.Response | None:
            """429 when this address has emptied a bucket that is its own."""
            if (
                client in _SHARED_PEERS
                or not auth_failed_limiter.tracked(auth_failed_key)
                or _has_budget()
            ):
                return None
            # Debug: a locked-out flood must not be able to fill the logs.
            logger.debug(
                "triggers.webhook.auth_locked_out",
                webhook_id=webhook.id,
                path=path,
                remote=client,
            )
            return web.Response(status=429, text="rate limited")

        # Advisory: turn a locked-out address away before reading its body.
        if (refused := _locked_out()) is not None:
            return refused

        # Size check
        if request.content_length and request.content_length > max_body:
            return web.Response(status=413, text="payload too large")

        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            # #906: a chunked body has no Content-Length for the check above,
            # so aiohttp enforces ``client_max_size`` here. Without this the
            # catch-all answered 500 and logged a traceback per request, for
            # a caller that has not authenticated.
            return web.Response(status=413, text="payload too large")
        if len(raw_body) > max_body:
            return web.Response(status=413, text="payload too large")

        # Auth. The check above ran before an await, so any number of parallel
        # requests may have passed it. These are authoritative: from here to
        # the spend below nothing awaits (``verify_auth`` is synchronous), so
        # no other request can run in between, a burst can't get more 401s
        # than the bucket holds, and each request gets one credential check.
        if (refused := _locked_out()) is not None:
            return refused
        has_budget = _has_budget()
        if not verify_auth(webhook, request.headers, raw_body):
            if not has_budget:
                # A shared or untracked peer over the budget. Debug, like the
                # lockout: a flood must not be able to fill the logs.
                logger.debug(
                    "triggers.webhook.auth_failed_over_limit",
                    webhook_id=webhook.id,
                    path=path,
                    remote=client,
                )
                return web.Response(status=429, text="rate limited")
            auth_failed_limiter.allow(auth_failed_key)
            out_of_budget = not auth_failed_limiter.available(auth_failed_key)
            # A warning per failure, up to ``rate_limit`` a minute for the
            # webhook; past that, debug. Slow guessing stays visible and a
            # flood from any number of addresses can't write a line each.
            log = (
                logger.warning if auth_warn_limiter.allow(webhook.id) else logger.debug
            )
            log(
                "triggers.webhook.auth_failed",
                webhook_id=webhook.id,
                path=path,
                remote=client,
                locked_out=out_of_budget and client not in _SHARED_PEERS,
            )
            return web.Response(status=401, text="unauthorized")

        # Rate limit (per-webhook + global). #906: all-or-nothing, so a
        # request the global bucket refuses doesn't spend the webhook's token.
        if not rate_limiter.allow_all(webhook.id, "__global__"):
            logger.warning(
                "triggers.webhook.rate_limited",
                webhook_id=webhook.id,
                path=path,
            )
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
    manager: TriggerManager | None = None,
) -> None:
    """Run the webhook HTTP server until cancelled.

    Degrades gracefully when the configured port cannot be bound: logs
    ``triggers.server.bind_failed`` with remediation hints and returns without
    raising, so the rest of the bot (polling, commands, crons) stays up.
    """
    # #382: refuse to expose unauthenticated webhooks on a non-loopback bind.
    # Like the bind-failure path below, this degrades gracefully — the webhook
    # server stays down but polling / commands / crons keep running.
    unauth = _unauthenticated_on_public_bind(settings)
    if unauth:
        logger.error(
            "triggers.server.refused_unauthenticated",
            host=settings.server.host,
            webhook_ids=unauth,
            hint=(
                'Webhooks with auth="none" on a non-loopback host are an '
                "unauthenticated remote-agent-run primitive. Add auth to these "
                "webhooks, bind [triggers.server] host to 127.0.0.1, or set "
                "[triggers] allow_unauthenticated_webhooks = true to opt in "
                "(local demos only)."
            ),
        )
        return

    app = build_webhook_app(settings, dispatcher, manager=manager)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(
            runner,
            settings.server.host,
            settings.server.port,
        )
        try:
            await site.start()
        except OSError as exc:
            # #320: another process holds the port (or similar bind failure).
            # Don't crash the bot — log an actionable error and disable
            # webhooks for this session.  Polling, commands, and crons are
            # unaffected.
            logger.error(
                "triggers.server.bind_failed",
                host=settings.server.host,
                port=settings.server.port,
                error=str(exc),
                hint=(
                    f"Another process may be using this port. "
                    f"Check with: ss -tlnp | grep {settings.server.port}"
                ),
                fix=(
                    "Set [triggers.server] port = <N> in untether.toml "
                    f"(current: {settings.server.port})"
                ),
            )
            return
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
