"""Codex app-server JSON-RPC peer (#962 phase 1): single reader, fail-closed."""

from __future__ import annotations

import json

import anyio
import pytest
from structlog.testing import capture_logs

from untether.runners.codex_rpc import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    CodexRpcClosed,
    CodexRpcPeer,
    CodexRpcWriteTimeout,
    Notification,
    Response,
    retry_delay,
)

pytestmark = pytest.mark.anyio


class Sink:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.chunks.append(data)

    @property
    def messages(self) -> list[dict]:
        return [json.loads(c) for c in self.chunks]


def _line(obj: dict) -> bytes:
    return json.dumps(obj).encode() + b"\n"


# --- responses ---------------------------------------------------------------


async def test_submit_returns_increasing_ids_without_waiting() -> None:
    sink = Sink()
    peer = CodexRpcPeer(sink.send)
    assert await peer.submit("initialize", {"clientInfo": {}}) == 1
    assert await peer.submit("thread/start") == 2
    assert sink.messages == [
        {"method": "initialize", "id": 1, "params": {"clientInfo": {}}},
        {"method": "thread/start", "id": 2},
    ]
    assert all(c.endswith(b"\n") and b"jsonrpc" not in c for c in sink.chunks)
    assert peer.pending == {1: "initialize", 2: "thread/start"}


async def test_responses_match_by_id_out_of_order_and_carry_the_method() -> None:
    peer = CodexRpcPeer(Sink().send)
    await peer.submit("initialize")
    await peer.submit("thread/start")
    second = await peer.feed(_line({"id": 2, "result": {"thread": {"id": "t"}}}))
    first = await peer.feed(_line({"id": 1, "result": {}}))
    assert second == Response(2, "thread/start", result={"thread": {"id": "t"}})
    assert first == Response(1, "initialize", result={})
    assert peer.pending == {}


async def test_error_response_carries_code_and_message() -> None:
    peer = CodexRpcPeer(Sink().send)
    await peer.submit("thread/resume")
    resp = await peer.feed(
        _line({"id": 1, "error": {"code": -32600, "message": "no rollout found"}})
    )
    assert isinstance(resp, Response) and resp.result is None
    assert resp.error is not None
    assert (resp.error.code, resp.error.message) == (-32600, "no rollout found")


async def test_late_or_duplicate_response_is_dropped_and_logged() -> None:
    peer = CodexRpcPeer(Sink().send)
    await peer.submit("initialize")
    assert await peer.feed(_line({"id": 1, "result": {}})) is not None
    with capture_logs() as logs:
        assert await peer.feed(_line({"id": 1, "result": {}})) is None
        assert await peer.feed(_line({"id": 99, "result": {}})) is None
    events = [e for e in logs if e["event"] == "codex.rpc.unmatched_response"]
    assert [e["id"] for e in events] == [1, 99]


# --- server requests ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("mcpServer/elicitation/request", {"action": "decline", "content": None}),
    ],
)
async def test_known_server_requests_get_the_schema_shaped_decline(
    method: str, result: dict
) -> None:
    sink = Sink()
    seen: list[str] = []
    peer = CodexRpcPeer(sink.send, on_server_request=lambda m, _p: seen.append(m))
    with capture_logs() as logs:
        out = await peer.feed(_line({"id": 0, "method": method, "params": {}}))
    assert out is None
    assert sink.messages == [{"id": 0, "result": result}]
    assert seen == [method]
    assert any(
        e["event"] == "codex.app_server.unexpected_request" and e["method"] == method
        for e in logs
    )


async def test_unknown_server_request_gets_method_not_found() -> None:
    sink = Sink()
    peer = CodexRpcPeer(sink.send)
    await peer.feed(_line({"id": 3, "method": "item/tool/requestUserInput"}))
    assert sink.messages[0]["id"] == 3
    assert sink.messages[0]["error"]["code"] == METHOD_NOT_FOUND


async def test_handler_exception_gets_internal_error() -> None:
    def boom(_params: dict) -> dict:
        raise RuntimeError("x")

    sink = Sink()
    peer = CodexRpcPeer(sink.send, handlers={"x/y": boom})
    await peer.feed(_line({"id": 4, "method": "x/y", "params": {}}))
    assert sink.messages == [
        {"id": 4, "error": {"code": INTERNAL_ERROR, "message": "internal error"}}
    ]


async def test_observer_exception_never_breaks_the_reply() -> None:
    def boom(_m: str, _p: dict) -> None:
        raise RuntimeError("x")

    sink = Sink()
    peer = CodexRpcPeer(sink.send, on_server_request=boom)
    await peer.feed(
        _line({"id": 5, "method": "item/fileChange/requestApproval", "params": {}})
    )
    assert sink.messages == [{"id": 5, "result": {"decision": "decline"}}]


