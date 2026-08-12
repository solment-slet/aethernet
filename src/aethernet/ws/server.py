from __future__ import annotations

import asyncio
import logging
from typing import Any

import websockets

from aethernet.transport import AggregatingLink
from aethernet.transport.utils import decode_json_bytes, encode_json_bytes
from aethernet.typing import LoggerLike
from aethernet.ws.client import PROTOCOL_NAME

# Поля ws_open meta, которые не передаются напрямую в websockets.connect(),
# а обрабатываются отдельно / служебные.
_EXCLUDED_META_FIELDS = {"uri", "kind"}

# Поля, которые websockets.connect ожидает как tuple, а по JSON приходят как list.
_TUPLE_KWARGS = {"max_size", "max_queue", "write_limit"}


class LinkWebSocketProxyServer:
    """
    Серверная часть WS-over-link протокола (машина B).

    Синхронизировано с ServerRouter._handle_ws:
      - ключ URL в ws_open meta — "uri" (а не "url").
      - все поля meta, кроме "uri"/"kind" и None-значений, прозрачно
        прокидываются в websockets.connect(**kwargs), включая произвольные
        параметры (headers, subprotocols, max_size, max_queue, write_limit
        и т.д.), а не только захардкоженный набор.
    """

    def __init__(
        self,
        link: AggregatingLink,
        *,
        recv_flush_interval: float = 0.2,
        logger: LoggerLike | None = None,
    ) -> None:
        self._link = link
        self._recv_flush_interval = recv_flush_interval
        self._logger = logger if logger is not None else logging.getLogger(__name__)
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
            stream_id = await self._link.accept_stream(PROTOCOL_NAME)
            asyncio.create_task(
                self._handle_stream(stream_id), name=f"ws_proxy.{stream_id}"
            )

    async def _handle_stream(self, stream_id: str) -> None:
        try:
            first = await self._link.recv_frame(stream_id)
            if first.frame_type != "meta":
                await self._send_error(
                    stream_id, "Protocol error: expected ws_open meta"
                )
                return

            meta = decode_json_bytes(first.payload)
            if meta.get("kind") != "ws_open":
                await self._send_error(
                    stream_id,
                    f"Protocol error: expected ws_open, got {meta.get('kind')!r}",
                )
                return

            await self._handle_ws_stream(stream_id, meta)
        except Exception as e:
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

    def _build_ws_kwargs(self, open_meta: dict[str, Any]) -> dict[str, Any]:
        ws_kwargs = {
            k: v
            for k, v in open_meta.items()
            if k not in _EXCLUDED_META_FIELDS and v is not None
        }

        for key in _TUPLE_KWARGS:
            if key in ws_kwargs and isinstance(ws_kwargs[key], list):
                ws_kwargs[key] = tuple(ws_kwargs[key])

        # headers/subprotocols приходят как списки, где нужно — приводим к tuple
        if "additional_headers" in ws_kwargs and isinstance(
            ws_kwargs["additional_headers"], list
        ):
            ws_kwargs["additional_headers"] = [
                tuple(h) for h in ws_kwargs["additional_headers"]
            ]
        if "subprotocols" in ws_kwargs and isinstance(ws_kwargs["subprotocols"], list):
            ws_kwargs["subprotocols"] = ws_kwargs["subprotocols"] or None

        return ws_kwargs

    async def _handle_ws_stream(
        self, stream_id: str, open_meta: dict[str, Any]
    ) -> None:
        uri = open_meta["uri"]
        ws_kwargs = self._build_ws_kwargs(open_meta)

        self._logger.info(f"WS proxy: connecting upstream stream={stream_id} uri={uri}")

        try:
            async with websockets.connect(uri, **ws_kwargs) as ws:
                self._logger.debug(f"WS proxy: connected upstream stream={stream_id}")

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

                _done, pending = await asyncio.wait(
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
            self._logger.error(f"WS proxy: exception stream={stream_id}: {e!r}")
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

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
            await self._send_error(stream_id, f"{type(e).__name__}: {e}")

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

    async def _send_error(self, stream_id: str, message: str) -> None:
        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes({"kind": "error", "message": message}),
            end=True,
        )
