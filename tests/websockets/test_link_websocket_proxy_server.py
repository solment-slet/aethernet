import asyncio
import json
import pytest

from unittest.mock import MagicMock, patch, AsyncMock

from aethernet.transport.ws_over_link.ws_over_link_server import (
    LinkWebSocketProxyServer,
)
from tests.helpers import make_frame, meta_payload

# ======================================================================
# Тесты start() / close()
# ======================================================================


@pytest.mark.asyncio
async def test_start_creates_dispatcher_task(mock_link):
    """start() должен создать фоновую задачу-диспетчер."""
    server = LinkWebSocketProxyServer(link=mock_link)

    # accept_stream висит вечно, чтобы диспетчер не упал
    mock_link.accept_stream = AsyncMock(side_effect=asyncio.CancelledError)

    await server.start()
    assert server._dispatcher_task is not None
    await server.close()


@pytest.mark.asyncio
async def test_close_cancels_dispatcher(mock_link):
    """close() должен отменить диспетчерскую задачу."""
    server = LinkWebSocketProxyServer(link=mock_link)
    mock_link.accept_stream = AsyncMock(side_effect=asyncio.CancelledError)

    await server.start()
    await server.close()

    assert server._dispatcher_task.cancelled() or server._dispatcher_task.done()
    assert server._closed is True


@pytest.mark.asyncio
async def test_close_without_start_is_safe(mock_link):
    """close() без предшествующего start() не должен падать."""
    server = LinkWebSocketProxyServer(link=mock_link)
    await server.close()  # не должно бросить исключение


# ======================================================================
# Тесты _try_handle_stream
# ======================================================================


@pytest.mark.asyncio
async def test_try_handle_stream_ignores_non_meta_frame(mock_link):
    """Если первый фрейм не meta — stream игнорируется."""
    server = LinkWebSocketProxyServer(link=mock_link)
    mock_link.recv_frame.return_value = make_frame("ws_text", b"hello")

    # Не должно упасть и не должно ничего отправить
    await server._try_handle_stream("stream-1")

    mock_link.send_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_handle_stream_ignores_unknown_meta_kind(mock_link):
    """Если kind != ws_open — stream тоже игнорируется."""
    server = LinkWebSocketProxyServer(link=mock_link)
    mock_link.recv_frame.return_value = make_frame(
        "meta", meta_payload({"kind": "http_request"})
    )

    await server._try_handle_stream("stream-1")

    mock_link.send_frame.assert_not_awaited()


# ======================================================================
# Тесты _handle_ws_stream — ошибка подключения
# ======================================================================


@pytest.mark.asyncio
async def test_handle_ws_stream_connect_error_sends_error_frame(mock_link):
    """Если websockets.connect() падает — клиенту уходит error-фрейм."""
    server = LinkWebSocketProxyServer(link=mock_link)

    open_meta = {"url": "ws://bad-host", "headers": [], "subprotocols": []}

    with patch(
        "aethernet.transport.ws_over_link.ws_over_link_server.websockets.connect",
        side_effect=OSError("connection refused"),
    ):
        await server._handle_ws_stream("stream-1", open_meta)

    mock_link.send_frame.assert_awaited_once()
    call_args = mock_link.send_frame.call_args

    assert call_args.args[1] == "meta"
    payload = json.loads(call_args.args[2])
    assert payload["kind"] == "error"
    assert "OSError" in payload["message"]
    assert call_args.kwargs.get("end") is True


# ======================================================================
# Тесты _pump_upstream_to_link
# ======================================================================


@pytest.mark.asyncio
async def test_pump_upstream_text_and_binary(mock_link):
    """Текстовые и бинарные сообщения от upstream корректно пересылаются в link."""
    server = LinkWebSocketProxyServer(link=mock_link)

    async def fake_aiter(self):
        yield "hello"
        yield b"\x01\x02"

    ws = MagicMock()
    ws.__aiter__ = fake_aiter
    ws.close_code = 1000
    ws.close_reason = "ok"

    await server._pump_upstream_to_link("stream-1", ws)

    calls = mock_link.send_frame.await_args_list
    assert calls[0].args == ("stream-1", "ws_text", b"hello")
    assert calls[1].args == ("stream-1", "ws_binary", b"\x01\x02")

    # Последний фрейм — ws_closed
    last = calls[2]
    assert last.args[1] == "meta"
    payload = json.loads(last.args[2])
    assert payload["kind"] == "ws_closed"
    assert payload["code"] == 1000
    assert last.kwargs.get("end") is True


