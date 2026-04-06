from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

from transport import AggregatingLink


def _encode_json_bytes(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_json_bytes(data: bytes) -> dict[str, Any]:
    return json.loads(data.decode("utf-8"))


@dataclass(slots=True)
class WSOpenResult:
    subprotocol: str | None


class LinkWebSocketClosed(Exception):
    def __init__(self, code: int | None = None, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket closed code={code} reason={reason!r}")


class LinkWebSocketClient:
    """
    Клиентская сторона на машине A.
    Похожа на websockets connection:
      - send(str|bytes)
      - recv() -> str|bytes
      - close()
      - async for ...
    """

    def __init__(
        self, link: AggregatingLink, stream_id: str, subprotocol: str | None
    ) -> None:
        self._link = link
        self._stream_id = stream_id
        self.subprotocol = subprotocol
        self._closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""

    @property
    def stream_id(self) -> str:
        return self._stream_id

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise LinkWebSocketClosed(self.close_code, self.close_reason)

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
            raise TypeError("WebSocket send() accepts str | bytes")

    async def recv(self) -> str | bytes:
        if self._closed:
            raise LinkWebSocketClosed(self.close_code, self.close_reason)

        while True:
            frame = await self._link.recv_frame(self._stream_id)

            if frame.frame_type == "ws_text":
                return frame.payload.decode("utf-8")

            if frame.frame_type == "ws_binary":
                return frame.payload

            if frame.frame_type != "meta":
                continue

            meta = _decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "ws_closed":
                self._closed = True
                self.close_code = meta.get("code")
                self.close_reason = meta.get("reason", "")
                raise LinkWebSocketClosed(self.close_code, self.close_reason)

            if kind == "error":
                self._closed = True
                raise RuntimeError(meta.get("message", "remote websocket error"))

    async def close(self, code: int = 1000, reason: str = "") -> None:
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
        while True:
            try:
                yield await self.recv()
            except LinkWebSocketClosed:
                return


async def link_websockets_connect(
    link: AggregatingLink,
    *,
    url: str,
    headers: list[tuple[str, str]] | None = None,
    subprotocols: list[str] | None = None,
) -> LinkWebSocketClient:
    stream_id = link.new_stream_id()

    await link.send_frame(
        stream_id,
        "meta",
        _encode_json_bytes(
            {
                "kind": "ws_open",
                "url": url,
                "headers": headers or [],
                "subprotocols": subprotocols or [],
            }
        ),
    )

    first = await link.recv_frame(stream_id)
    if first.frame_type != "meta":
        raise RuntimeError("Protocol error: expected ws_opened/error")

    meta = _decode_json_bytes(first.payload)
    kind = meta.get("kind")

    if kind == "error":
        raise RuntimeError(meta.get("message", "ws open failed"))

    if kind != "ws_opened":
        raise RuntimeError(f"Protocol error: expected ws_opened, got {kind!r}")

    return LinkWebSocketClient(
        link=link,
        stream_id=stream_id,
        subprotocol=meta.get("subprotocol"),
    )
