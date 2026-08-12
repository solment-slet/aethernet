from __future__ import annotations

import io
import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable
from functools import partial

from PIL import Image

from aethernet.exceptions import TransportClosedError
from aethernet.transport.low_transport import (
    BytesAndImages,
    LowTransport,
    LowTransportConfig,
)

_HEADER_KIND_SIZE = 1  # 0 = bytes, 1 = image
_HEADER_LEN_SIZE = 4  # длина payload, big-endian, unsigned
_STREAM_ID_SIZE = 16  # uuid.UUID.bytes
_KIND_BYTES = 0
_KIND_IMAGE = 1

_DEFAULT_IMAGE_FORMAT = "JPEG"
_RECV_CHUNK = 64 * 1024

# TCP keepalive defaults: with these, a peer that vanishes without a clean
# FIN/RST (network partition, crashed process, dropped wifi/NAT mapping)
# gets detected by the OS - and a blocking recv() stuck on that dead
# connection gets woken up with an error - within roughly
# idle + interval * count seconds (~11s with these defaults), instead of
# hanging forever. Without this, TCP has no idle timeout by default.
_KEEPALIVE_IDLE_SECONDS = 5
_KEEPALIVE_INTERVAL_SECONDS = 2
_KEEPALIVE_PROBE_COUNT = 3