@pytest.mark.asyncio
async def test_pump_upstream_exception_sends_error(mock_link):
    """Если при чтении из upstream падает исключение — уходит error-фрейм."""
    server = LinkWebSocketProxyServer(link=mock_link)

    async def bad_aiter(self):
        raise RuntimeError("upstream died")
        if False:
            yield

    ws = MagicMock()
    ws.__aiter__ = bad_aiter

    await server._pump_upstream_to_link("stream-1", ws)

    call_args = mock_link.send_frame.call_args
    payload = json.loads(call_args.args[2])
    assert payload["kind"] == "error"
    assert "RuntimeError" in payload["message"]
    assert call_args.kwargs.get("end") is True


# ======================================================================
# Тесты _pump_link_to_upstream
# ======================================================================


@pytest.mark.asyncio
async def test_pump_link_text_forwarded_to_ws(mock_link):
    """ws_text фреймы из link декодируются и отправляются в upstream."""
    server = LinkWebSocketProxyServer(link=mock_link)

    ws = AsyncMock()
    mock_link.recv_frame.side_effect = [
        make_frame("ws_text", b"hi there"),
        make_frame(
            "meta", meta_payload({"kind": "ws_close", "code": 1000, "reason": ""})
        ),
    ]

    await server._pump_link_to_upstream("stream-1", ws)

    ws.send.assert_any_await("hi there")
    ws.close.assert_awaited_once_with(code=1000, reason="")


@pytest.mark.asyncio
async def test_pump_link_binary_forwarded_to_ws(mock_link):
    """ws_binary фреймы передаются в upstream как bytes."""
    server = LinkWebSocketProxyServer(link=mock_link)

    ws = AsyncMock()
    mock_link.recv_frame.side_effect = [
        make_frame("ws_binary", b"\xde\xad\xbe\xef"),
        make_frame(
            "meta", meta_payload({"kind": "ws_close", "code": 1000, "reason": "done"})
        ),
    ]

    await server._pump_link_to_upstream("stream-1", ws)

    ws.send.assert_any_await(b"\xde\xad\xbe\xef")
    ws.close.assert_awaited_once_with(code=1000, reason="done")


@pytest.mark.asyncio
async def test_pump_link_ws_close_closes_upstream(mock_link):
    """ws_close мета-фрейм закрывает upstream с переданным кодом и причиной."""
    server = LinkWebSocketProxyServer(link=mock_link)

    ws = AsyncMock()
    mock_link.recv_frame.return_value = make_frame(
        "meta", meta_payload({"kind": "ws_close", "code": 1001, "reason": "going away"})
    )

    await server._pump_link_to_upstream("stream-1", ws)

    ws.close.assert_awaited_once_with(code=1001, reason="going away")


@pytest.mark.asyncio
async def test_pump_link_error_meta_closes_upstream_with_1011(mock_link):
    """error мета-фрейм закрывает upstream с кодом 1011."""
    server = LinkWebSocketProxyServer(link=mock_link)

    ws = AsyncMock()
    mock_link.recv_frame.return_value = make_frame(
        "meta", meta_payload({"kind": "error", "message": "something went wrong"})
    )

    await server._pump_link_to_upstream("stream-1", ws)

    ws.close.assert_awaited_once_with(code=1011, reason="remote error")


@pytest.mark.asyncio
async def test_pump_link_unknown_meta_kind_is_ignored(mock_link):
    """Неизвестный kind в meta-фрейме игнорируется, цикл продолжается."""
    server = LinkWebSocketProxyServer(link=mock_link)

    ws = AsyncMock()
    mock_link.recv_frame.side_effect = [
        make_frame("meta", meta_payload({"kind": "ping"})),  # игнорируем
        make_frame(
            "meta", meta_payload({"kind": "ws_close", "code": 1000, "reason": ""})
        ),
    ]

    await server._pump_link_to_upstream("stream-1", ws)

    # ws.send не вызывался, только ws.close в конце
    ws.send.assert_not_awaited()
    ws.close.assert_awaited_once()


# ======================================================================
# Тесты диспетчера — интеграционный уровень
# ======================================================================


@pytest.mark.asyncio
async def test_dispatcher_spawns_task_per_stream(mock_link):
    """Диспетчер должен создавать задачу для каждого принятого stream."""
    server = LinkWebSocketProxyServer(link=mock_link)

    # Два stream, потом CancelledError чтобы остановить диспетчер
    mock_link.accept_stream.side_effect = [
        "stream-A",
        "stream-B",
        asyncio.CancelledError(),
    ]
    # Оба stream — не наши (не meta), просто выходим
    mock_link.recv_frame.return_value = make_frame("ws_text", b"ignore me")

    await server.start()
    await asyncio.sleep(0.05)  # даём диспетчеру поработать
    await server.close()

    # accept_stream вызван минимум дважды (для двух stream-ов)
    assert mock_link.accept_stream.await_count >= 2
