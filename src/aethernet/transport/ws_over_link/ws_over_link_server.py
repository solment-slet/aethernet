from __future__ import annotations

import asyncio
from typing import Any

import websockets

from aethernet.transport import AggregatingLink
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes


class LinkWebSocketProxyServer:
    def __init__(
        self,
        link: AggregatingLink,
        *,
        recv_flush_interval: float = 0.2,
    ) -> None:
        self._link = link
        self._recv_flush_interval = recv_flush_interval
        self._closed = False
        self._dispatcher_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(), name="LinkWebSocketProxyServer.dispatcher"
        )

    async def close(self) -> None:
        self._closed = True
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass

    async def _dispatcher_loop(self) -> None:
        while not self._closed:
            stream_id = await self._link.accept_stream()
            asyncio.create_task(
                self._try_handle_stream(stream_id), name=f"ws_proxy.{stream_id}"
            )

    async def _try_handle_stream(self, stream_id: str) -> None:
        """
        Пробуем понять, это ws_open или не наш stream.
        Если не ws_open — просто выходим, другой сервер (HTTP) обработает свой stream.
        """
        first = await self._link.recv_frame(stream_id)
        if first.frame_type != "meta":
            return

        meta = decode_json_bytes(first.payload)
        if meta.get("kind") != "ws_open":
            # stream не наш; в текущем дизайне это проблема,
            # потому что мы уже съели первый frame.
            # Ниже объясню, как правильно решить через единый router.
            return

        await self._handle_ws_stream(stream_id, meta)

    async def _handle_ws_stream(
        self, stream_id: str, open_meta: dict[str, Any]
    ) -> None:
        url = open_meta["url"]
        headers = [tuple(x) for x in open_meta.get("headers", [])]
        subprotocols = open_meta.get("subprotocols", [])

        try:
            async with websockets.connect(
                url,
                additional_headers=headers or None,
                subprotocols=subprotocols or None,
            ) as ws:
                await self._link.send_frame(
                    stream_id,
                    "meta",
                    encode_json_bytes(
                        {
                            "kind": "ws_opened",
                            "subprotocol": ws.subprotocol,
                        }
                    ),
                )

                task_up = asyncio.create_task(
                    self._pump_upstream_to_link(stream_id, ws)
                )
                task_down = asyncio.create_task(
                    self._pump_link_to_upstream(stream_id, ws)
                )

                done, pending = await asyncio.wait(
                    {task_up, task_down},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            await self._link.send_frame(
                stream_id,
                "meta",
                encode_json_bytes(
                    {"kind": "error", "message": f"{type(e).__name__}: {e}"}
                ),
                end=True,
            )

    async def _pump_upstream_to_link(self, stream_id: str, ws) -> None:
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
                encode_json_bytes(
                    {
                        "kind": "ws_closed",
                        "code": getattr(ws, "close_code", None),
                        "reason": getattr(ws, "close_reason", "") or "",
                    }
                ),
                end=True,
            )
        except Exception as e:
            await self._link.send_frame(
                stream_id,
                "meta",
                encode_json_bytes(
                    {"kind": "error", "message": f"{type(e).__name__}: {e}"}
                ),
                end=True,
            )

    async def _pump_link_to_upstream(self, stream_id: str, ws) -> None:
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

            meta = decode_json_bytes(frame.payload)
            kind = meta.get("kind")

            if kind == "ws_close":
                await ws.close(
                    code=int(meta.get("code", 1000)), reason=meta.get("reason", "")
                )
                return

            if kind == "error":
                await ws.close(code=1011, reason="remote error")
                return
