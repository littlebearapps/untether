"""JSON-RPC peer for ``codex app-server --listen stdio://`` (#962).

Wire format (codex-cli 0.170.0, developers.openai.com/codex/app-server):
newline-delimited JSON-RPC 2.0 with the ``"jsonrpc"`` header omitted.

Single reader: the runner's read loop is the only consumer of stdout, so the
peer never awaits a response. ``submit`` writes a request and returns its id;
``feed`` classifies each line and hands responses back to the runner's state
machine. An awaited ``request()`` called from that loop would deadlock.

Server requests are answered here, fail-closed, unless the observer asks to
answer later (``DEFER``, #963 approval cards): the ones Untether knows get the
schema's decline shape, anything else a JSON-RPC error (Codex treats an error
as a denial). A deferred request comes back from ``feed`` as a
``ServerRequest`` and is answered with ``reply`` (from any task: writes are
locked). Never leave one unanswered while the turn runs, or it blocks.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import msgspec

from ..logging import get_logger

logger = get_logger(__name__)

type RequestId = int | str
type Handler = Callable[[dict[str, Any]], Any]

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
# "Server overloaded; retry later." (doc §Protocol): retry with backoff + jitter.
SERVER_OVERLOADED = -32001
MAX_OVERLOAD_RETRIES = 3
WRITE_TIMEOUT_S = 10.0

# Schema-shaped declines (schema-subset-0.171.0.json): the fail-closed
# answer for anything the runner doesn't defer (unattended runs, elicitation).
DECLINE_HANDLERS: dict[str, Handler] = {
    "item/commandExecution/requestApproval": lambda _p: {"decision": "decline"},
    "item/fileChange/requestApproval": lambda _p: {"decision": "decline"},
    # Deny = grant nothing (PermissionsRequestApprovalResponse).
    "item/permissions/requestApproval": lambda _p: {
        "permissions": {},
        "scope": "turn",
    },
    "mcpServer/elicitation/request": lambda _p: {"action": "decline", "content": None},
}

# Returned by ``on_server_request`` to keep a request open for ``reply``.
DEFER = object()


class CodexRpcClosed(Exception):
    """The app-server's stdin is closed or broken."""


class CodexRpcWriteTimeout(Exception):
    """A write to the app-server's stdin stalled past the bound."""


@dataclass(frozen=True, slots=True)
class RpcError:
    code: int
    message: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class Response:
    id: RequestId
    method: str
    result: Any = None
    error: RpcError | None = None


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """A server request the observer deferred; answer it with ``reply``."""

    id: RequestId
    method: str
    params: dict[str, Any] = field(default_factory=dict)


