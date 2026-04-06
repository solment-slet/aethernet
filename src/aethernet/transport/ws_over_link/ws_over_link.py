from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.frames import Close

from aethernet.transport import AggregatingLink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_json_bytes(obj: dict[str, Any]) -> bytes:
    """Сериализует dict в компактный UTF-8 JSON."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_json_bytes(data: bytes) -> dict[str, Any]:
    """Десериализует UTF-8 JSON bytes в dict."""
    return json.loads(data.decode("utf-8"))


def _make_connection_closed(code: int | None, reason: str) -> ConnectionClosed:
    """
    Создаёт нативное исключение websockets на основе кода закрытия.

    Код 1000/1001 считается нормальным завершением (ConnectionClosedOK),
    всё остальное — аварийным (ConnectionClosedError).
    """
    rcvd = Close(code=code or 1006, reason=reason)
    if code in (1000, 1001):
        return ConnectionClosedOK(rcvd=rcvd, sent=None)
    return ConnectionClosedError(rcvd=rcvd, sent=None)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WSOpenResult:
    """Результат успешного открытия WebSocket-соединения."""

    subprotocol: str | None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LinkWebSocketClient:
    """
    Клиентская сторона WebSocket на машине A поверх AggregatingLink.

    Имитирует интерфейс ``websockets.ClientConnection``:
      - ``send(str | bytes)``
      - ``recv() -> str | bytes``
      - ``close()``
      - ``async for message in ws: ...``

    Исключения при закрытии соединения — нативные ``ConnectionClosed``
    из библиотеки ``websockets``, поэтому ловцы не видят разницы с
    обычным websockets-клиентом.
    """

    def __init__(
        self,
        link: AggregatingLink,
        stream_id: str,
        websockets: AethernetWebSockets,
        subprotocol: str | None,
    ) -> None:
        self._link = link
        self._stream_id = stream_id
        self._websockets = websockets

        #: Согласованный субпротокол (None если не использовался).
        self.subprotocol = subprotocol

        self._closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""

    @property
    def stream_id(self) -> str:
        """Идентификатор потока внутри AggregatingLink."""
        return self._stream_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(self, data: str | bytes) -> None:
        """
        Отправляет текстовое или бинарное сообщение на удалённую сторону.

        :param data: Строка отправляется как ``ws_text``,
                     bytes/bytearray/memoryview — как ``ws_binary``.
        :raises ConnectionClosed: Если соединение уже закрыто.
        :raises TypeError: Если передан неподдерживаемый тип.
        """
        if self._closed:
            raise _make_connection_closed(self.close_code, self.close_reason)

        if isinstance(data, str):
            await self._link.send_frame(
                self._stream_id,
                "ws_text",
                data.encode("utf-8"),
            )
        elif isinstance(data, (bytes, bytearray, memoryview)):
            await self._link.send_frame(
                self._stream_id,
                "ws_binary",
                bytes(data),
            )
        else:
            raise TypeError(f"send() принимает str | bytes, получен {type(data).__name__!r}")

    async def recv(self) -> str | bytes:
        """
        Ожидает и возвращает следующее сообщение от удалённой стороны.

        :returns: ``str`` для текстовых фреймов, ``bytes`` для бинарных.
        :raises ConnectionClosed: Если соединение закрыто нормально или с ошибкой.
        :raises RuntimeError: При протокольной ошибке на удалённой стороне.
        """
        if self._closed:
            raise _make_connection_closed(self.close_code, self.close_reason)

        while True:
            frame = await self._link.recv_frame(self._stream_id)

            if frame.frame_type == "ws_text":
                return frame.payload.decode("utf-8")

            if frame.frame_type == "ws_binary":
                return frame.payload

            # Не-meta фреймы пропускаем (служебные, future-proof)
            if frame.frame_type != "meta":
                continue

            meta = _decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "ws_closed":
                self._closed = True
                self.close_code = meta.get("code")
                self.close_reason = meta.get("reason", "")
                raise _make_connection_closed(self.close_code, self.close_reason)

            if kind == "error":
                self._closed = True
                raise RuntimeError(meta.get("message", "remote websocket error"))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Инициирует закрытие соединения с удалённой стороной.

        Повторный вызов на уже закрытом соединении — no-op.

        :param code: Код закрытия WebSocket (по умолчанию 1000 — нормальное закрытие).
        :param reason: Человекочитаемая причина закрытия.
        """
        if self._closed:
            return

        await self._link.send_frame(
            self._stream_id,
            "meta",
            _encode_json_bytes(
                {
                    "kind": "ws_close",
                    "code": code,
                    "reason": reason,
                }
            ),
            end=True,
        )

        self._closed = True
        self.close_code = code
        self.close_reason = reason

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        """
        Итерирует входящие сообщения до закрытия соединения.

        Совместим с ``async for message in ws``, как в стандартном websockets.
        ``ConnectionClosed`` поглощается — итерация просто завершается.
        """
        while True:
            try:
                yield await self.recv()
            except ConnectionClosed:
                return


