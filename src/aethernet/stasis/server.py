import uuid
import signal
import asyncio
import logging
from typing import get_args

from aethernet.typing import LoggerLike
from aethernet.exceptions import StreamClosed
from aethernet.transport.enums import ReliabilityMode
from aethernet.transport import AggregatingLink, Frame
from aethernet.stasis.actions import action_from_dict
from aethernet.stasis.dataclasses import StreamMode, StreamModes
from aethernet.stasis.managers.screen_manager import ScreenManager
from aethernet.stasis.managers.input_manager import InputManager
from aethernet.stasis.managers.clipboard_manager import ClipboardManager
from aethernet.stasis.managers.action_executor import ActionExecutor
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes

PROTOCOL_NAME = "stasis"

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
        self._stop_event = asyncio.Event()
        self._closed = False

        self.screen = ScreenManager()
        self.input = InputManager(self.screen.x, self.screen.y)
        self.clipboard = ClipboardManager()
        self.stream_mode: StreamMode = StreamMode(
            mode="on_request",
            interval_ms=None,
        )
        self._action_executor = ActionExecutor(self.input, self.clipboard)

        self.connected = False
        self.screen_stream_id: str | None = None
        self.callbacks_stream_id: str | None = None
        self.session_id: str | None = None

    async def start(self) -> None:
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(), name="AethernetStasisServer.dispatcher"
        )

    async def start_and_wait(self) -> None:
        await self.start()

        event_loop = asyncio.get_event_loop()

        event_loop.add_signal_handler(signal.SIGTERM, self._stop_event.set)  # type: ignore[arg-type]
        event_loop.add_signal_handler(signal.SIGINT, self._stop_event.set)  # type: ignore[arg-type]

        try:
            await self._stop_event.wait()
        finally:
            await self.close()

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

        self._stop_event.set()

    @staticmethod
    def generate_session_id() -> str:
        return uuid.uuid4().hex

    def _log_task_result(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.error("stream_loop died silently", exc_info=exc)

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
            self._stream_task.add_done_callback(self._log_task_result)
            self._logger.info(f"New connection has been established: {self.session_id}")

        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            self._logger.error(message)
            await self._send_error("unknown_error", message, stream_id)

    async def _handle_frame(self, frame: Frame) -> None:
        json = decode_json_bytes(frame.payload)
        kind = json["kind"]
        if json.get("session_id") != self.session_id and kind != "disconnect":
            await self._send_error("invalid_session_error", "The session ID is missing or invalid.")
            return

        if kind == "disconnect":
            # Client requested a graceful disconnect
            self._logger.info(f"Session ended by client: {self.session_id}")
            await self._reset_session()
        elif kind == "set_stream_mode":
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
            self._logger.info("Stream mode has been changed")
        elif kind == "execute_actions":
            # Execute Actions
            raw_actions = json.get("actions")
            if not raw_actions:
                await self._send_error("protocol_error", "The 'actions' field is missing.")
                return

            try:
                actions = [action_from_dict(a) for a in raw_actions]
            except ValueError as e:
                await self._send_error("protocol_error", str(e))
                return

            try:
                results = await self._action_executor.execute(actions)
            except Exception as e:
                await self._send_error(
                    "execution_error", f"{type(e).__name__}: {e}",
                )
                return

            if results:
                await self._link.send_frame(
                    self.callbacks_stream_id,
                    "data",
                    encode_json_bytes({
                        "kind": "clipboard",
                        "clipboard": results,
                    }),
                    protocol=PROTOCOL_NAME,
                )
        elif kind == "screen_request":
            # Execute Screen Request
            self._screen_request_event.set()
        else:
            await self._send_error("protocol_error", "Unknown 'kind'.")

    async def _stream_loop(self) -> None:
        while self.connected:
            try:
                mode = self.stream_mode
                self._logger.debug(f"[loop] mode={mode.mode}")

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
                    self._logger.debug("[loop] waiting for interval/mode_change")
                    mode_wait = asyncio.create_task(self._mode_changed_event.wait())
                    try:
                        await asyncio.wait_for(asyncio.shield(mode_wait), timeout=interval_s)
                        self._mode_changed_event.clear()
                    except asyncio.TimeoutError:
                        self._logger.debug("[loop] timeout -> sending screenshot")
                        await self._send_screenshot()
                        self._logger.debug("[loop] screenshot sent, looping back")
                    finally:
                        mode_wait.cancel()
            except asyncio.CancelledError:
                return
            except StreamClosed:
                return
            except Exception as e:
                message = (
                    "Stream loop error. His work will be restored in "
                    f"{AethernetStasisServer._SECONDS_TO_RECOVERY_STREAM_LOOP_AFTER_EXCEPTION} seconds: "
                    f"{type(e).__name__}: {e}"
                )
                await self._send_error("internal_error", message)
                await asyncio.sleep(
                    AethernetStasisServer._SECONDS_TO_RECOVERY_STREAM_LOOP_AFTER_EXCEPTION,
                ) # anti-tight loop

    async def _send_screenshot(self) -> None:
        if self.screen_stream_id is None:
            return
        if self._link.image_reliability_mode != ReliabilityMode.NONE:
            # См. ограничение в AggregatingLink.send_image: dedup по stream_id
            # не позволяет слать поток разных картинок на одном stream_id
            # при reliability_mode != NONE.
            raise RuntimeError(
                "AethernetStasisServer sends a continuous stream of screenshots "
                "on a single stream_id, which is only safe with "
                "image_reliability_mode=ReliabilityMode.NONE. "
                "See AggregatingLink.send_image docstring."
            )
        image = await self.screen.screenshot()
        await self._link.send_image(self.screen_stream_id, image)

    async def _send_callback(self, frame_type: str, payload: bytes = b"") -> None:
        await self._link.send_frame(
            self.callbacks_stream_id,
            frame_type,
            payload,
            protocol=PROTOCOL_NAME,
        )

    async def _send_error(self, reason: str, message: str, stream_id: str | None = None, *, log: bool = True) -> None:
        if log:
            self._logger.error(f"Sending error: {message}")
        await self._link.send_frame(
            stream_id or self.callbacks_stream_id,
            "error",
            encode_json_bytes({"kind": "error", "reason": reason, "message": message}),
            protocol=PROTOCOL_NAME,
        )

    async def _reset_session(self) -> None:
        """Releases the current session so the server can accept a new one.

        Unlike close(), this does not stop the dispatcher loop — the server
        keeps running and listening for the next connect_request.
        """
        self.connected = False

        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None

        self._screen_request_event.clear()
        self._mode_changed_event.clear()
        self.session_id = None
        self.screen_stream_id = None
        self.callbacks_stream_id = None
