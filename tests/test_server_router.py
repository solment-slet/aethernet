import asyncio
import json
import pytest
import httpx
from unittest.mock import MagicMock, AsyncMock, patch

from aethernet.server_router import ServerRouter

# ======================================================================
# Helpers
# ======================================================================


def make_frame(frame_type: str, payload: bytes, end: bool = False) -> MagicMock:
    frame = MagicMock()
    frame.frame_type = frame_type
    frame.payload = payload
    frame.end = end
    return frame


def meta_payload(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def make_mock_response(
    status_code: int = 200,
    content_type: str = "text/plain",
    body: bytes = b"hello",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = httpx.Headers({"content-type": content_type})
    resp.aread = AsyncMock(return_value=body)
    resp.aclose = AsyncMock()
    resp.headers.multi_items = lambda: [("content-type", content_type)]
    return resp


# ======================================================================
# start / close
# ======================================================================


@pytest.mark.asyncio
async def test_start_creates_task(mock_link):
    server = ServerRouter(link=mock_link)
    mock_link.accept_stream.side_effect = asyncio.CancelledError()

    await server.start()
    assert server._task is not None
    await server.close()


@pytest.mark.asyncio
async def test_close_sets_closed_flag(mock_link):
    server = ServerRouter(link=mock_link)
    mock_link.accept_stream.side_effect = asyncio.CancelledError()

    await server.start()
    await server.close()

    assert server._closed is True
    assert server._task.done()


@pytest.mark.asyncio
async def test_close_without_start_is_safe(mock_link):
    server = ServerRouter(link=mock_link)
    await server.close()


# ======================================================================
# _handle_stream — роутинг
# ======================================================================


@pytest.mark.asyncio
async def test_handle_stream_non_meta_sends_error(mock_link):
    server = ServerRouter(link=mock_link)
    mock_link.recv_frame.return_value = make_frame("body", b"garbage")

    await server._handle_stream("s1")

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"


@pytest.mark.asyncio
async def test_handle_stream_unknown_kind_sends_error(mock_link):
    server = ServerRouter(link=mock_link)
    mock_link.recv_frame.return_value = make_frame(
        "meta", meta_payload({"kind": "something_unknown"})
    )

    await server._handle_stream("s1")

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"


@pytest.mark.asyncio
async def test_handle_stream_routes_to_http(mock_link):
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame(
            "meta",
            meta_payload(
                {
                    "kind": "request_start",
                    "method": "GET",
                    "url": "http://example.com/",
                    "headers": [],
                    "has_body": False,
                }
            ),
        ),
        make_frame("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    resp = make_mock_response()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=resp)
    server._http_client = mock_client

    await server._handle_stream("s1")

    frames = mock_link.send_frame.await_args_list
    first_meta = json.loads(frames[0].args[2])
    assert first_meta["kind"] == "response_start"


@pytest.mark.asyncio
async def test_handle_stream_routes_to_ws(mock_link):
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.return_value = make_frame(
        "meta", meta_payload({"kind": "ws_open", "uri": "ws://example.com"})
    )

    with patch(
        "aethernet.server_router.websockets.connect",
        side_effect=OSError("refused"),
    ):
        await server._handle_stream("s1")

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"
    assert "OSError" in payload["message"]


# ======================================================================
# _handle_http
# ======================================================================


@pytest.mark.asyncio
async def test_handle_http_simple_get(mock_link):
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    resp = make_mock_response(status_code=200, body=b"response body")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=resp)
    server._http_client = mock_client

    first_meta = {
        "kind": "request_start",
        "method": "GET",
        "url": "http://example.com/",
        "headers": [],
    }
    await server._handle_http("s1", first_meta)

    frames = mock_link.send_frame.await_args_list
    kinds = [json.loads(f.args[2])["kind"] for f in frames if f.args[1] == "meta"]
    assert "response_start" in kinds
    assert "response_end" in kinds


@pytest.mark.asyncio
async def test_handle_http_uses_proxy_client_when_header_present(mock_link):
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    resp = make_mock_response()
    proxy_client = AsyncMock(spec=httpx.AsyncClient)
    proxy_client.build_request.return_value = MagicMock()
    proxy_client.send = AsyncMock(return_value=resp)
    server._proxy_http_client = proxy_client

    http_client = AsyncMock(spec=httpx.AsyncClient)
    server._http_client = http_client

    first_meta = {
        "kind": "request_start",
        "method": "GET",
        "url": "http://example.com/",
        "headers": [("Slet-Aethernet-Use-Proxy", "1")],
    }
    await server._handle_http("s1", first_meta)

    proxy_client.send.assert_awaited_once()
    http_client.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_http_proxy_header_stripped_from_upstream(mock_link):
    """Заголовок Slet-Aethernet-Use-Proxy не должен уходить в upstream."""
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    resp = make_mock_response()
    proxy_client = AsyncMock(spec=httpx.AsyncClient)
    built_request = MagicMock()
    proxy_client.build_request.return_value = built_request
    proxy_client.send = AsyncMock(return_value=resp)
    server._proxy_http_client = proxy_client

    first_meta = {
        "kind": "request_start",
        "method": "GET",
        "url": "http://example.com/",
        "headers": [("Slet-Aethernet-Use-Proxy", "1"), ("x-custom", "value")],
    }
    await server._handle_http("s1", first_meta)

    _, kwargs = proxy_client.build_request.call_args
    sent_headers = dict(kwargs["headers"])
    assert "slet-aethernet-use-proxy" not in {k.lower() for k in sent_headers}


@pytest.mark.asyncio
async def test_handle_http_body_forwarded(mock_link):
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame("body", b"part1"),
        make_frame("body", b"part2", end=True),
    ]

    resp = make_mock_response()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=resp)
    server._http_client = mock_client

    first_meta = {
        "kind": "request_start",
        "method": "POST",
        "url": "http://example.com/upload",
        "headers": [],
    }
    await server._handle_http("s1", first_meta)

    _, kwargs = mock_client.build_request.call_args
    assert kwargs["content"] == b"part1part2"