def retry_delay(
    attempt: int,
    *,
    base: float = 0.25,
    cap: float = 4.0,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Back-off before resubmitting after ``-32001``: exponential, capped,
    plus up to 25% jitter. ``attempt`` counts from 0."""
    return min(cap, base * 2 ** max(0, attempt)) * (1 + 0.25 * jitter())


class CodexRpcPeer:
    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        *,
        handlers: dict[str, Handler] | None = None,
        on_server_request: Callable[[str, dict[str, Any]], Any] | None = None,
        write_timeout: float = WRITE_TIMEOUT_S,
    ) -> None:
        self._send = send
        self._handlers = DECLINE_HANDLERS if handlers is None else handlers
        # Sees every server request first; returning DEFER keeps it open for
        # ``reply`` (#963). Anything else: the handler answers it now.
        self.on_server_request = on_server_request
        self._write_timeout = write_timeout
        self._lock = anyio.Lock()
        self._next_id = 1
        self.pending: dict[RequestId, str] = {}
        # Server requests left open for ``reply``: id -> method.
        self.deferred: dict[RequestId, str] = {}
        self.closed_reason: str | None = None

    async def _write(self, message: dict[str, Any]) -> None:
        if self.closed_reason is not None:
            raise CodexRpcClosed(self.closed_reason)
        data = msgspec.json.encode(message) + b"\n"
        try:
            with anyio.fail_after(self._write_timeout):
                async with self._lock:
                    await self._send(data)
        except TimeoutError as exc:
            raise CodexRpcWriteTimeout(
                f"write stalled for {self._write_timeout:g}s"
            ) from exc
        except (
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            BrokenPipeError,
            ConnectionResetError,
        ) as exc:
            raise CodexRpcClosed(type(exc).__name__) from exc

    async def submit(self, method: str, params: dict[str, Any] | None = None) -> int:
        """Write a request; returns its id without waiting for the answer."""
        rid = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": rid}
        if params is not None:
            message["params"] = params
        self.pending[rid] = method
        try:
            await self._write(message)
        except BaseException:
            self.pending.pop(rid, None)
            raise
        return rid

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    def forget(self, rid: RequestId) -> None:
        self.pending.pop(rid, None)

    def close(self, reason: str) -> None:
        # Deferred requests stay unanswered: the process is going away and
        # Codex clears its own side.
        self.pending.clear()
        self.deferred.clear()
        self.closed_reason = reason

    async def reply(self, rid: RequestId, result: Any) -> bool:
        """Answer a deferred server request, once. False (logged) for an id
        that isn't open, e.g. a second tap. Raises ``CodexRpcClosed`` after
        ``close``."""
        if self.closed_reason is not None:
            raise CodexRpcClosed(self.closed_reason)
        if self.deferred.pop(rid, None) is None:
            logger.info("codex.rpc.reply_not_open", id=rid)
            return False
        await self._write({"id": rid, "result": result})
        return True

    async def feed(self, line: bytes) -> Response | Notification | ServerRequest | None:
        """One stdout line → a ``Response``, ``Notification`` or deferred
        ``ServerRequest`` for the runner, or ``None`` (blank, bad, unmatched,
        or a server request the peer already answered)."""
        if not line.strip():
            return None
        try:
            message = msgspec.json.decode(line)
        except msgspec.DecodeError:
            logger.warning("codex.rpc.bad_line", length=len(line))
            return None
        if not isinstance(message, dict):
            logger.warning("codex.rpc.bad_line", length=len(line))
            return None
        method = message.get("method")
        has_id = "id" in message
        if isinstance(method, str) and has_id:
            return await self._answer(message["id"], method, message.get("params"))
        if has_id:
            return self._response(message)
        if isinstance(method, str):
            params = message.get("params")
            return Notification(method, params if isinstance(params, dict) else {})
        logger.warning("codex.rpc.bad_line", length=len(line))
        return None

    def _response(self, message: dict[str, Any]) -> Response | None:
        rid = message["id"]
        method = self.pending.pop(rid, None) if isinstance(rid, int | str) else None
        if method is None:
            logger.warning("codex.rpc.unmatched_response", id=rid)
            return None
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            return Response(
                rid,
                method,
                error=RpcError(
                    code=code if isinstance(code, int) else INTERNAL_ERROR,
                    message=str(error.get("message") or ""),
                    data=error.get("data"),
                ),
            )
        return Response(rid, method, result=message.get("result"))

    async def _answer(self, rid: Any, method: str, params: Any) -> ServerRequest | None:
        params = params if isinstance(params, dict) else {}
        if self.on_server_request is not None:
            try:
                verdict = self.on_server_request(method, params)
            except Exception as exc:  # noqa: BLE001 — an observer never breaks the stream
                logger.error(
                    "codex.rpc.observer_error", method=method, error=type(exc).__name__
                )
                verdict = None
            if verdict is DEFER and isinstance(rid, int | str):
                self.deferred[rid] = method
                return ServerRequest(rid, method, params)
        logger.warning("codex.app_server.unexpected_request", method=method)
        handler = self._handlers.get(method)
        reply: dict[str, Any]
        if handler is None:
            reply = {
                "id": rid,
                "error": {"code": METHOD_NOT_FOUND, "message": "not supported"},
            }
        else:
            try:
                reply = {"id": rid, "result": handler(params)}
            except Exception as exc:  # noqa: BLE001 — always answer
                logger.error(
                    "codex.rpc.handler_error", method=method, error=type(exc).__name__
                )
                reply = {
                    "id": rid,
                    "error": {"code": INTERNAL_ERROR, "message": "internal error"},
                }
        await self._write(reply)
        return None
