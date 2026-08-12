from __future__ import annotations

import asyncio

import httpx

from aethernet.transport import AggregatingLink
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes
from aethernet.transport.http_over_link.common import PROTOCOL_NAME, split_proxy_header


class LinkHTTPProxyServer:
    """
    Серверная часть HTTP-over-link протокола (машина B).

    Поддерживает два upstream-клиента: обычный (`upstream_client`) и
    proxy (`proxy_upstream_client`). Выбор между ними делается по служебному
    заголовку Slet-Aethernet-Use-Proxy, который клиент (AethernetHttpx)
    добавляет при use_proxy=True. Заголовок вырезается перед отправкой
    апстриму — синхронизировано с ServerRouter._handle_http.
    """

    def __init__(
        self,
        link: AggregatingLink,
        *,
        upstream_client: httpx.AsyncClient | None = None,
        proxy_upstream_client: httpx.AsyncClient | None = None,
        sse_flush_bytes: int = 32 * 1024,
        sse_flush_interval: float = 0.5,
    ) -> None:
        self._link = link
        self._client = upstream_client or httpx.AsyncClient(timeout=None)
        self._proxy_client = proxy_upstream_client or httpx.AsyncClient(timeout=None)
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
        await self._proxy_client.aclose()

    async def _dispatcher_loop(self) -> None:
        while not self._closed:
            stream_id = await self._link.accept_stream(PROTOCOL_NAME)
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
            raw_headers = [tuple(x) for x in meta.get("headers", [])]
            headers, use_proxy = split_proxy_header(raw_headers)
            upstream = self._proxy_client if use_proxy else self._client

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

            req = upstream.build_request(
                method=method,
                url=url,
                headers=headers,
                content=body,
            )

            resp = await upstream.send(req, stream=True)

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