@pytest.mark.parametrize("rid", [0, 7, "req-abc"])
async def test_reply_id_round_trips_with_its_json_type(rid: object) -> None:
    sink = Sink()
    peer = CodexRpcPeer(sink.send)
    await peer.feed(
        _line({"id": rid, "method": "item/fileChange/requestApproval", "params": {}})
    )
    assert sink.messages[0]["id"] == rid
    assert type(sink.messages[0]["id"]) is type(rid)


# --- notifications -----------------------------------------------------------


async def test_known_and_unknown_notifications_are_returned() -> None:
    peer = CodexRpcPeer(Sink().send)
    known = await peer.feed(
        _line({"method": "turn/started", "params": {"threadId": "t"}})
    )
    unknown = await peer.feed(_line({"method": "brand/new", "params": {"a": 1}}))
    bare = await peer.feed(_line({"method": "initialized"}))
    assert known == Notification("turn/started", {"threadId": "t"})
    assert unknown == Notification("brand/new", {"a": 1})
    assert bare == Notification("initialized", {})


async def test_notify_writes_no_id() -> None:
    sink = Sink()
    await CodexRpcPeer(sink.send).notify("initialized", {})
    assert sink.messages == [{"method": "initialized", "params": {}}]


# --- writes ------------------------------------------------------------------


async def test_concurrent_submits_never_interleave() -> None:
    chunks: list[bytes] = []

    async def slow_send(data: bytes) -> None:
        half = len(data) // 2
        chunks.append(data[:half])
        await anyio.lowlevel.checkpoint()
        chunks.append(data[half:])

    peer = CodexRpcPeer(slow_send)
    async with anyio.create_task_group() as tg:
        for i in range(50):
            tg.start_soon(peer.submit, "thread/start", {"n": i})
    lines = b"".join(chunks).splitlines()
    assert len(lines) == 50
    assert sorted(json.loads(line)["id"] for line in lines) == list(range(1, 51))


async def test_stalled_send_raises_write_timeout() -> None:
    async def stall(_data: bytes) -> None:
        await anyio.sleep_forever()

    peer = CodexRpcPeer(stall, write_timeout=0.05)
    with anyio.fail_after(2):
        with pytest.raises(CodexRpcWriteTimeout):
            await peer.submit("initialize")
    assert peer.pending == {}


@pytest.mark.parametrize(
    "exc", [anyio.BrokenResourceError, anyio.ClosedResourceError, BrokenPipeError]
)
async def test_broken_or_closed_send_raises_closed(exc: type[Exception]) -> None:
    async def broken(_data: bytes) -> None:
        raise exc()

    peer = CodexRpcPeer(broken)
    with pytest.raises(CodexRpcClosed):
        await peer.submit("initialize")
    with pytest.raises(CodexRpcClosed):
        await peer.notify("initialized")


# --- cleanup -----------------------------------------------------------------


async def test_forget_then_late_response_is_dropped() -> None:
    peer = CodexRpcPeer(Sink().send)
    rid = await peer.submit("turn/interrupt")
    peer.forget(rid)
    assert await peer.feed(_line({"id": rid, "result": {}})) is None


async def test_close_clears_pending_and_refuses_writes() -> None:
    peer = CodexRpcPeer(Sink().send)
    await peer.submit("initialize")
    peer.close("eof")
    assert peer.pending == {} and peer.closed_reason == "eof"
    with pytest.raises(CodexRpcClosed):
        await peer.submit("thread/start")


@pytest.mark.parametrize("line", [b"not json\n", b"[1, 2]\n", b'{"foo": 1}\n'])
async def test_bad_line_is_skipped_and_logged(line: bytes) -> None:
    peer = CodexRpcPeer(Sink().send)
    with capture_logs() as logs:
        assert await peer.feed(line) is None
    assert any(e["event"] == "codex.rpc.bad_line" for e in logs)


async def test_blank_line_is_ignored_quietly() -> None:
    with capture_logs() as logs:
        assert await CodexRpcPeer(Sink().send).feed(b"  \n") is None
    assert logs == []


# --- retry -------------------------------------------------------------------


def test_retry_delay_is_bounded_and_increasing() -> None:
    no_jitter = [retry_delay(a, jitter=lambda: 0.0) for a in range(6)]
    assert no_jitter == sorted(no_jitter) and no_jitter[0] < no_jitter[2]
    assert max(retry_delay(a, jitter=lambda: 1.0) for a in range(20)) <= 5.0
    assert retry_delay(0) > 0
