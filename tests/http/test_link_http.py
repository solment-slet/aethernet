import pytest
import json
import asyncio
import httpx
from unittest.mock import MagicMock, AsyncMock
from aethernet.transport.http_over_link.http_over_link import (
    LinkResponseByteStream,
    AethernetHttpx,
    LinkHTTPProxyServer,
)
from ..helpers import meta_payload, make_frame_with_end

# ======================================================================
# LinkResponseByteStream
# ======================================================================


@pytest.mark.asyncio
async def test_response_byte_stream_body_frames(mock_link):
    stream = LinkResponseByteStream(mock_link, "stream-1")

    mock_link.recv_frame.side_effect = [
        make_frame_with_end("body", b"chunk1"),
        make_frame_with_end("body", b"chunk2", end=True),
    ]

    chunks = [chunk async for chunk in stream]
    assert chunks == [b"chunk1", b"chunk2"]
    assert stream._closed is True


@pytest.mark.asyncio
async def test_response_byte_stream_response_end_meta(mock_link):
    stream = LinkResponseByteStream(mock_link, "stream-1")

    mock_link.recv_frame.side_effect = [
        make_frame_with_end("body", b"data"),
        make_frame_with_end("meta", meta_payload({"kind": "response_end"})),
    ]

    chunks = [chunk async for chunk in stream]
    assert chunks == [b"data"]
    assert stream._closed is True


@pytest.mark.asyncio
async def test_response_byte_stream_error_meta_raises(mock_link):
    stream = LinkResponseByteStream(mock_link, "stream-1")

    mock_link.recv_frame.return_value = make_frame_with_end(
        "meta", meta_payload({"kind": "error", "message": "boom"})
    )

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in stream:
            pass


@pytest.mark.asyncio
async def test_response_byte_stream_closed_yields_nothing(mock_link):
    stream = LinkResponseByteStream(mock_link, "stream-1")
    stream._closed = True

    chunks = [chunk async for chunk in stream]
    assert chunks == []
    mock_link.recv_frame.assert_not_awaited()


# ======================================================================
# AethernetHttpTransport
# ======================================================================


@pytest.mark.asyncio
async def test_transport_sends_request_frames(mock_link):
    transport = AethernetHttpx(mock_link)
    mock_link.new_stream_id.return_value = "s1"

    mock_link.recv_frame.side_effect = [
        make_frame_with_end(
            "meta",
            meta_payload(
                {
                    "kind": "response_start",
                    "status_code": 200,
                    "headers": [],
                    "streaming": False,
                }
            ),
        ),
        make_frame_with_end("meta", meta_payload({"kind": "response_end"}), end=True),
    ]

    request = httpx.Request("GET", "http://example.com/path")
    response = await transport.handle_async_request(request)

    assert response.status_code == 200

    calls = mock_link.send_frame.await_args_list
    first_meta = json.loads(calls[0].args[2])
    assert first_meta["kind"] == "request_start"
    assert first_meta["method"] == "GET"
    assert first_meta["url"] == "http://example.com/path"


@pytest.mark.asyncio
async def test_transport_sends_body_if_present(mock_link):
    transport = AethernetHttpx(mock_link)
    mock_link.new_stream_id.return_value = "s1"

    mock_link.recv_frame.side_effect = [
        make_frame_with_end(
            "meta",
            meta_payload(
                {
                    "kind": "response_start",
                    "status_code": 201,
                    "headers": [],
                    "streaming": False,
                }
            ),
        ),
        make_frame_with_end("meta", meta_payload({"kind": "response_end"}), end=True),
    ]

    request = httpx.Request("POST", "http://example.com/", content=b"hello body")
    await transport.handle_async_request(request)

    frame_types = [c.args[1] for c in mock_link.send_frame.await_args_list]
    assert "body" in frame_types


@pytest.mark.asyncio
async def test_transport_adds_proxy_header(mock_link):
    transport = AethernetHttpx(mock_link, use_proxy=True)
    mock_link.new_stream_id.return_value = "s1"

    mock_link.recv_frame.side_effect = [
        make_frame_with_end(
            "meta",
            meta_payload(
                {
                    "kind": "response_start",
                    "status_code": 200,
                    "headers": [],
                    "streaming": False,
                }
            ),
        ),
        make_frame_with_end("meta", meta_payload({"kind": "response_end"}), end=True),
    ]

    request = httpx.Request("GET", "http://example.com/")
    await transport.handle_async_request(request)

    first_meta = json.loads(mock_link.send_frame.await_args_list[0].args[2])
    headers_dict = dict(first_meta["headers"])
    assert headers_dict.get("Slet-Aethernet-Use-Proxy") == "1"