def _enable_tcp_keepalive(
    sock: socket.socket,
    *,
    idle: int = _KEEPALIVE_IDLE_SECONDS,
    interval: int = _KEEPALIVE_INTERVAL_SECONDS,
    count: int = _KEEPALIVE_PROBE_COUNT,
) -> None:
    """Enables OS-level TCP keepalive with aggressive-ish intervals.

    Without this, a half-open connection (the peer disappeared but no
    FIN/RST ever reached us) can leave a blocking recv() call stuck
    forever - TCP itself has no idle timeout unless keepalive is enabled.
    This is what makes shutdown/close() eventually able to interrupt a
    stuck reader thread even when the peer is truly gone rather than just
    quiet: once the OS gives up on the peer, the blocked recv() returns
    with an error on its own, which the existing exception handling in
    _readexactly already turns into _ConnectionLost / TransportClosedError.

    Platform coverage:
      - Linux: TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT, fully configurable.
      - macOS: TCP_KEEPALIVE (idle time only; interval/count aren't exposed
        via the stdlib socket module).
      - Windows: SO_KEEPALIVE alone uses system-wide defaults (often
        several minutes) - the stdlib socket module doesn't expose
        per-socket idle/interval/count knobs here. If tighter detection is
        needed on Windows, use socket.ioctl(SIO_KEEPALIVE_VALS, ...)
        separately; not done here to keep this helper cross-platform.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):  # Linux
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
    elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, idle)


def _build_bytes_frame(data: bytes) -> bytes:
    return bytes([_KIND_BYTES]) + len(data).to_bytes(_HEADER_LEN_SIZE, "big") + data


def _build_image_frame(
    image: Image.Image, stream_id: uuid.UUID, image_format: str, image_quality: int
) -> bytes:
    buf = io.BytesIO()
    save_kwargs = {}
    if image_format.upper() in ("JPEG", "JPG", "WEBP"):
        save_kwargs["quality"] = image_quality
    image.save(buf, format=image_format, **save_kwargs)
    payload = buf.getvalue()
    header = (
        bytes([_KIND_IMAGE])
        + stream_id.bytes
        + len(payload).to_bytes(_HEADER_LEN_SIZE, "big")
    )
    return header + payload


def _read_frame(readexactly: Callable[[int], bytes]) -> BytesAndImages:
    kind_byte = readexactly(_HEADER_KIND_SIZE)
    kind = kind_byte[0]

    if kind == _KIND_IMAGE:
        stream_id = uuid.UUID(bytes=readexactly(_STREAM_ID_SIZE))
        length = int.from_bytes(readexactly(_HEADER_LEN_SIZE), "big")
        payload = readexactly(length)
        image = Image.open(io.BytesIO(payload))
        image.load()
        return image, stream_id

    if kind == _KIND_BYTES:
        length = int.from_bytes(readexactly(_HEADER_LEN_SIZE), "big")
        return readexactly(length)

    raise ValueError(f"Unknown frame kind byte: {kind}")


class TCPLowTransport(LowTransport[BytesAndImages]):
    """
    LowTransport поверх ОДНОГО TCP-соединения, синхронный (блокирующие
    сокеты). Как только это соединение рвётся — транспорт закрыт навсегда,
    переподключения нет. Годится для клиента (connect()) и для сценариев,
    где сервер сам управляет своим accept-циклом вручную.

    Если нужен серверный transport, который сам слушает порт и переживает
    переподключения клиента без падения (и без падения AggregatingLink
    поверх него) — используйте TCPLowTransportServer ниже.

    Режим BytesAndImages: произвольные bytes-сообщения и кадры экрана
    (Image + uuid потока) мультиплексируются в одном соединении через
    фрейминг [kind:1][stream_id:16 если kind=image][len:4][payload].

    TCP keepalive включён на сокете по умолчанию (см. _enable_tcp_keepalive):
    без него полуразорванное соединение (пир исчез без FIN/RST) может
    держать блокирующий recv() зависшим бесконечно — ни один вызов close()
    или отмена asyncio-задачи это не прервут, пока ОС сама не признает
    соединение мёртвым.
    """

    CONFIG = LowTransportConfig(
        mode="bytes",
        supports_images=True,
        max_message_bytes=1024 * 1024,
        min_send_interval=0.0,
        min_recv_interval=0.0,
        delay_before_resending=2,
    )

    def __init__(
        self,
        sock: socket.socket,
        config: LowTransportConfig | None = None,
        *,
        image_format: str = _DEFAULT_IMAGE_FORMAT,
        image_quality: int = 80,
        keepalive_idle: int = _KEEPALIVE_IDLE_SECONDS,
        keepalive_interval: int = _KEEPALIVE_INTERVAL_SECONDS,
        keepalive_count: int = _KEEPALIVE_PROBE_COUNT,
    ) -> None:
        super().__init__(config)
        self._sock = sock
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _enable_tcp_keepalive(
            self._sock,
            idle=keepalive_idle,
            interval=keepalive_interval,
            count=keepalive_count,
        )
        self._image_format = image_format
        self._image_quality = image_quality

        self._last_send_ts = 0.0
        self._last_recv_ts = 0.0
        self._closed = False

    # ------------------------------------------------------------------ #
    #  Construction helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def connect(
        cls, host: str, port: int, timeout: float | None = None, **kwargs
    ) -> TCPLowTransport:
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
    def accept(cls, server_sock: socket.socket, **kwargs) -> TCPLowTransport:
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
                raise ValueError(
                    "This transport instance is not configured with supports_images=True."
                )
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
        result = _read_frame(self._readexactly)
        self._last_recv_ts = time.monotonic()
        return result

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #

    def _send_bytes(self, data: bytes) -> None:
        self._throttle(self.config.min_send_interval, is_send=True)
        self._sendall(_build_bytes_frame(data))
        self._last_send_ts = time.monotonic()

    def _send_image(self, image: Image.Image, stream_id: uuid.UUID) -> None:
        frame = _build_image_frame(
            image, stream_id, self._image_format, self._image_quality
        )
        self._throttle(self.config.min_send_interval, is_send=True)
        self._sendall(frame)
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


class TCPLowTransportServer(LowTransport[BytesAndImages]):
    """
    Серверный LowTransport, который сам управляет своим жизненным циклом:
    сам слушает порт и сам принимает клиентов ВНУТРИ send()/recv(). Если
    текущий клиент отключается, transport НЕ закрывается и не бросает
    TransportClosedError наружу — он просто блокируется в ожидании
    следующего подключения и продолжает работать как раньше. Благодаря
    этому AggregatingLink, поднятый поверх этого transport, переживает
    переподключения клиента: не нужно вручную оборачивать accept() в цикл
    снаружи и пересоздавать весь стек при каждом обрыве связи.

    TCP keepalive включён на каждом принятом соединении (см.
    _enable_tcp_keepalive): без него "тихая" смерть клиента (сеть
    оборвалась, процесс убит без FIN/RST) оставляет блокирующий recv()
    зависшим бесконечно, и ни close(), ни отмена asyncio-задачи это не
    прерывают — только сама ОС, дождавшись неответа на keepalive-пробы.

    Пример использования (было — TCPLowTransport.listen/accept в ручном
    цикле снаружи, из-за чего разрыв клиента ронял весь transport и
    AggregatingLink; стало):

        transport = TCPLowTransportServer("0.0.0.0", 9876, logger=logger)
        link = await get_link(transport, ...)
        # клиент может отключаться и переподключаться сколько угодно раз —
        # transport и link продолжают жить.

    Если нужно единоразовое соединение без переподключений — используйте
    TCPLowTransport.listen()/.accept() напрямую.
    """

    CONFIG = TCPLowTransport.CONFIG

    def __init__(
        self,
        host: str,
        port: int,
        backlog: int = 5,
        config: LowTransportConfig | None = None,
        *,
        image_format: str = _DEFAULT_IMAGE_FORMAT,
        image_quality: int = 80,
        logger: logging.Logger | None = None,
        on_client_connected: Callable[[tuple], None] | None = None,
        on_client_disconnected: Callable[[str], None] | None = None,
        keepalive_idle: int = _KEEPALIVE_IDLE_SECONDS,
        keepalive_interval: int = _KEEPALIVE_INTERVAL_SECONDS,
        keepalive_count: int = _KEEPALIVE_PROBE_COUNT,
        accept_poll_interval: float = 0.5,
    ) -> None:
        super().__init__(config)
        self._host = host
        self._port = port
        self._image_format = image_format
        self._image_quality = image_quality
        self._logger = logger or logging.getLogger(__name__)
        self._on_client_connected = on_client_connected
        self._on_client_disconnected = on_client_disconnected
        self._keepalive_idle = keepalive_idle
        self._keepalive_interval = keepalive_interval
        self._keepalive_count = keepalive_count

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((host, port))
        self._server_sock.listen(backlog)
        self._server_sock.settimeout(accept_poll_interval)

        # Текущее клиентское соединение (или None, если никто не подключён).
        self._conn: socket.socket | None = None
        # Гарантирует, что только один поток одновременно принимает нового
        # клиента / закрывает старое соединение — send() и recv() могут
        # дёргаться из разных потоков (например, отдельные to_thread на
        # чтение и запись у AggregatingLink) одновременно.
        self._conn_lock = threading.Lock()

        self._last_send_ts = 0.0
        self._last_recv_ts = 0.0
        self._closed = False  # весь сервер (слушающий сокет) закрыт

        self._logger.info(f"TCPLowTransportServer listening on {host}:{port}")

    # ------------------------------------------------------------------ #
    #  Connection management
    # ------------------------------------------------------------------ #

    def _ensure_connection(self) -> socket.socket:
        with self._conn_lock:
            if self._conn is not None:
                return self._conn
            if self._closed:
                raise TransportClosedError("TCPLowTransportServer is closed.")

            self._logger.info(f"Waiting for a client on {self._host}:{self._port}...")

            while True:
                if self._closed:
                    raise TransportClosedError("TCPLowTransportServer is closed.")
                try:
                    conn, addr = self._server_sock.accept()
                    break
                except TimeoutError:
                    continue
                except OSError as e:
                    if self._closed:
                        raise TransportClosedError(
                            "TCPLowTransportServer is closed."
                        ) from e
                    raise

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _enable_tcp_keepalive(
                conn,
                idle=self._keepalive_idle,
                interval=self._keepalive_interval,
                count=self._keepalive_count,
            )
            self._conn = conn
            self._logger.info(f"Client connected from {addr}")
            if self._on_client_connected is not None:
                try:
                    self._on_client_connected(addr)
                except Exception:
                    self._logger.exception("on_client_connected callback failed")
            return conn

    def _drop_connection(self, sock: socket.socket, reason: str) -> None:
        """
        Закрывает переданное соединение, если оно всё ещё текущее, и
        сбрасывает self._conn — следующий вызов _ensure_connection() снова
        заблокируется на accept() нового клиента. Идемпотентно: если
        соединение уже было заменено/сброшено другим потоком, ничего не
        делает.
        """
        with self._conn_lock:
            if self._conn is not sock:
                return
            self._logger.warning(f"Client disconnected: {reason}")
            try:
                sock.close()
            except OSError:
                pass
            self._conn = None
            if self._on_client_disconnected is not None:
                try:
                    self._on_client_disconnected(reason)
                except Exception:
                    self._logger.exception("on_client_disconnected callback failed")

    # ------------------------------------------------------------------ #
    #  I/O — реализация абстрактных send/recv базового класса
    # ------------------------------------------------------------------ #

    def send(self, data: BytesAndImages) -> None:
        if self._closed:
            raise TransportClosedError("TCPLowTransportServer is closed.")

        if isinstance(data, tuple):
            image, stream_id = data
            if not self.config.supports_images:
                raise ValueError(
                    "This transport instance is not configured with supports_images=True."
                )
            frame = _build_image_frame(
                image, stream_id, self._image_format, self._image_quality
            )
        else:
            if len(data) > self.config.max_message_bytes:
                raise ValueError(
                    f"Message exceeds max_message_bytes ({len(data)} > {self.config.max_message_bytes})."
                )
            frame = _build_bytes_frame(data)

        while True:
            if self._closed:
                raise TransportClosedError("TCPLowTransportServer is closed.")

            sock = self._ensure_connection()
            self._throttle(self.config.min_send_interval, is_send=True)

            try:
                sock.sendall(frame)
            except OSError as e:
                self._drop_connection(sock, f"send failed: {e}")
                continue  # ждём нового клиента и повторяем отправку целиком

            self._last_send_ts = time.monotonic()
            return

    def recv(self) -> BytesAndImages:
        while True:
            if self._closed:
                raise TransportClosedError("TCPLowTransportServer is closed.")

            sock = self._ensure_connection()
            self._throttle(self.config.min_recv_interval, is_send=False)

            try:
                result = _read_frame(partial(self._readexactly, sock))
            except _ConnectionLost as e:
                self._drop_connection(sock, str(e))
                continue  # ждём нового клиента и читаем следующий фрейм с начала
            except (ValueError, TypeError):
                # Битый/неизвестный фрейм — соединение больше не доверенное,
                # но сам transport остаётся живым для следующего клиента.
                self._drop_connection(sock, "protocol error: unknown frame kind")
                continue

            self._last_recv_ts = time.monotonic()
            return result

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #

    def _readexactly(self, sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        try:
            while len(buf) < n:
                chunk = sock.recv(min(_RECV_CHUNK, n - len(buf)))
                if not chunk:
                    raise _ConnectionLost("peer closed the connection")
                buf.extend(chunk)
        except OSError as e:
            raise _ConnectionLost(f"recv failed: {e}") from e
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
            self._server_sock.close()
        except OSError:
            pass

        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._conn.close()
                except OSError:
                    pass
                self._conn = None

        self._logger.info(f"TCPLowTransportServer on {self._host}:{self._port} closed")


class _ConnectionLost(Exception):
    """Внутреннее исключение: текущее клиентское соединение оборвалось.

    В отличие от TransportClosedError, НЕ означает, что весь
    TCPLowTransportServer закрыт — только то, что нужно дождаться нового
    клиента и повторить операцию.
    """