@pytest.mark.asyncio
async def test_handle_http_sse_response(mock_link):
    server = ServerRouter(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    async def fake_aiter_bytes():
        yield b"data: event1\n\n"
        yield b"data: event2\n\n"

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = httpx.Headers({"content-type": "text/event-stream"})
    resp.headers.multi_items = lambda: [("content-type", "text/event-stream")]
    resp.aiter_bytes = fake_aiter_bytes
    resp.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=resp)
    server._http_client = mock_client

    first_meta = {
        "kind": "request_start",
        "method": "GET",
        "url": "http://example.com/sse",
        "headers": [],
    }
    await server._handle_http("s1", first_meta)

    body_frames = [
        f for f in mock_link.send_frame.await_args_list if f.args[1] == "body"
    ]
    all_body = b"".join(f.args[2] for f in body_frames)
    assert b"data: event1" in all_body
    assert b"data: event2" in all_body


# ======================================================================
# _handle_ws
# ======================================================================


@pytest.mark.asyncio
async def test_handle_ws_connect_error_sends_error(mock_link):
    server = ServerRouter(link=mock_link)

    with patch(
        "aethernet.server_router.websockets.connect",
        side_effect=OSError("connection refused"),
    ):
        await server._handle_ws("s1", {"kind": "ws_open", "uri": "ws://bad"})

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"
    assert "OSError" in payload["message"]


# ======================================================================
# _ws_upstream_to_link
# ======================================================================


@pytest.mark.asyncio
async def test_ws_upstream_text_and_binary(mock_link):
    server = ServerRouter(link=mock_link)

    async def fake_aiter(self):
        yield "hello"
        yield b"\xde\xad"

    ws = MagicMock()
    ws.__aiter__ = fake_aiter
    ws.close_code = 1000
    ws.close_reason = "ok"

    await server._ws_upstream_to_link("s1", ws)

    calls = mock_link.send_frame.await_args_list
    assert calls[0].args == ("s1", "ws_text", b"hello")
    assert calls[1].args == ("s1", "ws_binary", b"\xde\xad")

    last = calls[2]
    payload = json.loads(last.args[2])
    assert payload["kind"] == "ws_closed"
    assert payload["code"] == 1000
    assert last.kwargs.get("end") is True


@pytest.mark.asyncio
async def test_ws_upstream_exception_sends_error(mock_link):
    server = ServerRouter(link=mock_link)

    async def bad_aiter(self):
        raise RuntimeError("upstream died")
        yield

    ws = MagicMock()
    ws.__aiter__ = bad_aiter

    await server._ws_upstream_to_link("s1", ws)

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"
    assert "RuntimeError" in payload["message"]


# ======================================================================
# _ws_link_to_upstream
# ======================================================================


@pytest.mark.asyncio
async def test_ws_link_text_forwarded(mock_link):
    server = ServerRouter(link=mock_link)
    ws = AsyncMock()

    mock_link.recv_frame.side_effect = [
        make_frame("ws_text", b"hi"),
        make_frame(
            "meta", meta_payload({"kind": "ws_close", "code": 1000, "reason": ""})
        ),
    ]

    await server._ws_link_to_upstream("s1", ws)

    ws.send.assert_any_await("hi")
    ws.close.assert_awaited_once_with(code=1000, reason="")


@pytest.mark.asyncio
async def test_ws_link_binary_forwarded(mock_link):
    server = ServerRouter(link=mock_link)
    ws = AsyncMock()

    mock_link.recv_frame.side_effect = [
        make_frame("ws_binary", b"\x01\x02"),
        make_frame(
            "meta", meta_payload({"kind": "ws_close", "code": 1000, "reason": ""})
        ),
    ]

    await server._ws_link_to_upstream("s1", ws)

    ws.send.assert_any_await(b"\x01\x02")


@pytest.mark.asyncio
async def test_ws_link_error_closes_with_1011(mock_link):
    server = ServerRouter(link=mock_link)
    ws = AsyncMock()

    mock_link.recv_frame.return_value = make_frame(
        "meta", meta_payload({"kind": "error", "message": "boom"})
    )

    await server._ws_link_to_upstream("s1", ws)

    ws.close.assert_awaited_once_with(code=1011, reason="remote error")


@pytest.mark.asyncio
async def test_ws_link_unknown_meta_ignored(mock_link):
    server = ServerRouter(link=mock_link)
    ws = AsyncMock()

    mock_link.recv_frame.side_effect = [
        make_frame("meta", meta_payload({"kind": "ping"})),
        make_frame(
            "meta", meta_payload({"kind": "ws_close", "code": 1000, "reason": ""})
        ),
    ]

    await server._ws_link_to_upstream("s1", ws)

    ws.send.assert_not_awaited()
    ws.close.assert_awaited_once()
