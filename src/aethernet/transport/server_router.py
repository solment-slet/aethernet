from __future__ import annotations

import asyncio
import json
from typing import Any
import logging

import httpx
import websockets

from aethernet.transport import AggregatingLink
from aethernet.typing import LoggerLike


def _encode_json_bytes(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_json_bytes(data: bytes) -> dict[str, Any]:
    return json.loads(data.decode("utf-8"))


class ServerRouter:
    def __init__(
        self,
        link: AggregatingLink,
        *,
        http_client: httpx.AsyncClient | None = None,
        proxy_http_client: httpx.AsyncClient | None = None,
        sse_flush_bytes: int = 32 * 1024,
        sse_flush_interval: float = 0.5,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self._link = link
        self._http_client = http_client or httpx.AsyncClient(timeout=None)
        self._proxy_http_client = proxy_http_client or httpx.AsyncClient(timeout=None)
        self._sse_flush_bytes = sse_flush_bytes
        self._sse_flush_interval = sse_flush_interval
        self._logger = logger
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="MachineBRouter")

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._http_client.aclose()

    async def _loop(self) -> None:
        while not self._closed:
            stream_id = await self._link.accept_stream()
            asyncio.create_task(
                self._handle_stream(stream_id), name=f"MachineBRouter.{stream_id}"
            )

    async def _handle_stream(self, stream_id: str) -> None:
        print(f"B: new stream {stream_id}")
        try:
            first = await self._link.recv_frame(stream_id)
            print(f"B: got first frame stream={stream_id} type={first.frame_type}")

            if first.frame_type != "meta":
                await self._send_error(
                    stream_id, "Protocol error: first frame must be meta"
                )
                return

            meta = _decode_json_bytes(first.payload)
            kind = meta.get("kind")
            print(f"B: first meta kind={kind} stream={stream_id}")

            if kind == "request_start":
                await self._handle_http(stream_id, meta)
                return

            if kind == "ws_open":
                await self._handle_ws(stream_id, meta)
                return

            await self._send_error(stream_id, f"Unknown initial kind: {kind!r}")

        except Exception as e:
            print(f"B: exception in _handle_stream stream={stream_id}: {e!r}")
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

    async def _handle_http(self, stream_id: str, first_meta: dict[str, Any]) -> None:
        print("Выполняется _handle_http")
        method = first_meta["method"]
        url = first_meta["url"]
        headers: list[tuple[str, str]] = [
            (str(k), str(v)) for k, v in first_meta.get("headers", [])
        ]

        use_proxy = False
        new_headers = []

        for k, v in headers:
            if k.lower() == "slet-aethernet-use-proxy":
                use_proxy = True
                continue
            new_headers.append((k, v))

        headers = new_headers
        http_proxy = self._proxy_http_client if use_proxy else self._http_client

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

            meta = _decode_json_bytes(frame.payload)
            if meta.get("kind") == "request_end":
                break
            if frame.end:
                break

        body = b"".join(body_parts)

        req = http_proxy.build_request(
            method=method,
            url=url,
            headers=headers,
            content=body,
        )

        print("Готовимся к отправке сообщения")
        resp = await http_proxy.send(req, stream=True)
        print("Получили ответ!")

        content_type = resp.headers.get("content-type", "")
        is_streaming = "text/event-stream" in content_type.lower()

        await self._link.send_frame(
            stream_id,
            "meta",
            _encode_json_bytes(
                {
                    "kind": "response_start",
                    "status_code": resp.status_code,
                    "headers": list(resp.headers.multi_items()),
                    "streaming": is_streaming,
                }
            ),
        )

        try:
            if is_streaming:
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
            else:
                content = await resp.aread()
                if content:
                    await self._link.send_frame(stream_id, "body", content)

            await self._link.send_frame(
                stream_id,
                "meta",
                _encode_json_bytes({"kind": "response_end"}),
                end=True,
            )
        finally:
            await resp.aclose()

    async def _handle_ws(self, stream_id: str, first_meta: dict[str, Any]) -> None:
        url = first_meta["url"]
        headers = [tuple(x) for x in first_meta.get("headers", [])]
        subprotocols = first_meta.get("subprotocols", [])

        print(f"B: WS open stream={stream_id} url={url}")

        try:
            print(f"B: WS connecting upstream stream={stream_id}")
            async with websockets.connect(
                url,
                # Возможно тут надо extra_headers, см. ниже
                additional_headers=headers or None,
                subprotocols=subprotocols or None,
            ) as ws:
                print(f"B: WS connected upstream stream={stream_id}")

                await self._link.send_frame(
                    stream_id,
                    "meta",
                    _encode_json_bytes(
                        {
                            "kind": "ws_opened",
                            "subprotocol": ws.subprotocol,
                        }
                    ),
                )
                print(f"B: WS sent ws_opened stream={stream_id}")

                up = asyncio.create_task(self._ws_upstream_to_link(stream_id, ws))
                down = asyncio.create_task(self._ws_link_to_upstream(stream_id, ws))

                done, pending = await asyncio.wait(
                    {up, down}, return_when=asyncio.FIRST_COMPLETED
                )

                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            print(f"B: WS exception stream={stream_id}: {e!r}")
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

    async def _ws_upstream_to_link(self, stream_id: str, ws) -> None:
        try:
            async for message in ws:
                if isinstance(message, str):
                    await self._link.send_frame(
                        stream_id, "ws_text", message.encode("utf-8")
                    )
                else:
                    await self._link.send_frame(stream_id, "ws_binary", bytes(message))

            await self._link.send_frame(
                stream_id,
                "meta",
                _encode_json_bytes(
                    {
                        "kind": "ws_closed",
                        "code": getattr(ws, "close_code", None),
                        "reason": getattr(ws, "close_reason", "") or "",
                    }
                ),
                end=True,
            )
        except Exception as e:
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

    async def _ws_link_to_upstream(self, stream_id: str, ws) -> None:
        while True:
            frame = await self._link.recv_frame(stream_id)

            if frame.frame_type == "ws_text":
                await ws.send(frame.payload.decode("utf-8"))
                continue

            if frame.frame_type == "ws_binary":
                await ws.send(frame.payload)
                continue

            if frame.frame_type != "meta":
                continue

            meta = _decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "ws_close":
                await ws.close(
                    code=int(meta.get("code", 1000)), reason=meta.get("reason", "")
                )
                return

            if kind == "error":
                await ws.close(code=1011, reason="remote error")
                return

    async def _send_error(self, stream_id: str, message: str) -> None:
        await self._link.send_frame(
            stream_id,
            "meta",
            _encode_json_bytes({"kind": "error", "message": message}),
            end=True,
        )
