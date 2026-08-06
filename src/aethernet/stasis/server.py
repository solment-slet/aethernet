import asyncio
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor

import mss
from PIL import Image
from screeninfo import get_monitors

from aethernet.transport import AggregatingLink
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes
from aethernet.typing import LoggerLike

PROTOCOL_NAME = "stasis"

class ScreenManager:
    def __init__(self):
        monitors = get_monitors()
        self.monitor = next((m for m in monitors if m.is_primary), monitors[0])
        self.width =  self.monitor.width
        self.height =  self.monitor.height
        self.monitor_info = {
            "top": self.monitor.y,
            "left": self.monitor.x,
            "width": self.width,
            "height": self.height,
        }

        self._executor = ThreadPoolExecutor(max_workers=1)
        self._sct: mss.base.MSSBase | None = None

    def _ensure_sct(self) -> mss.base.MSSBase:
        if self._sct is None:
            self._sct = mss.MSS()
        return self._sct

    def _grab_sync(self) -> Image.Image:
        sct = self._ensure_sct()
        screenshot = sct.grab(self.monitor_info)
        return Image.frombytes(
            "RGB",
            (screenshot.width, screenshot.height),
            screenshot.bgra,
            "raw",
            "BGRX",
        )

    def _close_sync(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    async def screenshot(self) -> Image.Image:
        loop = asyncio.get_running_loop()
        # noinspection PyTypeChecker
        return await loop.run_in_executor(self._executor, self._grab_sync)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        # noinspection PyTypeChecker
        await loop.run_in_executor(self._executor, self._close_sync)
        self._executor.shutdown(wait=True)

class AethernetStasisServer:
    def __init__(
        self,
        link: AggregatingLink,
        *,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self._link = link
        self._logger = logger
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._closed = False

        self.connected = False
        self.session_id: str | None = None
        self.screen = ScreenManager()

    async def start(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(), name="AethernetStasisServer.dispatcher"
        )

    async def close(self) -> None:
        self._closed = True
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
        await self.screen.close()

    @staticmethod
    def generate_session_id() -> str:
        return uuid.uuid4().hex

    async def _dispatcher_loop(self) -> None:
        while not self._closed:
            stream_id = await self._link.accept_stream(PROTOCOL_NAME)
            asyncio.create_task(
                self._handle_stream(stream_id),
                name=f"AethernetStasisServer.stream.{stream_id}",
            )

    async def _handle_stream(self, stream_id: str) -> None:
        try:
            first = await self._link.recv_frame(stream_id)
            if first.frame_type != "meta":
                await self._send_error(
                    stream_id, "protocol_error", "Expected connect_request meta frame.",
                )
                return

            meta = decode_json_bytes(first.payload)
            if meta.get("kind") != "connect_request":
                await self._send_error(
                    stream_id, "protocol_error", "Expected connect_request kind.",
                )
                return

            if self.connected:
                # Если уже есть активное подключение
                await self._link.send_frame(
                    stream_id,
                    "meta",
                    encode_json_bytes(
                        {
                            "kind": "connect_response",
                            "status": "rejected",
                            "message": (
                                "A remote session is already active on this device. "
                                "Please wait until the current session ends."
                            ),
                        }
                    ),
                    end=True,
                )
                return

            # Установка подключения
            self.session_id = AethernetStasisServer.generate_session_id()
            await self._link.send_frame(
                stream_id,
                "meta",
                encode_json_bytes(
                    {
                        "kind": "connect_response",
                        "status": "accepted",
                        "session_id": self.session_id,
                        "host": {
                            "width": self.screen.width,
                            "height": self.screen.height,
                        }
                    }
                ),
                end=True,
            )

            self.connected = True
            self._logger.info(f"New connection has been established: {self.session_id}")

        except Exception as e:
            await self._send_error(stream_id, "unknown_error", f"{type(e).__name__}: {e}")

    async def _send_error(self, stream_id: str, reason: str, message: str, log: bool = True) -> None:
        if log:
            self._logger.error(f"Sending error: {message}")
        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes({"kind": "error", "reason": reason, "message": message}),
            end=True,
        )
