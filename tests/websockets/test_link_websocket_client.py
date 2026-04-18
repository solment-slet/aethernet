import pytest
import json
from unittest.mock import MagicMock, AsyncMock
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
    ConnectionClosedError,
)

from aethernet.transport.ws_over_link.ws_over_link import LinkWebSocketClient
from tests.helpers import meta_frame

# ======================================================================
# Тесты отправки сообщений
# ======================================================================


@pytest.mark.asyncio
async def test_send_text(mock_link):
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    await client.send("hello world")

    mock_link.send_frame.assert_awaited_once_with("stream-1", "ws_text", b"hello world")


@pytest.mark.asyncio
async def test_send_binary(mock_link):
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    await client.send(b"\x00\x01\x02")

    mock_link.send_frame.assert_awaited_once_with(
        "stream-1", "ws_binary", b"\x00\x01\x02"
    )


@pytest.mark.asyncio
async def test_send_raises_after_close(mock_link):
    """После закрытия send() должен бросать исключение."""
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)
    client._closed = True
    client.close_code = 1006
    client.close_reason = "connection lost"

    with pytest.raises(ConnectionClosedError):
        await client.send("hello")


# ======================================================================
# Тесты получения сообщений (recv)
# ======================================================================


@pytest.mark.asyncio
async def test_recv_text_and_binary(mock_link):
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    mock_link.recv_frame.side_effect = [
        MagicMock(frame_type="ws_text", payload=b"Hello"),
        MagicMock(frame_type="ws_binary", payload=b"\x01\x02\x03"),
    ]

    assert await client.recv() == "Hello"
    assert await client.recv() == b"\x01\x02\x03"
    assert mock_link.recv_frame.call_count == 2


# ======================================================================
# Тесты закрытия соединения через ws_closed мета-фрейм
# ======================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_code, expected_exception, is_normal_closure",
    [
        (1000, ConnectionClosedOK, True),
        (1001, ConnectionClosedOK, True),
        (1006, ConnectionClosedError, False),
        (1011, ConnectionClosedError, False),
        (4000, ConnectionClosedError, False),
        (None, ConnectionClosedError, False),
    ],
)
async def test_recv_raises_on_ws_closed(
    mock_link, close_code, expected_exception, is_normal_closure
):
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    mock_link.recv_frame.return_value = meta_frame(
        "ws_closed", code=close_code, reason="test reason"
    )

    with pytest.raises(expected_exception) as exc_info:
        await client.recv()

    assert client._closed is True
    assert client.close_code == close_code
    assert client.close_reason == "test reason"
    assert isinstance(exc_info.value, ConnectionClosed)
    assert isinstance(exc_info.value, expected_exception)


# ======================================================================
# Тесты метода close()
# ======================================================================


@pytest.mark.asyncio
async def test_close_sends_correct_frame(mock_link: AsyncMock):
    """close() должен отправить мета-фрейм ws_close и установить состояние."""
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    await client.close(code=1001, reason="normal shutdown")

    mock_link.send_frame.assert_awaited_once_with(
        "stream-1",
        "meta",
        json.dumps(
            {"kind": "ws_close", "code": 1001, "reason": "normal shutdown"},
            separators=(",", ":"),
        ).encode("utf-8"),
        end=True,
    )

    assert client._closed is True
    assert client.close_code == 1001
    assert client.close_reason == "normal shutdown"


@pytest.mark.asyncio
async def test_close_is_idempotent(mock_link: AsyncMock):
    """Повторный вызов close() не должен отправлять ничего."""
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    await client.close(1000, "first")
    await client.close(1000, "second")  # второй вызов

    # send_frame должен быть вызван только один раз
    mock_link.send_frame.assert_awaited_once()
    assert client.close_reason == "first"  # состояние не должно поменяться


# ======================================================================
# Тесты async iterator (__aiter__)
# ======================================================================


@pytest.mark.asyncio
async def test_async_for_iterates_until_closed(mock_link):
    """async for должен собирать сообщения до получения ConnectionClosed."""
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    mock_link.recv_frame.side_effect = [
        MagicMock(frame_type="ws_text", payload=b"msg1"),
        MagicMock(frame_type="ws_text", payload=b"msg2"),
        meta_frame("ws_closed", code=1000, reason="end"),
    ]

    messages = []
    async for msg in client:
        messages.append(msg)

    assert messages == ["msg1", "msg2"]
    assert client._closed is True


@pytest.mark.asyncio
async def test_async_for_stops_on_connection_closed_error(mock_link):
    """При аварийном закрытии async for тоже должен gracefully завершаться."""
    client = LinkWebSocketClient(link=mock_link, stream_id="stream-1", subprotocol=None)

    mock_link.recv_frame.side_effect = [
        MagicMock(frame_type="ws_text", payload=b"important message"),
        meta_frame("ws_closed", code=1011, reason="internal error"),
    ]

    messages = []
    async for msg in client:
        messages.append(msg)

    assert messages == ["important message"]
    assert client._closed is True


def test_subprotocol_property(mock_link):
    ws_client_kwargs = {
        "link": mock_link,
        "stream_id": "stream-1",
        "subprotocol": None,
    }
    client_none = LinkWebSocketClient(**ws_client_kwargs)
    ws_client_kwargs["subprotocol"] = "msgpack"
    client_string = LinkWebSocketClient(**ws_client_kwargs)

    assert client_none.subprotocol is None
    assert client_string.subprotocol == "msgpack"
