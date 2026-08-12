from __future__ import annotations

import httpx

from aethernet.http.common import (
    PROTOCOL_NAME,
    ResponseStart,
    headers_to_list,
)
from aethernet.transport import AggregatingLink
from aethernet.transport.utils import decode_json_bytes, encode_json_bytes


class LinkResponseByteStream(httpx.AsyncByteStream):
    def __init__(self, link: AggregatingLink, stream_id: str):
        self._link = link
        self._stream_id = stream_id
        self._closed = False

    async def __aiter__(self):
        if self._closed:
            return

        while True:
            frame = await self._link.recv_frame(self._stream_id)

            if frame.frame_type == "body":
                if frame.payload:
                    yield frame.payload
                if frame.end:
                    self._closed = True
                    return
                continue

            if frame.frame_type != "meta":
                continue

            meta = decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "response_end":
                self._closed = True
                return

            if kind == "error":
                self._closed = True
                raise RuntimeError(meta.get("message", "remote error"))

            if frame.end:
                self._closed = True
                return

    async def aclose(self) -> None:
        self._closed = True


class AethernetHttpx(httpx.AsyncBaseTransport):
    """
    Клиентский HTTP transport для машины A.

    Преобразует httpx.Request в link-протокол и получает ответ через link.

    Args:
        link: AggregatingLink для связи с машиной B.
        use_proxy: Если True, добавляет служебный заголовок
                   Slet-Aethernet-Use-Proxy, чтобы сервер использовал
                   свой proxy http client вместо обычного.
    """

    def __init__(
        self,
        link: AggregatingLink,
        *,
        use_proxy: bool = False,
    ) -> None:
        self._link = link
        self._use_proxy = use_proxy

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        stream_id = self._link.new_stream_id()
        body = await request.aread()

        headers = headers_to_list(request.headers)

        if self._use_proxy:
            headers.append(("Slet-Aethernet-Use-Proxy", "1"))

        request_start = {
            "kind": "request_start",
            "method": request.method,
            "url": str(request.url),
            "headers": headers,
            "has_body": bool(body),
        }

        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes(request_start),
            protocol=PROTOCOL_NAME,
        )

        if body:
            await self._link.send_frame(stream_id, "body", body)

        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes({"kind": "request_end"}),
            end=True,
        )

        first = await self._link.recv_frame(stream_id)
        if first.frame_type != "meta":
            raise RuntimeError("Protocol error: expected response_start meta frame")

        meta = decode_json_bytes(first.payload)
        kind = meta.get("kind")

        if kind == "error":
            raise RuntimeError(meta.get("message", "remote error"))

        if kind != "response_start":
            raise RuntimeError(f"Protocol error: expected response_start, got {kind!r}")

        response_start = ResponseStart(
            status_code=int(meta["status_code"]),
            headers=[tuple(x) for x in meta.get("headers", [])],
            streaming=bool(meta.get("streaming", False)),
        )

        if response_start.streaming:
            return httpx.Response(
                status_code=response_start.status_code,
                headers=response_start.headers,
                stream=LinkResponseByteStream(self._link, stream_id),
                request=request,
            )

        body_parts: list[bytes] = []
        while True:
            frame = await self._link.recv_frame(stream_id)

            if frame.frame_type == "body":
                body_parts.append(frame.payload)
                if frame.end:
                    break
                continue

            if frame.frame_type != "meta":
                continue

            meta = decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "response_end":
                break

            if kind == "error":
                raise RuntimeError(meta.get("message", "remote error"))

            if frame.end:
                break

        return httpx.Response(
            status_code=response_start.status_code,
            headers=response_start.headers,
            content=b"".join(body_parts),
            request=request,
        )

    async def aclose(self) -> None:
        return
