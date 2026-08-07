from __future__ import annotations

import asyncio
import math
import struct
import time
import uuid
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator

import msgpack
from PIL import Image

from aethernet.transport.enums import ReliabilityMode
from aethernet.exceptions import StreamClosed, TransportClosedError
from aethernet.typing import LoggerLike
from aethernet.transport.medium_transport import MediumTransport


@dataclass(slots=True)
class Frame:
    """
    Логический фрейм мультиплексированного канала.

    Несколько таких frame могут быть упакованы в один logical batch,
    а уже затем logical batch может быть отправлен либо одним physical
    пакетом, либо несколькими transport-chunk пакетами.

    Attributes:
        stream_id: Идентификатор логического стрима.
        frame_type: Тип фрейма.
        payload: Полезная нагрузка.
        end: Признак завершения стрима.
        image: Изображение, None для обычный фреймов
        protocol: Протокол стрима (например "http", "ws"). Имеет смысл
            только в ПЕРВОМ frame нового стрима — именно это значение
            попадёт в accept_stream() на другой стороне. Для всех
            последующих frame того же stream_id значение игнорируется
            получателем (стрим уже зарегистрирован). Для image-стримов
            выставляется автоматически в "image" (см. _IMAGE_PROTOCOL).
    """

    stream_id: str
    frame_type: str
    payload: bytes = b""
    end: bool = False
    image: Image.Image | None = None
    protocol: str | None = None


# ---------------------------------------------------------------------------
# Internal data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ChunkAssembly:
    """
    Буфер для сборки большого logical payload из нескольких transport chunks.

    Attributes:
        total_parts: Общее число частей.
        parts: Уже полученные части по их индексу.
        created_at: Время создания сборки, используется для TTL cleanup.
    """

    total_parts: int
    parts: dict[int, bytes]
    created_at: float


@dataclass(slots=True)
class _InFlightPacket:
    """
    Пакет, отправленный, но ещё не подтверждённый получателем.

    Attributes:
        seq: Порядковый номер пакета.
        packet_bytes: Готовые байты для повторной отправки.
        first_send_time: Время первой отправки (для метрик/логов).
        last_send_time: Время последней отправки (для retransmit таймера).
    """

    seq: int
    packet_bytes: bytes
    first_send_time: float
    last_send_time: float


# ---------------------------------------------------------------------------
# Packet header layout
#
# Единый формат физического пакета (заменяет SINGLE_MAGIC / CHUNK_MAGIC):
#
#   PACKET_MAGIC (4 bytes)
#   + _PACKET_HEADER_STRUCT:
#       seq        uint32   порядковый номер (0 = ACK-only, нет данных)
#       ack_count  uint8    количество ACK в списке
#   + ack_seqs: uint32 * ack_count   — SACK список
#   + payload: bytes                 — msgpack batch (может быть пустым)
#
# Режим NONE использует старые SINGLE/CHUNK форматы без изменений.
# ---------------------------------------------------------------------------

_PACKET_MAGIC = b"AGP1"
_PACKET_HEADER_STRUCT = struct.Struct(">IB")  # seq: uint32, ack_count: uint8
_ACK_SEQ_STRUCT = struct.Struct(">I")  # один ack_seq: uint32
_IMG_FRAME_TYPE = "\x00img"  # фрейм-обёртка для входящего изображения
_IMG_ACK_FRAME_TYPE = "\x00img_ack"  # подтверждение доставки изображения
_IMAGE_PROTOCOL = "image"  # synthetic protocol для accept_stream() на image-стримах

# Заголовок без ACK и без payload
_MIN_PACKET_HEADER_SIZE = len(_PACKET_MAGIC) + _PACKET_HEADER_STRUCT.size


def _build_packet(
    seq: int,
    ack_seqs: list[int],
    payload: bytes,
) -> bytes:
    """Собирает физический пакет нового формата AGP1."""
    ack_count = len(ack_seqs)
    header = _PACKET_MAGIC + _PACKET_HEADER_STRUCT.pack(seq, ack_count)
    acks = b"".join(_ACK_SEQ_STRUCT.pack(s) for s in ack_seqs)
    return header + acks + payload


def _parse_packet(raw: bytes) -> tuple[int, list[int], bytes]:
    """
    Разбирает физический пакет нового формата AGP1.

    Returns:
        (seq, ack_seqs, payload)

    Raises:
        ValueError: Если пакет повреждён или magic не совпадает.
    """
    if not raw.startswith(_PACKET_MAGIC):
        raise ValueError(f"unknown packet magic: {raw[:4]!r}")

    min_size = _MIN_PACKET_HEADER_SIZE
    if len(raw) < min_size:
        raise ValueError("packet too short")

    offset = len(_PACKET_MAGIC)
    seq, ack_count = _PACKET_HEADER_STRUCT.unpack_from(raw, offset)
    offset += _PACKET_HEADER_STRUCT.size

    ack_seqs: list[int] = []
    for _ in range(ack_count):
        if offset + _ACK_SEQ_STRUCT.size > len(raw):
            raise ValueError("packet truncated in ack list")
        (ack_seq,) = _ACK_SEQ_STRUCT.unpack_from(raw, offset)
        offset += _ACK_SEQ_STRUCT.size
        ack_seqs.append(ack_seq)

    payload = raw[offset:]
    return seq, ack_seqs, payload


# ---------------------------------------------------------------------------
# AggregatingLink
# ---------------------------------------------------------------------------


