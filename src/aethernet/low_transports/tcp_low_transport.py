from __future__ import annotations

import io
import socket
import time
import uuid

from PIL import Image

from aethernet.exceptions import TransportClosedError
from aethernet.transport.low_transport import LowTransport, LowTransportConfig, BytesAndImages

_HEADER_KIND_SIZE = 1      # 0 = bytes, 1 = image
_HEADER_LEN_SIZE = 4       # длина payload, big-endian, unsigned
_STREAM_ID_SIZE = 16       # uuid.UUID.bytes
_KIND_BYTES = 0
_KIND_IMAGE = 1

_DEFAULT_IMAGE_FORMAT = "JPEG"
_RECV_CHUNK = 64 * 1024


class TCPLowTransportConfig(LowTransportConfig):
    pass


class TCPLowTransport(LowTransport[BytesAndImages]):
    """
    LowTransport поверх голого TCP/IP, синхронный (блокирующие сокеты).

    Режим BytesAndImages: произвольные bytes-сообщения и кадры экрана
    (Image + uuid потока) мультиплексируются в одном соединении через
    фрейминг [kind:1][stream_id:16 если kind=image][len:4][payload].
    """

    CONFIG = TCPLowTransportConfig(
        mode="bytes",
        supports_images=True,
        max_message_bytes=1024 * 1024,
        min_send_interval=0.0,
        min_recv_interval=0.0,
    )

    def __init__(
        self,
        sock: socket.socket,
        config: LowTransportConfig | None = None,
        *,
        image_format: str = _DEFAULT_IMAGE_FORMAT,
        image_quality: int = 80,
    ) -> None:
        super().__init__(config)
        self._sock = sock
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._image_format = image_format
        self._image_quality = image_quality

        self._last_send_ts = 0.0
        self._last_recv_ts = 0.0
        self._closed = False

    # ------------------------------------------------------------------ #
    #  Construction helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def connect(cls, host: str, port: int, timeout: float | None = None, **kwargs) -> "TCPLowTransport":
        sock = socket.create_connection((host, port), timeout=timeout)
        return cls(sock, **kwargs)

    @classmethod
    def listen(cls, host: str, port: int, backlog: int = 5) -> socket.socket:
        """Создаёт слушающий сокет. Каждое accept() оборачивайте в TCPLowTransport(conn, ...)."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((host, port))
        server_sock.listen(backlog)
        return server_sock

    @classmethod
    def accept(cls, server_sock: socket.socket, **kwargs) -> "TCPLowTransport":
        conn, _addr = server_sock.accept()
        return cls(conn, **kwargs)

    # ------------------------------------------------------------------ #
    #  I/O — реализация абстрактных send/recv базового класса
    # ------------------------------------------------------------------ #

    def send(self, data: BytesAndImages) -> None:
        if self._closed:
            raise TransportClosedError("TCPLowTransport is closed.")

        if isinstance(data, tuple):
            image, stream_id = data
            if not self.config.supports_images:
                raise ValueError("This transport instance is not configured with supports_images=True.")
            self._send_image(image, stream_id)
        else:
            if len(data) > self.config.max_message_bytes:
                raise ValueError(
                    f"Message exceeds max_message_bytes ({len(data)} > {self.config.max_message_bytes})."
                )
            self._send_bytes(data)

    def recv(self) -> BytesAndImages:
        if self._closed:
            raise TransportClosedError("TCPLowTransport is closed.")

        self._throttle(self.config.min_recv_interval, is_send=False)

        kind_byte = self._readexactly(_HEADER_KIND_SIZE)
        kind = kind_byte[0]

        if kind == _KIND_IMAGE:
            stream_id_raw = self._readexactly(_STREAM_ID_SIZE)
            stream_id = uuid.UUID(bytes=stream_id_raw)
            length = int.from_bytes(self._readexactly(_HEADER_LEN_SIZE), "big")
            payload = self._readexactly(length)
            self._last_recv_ts = time.monotonic()

            image = Image.open(io.BytesIO(payload))
            image.load()
            return image, stream_id

        elif kind == _KIND_BYTES:
            length = int.from_bytes(self._readexactly(_HEADER_LEN_SIZE), "big")
            payload = self._readexactly(length)
            self._last_recv_ts = time.monotonic()
            return payload

        else:
            self._closed = True
            raise ValueError(f"Unknown frame kind byte: {kind}")

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #

    def _send_bytes(self, data: bytes) -> None:
        self._throttle(self.config.min_send_interval, is_send=True)
        header = bytes([_KIND_BYTES]) + len(data).to_bytes(_HEADER_LEN_SIZE, "big")
        self._sendall(header + data)
        self._last_send_ts = time.monotonic()

    def _send_image(self, image: Image.Image, stream_id: uuid.UUID) -> None:
        buf = io.BytesIO()
        save_kwargs = {}
        if self._image_format.upper() in ("JPEG", "JPG", "WEBP"):
            save_kwargs["quality"] = self._image_quality
        image.save(buf, format=self._image_format, **save_kwargs)
        payload = buf.getvalue()

        self._throttle(self.config.min_send_interval, is_send=True)
        header = (
            bytes([_KIND_IMAGE])
            + stream_id.bytes
            + len(payload).to_bytes(_HEADER_LEN_SIZE, "big")
        )
        self._sendall(header + payload)
        self._last_send_ts = time.monotonic()

    def _sendall(self, chunk: bytes) -> None:
        try:
            self._sock.sendall(chunk)
        except OSError as e:
            self._closed = True
            raise TransportClosedError(f"Send failed: {e}") from e

    def _readexactly(self, n: int) -> bytes:
        buf = bytearray()
        try:
            while len(buf) < n:
                chunk = self._sock.recv(min(_RECV_CHUNK, n - len(buf)))
                if not chunk:
                    self._closed = True
                    raise TransportClosedError("Peer closed the connection.")
                buf.extend(chunk)
        except OSError as e:
            self._closed = True
            raise TransportClosedError(f"Recv failed: {e}") from e
        return bytes(buf)

    def _throttle(self, interval: float, *, is_send: bool) -> None:
        if interval <= 0:
            return
        last_ts = self._last_send_ts if is_send else self._last_recv_ts
        remaining = interval - (time.monotonic() - last_ts)
        if remaining > 0:
            time.sleep(remaining)

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()