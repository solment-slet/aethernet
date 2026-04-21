from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from aethernet.transport import AggregatingLink
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes

# =========================
# Общие сериализаторы
# =========================


def _headers_to_list(
    headers: httpx.Headers | list[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, httpx.Headers):
        return list(headers.multi_items())
    return list(headers)


# =========================
# Протокол поверх stream_id
# =========================

# frame_type = "meta"  -> JSON payload
# frame_type = "body"  -> raw bytes
#
# Для request:
#   meta: {
#       "kind": "request_start",
#       "method": "...",
#       "url": "...",
#       "headers": [[k,v], ...],
#       "has_body": bool,
#   }
#   body: ... bytes ... (если есть)
#   meta(end=True): {"kind": "request_end"}
#
# Для response non-stream:
#   meta: {
#       "kind": "response_start",
#       "status_code": 200,
#       "headers": [[k,v], ...],
#       "streaming": false
#   }
#   body: ... bytes ...
#   meta(end=True): {"kind": "response_end"}
#
# Для response stream:
#   meta: {
#       "kind": "response_start",
#       "status_code": 200,
#       "headers": [[k,v], ...],
#       "streaming": true
#   }
#   body: ... bytes chunk ...
#   body: ... bytes chunk ...
#   ...
#   meta(end=True): {"kind": "response_end"}
#
# Для errors:
#   meta(end=True): {
#       "kind": "error",
#       "message": "..."
#   }


@dataclass(slots=True)
class ResponseStart:
    status_code: int
    headers: list[tuple[str, str]]
    streaming: bool


# =========================
# Машина A: stream для httpx.Response
# =========================


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


# =========================
# Машина A: кастомный transport для httpx
# =========================


class AethernetHttpx(httpx.AsyncBaseTransport):
    """
    Клиентский HTTP transport для машины A.

    Преобразует httpx.Request в link-протокол и получает ответ через link.

    Args:
        link: AggregatingLink для связи с машиной B.
        use_proxy: Если True, добавляет заголовок Slet-Aethernet-Use-Proxy,
                   чтобы сервер использовал proxy http client.
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

        # Собираем заголовки
        headers = _headers_to_list(request.headers)

        # Если включен proxy режим — добавляем служебный заголовок
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
        )

        if body:
            await self._link.send_frame(
                stream_id,
                "body",
                body,
            )

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


# =========================
# Машина B: прокси-сервер поверх link
# =========================


class LinkHTTPProxyServer:
    def __init__(
        self,
        link: AggregatingLink,
        *,
        upstream_client: httpx.AsyncClient | None = None,
        sse_flush_bytes: int = 32 * 1024,
        sse_flush_interval: float = 0.5,
    ) -> None:
        self._link = link
        self._client = upstream_client or httpx.AsyncClient(timeout=None)
        self._sse_flush_bytes = sse_flush_bytes
        self._sse_flush_interval = sse_flush_interval
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(), name="LinkHTTPProxyServer.dispatcher"
        )

    async def close(self) -> None:
        self._closed = True
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()

    async def _dispatcher_loop(self) -> None:
        while not self._closed:
            stream_id = await self._link.accept_stream()
            asyncio.create_task(
                self._handle_stream(stream_id),
                name=f"LinkHTTPProxyServer.stream.{stream_id}",
            )

    async def _handle_stream(self, stream_id: str) -> None:
        try:
            first = await self._link.recv_frame(stream_id)
            if first.frame_type != "meta":
                await self._send_error(
                    stream_id, "Protocol error: expected request_start meta"
                )
                return

            meta = decode_json_bytes(first.payload)
            if meta.get("kind") != "request_start":
                await self._send_error(
                    stream_id, "Protocol error: expected request_start"
                )
                return

            method = meta["method"]
            url = meta["url"]
            headers = [tuple(x) for x in meta.get("headers", [])]

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

                if kind == "request_end":
                    break

                if kind == "error":
                    await self._send_error(
                        stream_id,
                        f"Remote request error: {meta.get('message', 'unknown')}",
                    )
                    return

                if frame.end:
                    break

            body = b"".join(body_parts)

            req = self._client.build_request(
                method=method,
                url=url,
                headers=headers,
                content=body,
            )

            resp = await self._client.send(req, stream=True)

            content_type = resp.headers.get("content-type", "")
            is_streaming = "text/event-stream" in content_type.lower()

            await self._link.send_frame(
                stream_id,
                "meta",
                encode_json_bytes(
                    {
                        "kind": "response_start",
                        "status_code": resp.status_code,
                        "headers": list(resp.headers.multi_items()),
                        "streaming": is_streaming,
                    }
                ),
            )

            if is_streaming:
                await self._proxy_streaming_response(stream_id, resp)
            else:
                content = await resp.aread()
                if content:
                    await self._link.send_frame(stream_id, "body", content)
                await self._link.send_frame(
                    stream_id,
                    "meta",
                    encode_json_bytes({"kind": "response_end"}),
                    end=True,
                )
                await resp.aclose()

        except Exception as e:
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

    async def _proxy_streaming_response(
        self, stream_id: str, resp: httpx.Response
    ) -> None:
        try:
            buffer = bytearray()
            loop = asyncio.get_running_loop()
            last_flush = loop.time()

            async for chunk in resp.aiter_bytes():
                if chunk:
                    buffer.extend(chunk)

                now = loop.time()
                if len(buffer) >= self._sse_flush_bytes or (
                    buffer and now - last_flush >= self._sse_flush_interval
                ):
                    await self._link.send_frame(stream_id, "body", bytes(buffer))
                    buffer.clear()
                    last_flush = now

            if buffer:
                await self._link.send_frame(stream_id, "body", bytes(buffer))

            await self._link.send_frame(
                stream_id,
                "meta",
                encode_json_bytes({"kind": "response_end"}),
                end=True,
            )
        finally:
            await resp.aclose()

    async def _send_error(self, stream_id: str, message: str) -> None:
        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes({"kind": "error", "message": message}),
            end=True,
        )
