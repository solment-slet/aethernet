import asyncio
import logging
from typing import Any

from PIL import Image

from aethernet.stasis.server import PROTOCOL_NAME
from aethernet.stasis.dataclasses import StreamMode, StreamModes, ServerMetadata, RemoteHostMetadata
from aethernet.stasis.exceptions import (
    LocalDeviceError,
    ConnectionRejectedError,
    LocalDeviceProtocolError,
    RemoteDeviceError,
    RemoteDeviceProtocolError,
    InvalidSessionError,
    NotConnectedError,
    InternalError,
)
from aethernet.transport import AggregatingLink, Frame
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes
from aethernet.typing import LoggerLike


class AethernetStasisClient:
    _SECONDS_TO_RECOVERY_CALLBACK_LOOP_AFTER_EXCEPTION = 5
    _CALLBACKS_QUEUE_MAXSIZE = 300
    _ERRORS_QUEUE_MAXSIZE = 300

    def __init__(
        self,
        link: AggregatingLink,
        *,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self._link = link
        self._logger = logger
        self._callback_task: asyncio.Task[None] | None = None
        self._callbacks_queue: asyncio.Queue[Frame] = asyncio.Queue(
            maxsize=AethernetStasisClient._CALLBACKS_QUEUE_MAXSIZE,
        ) # Messages sent by the server via callbacks_stream_id, without errors.
        self._errors_queue: asyncio.Queue[Exception] = asyncio.Queue(
            maxsize=AethernetStasisClient._ERRORS_QUEUE_MAXSIZE,
        ) # Errors sent by the server via callbacks_stream_id

        self.connected = False
        self.screen_stream_id: str | None = None
        self.callbacks_stream_id: str | None = None
        self.session_id: str | None = None
        self.server_metadata: ServerMetadata | None = None

    async def close(self) -> None:
        self.connected = False
        if self._callback_task:
            self._callback_task.cancel()
            try:
                await self._callback_task
            except asyncio.CancelledError:
                pass

    async def connect(self) -> ServerMetadata:
        stream_id = self._link.new_stream_id()

        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes({
                "kind": "connect_request",
            }),
            end=True,
            protocol=PROTOCOL_NAME,
        )

        frame = await self._link.recv_frame(stream_id)

        meta = decode_json_bytes(frame.payload)
        kind = meta.get("kind")
        status = meta.get("status")

        if kind == "error":
            raise AethernetStasisClient._handle_error(meta)
        if kind != "connect_response":
            raise LocalDeviceProtocolError(f"Expected connect_response, got {kind!r}")
        if status != "accepted":
            raise ConnectionRejectedError(message=meta.get("message", "The server rejected the connection request."))

        try:
            self.session_id = meta["session_id"]
            host = meta["host"]
            stream_mode = meta["stream_mode"]
            self.callbacks_stream_id = meta["callbacks_stream_id"]
            self.server_metadata = ServerMetadata(
                remote_host=RemoteHostMetadata(
                    width=host["width"],
                    height=host["height"],
                ),
                stream_mode=StreamMode(
                    mode=stream_mode["mode"],
                    interval_ms=stream_mode["interval_ms"],
                ),
            )
        except KeyError as e:
            raise LocalDeviceProtocolError(f"The server response is missing the {e.args[0]} key.")

        self.screen_stream_id = stream_id
        self.connected = True
        self._callback_task = asyncio.create_task(
            self._callback_loop(), name=f"AethernetStasisClient.callback_loop.{self.session_id}"
        )

        return self.server_metadata

    async def wait_screen(self) -> Image.Image:
        """Returns the latest screen image without returning the old ones, even if they were not received."""
        return await self._link.recv_image(self.screen_stream_id)

    async def set_stream_mode(self, mode: StreamModes, interval_ms: int | None = None) -> None:
        self._check_connect()
        stream_id = self._link.new_stream_id()
        json = {
            "kind": "set_stream_mode",
            "session_id": self.session_id,
            "mode": mode,
        }
        if interval_ms is not None:
            # noinspection PyTypeChecker
            json["interval_ms"] = interval_ms
        await self._link.send_frame(
            stream_id,
            "meta",
            encode_json_bytes(json),
            end=True,
            protocol=PROTOCOL_NAME,
        )

    async def _callback_loop(self) -> None:
        while self.connected:
            try:
                callback_frame = await self._link.recv_frame(self.callbacks_stream_id)
                if callback_frame.frame_type == "error":
                    AethernetStasisClient.put_latest(
                        self._errors_queue,
                        AethernetStasisClient._handle_error(decode_json_bytes(callback_frame.payload)),
                    )
                    continue
                AethernetStasisClient.put_latest(self._callbacks_queue, callback_frame)
            except asyncio.CancelledError:
                return
            except Exception as e:
                message = (
                    "Callback loop error. His work will be restored in "
                    f"{AethernetStasisClient._SECONDS_TO_RECOVERY_CALLBACK_LOOP_AFTER_EXCEPTION} seconds: "
                    f"{type(e).__name__}: {e}"
                )
                self._logger.error(message)
                AethernetStasisClient.put_latest(self._errors_queue, LocalDeviceError(message))
                await asyncio.sleep(
                    AethernetStasisClient._SECONDS_TO_RECOVERY_CALLBACK_LOOP_AFTER_EXCEPTION,
                ) # anti-tight loop

    def _check_connect(self) -> None:
        if not self.connected or not self.server_metadata or not self.session_id or not self.screen_stream_id:
            raise NotConnectedError("This action requires a connection to the server.")

    @staticmethod
    def _handle_error(json: dict) -> RemoteDeviceError:
        reason = json.get("reason")
        message = json.get("message")

        if reason == "protocol_error":
            return RemoteDeviceProtocolError(reason=reason, message=message)
        elif reason == "invalid_session_error":
            return InvalidSessionError(reason=reason, message=message)
        elif reason == "internal_error":
            return InternalError(reason=reason, message=message)
        else:
            return RemoteDeviceError(reason=reason, message=message)

    @staticmethod
    def put_latest(queue: asyncio.Queue, item: Any) -> None:
        """When adding a new element, it removes the old element if the queue is full."""
        if queue.full():
            queue.get_nowait()
            queue.task_done()
        queue.put_nowait(item)
