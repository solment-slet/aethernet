import asyncio
import uuid
import logging
from typing import get_args
from concurrent.futures import ThreadPoolExecutor

import mss
from PIL import Image
from screeninfo import get_monitors

from aethernet.stasis.dataclasses import StreamMode, StreamModes
from aethernet.transport import AggregatingLink, Frame
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
    _SECONDS_TO_RECOVERY_STREAM_LOOP_AFTER_EXCEPTION = 5

    def __init__(
        self,
        link: AggregatingLink,
        *,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self._link = link
        self._logger = logger
        self._screen_request_event = asyncio.Event()
        self._mode_changed_event = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._closed = False

        self.connected = False
        self.screen_stream_id: str | None = None
        self.callbacks_stream_id: str | None = None
        self.session_id: str | None = None
        self.screen = ScreenManager()
        self.stream_mode: StreamMode = StreamMode(
            mode="on_request",
            interval_ms=None,
        )

    async def start(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(), name="AethernetStasisServer.dispatcher"
        )

    async def close(self) -> None:
        self._closed = True
        self.connected = False

        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

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
            frame = await self._link.recv_frame(stream_id)

            if self.connected and self.session_id:
                await self._handle_frame(frame)
                return

            if frame.frame_type != "meta":
                await self._send_error(
                    "protocol_error", "Expected connect_request meta frame.", stream_id,
                )
                return

            meta = decode_json_bytes(frame.payload)
            if meta.get("kind") != "connect_request":
                await self._send_error(
                    "protocol_error", "Expected connect_request kind.", stream_id,
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
            self.callbacks_stream_id = self._link.new_stream_id()
            await self._link.send_frame(
                stream_id,
                "meta",
                encode_json_bytes(
                    {
                        "kind": "connect_response",
                        "status": "accepted",
                        "session_id": self.session_id,
                        "callbacks_stream_id": self.callbacks_stream_id,
                        "host": {
                            "width": self.screen.width,
                            "height": self.screen.height,
                        },
                        "stream_mode": {
                            "mode": self.stream_mode.mode,
                            "interval_ms": self.stream_mode.interval_ms,
                        }
                    }
                ),
            )

            self.screen_stream_id = stream_id
            self.connected = True
            self._screen_request_event.clear()
            self._mode_changed_event.clear()
            self._stream_task = asyncio.create_task(
                self._stream_loop(), name=f"AethernetStasisServer.stream_loop.{self.session_id}"
            )
            self._logger.info(f"New connection has been established: {self.session_id}")

        except Exception as e:
            await self._send_error("unknown_error", f"{type(e).__name__}: {e}", stream_id)

    async def _handle_frame(self, frame: Frame) -> None:
        json = decode_json_bytes(frame.payload)
        kind = json["kind"]
        if json.get("session_id") != self.session_id:
            await self._send_error("invalid_session_error", "The session ID is missing or invalid.")
            return

        if kind == "set_stream_mode":
            # Set Screen Stream Mode
            mode = json.get("mode")
            interval_ms = json.get("interval_ms")
            if mode is None:
                await self._send_error("protocol_error", "The 'mode' field is missing.")
                return
            if not mode in get_args(StreamModes):
                await self._send_error("protocol_error", f"Unknown stream mode '{mode}'.")
                return
            if mode == "interval" and not isinstance(interval_ms, int):
                await self._send_error(
                    "protocol_error",
                    "The 'interval_ms' field must be an integer for interval 'mode'.",
                )
                return
            self.stream_mode = StreamMode(mode=mode, interval_ms=interval_ms)
            self._mode_changed_event.set()
        elif kind == "screen_request":
            self._screen_request_event.set()
        else:
            await self._send_error("protocol_error", "Unknown 'kind'.")

    async def _stream_loop(self) -> None:
        while self.connected:
            try:
                mode = self.stream_mode

                if mode.mode == "on_request":
                    self._screen_request_event.clear()
                    request_wait = asyncio.create_task(self._screen_request_event.wait())
                    mode_wait = asyncio.create_task(self._mode_changed_event.wait())
                    done, pending = await asyncio.wait(
                        {request_wait, mode_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                    if mode_wait in done:
                        self._mode_changed_event.clear()
                        continue
                    await self._send_screenshot()

                else: # interval
                    interval_s = mode.interval_ms / 1000
                    mode_wait = asyncio.create_task(self._mode_changed_event.wait())
                    try:
                        await asyncio.wait_for(asyncio.shield(mode_wait), timeout=interval_s)
                        self._mode_changed_event.clear()
                    except asyncio.TimeoutError:
                        await self._send_screenshot()
                    finally:
                        mode_wait.cancel()
            except asyncio.CancelledError:
                return
            except Exception as e:
                message = (
                    "Stream loop error. His work will be restored in "
                    f"{AethernetStasisServer._SECONDS_TO_RECOVERY_STREAM_LOOP_AFTER_EXCEPTION} seconds: "
                    f"{type(e).__name__}: {e}"
                )
                self._logger.error(message)
                await self._send_error("internal_error", message)
                await asyncio.sleep(
                    AethernetStasisServer._SECONDS_TO_RECOVERY_STREAM_LOOP_AFTER_EXCEPTION,
                ) # anti-tight loop

    async def _send_screenshot(self) -> None:
        if self.screen_stream_id is None:
            return
        image = await self.screen.screenshot()
        await self._link.send_image(self.screen_stream_id, image)

    async def _send_error(self, reason: str, message: str, stream_id: str | None = None, *, log: bool = True) -> None:
        if log:
            self._logger.error(f"Sending error: {message}")
        await self._link.send_frame(
            stream_id or self.callbacks_stream_id,
            "error",
            encode_json_bytes({"kind": "error", "reason": reason, "message": message}),
            end=True,
        )