@pytest.mark.asyncio
async def test_transport_raises_on_error_response(mock_link):
    transport = AethernetHttpx(mock_link)
    mock_link.new_stream_id.return_value = "s1"

    mock_link.recv_frame.return_value = make_frame_with_end(
        "meta", meta_payload({"kind": "error", "message": "upstream failed"})
    )

    request = httpx.Request("GET", "http://example.com/")
    with pytest.raises(RuntimeError, match="upstream failed"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_transport_streaming_response_returns_stream(mock_link):
    transport = AethernetHttpx(mock_link)
    mock_link.new_stream_id.return_value = "s1"

    mock_link.recv_frame.side_effect = [
        make_frame_with_end(
            "meta",
            meta_payload(
                {
                    "kind": "response_start",
                    "status_code": 200,
                    "headers": [],
                    "streaming": True,
                }
            ),
        ),
    ]

    request = httpx.Request("GET", "http://example.com/sse")
    response = await transport.handle_async_request(request)

    assert isinstance(response.stream, LinkResponseByteStream)


# ======================================================================
# LinkHTTPProxyServer — start / close
# ======================================================================


@pytest.mark.asyncio
async def test_proxy_server_start_and_close(mock_link):
    server = LinkHTTPProxyServer(link=mock_link)
    mock_link.accept_stream.side_effect = asyncio.CancelledError()

    await server.start()
    await server.close()

    assert server._closed is True
    assert server._dispatcher_task.done()


# ======================================================================
# LinkHTTPProxyServer — _handle_stream
# ======================================================================


@pytest.mark.asyncio
async def test_handle_stream_non_meta_sends_error(mock_link):
    server = LinkHTTPProxyServer(link=mock_link)
    mock_link.recv_frame.return_value = make_frame_with_end("body", b"garbage")

    await server._handle_stream("stream-1")

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"


@pytest.mark.asyncio
async def test_handle_stream_wrong_kind_sends_error(mock_link):
    server = LinkHTTPProxyServer(link=mock_link)
    mock_link.recv_frame.return_value = make_frame_with_end(
        "meta", meta_payload({"kind": "ws_open"})
    )

    await server._handle_stream("stream-1")

    payload = json.loads(mock_link.send_frame.call_args.args[2])
    assert payload["kind"] == "error"


@pytest.mark.asyncio
async def test_handle_stream_simple_get(mock_link):
    """GET без тела — сервер делает запрос и возвращает ответ."""
    server = LinkHTTPProxyServer(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame_with_end(
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
        make_frame_with_end("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"content-type": "text/plain"})
    mock_response.aread = AsyncMock(return_value=b"hello")
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    server._client = mock_client

    await server._handle_stream("stream-1")

    frames = mock_link.send_frame.await_args_list
    response_start = json.loads(frames[0].args[2])
    assert response_start["kind"] == "response_start"
    assert response_start["status_code"] == 200

    response_end = json.loads(frames[-1].args[2])
    assert response_end["kind"] == "response_end"


@pytest.mark.asyncio
async def test_handle_stream_sse_response_calls_streaming(mock_link):
    """SSE ответ (text/event-stream) уходит через _proxy_streaming_response."""
    server = LinkHTTPProxyServer(link=mock_link)

    mock_link.recv_frame.side_effect = [
        make_frame_with_end(
            "meta",
            meta_payload(
                {
                    "kind": "request_start",
                    "method": "GET",
                    "url": "http://example.com/events",
                    "headers": [],
                    "has_body": False,
                }
            ),
        ),
        make_frame_with_end("meta", meta_payload({"kind": "request_end"}), end=True),
    ]

    async def fake_aiter_bytes():
        yield b"data: hello\n\n"

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"content-type": "text/event-stream"})
    mock_response.aiter_bytes = fake_aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    server._client = mock_client

    await server._handle_stream("stream-1")

    frames = mock_link.send_frame.await_args_list
    kinds = [json.loads(f.args[2])["kind"] for f in frames if f.args[1] == "meta"]
    assert "response_start" in kinds
    assert "response_end" in kinds

    body_frames = [f for f in frames if f.args[1] == "body"]
    assert any(b"data: hello" in f.args[2] for f in body_frames)
