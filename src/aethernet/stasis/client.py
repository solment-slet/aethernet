import logging
from dataclasses import dataclass

from aethernet.stasis.exceptions import ConnectionRejectedError, ProtocolError, ServerError, ServerProtocolError
from aethernet.transport import AggregatingLink
from aethernet.transport.utils import encode_json_bytes, decode_json_bytes
from aethernet.transport.ws_over_link.ws_over_link import PROTOCOL_NAME
from aethernet.typing import LoggerLike


@dataclass
class RemoteHostMetadata:
    width: int
    height: int


class AethernetStasisClient:
    def __init__(
        self,
        link: AggregatingLink,
        *,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self._link = link
        self._logger = logger
        self.connected = False
        self.session_id: str | None = None
        self.host_metadata: RemoteHostMetadata | None = None

    async def connect(self) -> None:
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

        if frame.frame_type != "meta":
            raise ProtocolError("Expected connect_response meta frame.")

        meta = decode_json_bytes(frame.payload)
        kind = meta.get("kind")
        status = meta.get("status")

        if kind == "error":
            raise AethernetStasisClient.handle_error(meta)
        if kind != "connect_response":
            raise ProtocolError(f"Expected connect_response, got {kind!r}")
        if status != "accepted":
            raise ConnectionRejectedError(message=meta.get("message", "The server rejected the connection request."))

        try:
            self.session_id = meta["session_id"]
            host = meta["host"]
            self.host_metadata = RemoteHostMetadata(
                width=host["width"],
                height=host["height"],
            )
        except KeyError as e:
            raise ProtocolError(f"The server response is missing the {e.args[0]} key.")

        self.connected = True

    @staticmethod
    def handle_error(meta: dict) -> ServerError:
        reason = meta.get("reason")
        message = meta.get("message")

        if reason == "protocol_error":
            raise ServerProtocolError(reason=reason, message=message)
        else:
            raise ServerError(reason=reason, message=message)