class AggregatingLink:
    """
    Надстройка над синхронным капризным Transport.

    Основные идеи:
    - один постоянный reader loop, почти постоянно сидящий в transport.recv()
    - один writer loop, агрегирующий логические frame в батчи
    - маршрутизация входящих frame по stream_id
    - если logical batch слишком большой для Transport, он режется на transport chunks

    Режимы надёжности (ReliabilityMode):
    - NONE          — без подтверждений (оригинальное поведение, порядок не гарантирован)
    - STOP_AND_WAIT — Stop-and-Wait ARQ (window_size=1, порядок гарантирован)
    - PARALLEL      — Selective-Repeat ARQ с окном window_size > 1

    Порядок доставки в режиме PARALLEL:
        Отправитель нумерует каждый физический пакет (seq).
        Получатель держит reorder buffer: батчи не передаются в стримы
        пока не получены все батчи с меньшим seq. Это гарантирует строгий
        порядок доставки frame между батчами несмотря на то, что
        ретрансмиты могут приходить позже изначально следующих пакетов.

        Пример без reorder buffer (неправильно):
            Отправлено:   batch[seq=1], batch[seq=2], batch[seq=3]
            Потерян seq=1, ретрансмит приходит позже:
            Получено:     seq=2 → отдаём, seq=3 → отдаём, seq=1 → отдаём  (порядок сломан)

        С reorder buffer (правильно):
            Получено:     seq=2 → буфер, seq=3 → буфер, seq=1 → flush [1,2,3] (порядок верный)

    При STOP_AND_WAIT и PARALLEL:
    - каждый physical send получает порядковый номер seq
    - получатель шлёт SACK (список подтверждённых seq)
    - отправитель повторяет пакеты из in_flight каждые delay_before_resending секунд
      пока не получит ACK; цикл бесконечный — ACK может теряться много раз
    - получатель при дубликате всё равно шлёт ACK (не обрабатывает payload повторно)
    - ACK агрегируются и отправляются либо piggyback с данными,
      либо отдельным ACK-only пакетом

    Формат нового физического пакета (режимы STOP_AND_WAIT / PARALLEL):
        AGP1
        + struct(">IB"): seq (uint32), ack_count (uint8)
        + uint32 * ack_count  (SACK список)
        + payload bytes       (msgpack batch, может быть пустым)

    Старые форматы (режим NONE):
        AGS1 + logical_payload         (single)
        AGC1 + chunk_meta + chunk_data (chunk)
    """

    # --- старые magic (режим NONE) ---
    SINGLE_MAGIC = b"AGS1"
    CHUNK_MAGIC = b"AGC1"
    _CHUNK_META_STRUCT = struct.Struct(">16sHH")  # msg_id, part_index, total_parts

    def __init__(
        self,
        transport: MediumTransport,
        *,
        logger: LoggerLike = logging.getLogger(__name__),
        flush_interval: float = 0.5,
        max_batch_size: int = 64 * 1024,
        chunk_assembly_ttl: float = 60.0,
        # --- reliability ---
        reliability_mode: ReliabilityMode = ReliabilityMode.NONE,
        image_reliability_mode: ReliabilityMode = ReliabilityMode.NONE,
        window_size: int = 8,
        ack_flush_interval: float = 0.1,
        ack_batch_size: int = 8,
        received_seqs_window: int = 256,
        reorder_buffer_ttl: float = 500.0,
    ) -> None:
        """
        Args:
            transport: Синхронный низкоуровневый транспорт.
            flush_interval: Максимальное время ожидания перед отправкой батча.
            max_batch_size: Максимальный размер logical batch в байтах.
            chunk_assembly_ttl: Время жизни незавершённой сборки chunk-пакетов.
            reliability_mode: Режим надёжности доставки.
            image_reliability_mode: Режим надёжности для send_image()/recv_image(),
                независимый от reliability_mode. По умолчанию NONE (fire-and-forget) —
                разумно для видеопотока с высоким FPS, где актуальность важнее
                гарантии доставки. Можно поставить STOP_AND_WAIT/PARALLEL, если
                отдельные изображения должны доставляться гарантированно
                (см. также override-аргумент reliability_mode в самом send_image()).
            window_size: Размер окна для режима PARALLEL.
            ack_flush_interval: Максимальное время накопления ACK перед отправкой.
            ack_batch_size: Максимальное количество ACK в одном flush.
            received_seqs_window: Сколько последних seq хранить для дедупликации.
                Разумный минимум: window_size * 4. Default: 256.
            reorder_buffer_ttl: Время жизни записи в reorder buffer без flush (сек).
                Защита от бесконечного роста если пакет потерян навсегда.
                На практике не срабатывает — ретрансмит бесконечный.
        """
        self._logger = logger
        self._transport = transport
        self._config = self._transport.config

        self._flush_interval = flush_interval
        self._chunk_assembly_ttl = chunk_assembly_ttl
        self._min_send_interval = self._config.min_send_interval
        self._recv_restart_delay = self._config.min_recv_interval
        self._delay_before_resending = self._config.delay_before_resending

        self._data_loss_subscribers: set[asyncio.Queue[list[int] | None]] = set()

        # --- reliability config ---
        self._reliability_mode = reliability_mode
        self._image_reliability_mode = image_reliability_mode
        self._window_size = (
            1 if reliability_mode == ReliabilityMode.STOP_AND_WAIT else window_size
        )
        self._ack_flush_interval = ack_flush_interval
        self._ack_batch_size = ack_batch_size
        self._received_seqs_window = received_seqs_window
        self._reorder_buffer_ttl = reorder_buffer_ttl

        self._shutdown_task = asyncio.create_task(self._shutdown_watcher())

        self._transport_limit: int = self._transport.max_payload_bytes

        self._single_packet_payload_limit = self._transport_limit - len(
            self.SINGLE_MAGIC
        )
        self._chunk_packet_payload_limit = (
                self._transport_limit - len(self.CHUNK_MAGIC) - self._CHUNK_META_STRUCT.size
        )
        self._agp1_payload_limit = self._transport_limit - _MIN_PACKET_HEADER_SIZE

        if self._single_packet_payload_limit <= 0:
            raise ValueError(
                "transport.max_payload_bytes too small for SINGLE packet framing"
            )
        if self._chunk_packet_payload_limit <= 0:
            raise ValueError(
                "transport.max_payload_bytes too small for CHUNK packet framing"
            )
        if self._agp1_payload_limit <= 0:
            raise ValueError(
                "transport.max_payload_bytes too small for AGP1 packet framing"
            )

        # Для AGP1 в физический пакет помимо logical_payload попадают ещё
        # piggyback-ACK (до ack_batch_size штук по _ACK_SEQ_STRUCT.size байт).
        # Резервируем под них место заранее, иначе при большом batch_size
        # + полном наборе ACK физический пакет может превысить
        # transport.max_payload_bytes — а chunking для AGP1 не реализован.
        self._agp1_max_logical_payload = (
                self._agp1_payload_limit - ack_batch_size * _ACK_SEQ_STRUCT.size
        ) + 64 # страховка
        if self._agp1_max_logical_payload <= 0:
            raise ValueError(
                "transport.max_payload_bytes too small for AGP1 framing "
                "with the configured ack_batch_size"
            )

        if reliability_mode == ReliabilityMode.NONE:
            self._max_batch_size = min(max_batch_size, self._single_packet_payload_limit)
        else:
            self._max_batch_size = min(max_batch_size, self._agp1_max_logical_payload)

        self._received_image_uuids: deque[str] = deque(maxlen=64)
        self._received_image_uuids_set: set[str] = set()
        self._image_ack_events: dict[str, asyncio.Event] = {}

        self._send_lock: asyncio.Lock = asyncio.Lock()

        # --- outgoing / incoming queues ---
        self._outgoing: asyncio.Queue[Frame] = asyncio.Queue()
        self._incoming_by_stream: dict[str, asyncio.Queue[Frame]] = {}
        self._stream_notification_subscribers: set[
            asyncio.Queue[tuple[str, str] | None]
        ] = set()
        self._seen_incoming_streams: set[str] = set()
        self._pending_outgoing: Frame | None = None

        # --- chunk assembly (режим NONE) ---
        self._chunk_assemblies: dict[bytes, _ChunkAssembly] = {}

        # --- reliability: sender side ---
        self._in_flight: dict[int, _InFlightPacket] = {}
        self._seq_counter: int = 1
        self._window_semaphore: asyncio.Semaphore = asyncio.Semaphore(self._window_size)
        self._ack_received_event: asyncio.Event = asyncio.Event()

        # --- reliability: receiver side ---
        # Скользящее окно для дедупликации входящих seq.
        self._received_seqs: deque[int] = deque(maxlen=self._received_seqs_window)
        self._received_seqs_set: set[int] = set()
        # Очередь seq ожидающих отправки ACK
        self._pending_acks: asyncio.Queue[int] = asyncio.Queue()
        self._has_pending_acks: asyncio.Event = asyncio.Event()

        # --- reorder buffer (только PARALLEL) ---
        # Хранит батчи которые пришли раньше чем их предшественники.
        # Ключ — seq пакета. Значение — (logical_payload, arrival_time).
        # next_expected_seq — следующий seq который должен быть передан в стримы.
        # В STOP_AND_WAIT и NONE reorder buffer не используется:
        #   NONE       — нет seq вообще
        #   SAW        — window=1, out-of-order невозможен физически
        self._reorder_buffer: dict[int, tuple[bytes, float]] = {}
        self._next_expected_seq: int = 1

        # --- tasks ---
        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._retransmit_task: asyncio.Task[None] | None = None
        self._ack_sender_task: asyncio.Task[None] | None = None

        # --- lifecycle ---
        self._started = False
        self._closed = False
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_send_ts = 0.0

        self._logger.info(
            f"AggregatingLink initialized: transport_limit={self._transport_limit}, "
            f"reliability={reliability_mode.value}, window_size={self._window_size}, "
            f"agp1_payload_limit={self._agp1_payload_limit}"
        )

    @property
    def image_reliability_mode(self) -> ReliabilityMode:
        return self._image_reliability_mode

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Запускает reader, writer и (если нужно) reliability tasks.

        Повторный вызов безопасен.

        Raises:
            RuntimeError: Если link уже закрыт.
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("AggregatingLink is closed")
            if self._started:
                return

            self._started = True
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="AggregatingLink.reader"
            )
            self._writer_task = asyncio.create_task(
                self._writer_loop(), name="AggregatingLink.writer"
            )

            if self._reliability_mode != ReliabilityMode.NONE:
                self._retransmit_task = asyncio.create_task(
                    self._retransmit_loop(), name="AggregatingLink.retransmit"
                )
                self._ack_sender_task = asyncio.create_task(
                    self._ack_sender_loop(), name="AggregatingLink.ack_sender"
                )

            self._logger.info("AggregatingLink started")

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return

            self._logger.info("Closing...")
            self._closed = True
            self._stop_event.set()

            for queue in self._data_loss_subscribers:
                queue.put_nowait(None)
            for queue in self._stream_notification_subscribers:
                queue.put_nowait(None)

            try:
                await asyncio.to_thread(self._transport.low_transport.close)
            except Exception:
                self._logger.exception("Ошибка при закрытии low_transport")

            tasks = [
                t
                for t in (
                    self._reader_task,
                    self._writer_task,
                    self._retransmit_task,
                    self._ack_sender_task,
                )
                if t is not None
            ]
            current = asyncio.current_task()
            if self._shutdown_task is not current:
                tasks.append(self._shutdown_task)

            for task in tasks:
                task.cancel()

        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def accept_stream(self, protocol: str) -> str:
        """
        Ждёт появления нового входящего стрима с указанным protocol.

        protocol сравнивается со значением Frame.protocol, присланным
        отправителем в первом frame стрима (см. send_frame). Image-стримы
        сигнализируются с protocol == "image" (см. _IMAGE_PROTOCOL).

        Каждый вызов accept_stream заводит собственную подписку (как
        iter_data_loss), поэтому несколько параллельных вызовов — в т.ч.
        ожидающих разные protocol — не воруют уведомления друг у друга:
        не совпавшее уведомление просто игнорируется этим конкретным
        вызовом и достаётся следующее.

        Raises:
            StreamClosed: Если link закрылся до появления подходящего стрима.
        """
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")

        queue = self._subscribe_stream_notifications()
        try:
            while True:
                get_task = asyncio.ensure_future(queue.get())
                stop_task = asyncio.ensure_future(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()

                if stop_task in done:
                    raise StreamClosed("link closed while waiting for a new stream")

                item = get_task.result()
                if item is None:
                    raise StreamClosed("link closed while waiting for a new stream")

                stream_id, stream_protocol = item
                if stream_protocol == protocol:
                    return stream_id

                self._logger.debug(
                    f"accept_stream({protocol!r}): skipping stream={stream_id!r} "
                    f"with protocol={stream_protocol!r}"
                )
        finally:
            self._unsubscribe_stream_notifications(queue)

    async def send_frame(
        self,
        stream_id: str,
        frame_type: str,
        payload: bytes = b"",
        *,
        end: bool = False,
        protocol: str | None = None,
    ) -> None:
        """
        Отправляет frame в исходящую очередь.

        Args:
            stream_id: Идентификатор стрима.
            frame_type: Тип фрейма.
            payload: Полезная нагрузка.
            end: Признак завершения стрима.
            protocol: Указывается только для ПЕРВОГО frame нового стрима —
                именно это значение получит удалённая сторона в
                accept_stream(). Для последующих frame того же stream_id
                можно не указывать: получатель уже знает стрим и значение
                будет проигнорировано.
        """
        if self._closed:
            raise StreamClosed("AggregatingLink is closed")
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")

        frame = Frame(
            stream_id=stream_id,
            frame_type=frame_type,
            payload=bytes(payload),
            end=end,
            protocol=protocol,
        )
        await self._outgoing.put(frame)

    async def recv_frame(self, stream_id: str) -> Frame:
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")

        queue = self._incoming_by_stream.setdefault(stream_id, asyncio.Queue())

        get_task = asyncio.ensure_future(queue.get())
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        done, pending = await asyncio.wait(
            {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        if stop_task in done:
            raise StreamClosed(f"link closed while waiting for stream {stream_id}")

        frame = get_task.result()
        if frame.end and queue.empty():
            self._incoming_by_stream.pop(stream_id, None)
            self._seen_incoming_streams.discard(stream_id)
        return frame

    async def send_image(self, stream_id: str, image: Image.Image) -> None:
        """
        Отправить изображение в поток stream_id.

        stream_id должен быть UUID hex (результат new_stream_id()),
        потому что UUID используется как идентификатор при передаче.

        Режим надёжности берётся из self._image_reliability_mode (задаётся
        в конструкторе через image_reliability_mode), независимо от
        self._reliability_mode для обычных data-фреймов.

        В режиме NONE — fire-and-forget.
        В режимах STOP_AND_WAIT / PARALLEL — ждёт ACK с повторной
        отправкой каждые delay_before_resending секунд.

        Нельзя вызывать одновременно дважды для одного stream_id, если
        image_reliability_mode — не NONE.

        ВАЖНО — ограничение дедупликации при image_reliability_mode != NONE:
        dedup входящих картинок сейчас идёт по stream_id (см. _dispatch_image),
        а не по отдельному id сообщения. Это значит, что при STOP_AND_WAIT/
        PARALLEL можно безопасно отправлять ТОЛЬКО ОДНО изображение за время
        жизни данного stream_id: если на один и тот же stream_id вызвать
        send_image() повторно (например, скриншот за скриншотом, как делает
        AethernetStasisServer), то второй и все последующие вызовы будут
        считаться дубликатами первого на стороне получателя, ACK на них уйдёт
        не глядя на payload, а сам кадр до recv_image() не дойдёт —
        отправитель решит, что доставлено, и получатель зависнет.

        Если нужно слать поток разных изображений по одному stream_id
        (текущий кейс с скриншотами) — используйте только
        image_reliability_mode=ReliabilityMode.NONE. Для reliable-доставки
        отдельных изображений либо шлите каждое на новом stream_id
        (new_stream_id() перед каждым send_image()), либо потребуется
        доработка dedup-механизма (разделение routing id и msg id) —
        это НЕ реализовано.
        """
        if self._closed:
            raise RuntimeError("AggregatingLink is closed")
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")

        if (
                self._image_reliability_mode != ReliabilityMode.NONE
                and stream_id in self._image_ack_events
        ):
            raise RuntimeError(
                f"Another send_image is already in progress for stream {stream_id!r}"
            )

        try:
            img_uuid = uuid.UUID(hex=stream_id)
        except ValueError:
            raise ValueError(
                f"stream_id must be a valid UUID hex string for image transport: {stream_id!r}"
            )

        if self._image_reliability_mode == ReliabilityMode.NONE:
            await self._send_image_physical(image, img_uuid)
            return

        # ── Reliable mode: Stop-and-Wait на уровне изображений ──────────────
        ack_event = asyncio.Event()
        self._image_ack_events[stream_id] = ack_event

        try:
            first_send = time.monotonic()
            attempt = 0
            while not self._stop_event.is_set():
                await self._send_image_physical(image, img_uuid)
                attempt += 1
                if attempt > 1:
                    self._logger.warning(
                        f"Image retransmit #{attempt} for stream={stream_id}, "
                        f"age={time.monotonic() - first_send:.2f}s"
                    )

                try:
                    await asyncio.wait_for(
                        asyncio.shield(ack_event.wait()),
                        timeout=self._delay_before_resending,
                    )
                    return  # ACK получен
                except asyncio.TimeoutError:
                    continue

        finally:
            self._image_ack_events.pop(stream_id, None)

    async def recv_image(self, stream_id: str) -> Image.Image:
        """
        Получить следующее изображение из потока stream_id.

        Фреймы с данными (frame.image is None), которые придут раньше —
        кладутся обратно в очередь, чтобы не потерять их для recv_frame.

        Не вызывайте одновременно recv_frame и recv_image на одном stream_id:
        первый .get() заберёт фрейм из общей очереди, второй его уже не увидит.
        """
        pending_data: list[Frame] = []
        try:
            while True:
                frame = await self.recv_frame(stream_id)
                if frame.image is not None:
                    return frame.image
                # Неожиданный data-фрейм на «image-стриме» — буферизуем
                self._logger.debug(
                    f"recv_image: non-image frame type={frame.frame_type!r} "
                    f"on stream {stream_id!r}, buffering"
                )
                pending_data.append(frame)
        finally:
            # Возвращаем data-фреймы в начало очереди (LIFO-возврат в FIFO-очередь)
            if pending_data:
                queue = self._incoming_by_stream.setdefault(stream_id, asyncio.Queue())
                for f in reversed(pending_data):
                    # put_nowait безопасен — очередь безлимитная
                    # noinspection PyProtectedMember
                    queue._queue.appendleft(f)  # type: ignore[attr-defined]

    async def iter_stream(self, stream_id: str) -> AsyncIterator[Frame]:
        while True:
            frame = await self.recv_frame(stream_id)
            yield frame
            if frame.end:
                return

    async def _send_image_physical(
        self, image: Image.Image, img_uuid: uuid.UUID
    ) -> None:
        """Отправить изображение с соблюдением min_send_interval."""
        await self._throttle_send()
        await asyncio.to_thread(self._transport.send_image, image, img_uuid)
        self._logger.debug(f"Sent image uuid={img_uuid.hex}")

    def _dispatch_image(self, img_uuid: uuid.UUID, image: Image.Image) -> None:
        """
        Вызывается из reader loop когда пришло изображение.

        1. Дедупликация по img_uuid (ретрансмиты от отправителя).
        2. Создаёт Frame с image и кладёт в очередь стрима.
        3. Ставит ACK-фрейм в _outgoing (если режим ARQ).
        """
        stream_id = img_uuid.hex
        uuid_key = img_uuid.hex

        is_duplicate = (
            self._image_reliability_mode != ReliabilityMode.NONE
            and uuid_key in self._received_image_uuids_set
        )

        if is_duplicate:
            self._logger.debug(
                f"Duplicate image uuid={uuid_key[:8]}…, "
                f"dropping payload, re-queueing ACK"
            )
        else:
            # Регистрируем как полученный (скользящее окно 64 UUID)
            evicted = (
                self._received_image_uuids[0]
                if len(self._received_image_uuids) == self._received_image_uuids.maxlen
                else None
            )
            self._received_image_uuids.append(uuid_key)
            self._received_image_uuids_set.add(uuid_key)
            if evicted is not None:
                self._received_image_uuids_set.discard(evicted)

            # Кладём image-фрейм в очередь стрима
            frame = Frame(
                stream_id=stream_id,
                frame_type=_IMG_FRAME_TYPE,
                image=image,
                protocol=_IMAGE_PROTOCOL,
            )
            self._dispatch_image_frame(frame)
            self._logger.debug(f"Dispatched image uuid={uuid_key[:8]}… to stream {stream_id!r}")

        # ACK отправляем в любом случае (дубликат или нет) — отправитель мог
        # не получить предыдущий ACK и поэтому ретрансмитит.
        # Условие завязано на image_reliability_mode (а не на общий
        # reliability_mode для data-фреймов), так как send_image()/recv_image()
        # используют свой независимый режим надёжности.
        if self._image_reliability_mode != ReliabilityMode.NONE:
            ack_frame = Frame(
                stream_id=stream_id,
                frame_type=_IMG_ACK_FRAME_TYPE,
            )
            self._outgoing.put_nowait(ack_frame)
            self._logger.debug(f"Queued IMG_ACK for uuid={uuid_key[:8]}…")

    def _subscribe_data_loss(self) -> asyncio.Queue[list[int] | None]:
        """Регистрирует нового подписчика на data-loss события."""
        queue: asyncio.Queue[list[int] | None] = asyncio.Queue()
        self._data_loss_subscribers.add(queue)
        return queue

    def _unsubscribe_data_loss(self, queue: asyncio.Queue[list[int] | None]) -> None:
        self._data_loss_subscribers.discard(queue)

    def _subscribe_stream_notifications(
        self,
    ) -> asyncio.Queue[tuple[str, str] | None]:
        """Регистрирует нового подписчика на уведомления о новых стримах."""
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        self._stream_notification_subscribers.add(queue)
        return queue

    def _unsubscribe_stream_notifications(
        self, queue: asyncio.Queue[tuple[str, str] | None]
    ) -> None:
        self._stream_notification_subscribers.discard(queue)

    async def iter_data_loss(self) -> AsyncIterator[list[int]]:
        """
        Асинхронный итератор по событиям потери данных.

        Каждый вызов iter_data_loss() создаёт независимую подписку —
        несколько одновременных подписчиков получают одно и то же
        событие, не "съедая" его друг у друга (в отличие от единого
        общего Event). Автоматически отписывается при выходе из
        генератора (break, return, отмена, GeneratorExit).

        Yields:
            Список seq пакетов, отброшенных из reorder buffer по TTL.
            Не 100% гарантия потери данных именно в текущем стриме —
            гарантия того, что *какой-то* batch был безвозвратно отброшен.
        """
        queue = self._subscribe_data_loss()
        try:
            while True:
                seqs = await queue.get()
                if seqs is None:  # сигнал закрытия линка
                    return
                yield seqs
        finally:
            self._unsubscribe_data_loss(queue)

    @staticmethod
    def new_stream_id() -> str:
        """
        Генерирует новый stream_id.

        Формат — чистый uuid4 hex без префиксов: это единственный формат,
        который понимает send_image()/recv_image() (uuid.UUID(hex=stream_id)),
        поэтому он используется одинаково и для обычных, и для image-стримов.
        Protocol стрима передаётся отдельно — через Frame.protocol в первом
        frame (см. send_frame / accept_stream).
        """
        return uuid.uuid4().hex

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    raw_packet = await asyncio.to_thread(
                        self._transport.recv, self._recv_restart_delay
                    )

                    # ── изображение от medium_transport ──────────────────────────
                    if isinstance(raw_packet, tuple):
                        image, img_uuid = raw_packet
                        self._logger.debug(
                            f"recv image: uuid={img_uuid.hex[:8]}…, size={image.size}"
                        )
                        self._dispatch_image(img_uuid, image)
                        self._cleanup_stale_chunk_assemblies()
                        flushed = self._cleanup_stale_reorder_buffer()
                        self._dispatch_flushed_payloads(flushed)
                        if self._recv_restart_delay > 0:
                            await asyncio.sleep(self._recv_restart_delay)
                        continue

                    # ── байтовый пакет ────────────────────────────────────────────
                    self._logger.debug(
                        f"recv physical packet: size={len(raw_packet)}, "
                        f"prefix={raw_packet[:32].hex()}"
                    )
                except asyncio.CancelledError:
                    raise
                except TransportClosedError:
                    self._logger.warning(
                        "Transport closed, stopping reader loop and closing link."
                    )
                    asyncio.create_task(self.close())
                    return
                except Exception as e:
                    self._logger.error(
                        "Error while receiving a packet from medium_transport.recv, "
                        f"retrying in {self._recv_restart_delay}: {e}."
                    )
                    await asyncio.sleep(self._recv_restart_delay)
                    continue

                try:
                    if self._reliability_mode == ReliabilityMode.NONE:
                        logical_payloads = self._decode_legacy_transport_packet(
                            raw_packet
                        )
                        ack_seqs_received: list[int] = []
                    else:
                        logical_payloads, ack_seqs_received = self._decode_agp1_packet(
                            raw_packet
                        )
                except Exception as e:
                    self._logger.error(f"Получен битый transport packet: {e}")
                    continue

                if ack_seqs_received:
                    self._process_incoming_acks(ack_seqs_received)

                self._cleanup_stale_chunk_assemblies()
                flushed = self._cleanup_stale_reorder_buffer()
                if flushed:
                    # Флашнутые из-за TTL-разрыва payload'ы должны идти ПЕРЕД
                    # только что декодированными — они логически раньше по seq.
                    logical_payloads = flushed + logical_payloads

                self._dispatch_flushed_payloads(logical_payloads)

                if self._recv_restart_delay > 0:
                    await asyncio.sleep(self._recv_restart_delay)

        except asyncio.CancelledError:
            self._logger.debug("reader loop cancelled")
            raise

    def _dispatch_flushed_payloads(self, logical_payloads: list[bytes]) -> None:
        """Декодирует список logical batch и рассылает frame'ы по стримам."""
        for logical_payload in logical_payloads:
            try:
                frames = self._decode_batch(logical_payload)
            except Exception as e:
                self._logger.error(f"Получен битый logical batch: {e}")
                continue

            self._logger.debug(
                f"Decoded logical batch: frames={len(frames)}, "
                f"payload_size={len(logical_payload)}"
            )

            for frame in frames:
                self._dispatch_frame(frame)

    def _dispatch_frame(self, frame: Frame) -> None:
        """
        Кладёт frame в очередь нужного стрима и, если стрим новый,
        рассылает (stream_id, protocol) всем подписчикам accept_stream().
        """
        # ── перехват внутреннего IMG_ACK ─────────────────────────────────────
        if frame.frame_type == _IMG_ACK_FRAME_TYPE:
            event = self._image_ack_events.get(frame.stream_id)
            if event is not None:
                self._logger.debug(f"IMG_ACK received for stream={frame.stream_id!r}")
                event.set()
            else:
                self._logger.debug(
                    f"IMG_ACK for stream={frame.stream_id!r} — no waiter (already acked?)"
                )
            return  # не попадает в пользовательскую очередь

        # ── обычная маршрутизация ─────────────────────────────────────────────
        is_new_stream = frame.stream_id not in self._incoming_by_stream
        queue = self._incoming_by_stream.setdefault(frame.stream_id, asyncio.Queue())
        queue.put_nowait(frame)

        if is_new_stream and frame.stream_id not in self._seen_incoming_streams:
            self._seen_incoming_streams.add(frame.stream_id)
            protocol = frame.protocol or ""
            if not frame.protocol:
                self._logger.warning(
                    f"New stream={frame.stream_id!r} without protocol in first frame; "
                    f"accept_stream() filters won't match it unless they ask for protocol=''"
                )
            for sub_queue in self._stream_notification_subscribers:
                sub_queue.put_nowait((frame.stream_id, protocol))

    def _dispatch_image_frame(self, frame: Frame) -> None:
        """
        Как _dispatch_frame, но для image-фреймов: если в очереди стрима уже
        лежит непрочитанный image-фрейм — он удаляется перед вставкой нового.

        Гарантирует, что в очереди image-стрима одновременно лежит не более
        одного изображения (UDP-like поведение: RAM не растёт, потребитель
        всегда получает самое свежее, а не застрявшее старое).
        """
        stream_id = frame.stream_id
        is_new_stream = stream_id not in self._incoming_by_stream
        queue = self._incoming_by_stream.setdefault(stream_id, asyncio.Queue())

        # noinspection PyProtectedMember
        dq = queue._queue  # type: ignore[attr-defined]
        for i, existing in enumerate(dq):
            if existing.image is not None:
                del dq[i]
                self._logger.debug(
                    f"Dropped stale queued image for stream={stream_id!r} "
                    f"(consumer too slow)"
                )
                break

        queue.put_nowait(frame)

        if is_new_stream and stream_id not in self._seen_incoming_streams:
            self._seen_incoming_streams.add(stream_id)
            protocol = frame.protocol or ""
            for sub_queue in self._stream_notification_subscribers:
                sub_queue.put_nowait((stream_id, protocol))

    def _decode_agp1_packet(self, raw: bytes) -> tuple[list[bytes], list[int]]:
        """
        Декодирует пакет формата AGP1 с учётом reorder buffer.

        Returns:
            (logical_payloads, ack_seqs)
            logical_payloads — список готовых logical payload в правильном порядке.
            ack_seqs — список seq которые нужно подтвердить отправителю.
        """
        if not raw.startswith(_PACKET_MAGIC):
            raise ValueError("not an AGP1 packet")

        seq, ack_seqs, payload = _parse_packet(raw)

        if seq == 0:
            # ACK-only пакет, данных нет
            return [], ack_seqs

        if seq in self._received_seqs_set:
            # Дубликат: payload не обрабатываем, но ACK шлём
            self._logger.debug(f"Duplicate packet seq={seq}, sending ACK again")
            self._pending_acks.put_nowait(seq)
            self._has_pending_acks.set()
            return [], ack_seqs

        # Новый пакет — регистрируем в deque дедупликации
        evicted = (
            self._received_seqs[0]
            if len(self._received_seqs) == self._received_seqs.maxlen
            else None
        )
        self._received_seqs.append(seq)
        self._received_seqs_set.add(seq)
        if evicted is not None:
            self._received_seqs_set.discard(evicted)

        # ACK шлём сразу — отправитель может убрать из in_flight
        self._pending_acks.put_nowait(seq)
        self._has_pending_acks.set()

        # --- reorder buffer ---
        # В STOP_AND_WAIT window=1, out-of-order невозможен — идём напрямую.
        if self._reliability_mode == ReliabilityMode.STOP_AND_WAIT:
            return ([payload] if payload else []), ack_seqs

        # PARALLEL: кладём в буфер и флашим всё что уже можно отдать по порядку.
        if payload:
            self._reorder_buffer[seq] = (payload, time.monotonic())
            self._logger.debug(
                f"Reorder buffer: seq={seq}, next_expected={self._next_expected_seq}, "
                f"buffered={sorted(self._reorder_buffer)}"
            )

        logical_payloads = self._flush_reorder_buffer()
        return logical_payloads, ack_seqs

    def _flush_reorder_buffer(self) -> list[bytes]:
        """
        Извлекает из reorder buffer все последовательные батчи начиная
        с next_expected_seq и возвращает их в правильном порядке.
        """
        result: list[bytes] = []
        while self._next_expected_seq in self._reorder_buffer:
            payload, _ = self._reorder_buffer.pop(self._next_expected_seq)
            self._logger.debug(f"Reorder buffer flush: seq={self._next_expected_seq}")
            result.append(payload)
            self._next_expected_seq += 1
        return result

    def _cleanup_stale_reorder_buffer(self) -> list[bytes]:
        """
        Удаляет записи из reorder buffer которые ждут слишком долго.

        На практике не должно срабатывать — ретрансмит бесконечный.
        Защита от крайнего случая когда соединение закрылось в середине.

        Returns:
            Список logical_payload, которые стало можно доставить сразу
            после сдвига next_expected_seq (если после "дыры" в буфере уже
            лежала непрерывная последовательность). Пустой список, если
            ничего не изменилось.
        """
        if not self._reorder_buffer:
            return []

        now = time.monotonic()
        stale = [
            seq
            for seq, (_, arrival) in self._reorder_buffer.items()
            if now - arrival > self._reorder_buffer_ttl
        ]
        if not stale:
            return []

        for seq in stale:
            self._logger.warning(f"Dropping stale reorder buffer entry: seq={seq}")
            del self._reorder_buffer[seq]

        flushed: list[bytes] = []
        if self._next_expected_seq in stale:
            if self._reorder_buffer:
                self._next_expected_seq = min(self._reorder_buffer)
            else:
                self._next_expected_seq = max(stale) + 1

            self._logger.error(
                f"Reorder buffer gap: {len(stale)} batch(es) permanently lost, "
                f"data corruption possible for active streams"
            )
            for queue in self._data_loss_subscribers:
                queue.put_nowait(list(stale))

            # После сдвига next_expected_seq в буфере может уже лежать
            # непрерывный хвост (например buffer={5,6,7}, потеряли 3-4,
            # next стал 5) — не ждём следующего физического пакета, а
            # отдаём его сразу.
            flushed = self._flush_reorder_buffer()

        return flushed

    def _process_incoming_acks(self, ack_seqs: list[int]) -> None:
        """
        Обрабатывает входящие ACK: удаляет подтверждённые пакеты из in_flight
        и освобождает слоты в window semaphore.
        """
        for ack_seq in ack_seqs:
            if ack_seq in self._in_flight:
                pkt = self._in_flight.pop(ack_seq)
                rtt = time.monotonic() - pkt.first_send_time
                self._logger.debug(
                    f"ACK received for seq={ack_seq}, RTT={rtt:.3f}s, "
                    f"in_flight={len(self._in_flight)}"
                )
                self._window_semaphore.release()
                self._ack_received_event.set()
            else:
                self._logger.debug(f"ACK for unknown/already-acked seq={ack_seq}")

    # ------------------------------------------------------------------
    # Writer loop
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._pending_outgoing is not None:
                    first = self._pending_outgoing
                    self._pending_outgoing = None
                else:
                    first = await self._outgoing.get()

                batch = [first]
                batch_size_estimate = self._estimate_frame_size(first)
                deadline = time.monotonic() + self._flush_interval

                while batch_size_estimate < self._max_batch_size:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    try:
                        next_frame = await asyncio.wait_for(
                            self._outgoing.get(), timeout=timeout
                        )
                    except asyncio.TimeoutError:
                        break

                    next_estimate = self._estimate_frame_size(next_frame)
                    if batch_size_estimate + next_estimate > self._max_batch_size:
                        self._pending_outgoing = next_frame
                        break

                    batch.append(next_frame)
                    batch_size_estimate += next_estimate

                logical_payload = self._encode_batch(batch)

                try:
                    if self._reliability_mode == ReliabilityMode.NONE:
                        await self._send_legacy(logical_payload)
                    else:
                        await self._send_reliable(logical_payload)
                except asyncio.CancelledError:
                    raise
                except TransportClosedError:
                    self._logger.warning(
                        "Transport closed, stopping writer loop and closing link."
                    )
                    asyncio.create_task(self.close())
                    return
                except Exception:
                    self._logger.exception("Error when sending a message via medium_transport.")
                    await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            self._logger.debug("writer loop cancelled")
            raise

    async def _send_reliable(self, logical_payload: bytes) -> None:
        """
        Отправляет logical payload в режиме ARQ.

        Ждёт свободного слота в window (semaphore), назначает seq,
        строит AGP1 пакет (piggyback любые накопленные ACK),
        кладёт в in_flight и шлёт физически.
        Повторная отправка при таймауте — в _retransmit_loop.
        """
        await self._window_semaphore.acquire()

        if self._stop_event.is_set():
            self._window_semaphore.release()
            return

        seq = self._seq_counter
        self._seq_counter += 1
        if self._seq_counter > 0xFFFFFFFF:
            self._seq_counter = 1

        ack_seqs = self._drain_pending_acks()
        packet = _build_packet(seq, ack_seqs, logical_payload)

        now = time.monotonic()
        self._in_flight[seq] = _InFlightPacket(
            seq=seq,
            packet_bytes=packet,
            first_send_time=now,
            last_send_time=now,
        )

        self._logger.debug(
            f"Sending reliable seq={seq}, payload={len(logical_payload)}b, "
            f"piggybacked_acks={ack_seqs}, in_flight={len(self._in_flight)}"
        )

        await self._send_physical_packet(packet)

    async def _send_legacy(self, logical_payload: bytes) -> None:
        """Отправляет в старом формате AGS1/AGC1 (режим NONE)."""
        transport_packets = self._encode_legacy_transport_packets(logical_payload)

        if len(transport_packets) > 1:
            self._logger.warning(
                f"Logical payload split into {len(transport_packets)} transport packets "
                f"(payload_size={len(logical_payload)}, transport_limit={self._transport_limit})"
            )

        for packet_index, packet in enumerate(transport_packets):
            self._logger.debug(
                f"Sending legacy packet {packet_index + 1}/{len(transport_packets)}: "
                f"size={len(packet)}, prefix={packet[:32].hex()}"
            )
            await self._send_physical_packet(packet)

    async def _throttle_send(self) -> None:
        """
        Гарантирует min_send_interval между физическими send, независимо
        от того, откуда вызов — _send_physical_packet или
        _send_image_physical. Использует lock, чтобы конкурентные вызовы
        (например send_frame из writer loop и send_image из пользовательской
        задачи) не считали wait от одного и того же устаревшего
        _last_send_ts.
        """
        async with self._send_lock:
            now = time.monotonic()
            wait = self._min_send_interval - (now - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_ts = time.monotonic()

    # ------------------------------------------------------------------
    # Retransmit loop
    # ------------------------------------------------------------------

    async def _retransmit_loop(self) -> None:
        """
        Фоновая задача: каждые ~50мс проверяет in_flight.
        Если пакет не получил ACK за delay_before_resending секунд — шлёт снова.
        Цикл бесконечный: повторяет пока ACK не придёт.
        """
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)

                if not self._in_flight:
                    continue

                now = time.monotonic()
                to_retransmit = [
                    pkt
                    for pkt in self._in_flight.values()
                    if now - pkt.last_send_time >= self._delay_before_resending
                ]

                for pkt in to_retransmit:
                    if pkt.seq not in self._in_flight:
                        continue

                    self._logger.warning(
                        f"Retransmitting seq={pkt.seq}, "
                        f"age={now - pkt.first_send_time:.2f}s"
                    )

                    ack_seqs = self._drain_pending_acks()
                    if ack_seqs:
                        seq, _, payload = _parse_packet(pkt.packet_bytes)
                        new_packet = _build_packet(seq, ack_seqs, payload)
                        pkt.packet_bytes = new_packet

                    pkt.last_send_time = time.monotonic()

                    try:
                        await self._send_physical_packet(pkt.packet_bytes)
                    except TransportClosedError:
                        self._logger.warning(
                            "Transport closed, stopping retransmit loop and closing link."
                        )
                        asyncio.create_task(self.close())
                        return
                    except Exception:
                        self._logger.exception(f"Retransmit failed for seq={pkt.seq}")

        except asyncio.CancelledError:
            self._logger.debug("retransmit loop cancelled")
            raise

    # ------------------------------------------------------------------
    # ACK sender loop
    # ------------------------------------------------------------------

    async def _ack_sender_loop(self) -> None:
        """
        Агрегирует pending ACK и отправляет их либо по таймеру,
        либо когда накопилось ack_batch_size штук.

        Страховка на случай когда исходящих данных долго нет и piggyback
        не происходит. Дублирующие ACK на другой стороне игнорируются.
        """
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._has_pending_acks.wait(),
                        timeout=self._ack_flush_interval,
                    )
                except asyncio.TimeoutError:
                    pass

                if self._pending_acks.empty():
                    self._has_pending_acks.clear()
                    continue

                ack_seqs = self._drain_pending_acks()
                if not ack_seqs:
                    self._has_pending_acks.clear()
                    continue

                packet = _build_packet(seq=0, ack_seqs=ack_seqs, payload=b"")
                self._logger.debug(f"Sending ACK-only packet: acks={ack_seqs}")

                try:
                    await self._send_physical_packet(packet)
                except asyncio.CancelledError:
                    raise
                except TransportClosedError:
                    self._logger.warning(
                        "Transport closed, stopping ack sender loop and closing link."
                    )
                    asyncio.create_task(self.close())
                    return
                except Exception:
                    self._logger.exception("Failed to send ACK-only packet")
                    for s in ack_seqs:
                        self._pending_acks.put_nowait(s)
                    self._has_pending_acks.set()

                self._has_pending_acks.clear()

        except asyncio.CancelledError:
            self._logger.debug("ack sender loop cancelled")
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _drain_pending_acks(self) -> list[int]:
        """Забирает до ack_batch_size ACK из очереди не блокируясь."""
        acks: list[int] = []
        while len(acks) < self._ack_batch_size:
            try:
                acks.append(self._pending_acks.get_nowait())
            except asyncio.QueueEmpty:
                break
        return acks

    async def _send_physical_packet(self, packet: bytes) -> None:
        """Отправляет ОДИН physical packet строго соблюдая min_send_interval."""
        await self._throttle_send()
        await asyncio.to_thread(self._transport.send, packet)
        self._logger.debug("Sent package")

    # ------------------------------------------------------------------
    # Legacy encoding / decoding (режим NONE)
    # ------------------------------------------------------------------

    def _encode_legacy_transport_packets(self, logical_payload: bytes) -> list[bytes]:
        if len(logical_payload) <= self._single_packet_payload_limit:
            return [self.SINGLE_MAGIC + logical_payload]

        msg_id = uuid.uuid4().bytes
        chunk_payload_limit = self._chunk_packet_payload_limit
        total_parts = math.ceil(len(logical_payload) / chunk_payload_limit)

        if total_parts > 0xFFFF:
            raise ValueError("logical payload too large: too many chunks")

        packets: list[bytes] = []
        for part_index in range(total_parts):
            start = part_index * chunk_payload_limit
            chunk_data = logical_payload[start : start + chunk_payload_limit]
            packet = (
                self.CHUNK_MAGIC
                + self._CHUNK_META_STRUCT.pack(msg_id, part_index, total_parts)
                + chunk_data
            )
            packets.append(packet)

        return packets

    def _decode_legacy_transport_packet(self, raw_packet: bytes) -> list[bytes]:
        if raw_packet.startswith(self.SINGLE_MAGIC):
            return [raw_packet[len(self.SINGLE_MAGIC) :]]

        if raw_packet.startswith(self.CHUNK_MAGIC):
            header_start = len(self.CHUNK_MAGIC)
            header_end = header_start + self._CHUNK_META_STRUCT.size

            if len(raw_packet) < header_end:
                raise ValueError("chunk packet too short")

            msg_id, part_index, total_parts = self._CHUNK_META_STRUCT.unpack(
                raw_packet[header_start:header_end]
            )
            chunk_data = raw_packet[header_end:]

            assembly = self._chunk_assemblies.get(msg_id)
            if assembly is None:
                assembly = _ChunkAssembly(
                    total_parts=total_parts,
                    parts={},
                    created_at=time.monotonic(),
                )
                self._chunk_assemblies[msg_id] = assembly
            elif assembly.total_parts != total_parts:
                raise ValueError("chunk total_parts mismatch")

            assembly.parts[part_index] = chunk_data

            if len(assembly.parts) == assembly.total_parts:
                payload = b"".join(
                    assembly.parts[i] for i in range(assembly.total_parts)
                )
                del self._chunk_assemblies[msg_id]
                return [payload]

            return []

        raise ValueError("unknown transport packet magic")

    def _cleanup_stale_chunk_assemblies(self) -> None:
        if not self._chunk_assemblies:
            return
        now = time.monotonic()
        stale_ids = [
            msg_id
            for msg_id, assembly in self._chunk_assemblies.items()
            if now - assembly.created_at > self._chunk_assembly_ttl
        ]
        for msg_id in stale_ids:
            self._logger.warning(
                f"Dropping stale chunk assembly: msg_id={msg_id.hex()}"
            )
            del self._chunk_assemblies[msg_id]

    # ------------------------------------------------------------------
    # Batch encoding / decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_frame_size(frame: Frame) -> int:
        return 64 + len(frame.stream_id) + len(frame.frame_type) + len(frame.payload)

    @staticmethod
    def _encode_batch(frames: list[Frame]) -> bytes:
        data: list[dict[str, Any]] = [
            {
                "s": frame.stream_id,
                "t": frame.frame_type,
                "p": frame.payload,
                "e": frame.end,
                "pr": frame.protocol,
            }
            for frame in frames
        ]
        return msgpack.packb(data, use_bin_type=True)

    @staticmethod
    def _decode_batch(raw: bytes) -> list[Frame]:
        items = msgpack.unpackb(raw, raw=False)

        if not isinstance(items, list):
            raise ValueError("batch must be a list")

        frames: list[Frame] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("frame must be dict")

            stream_id = item["s"]
            frame_type = item["t"]
            payload = item.get("p", b"")
            end = bool(item.get("e", False))
            protocol = item.get("pr")

            if not isinstance(stream_id, str):
                raise ValueError("stream_id must be str")
            if not isinstance(frame_type, str):
                raise ValueError("frame_type must be str")
            if not isinstance(payload, (bytes, bytearray)):
                raise ValueError("payload must be bytes")
            if protocol is not None and not isinstance(protocol, str):
                raise ValueError("protocol must be str or None")

            frames.append(
                Frame(
                    stream_id=stream_id,
                    frame_type=frame_type,
                    payload=bytes(payload),
                    end=end,
                    protocol=protocol,
                )
            )

        return frames

    # ------------------------------------------------------------------
    # Shutdown watcher
    # ------------------------------------------------------------------

    async def _shutdown_watcher(self):
        try:
            await asyncio.Future()
        finally:
            self._logger.info("Shutting down watcher")
            await self.close()