# ---------------------------------------------------------------------------
# Module-level facade
# ---------------------------------------------------------------------------


class AethernetWebSockets:
    """
    Фасад, имитирующий модуль ``websockets`` поверх AggregatingLink.

    Использование::

        ws = AethernetWebSockets(link)
        async with ws.connect("wss://example.com") as conn:
            await conn.send("hello")
            msg = await conn.recv()

    Ограничение: одновременно поддерживается только одно соединение.
    """

    def __init__(self, link: AggregatingLink) -> None:
        self._link = link

    async def connect(
        self,
        url: str,
        *,
        additional_headers: list[tuple[str, str]] | None = None,
        subprotocols: Sequence[str] | None = None,
    ) -> LinkWebSocketClient:
        """
        Открывает WebSocket-соединение к ``url`` через AggregatingLink.

        :param url: Целевой WebSocket URL (``ws://`` или ``wss://``).
        :param additional_headers: Дополнительные HTTP-заголовки для хендшейка.
        :param subprotocols: Список желаемых субпротоколов.
        :returns: Готовый к использованию ``LinkWebSocketClient``.
        :raises InvalidStatus: Если удалённая сторона отклонила соединение.
        :raises RuntimeError: При протокольной ошибке.
        """
        stream_id = self._link.new_stream_id()

        # Отправляем запрос на открытие соединения
        await self._link.send_frame(
            stream_id,
            "meta",
            _encode_json_bytes(
                {
                    "kind": "ws_open",
                    "url": url,
                    "headers": list(additional_headers) if additional_headers else [],
                    "subprotocols": list(subprotocols) if subprotocols else [],
                }
            ),
        )

        # Ждём подтверждения от удалённой стороны
        first = await self._link.recv_frame(stream_id)
        if first.frame_type != "meta":
            raise RuntimeError("Протокольная ошибка: ожидался meta-фрейм ws_opened/error")

        meta = _decode_json_bytes(first.payload)
        kind = meta.get("kind")

        if kind == "error":
            # Имитируем InvalidStatus как настоящий websockets
            raise InvalidStatus(_FakeResponse(message=meta.get("message", "ws open failed")))

        if kind != "ws_opened":
            raise RuntimeError(f"Протокольная ошибка: ожидался ws_opened, получен {kind!r}")

        return LinkWebSocketClient(
            link=self._link,
            stream_id=stream_id,
            websockets=self,
            subprotocol=meta.get("subprotocol"),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """
    Минимальная заглушка HTTP-ответа для создания ``InvalidStatus``.

    ``InvalidStatus`` из websockets требует объект с атрибутом ``status_code``.
    Используется только внутри ``AethernetWebSockets.connect``.
    """

    status_code: int = 403
    headers: dict[str, str] = {}
    body: bytes = b""

    def __init__(self, message: str = "") -> None:
        self.message = message