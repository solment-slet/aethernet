from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence, Mapping

from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatus,
)
from websockets.frames import Close
from websockets.http11 import Response
from websockets.datastructures import Headers

from aethernet.transport import AggregatingLink
from aethernet.transport._utils import encode_json_bytes, decode_json_bytes

HeadersLike = Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_headers(headers: HeadersLike) -> list[tuple[str, str]]:
    """
    Normalize headers into a list of (key, value) string tuples.
    """
    if isinstance(headers, Mapping):
        pairs = headers.items()
    else:
        pairs = headers

    result = []
    for item in pairs:
        k, v = item
        if not isinstance(k, str) or not isinstance(v, str):
            raise TypeError(
                f"Header key and value must be str, got {type(k)!r} and {type(v)!r}"
            )
        result.append((k, v))
    return result


def _make_connection_closed(code: int | None, reason: str) -> ConnectionClosed:
    """
    Construct a websocket ConnectionClosed exception from a close frame.
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
    """
    Result returned when a WebSocket connection is successfully opened.
    """

    subprotocol: str | None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LinkWebSocketClient:
    """
    WebSocket client running on node A over an AggregatingLink.

    This class mimics the interface of ``websockets.ClientConnection``.
    """

    def __init__(
        self,
        link: AggregatingLink,
        stream_id: str,
        subprotocol: str | None,
    ) -> None:
        self._link = link
        self._stream_id = stream_id
        self._subprotocol = subprotocol
        self._closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def subprotocol(self) -> str | None:
        return self._subprotocol

    async def send(self, data: str | bytes) -> None:
        """
        Send a text or binary message over the WebSocket.
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
            raise TypeError(f"send() accepts str | bytes, got {type(data).__name__!r}")

    async def recv(self) -> str | bytes:
        """
        Receive the next message from the WebSocket.
        """
        if self._closed:
            raise _make_connection_closed(self.close_code, self.close_reason)

        while True:
            frame = await self._link.recv_frame(self._stream_id)

            if frame.frame_type == "ws_text":
                return frame.payload.decode("utf-8")

            if frame.frame_type == "ws_binary":
                return frame.payload

            if frame.frame_type != "meta":
                continue

            meta = decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "ws_closed":
                self._closed = True
                self.close_code = meta.get("code")
                self.close_reason = meta.get("reason", "")
                raise _make_connection_closed(self.close_code, self.close_reason)

            if kind == "error":
                self._closed = True
                raise RuntimeError(meta.get("message", "Remote WebSocket error"))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Gracefully close the WebSocket connection.
        """
        if self._closed:
            return

        await self._link.send_frame(
            self._stream_id,
            "meta",
            encode_json_bytes({"kind": "ws_close", "code": code, "reason": reason}),
            end=True,
        )
        self._closed = True
        self.close_code = code
        self.close_reason = reason

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        """
        Async iterator over incoming WebSocket messages.
        """
        while True:
            try:
                yield await self.recv()
            except ConnectionClosed:
                return


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class _WebSocketConnector:
    """
    Returned by AethernetWebSockets.connect().

    Supports two usage styles:

    1) Await style (manual close):
        ws = await AethernetWebSockets(link).connect("wss://...")

    2) Async context manager (auto close):
        async with AethernetWebSockets(link).connect("wss://...") as ws:
            ...
    """

    def __init__(
        self,
        link: AggregatingLink,
        uri: str,
        *,
        origin: str | None = None,
        subprotocols: Sequence[str] | None = None,
        compression: str | None = None,
        additional_headers: HeadersLike | None = None,
        user_agent_header: str | None = None,
        proxy: str | None = None,
        open_timeout: float | None = None,
        ping_interval: float | None = None,
        ping_timeout: float | None = None,
        close_timeout: float | None = None,
        max_size: int | None | tuple[int | None, int | None] = 2**20,
        max_queue: int | None | tuple[int | None, int | None] = 16,
        write_limit: int | tuple[int, int | None] = 2**15,
    ) -> None:
        self._link = link
        self._uri = uri
        self._params = dict(
            origin=origin,
            subprotocols=None if subprotocols is None else list(subprotocols),
            compression=compression,
            additional_headers=(
                None
                if additional_headers is None
                else _normalize_headers(additional_headers)
            ),
            user_agent_header=user_agent_header,
            proxy=proxy,
            open_timeout=open_timeout,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            close_timeout=close_timeout,
            max_size=max_size,
            max_queue=max_queue,
            write_limit=write_limit,
        )
        self._client: LinkWebSocketClient | None = None

    async def _open(self) -> LinkWebSocketClient:
        """
        Perform the WebSocket handshake over AggregatingLink.
        """
        stream_id = self._link.new_stream_id()

        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes({"kind": "ws_open", "uri": self._uri, **self._params}),
        )

        first = await self._link.recv_frame(stream_id)
        if first.frame_type != "meta":
            raise RuntimeError("Protocol error: expected meta frame (ws_opened/error)")

        meta = decode_json_bytes(first.payload)
        kind = meta.get("kind")

        if kind == "error":
            message = meta.get("message", "WebSocket open failed")
            raise InvalidStatus(
                Response(
                    status_code=403,
                    reason_phrase="Forbidden",
                    headers=Headers(),
                    body=message.encode("utf-8"),
                )
            )

        if kind != "ws_opened":
            raise RuntimeError(f"Protocol error: expected ws_opened, got {kind!r}")

        return LinkWebSocketClient(
            link=self._link,
            stream_id=stream_id,
            subprotocol=meta.get("subprotocol"),
        )

    # --- awaitable interface ---

    def __await__(self):
        return self._open().__await__()

    # --- async context manager ---

    async def __aenter__(self) -> LinkWebSocketClient:
        self._client = await self._open()
        return self._client

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client is not None:
            await self._client.close()


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class AethernetWebSockets:
    """
    Facade that emulates the ``websockets`` module over AggregatingLink.

    Example usage:

        ws_factory = AethernetWebSockets(link)

        # Option 1: manual lifecycle
        ws = await ws_factory.connect("wss://example.com")
        await ws.send("hello")
        await ws.close()

        # Option 2: context manager
        async with ws_factory.connect("wss://example.com") as ws:
            await ws.send("hello")
            msg = await ws.recv()
    """

    def __init__(self, link: AggregatingLink) -> None:
        self._link = link

    def connect(
        self,
        uri: str,
        *,
        origin: str | None = None,
        subprotocols: Sequence[str] | None = None,
        compression: str | None = None,
        additional_headers: HeadersLike | None = None,
        user_agent_header: str | None = None,
        proxy: str | None = None,
        open_timeout: float | None = None,
        ping_interval: float | None = None,
        ping_timeout: float | None = None,
        close_timeout: float | None = None,
        max_size: int | None | tuple[int | None, int | None] = 2**20,
        max_queue: int | None | tuple[int | None, int | None] = 16,
        write_limit: int | tuple[int, int | None] = 2**15,
    ) -> _WebSocketConnector:
        """
        Create a WebSocket connection over AggregatingLink.
        """
        return _WebSocketConnector(
            self._link,
            uri,
            origin=origin,
            subprotocols=subprotocols,
            compression=compression,
            additional_headers=additional_headers,
            user_agent_header=user_agent_header,
            proxy=proxy,
            open_timeout=open_timeout,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            close_timeout=close_timeout,
            max_size=max_size,
            max_queue=max_queue,
            write_limit=write_limit,
        )
