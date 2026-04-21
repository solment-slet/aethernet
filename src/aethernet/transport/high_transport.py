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

from aethernet.transport.enums import ReliabilityMode
from aethernet.exceptions import StreamClosed
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
    """

    stream_id: str
    frame_type: str
    payload: bytes = b""
    end: bool = False


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
        ValueError: Если пакет повреждён.
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
    - NONE          — без подтверждений (оригинальное поведение)
    - STOP_AND_WAIT — Stop-and-Wait ARQ (window_size=1)
    - PARALLEL      — Selective-Repeat ARQ с окном window_size > 1

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
        window_size: int = 8,
        ack_flush_interval: float = 0.1,
        ack_batch_size: int = 8,
        received_seqs_window: int = 256,  # window_size * 4. Default: 256
    ) -> None:
        """
        Args:
            transport: Синхронный низкоуровневый транспорт.
            flush_interval: Maximum waiting time before sending a batch.
            max_batch_size: Maximum logical batch size in bytes.
            chunk_assembly_ttl: Lifetime of an incomplete chunk assembly.
            reliability_mode: Delivery reliability mode.
            window_size: Window size for PARALLEL mode.
            ack_flush_interval: Maximum ACK accumulation time before sending.
            ack_batch_size: Maximum number of ACKs in a single flush.
            received_seqs_window: How many recent seqs to store for deduplication.
        """
        self._logger = logger
        self._transport = transport
        self._config = self._transport.config

        self._flush_interval = flush_interval
        self._chunk_assembly_ttl = chunk_assembly_ttl
        self._min_send_interval = self._config.min_send_interval
        self._recv_restart_delay = self._config.min_recv_interval
        self._delay_before_resending = self._config.delay_before_resending

        # --- reliability config ---
        self._reliability_mode = reliability_mode
        self._window_size = (
            1 if reliability_mode == ReliabilityMode.STOP_AND_WAIT else window_size
        )
        self._ack_flush_interval = ack_flush_interval
        self._ack_batch_size = ack_batch_size
        self._received_seqs_window = received_seqs_window

        self._shutdown_task = asyncio.create_task(self._shutdown_watcher())

        self._transport_limit: int = self._transport.max_payload_bytes

        self._single_packet_payload_limit = self._transport_limit - len(
            self.SINGLE_MAGIC
        )
        self._chunk_packet_payload_limit = (
            self._transport_limit - len(self.CHUNK_MAGIC) - self._CHUNK_META_STRUCT.size
        )
        # payload limit для нового AGP1 формата (без ACK — их добавим позже при необходимости)
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

        self._max_batch_size = min(max_batch_size, self._single_packet_payload_limit)

        # --- outgoing / incoming queues ---
        self._outgoing: asyncio.Queue[Frame] = asyncio.Queue()
        self._incoming_by_stream: dict[str, asyncio.Queue[Frame]] = {}
        self._new_stream_notifications: asyncio.Queue[str] = asyncio.Queue()
        self._seen_incoming_streams: set[str] = set()
        self._pending_outgoing: Frame | None = None

        # --- chunk assembly (режим NONE) ---
        self._chunk_assemblies: dict[bytes, _ChunkAssembly] = {}

        # --- reliability: sender side ---
        # Пакеты в полёте: seq → _InFlightPacket
        self._in_flight: dict[int, _InFlightPacket] = {}
        # Счётчик seq (начинаем с 1, 0 зарезервирован для ACK-only)
        self._seq_counter: int = 1
        # Семафор ограничивает количество одновременно летящих пакетов
        self._window_semaphore: asyncio.Semaphore = asyncio.Semaphore(self._window_size)
        # Событие: пришёл новый ACK → retransmit loop может проснуться
        self._ack_received_event: asyncio.Event = asyncio.Event()

        # --- reliability: receiver side ---
        # Скользящее окно последних received_seqs_window seq для дедупликации.
        # deque с maxlen автоматически вытесняет старые записи — нет утечки памяти.
        self._received_seqs: deque[int] = deque(maxlen=self._received_seqs_window)
        # set-представление для O(1) проверки вхождения
        self._received_seqs_set: set[int] = set()
        # Очередь seq ожидающих отправки ACK
        self._pending_acks: asyncio.Queue[int] = asyncio.Queue()
        # Событие: есть ACK для отправки (будит ACK aggregator)
        self._has_pending_acks: asyncio.Event = asyncio.Event()

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

            await asyncio.to_thread(self._transport.low_transport.close)

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

    async def accept_stream(self) -> str:
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")
        return await self._new_stream_notifications.get()

    async def send_frame(
        self,
        stream_id: str,
        frame_type: str,
        payload: bytes = b"",
        *,
        end: bool = False,
    ) -> None:
        if self._closed:
            raise RuntimeError("AggregatingLink is closed")
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")

        frame = Frame(
            stream_id=stream_id,
            frame_type=frame_type,
            payload=bytes(payload),
            end=end,
        )
        await self._outgoing.put(frame)

    async def recv_frame(self, stream_id: str) -> Frame:
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")

        queue = self._incoming_by_stream.setdefault(stream_id, asyncio.Queue())
        frame = await queue.get()

        if frame.frame_type == "__closed__":
            raise StreamClosed(f"link closed while waiting for stream {stream_id}")

        return frame

    async def iter_stream(self, stream_id: str) -> AsyncIterator[Frame]:
        while True:
            frame = await self.recv_frame(stream_id)
            yield frame
            if frame.end:
                return

    @staticmethod
    def new_stream_id() -> str:
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
                    self._logger.debug(
                        f"recv physical packet: size={len(raw_packet)}, "
                        f"prefix={raw_packet[:32].hex()}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.exception("Ошибка транспорта recv, продолжаем работу.")
                    await asyncio.sleep(self._recv_restart_delay)
                    continue

                try:
                    if self._reliability_mode == ReliabilityMode.NONE:
                        logical_payloads = self._decode_legacy_transport_packet(
                            raw_packet
                        )
                        ack_seqs_received: list[int] = []
                    else:
                        seq, ack_seqs_received, logical_payloads = (
                            self._decode_agp1_packet(raw_packet)
                        )
                except Exception as e:
                    self._logger.error(f"Получен битый transport packet: {e}")
                    continue

                # --- обработка входящих ACK ---
                if ack_seqs_received:
                    self._process_incoming_acks(ack_seqs_received)

                self._cleanup_stale_chunk_assemblies()

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
                        is_new_stream = frame.stream_id not in self._incoming_by_stream
                        queue = self._incoming_by_stream.setdefault(
                            frame.stream_id, asyncio.Queue()
                        )
                        queue.put_nowait(frame)

                        if (
                            is_new_stream
                            and frame.stream_id not in self._seen_incoming_streams
                        ):
                            self._seen_incoming_streams.add(frame.stream_id)
                            self._new_stream_notifications.put_nowait(frame.stream_id)

                if self._recv_restart_delay > 0:
                    await asyncio.sleep(self._recv_restart_delay)

        except asyncio.CancelledError:
            self._logger.debug("reader loop cancelled")
            raise

    def _decode_agp1_packet(self, raw: bytes) -> tuple[int, list[int], list[bytes]]:
        """
        Декодирует пакет формата AGP1.

        Returns:
            (seq, ack_seqs, logical_payloads)
            logical_payloads — список готовых logical payload для обработки.
            Для дубликатов payload пустой, но ACK всё равно шлётся.
        """
        if not raw.startswith(_PACKET_MAGIC):
            raise ValueError("not an AGP1 packet")

        seq, ack_seqs, payload = _parse_packet(raw)

        logical_payloads: list[bytes] = []

        if seq == 0:
            # ACK-only пакет, данных нет
            pass
        elif seq in self._received_seqs_set:
            # Дубликат: payload не обрабатываем, но ACK шлём
            self._logger.debug(f"Duplicate packet seq={seq}, sending ACK again")
            self._pending_acks.put_nowait(seq)
            self._has_pending_acks.set()
        else:
            # Новый пакет: добавляем в deque (старый seq вытесняется автоматически)
            evicted = (
                self._received_seqs[0]
                if len(self._received_seqs) == self._received_seqs.maxlen
                else None
            )
            self._received_seqs.append(seq)
            self._received_seqs_set.add(seq)
            if evicted is not None:
                self._received_seqs_set.discard(evicted)
            self._pending_acks.put_nowait(seq)
            self._has_pending_acks.set()
            if payload:
                logical_payloads.append(payload)

        return seq, ack_seqs, logical_payloads

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
                # Берём первый frame
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
                except Exception:
                    self._logger.exception("Ошибка при отправке сообщения")
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
        # Ждём свободного слота в окне
        await self._window_semaphore.acquire()

        if self._stop_event.is_set():
            self._window_semaphore.release()
            return

        seq = self._seq_counter
        self._seq_counter += 1

        # Piggyback: забираем все накопленные ACK
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

    # ------------------------------------------------------------------
    # Retransmit loop
    # ------------------------------------------------------------------

    async def _retransmit_loop(self) -> None:
        """
        Фоновая задача: каждые ~50мс проверяет in_flight.
        Если пакет не получил ACK за delay_before_resending секунд — шлёт снова.
        Цикл бесконечный: повторяет пока ACK не придёт (сколько бы раз ни терялся).
        """
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)

                if not self._in_flight:
                    continue

                now = time.monotonic()
                # Копируем список чтобы не итерироваться по изменяемому dict
                to_retransmit = [
                    pkt
                    for pkt in self._in_flight.values()
                    if now - pkt.last_send_time >= self._delay_before_resending
                ]

                for pkt in to_retransmit:
                    if pkt.seq not in self._in_flight:
                        # ACK пришёл пока мы готовили список
                        continue

                    self._logger.warning(
                        f"Retransmitting seq={pkt.seq}, "
                        f"age={now - pkt.first_send_time:.2f}s, "
                        f"attempts since last send={now - pkt.last_send_time:.2f}s"
                    )

                    # Piggyback накопленные ACK и в ретрансмит тоже
                    ack_seqs = self._drain_pending_acks()
                    if ack_seqs:
                        # Перестраиваем пакет с новыми ACK
                        seq, _, payload = _parse_packet(pkt.packet_bytes)
                        new_packet = _build_packet(seq, ack_seqs, payload)
                        pkt.packet_bytes = new_packet

                    pkt.last_send_time = time.monotonic()

                    try:
                        await self._send_physical_packet(pkt.packet_bytes)
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

        Если в момент flush есть исходящие данные — writer сам
        сделает piggyback при следующей отправке. Здесь шлём
        ACK-only пакет только когда данных нет.

        Примечание: piggyback в writer/_retransmit_loop уже забирает
        ACK через _drain_pending_acks(). Этот loop — страховка для
        случая когда исходящих данных долго нет.
        """
        try:
            while not self._stop_event.is_set():
                # Ждём либо таймера, либо сигнала о новых ACK
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

                # Проверяем: если writer вот-вот отправит данные и сделает
                # piggyback — ждать не нужно, но мы не знаем наверняка.
                # Поэтому просто шлём ACK-only пакет. Дублирующий ACK
                # на другой стороне просто проигнорируется (уже удалён из in_flight).
                ack_seqs = self._drain_pending_acks()
                if not ack_seqs:
                    self._has_pending_acks.clear()
                    continue

                packet = _build_packet(seq=0, ack_seqs=ack_seqs, payload=b"")
                self._logger.debug(f"Sending ACK-only packet: acks={ack_seqs}")

                try:
                    await self._send_physical_packet(packet)
                except Exception:
                    self._logger.exception("Failed to send ACK-only packet")
                    # Возвращаем ACK обратно чтобы не потерять
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
        """
        Забирает до ack_batch_size ACK из очереди не блокируясь.
        """
        acks: list[int] = []
        while len(acks) < self._ack_batch_size:
            try:
                acks.append(self._pending_acks.get_nowait())
            except asyncio.QueueEmpty:
                break
        return acks

    async def _send_physical_packet(self, packet: bytes) -> None:
        """
        Отправляет ОДИН physical packet строго соблюдая min_send_interval.
        """
        now = time.monotonic()
        wait_more = self._min_send_interval - (now - self._last_send_ts)
        if wait_more > 0:
            self._logger.debug(f"Waiting {wait_more:.3f}s before next physical send")
            await asyncio.sleep(wait_more)

        await asyncio.to_thread(self._transport.send, packet)
        self._last_send_ts = time.monotonic()

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
    # Batch encoding / decoding (без изменений)
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

            if not isinstance(stream_id, str):
                raise ValueError("stream_id must be str")
            if not isinstance(frame_type, str):
                raise ValueError("frame_type must be str")
            if not isinstance(payload, (bytes, bytearray)):
                raise ValueError("payload must be bytes")

            frames.append(
                Frame(
                    stream_id=stream_id,
                    frame_type=frame_type,
                    payload=bytes(payload),
                    end=end,
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
            await self.close()
