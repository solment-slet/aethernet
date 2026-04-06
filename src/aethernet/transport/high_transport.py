from __future__ import annotations

import asyncio
import math
import struct
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Any

import msgpack

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


class StreamClosed(Exception):
    """Исключение, возникающее при закрытии link во время ожидания frame."""

    pass


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


class AggregatingLink:
    """
    Надстройка над синхронным капризным Transport.

    Основные идеи:
    - один постоянный reader loop, почти постоянно сидящий в transport.recv()
    - один writer loop, агрегирующий логические frame в батчи
    - маршрутизация входящих frame по stream_id
    - если logical batch слишком большой для Transport, он режется на transport chunks

    ВАЖНО:
    - min_send_interval соблюдается между ЛЮБЫМИ physical send(), включая chunk-пакеты
    - transport.max_payload_bytes, если есть, считается абсолютным лимитом payload для одного send()

    Формат logical batch:
        msgpack.packb(
            [
                {"s": stream_id, "t": frame_type, "p": payload, "e": end},
                ...
            ],
            use_bin_type=True
        )

    Формат transport packet:
        1) single:
            b"AGS1" + logical_payload

        2) chunk:
            b"AGC1" + struct.pack(">16sHH", msg_id, part_index, total_parts) + chunk_data
    """

    SINGLE_MAGIC = b"AGS1"
    CHUNK_MAGIC = b"AGC1"

    # 16 bytes  - msg_id
    # 2 bytes   - part_index
    # 2 bytes   - total_parts
    _CHUNK_META_STRUCT = struct.Struct(">16sHH")

    def __init__(
        self,
        transport: MediumTransport,
        *,
        logger: LoggerLike = logging.getLogger(__name__),
        flush_interval: float = 0.5,
        min_send_interval: float = 0.25,
        max_batch_size: int = 64 * 1024,
        recv_restart_delay: float = 0.0,
        chunk_assembly_ttl: float = 60.0,
    ) -> None:
        """
        Args:
            transport: Синхронный низкоуровневый транспорт.
            flush_interval: Максимальное время ожидания перед отправкой батча.
            min_send_interval: Минимальный интервал между ЛЮБЫМИ physical send().
            max_batch_size: Максимальный размер logical batch в байтах.
            recv_restart_delay: Пауза между итерациями recv loop.
            chunk_assembly_ttl: Время жизни незавершённой сборки chunk-пакетов.
        """
        self._logger = logger
        self._transport = transport

        self._flush_interval = flush_interval
        self._min_send_interval = min_send_interval
        self._recv_restart_delay = recv_restart_delay
        self._chunk_assembly_ttl = chunk_assembly_ttl

        # Если transport знает свой лимит полезной нагрузки для одного send(),
        # используем его для transport-level framing.
        self._transport_limit: int | None = getattr(
            transport, "max_payload_bytes", None
        )

        if self._transport_limit is not None:
            self._single_packet_payload_limit = self._transport_limit - len(
                self.SINGLE_MAGIC
            )
            self._chunk_packet_payload_limit = (
                self._transport_limit
                - len(self.CHUNK_MAGIC)
                - self._CHUNK_META_STRUCT.size
            )

            if self._single_packet_payload_limit <= 0:
                raise ValueError(
                    "transport.max_payload_bytes too small for SINGLE packet framing"
                )

            if self._chunk_packet_payload_limit <= 0:
                raise ValueError(
                    "transport.max_payload_bytes too small for CHUNK packet framing"
                )

            # Чтобы writer loop по умолчанию старался укладываться в один physical packet,
            # автоматически сужаем max_batch_size до single packet payload limit.
            self._max_batch_size = min(
                max_batch_size, self._single_packet_payload_limit
            )
        else:
            self._single_packet_payload_limit = None
            self._chunk_packet_payload_limit = None
            self._max_batch_size = max_batch_size

        # Очередь исходящих logical frame.
        self._outgoing: asyncio.Queue[Frame] = asyncio.Queue()

        # Входящие logical frame по stream_id.
        self._incoming_by_stream: dict[str, asyncio.Queue[Frame]] = {}

        # Уведомления о появлении нового stream_id.
        self._new_stream_notifications: asyncio.Queue[str] = asyncio.Queue()
        self._seen_incoming_streams: set[str] = set()

        # Один frame, который не влез в предыдущий batch.
        # Важно для сохранения строгого порядка без возврата в конец очереди.
        self._pending_outgoing: Frame | None = None

        # Буфера для сборки chunk-пакетов.
        self._chunk_assemblies: dict[bytes, _ChunkAssembly] = {}

        # Reader/writer tasks.
        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None

        # Lifecycle.
        self._started = False
        self._closed = False
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

        # Timestamp последнего physical send().
        self._last_send_ts = 0.0

        self._logger.info(
            "AggregatingLink initialized: transport_limit={}, single_limit={}, chunk_limit={}, max_batch_size={}",
            self._transport_limit,
            self._single_packet_payload_limit,
            self._chunk_packet_payload_limit,
            self._max_batch_size,
        )

    async def start(self) -> None:
        """
        Запускает reader и writer loop.

        Повторный вызов безопасен.

        Raises:
            RuntimeError: Если link уже закрыт.
        """
        async with self._lock:
            if self._started:
                return

            if self._closed:
                raise RuntimeError("AggregatingLink is closed")

            self._started = True
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="AggregatingLink.reader"
            )
            self._writer_task = asyncio.create_task(
                self._writer_loop(), name="AggregatingLink.writer"
            )

            self._logger.info("AggregatingLink started")

    async def close(self) -> None:
        """
        Останавливает link и уведомляет все активные stream-очереди о закрытии.
        """
        async with self._lock:
            if self._closed:
                return

            self._closed = True
            self._stop_event.set()

            tasks = [t for t in (self._reader_task, self._writer_task) if t is not None]
            for task in tasks:
                task.cancel()

        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        for q in self._incoming_by_stream.values():
            q.put_nowait(
                Frame(stream_id="__control__", frame_type="__closed__", end=True)
            )

        self._logger.info("AggregatingLink closed")

    async def accept_stream(self) -> str:
        """
        Ожидает появления нового входящего stream_id.

        Returns:
            Идентификатор нового стрима.
        """
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
        """
        Помещает logical frame в очередь на отправку.

        Args:
            stream_id: Идентификатор логического стрима.
            frame_type: Тип фрейма.
            payload: Полезная нагрузка.
            end: Завершает стрим, если True.

        Raises:
            RuntimeError: Если link не запущен или уже закрыт.
            TypeError: Если payload не bytes-like.
        """
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
        """
        Получает следующий входящий frame для указанного stream_id.

        Args:
            stream_id: Идентификатор стрима.

        Returns:
            Frame.

        Raises:
            RuntimeError: Если link не запущен.
            StreamClosed: Если link закрылся во время ожидания.
        """
        if not self._started:
            raise RuntimeError("AggregatingLink.start() must be called first")

        queue = self._incoming_by_stream.setdefault(stream_id, asyncio.Queue())
        frame = await queue.get()

        if frame.frame_type == "__closed__":
            raise StreamClosed(f"link closed while waiting for stream {stream_id}")

        return frame

    async def iter_stream(self, stream_id: str):
        """
        Асинхронный итератор по всем frame данного stream_id.

        Завершается при получении frame с end=True.
        """
        while True:
            frame = await self.recv_frame(stream_id)
            yield frame
            if frame.end:
                return

    @staticmethod
    def new_stream_id() -> str:
        """
        Генерирует новый уникальный stream_id.

        Returns:
            Строковый hex-идентификатор.
        """
        return uuid.uuid4().hex

    async def _reader_loop(self) -> None:
        """
        Постоянно читает physical packets из transport, при необходимости
        собирает chunk-пакеты, декодирует logical batches и раскладывает
        frame по stream-очередям.
        """
        try:
            while not self._stop_event.is_set():
                try:
                    raw_packet = await asyncio.to_thread(self._transport.recv)

                    self._logger.debug(
                        "AggregatingLink recv physical packet: size={}, prefix={}",
                        len(raw_packet),
                        raw_packet[:32].hex(),
                    )

                except asyncio.CancelledError:
                    raise

                except Exception:
                    self._logger.exception("Ошибка транспорта recv, продолжаем работу.")
                    await asyncio.sleep(0.2)
                    continue

                try:
                    logical_payloads = self._decode_transport_packet(raw_packet)
                except Exception as e:
                    self._logger.error("Получен битый transport packet: {}", e)
                    continue

                # Чистим подвисшие незавершенные chunk-сборки.
                self._cleanup_stale_chunk_assemblies()

                for logical_payload in logical_payloads:
                    try:
                        frames = self._decode_batch(logical_payload)
                    except Exception as e:
                        self._logger.error("Получен битый logical batch: {}", e)
                        continue

                    self._logger.debug(
                        "Decoded logical batch: frames={}, payload_size={}",
                        len(frames),
                        len(logical_payload),
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
            self._logger.debug("AggregatingLink reader loop cancelled")
            raise

    async def _writer_loop(self) -> None:
        """
        Постоянно собирает logical frame в logical batches и отправляет их в transport.

        ВАЖНО:
        порядок frame сохраняется строго.
        Если очередной frame не влезает в текущий batch, он НЕ возвращается
        в asyncio.Queue, а сохраняется в self._pending_outgoing и будет обработан
        первым на следующей итерации.
        """
        try:
            while not self._stop_event.is_set():
                # Берем первый frame либо из pending, либо из основной очереди.
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
                        # ВАЖНО: не возвращаем frame в очередь, иначе он окажется
                        # в конце и сломает порядок.
                        self._pending_outgoing = next_frame
                        break

                    batch.append(next_frame)
                    batch_size_estimate += next_estimate

                logical_payload = self._encode_batch(batch)
                transport_packets = self._encode_transport_packets(logical_payload)

                self._logger.debug(
                    "Prepared batch: frames={}, est_size={}, logical_payload_size={}, transport_packets={}",
                    len(batch),
                    batch_size_estimate,
                    len(logical_payload),
                    len(transport_packets),
                )

                if len(transport_packets) > 1:
                    self._logger.warning(
                        "Logical payload split into {} transport packets (payload_size={}, transport_limit={})",
                        len(transport_packets),
                        len(logical_payload),
                        self._transport_limit,
                    )

                try:
                    for packet_index, packet in enumerate(transport_packets):
                        self._logger.debug(
                            "Sending physical packet {}/{}: size={}, prefix={}",
                            packet_index + 1,
                            len(transport_packets),
                            len(packet),
                            packet[:32].hex(),
                        )
                        await self._send_physical_packet(packet)

                except asyncio.CancelledError:
                    raise

                except Exception:
                    self._logger.exception("Ошибка при отправке сообщения")
                    await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            self._logger.debug("AggregatingLink writer loop cancelled")
            raise

    async def _send_physical_packet(self, packet: bytes) -> None:
        """
        Отправляет ОДИН physical packet в transport, строго соблюдая min_send_interval.

        Args:
            packet: Уже полностью подготовленный transport packet.
        """
        now = time.monotonic()
        wait_more = self._min_send_interval - (now - self._last_send_ts)

        if wait_more > 0:
            self._logger.debug("Waiting {:.3f}s before next physical send", wait_more)
            await asyncio.sleep(wait_more)

        await asyncio.to_thread(self._transport.send, packet)
        self._last_send_ts = time.monotonic()

    def _encode_transport_packets(self, logical_payload: bytes) -> list[bytes]:
        """
        Преобразует один logical payload в один или несколько transport packet.

        Args:
            logical_payload: Результат _encode_batch(...)

        Returns:
            Список physical transport packet.
        """
        if self._transport_limit is None:
            return [logical_payload]

        if len(logical_payload) <= self._single_packet_payload_limit:
            return [self.SINGLE_MAGIC + logical_payload]

        chunk_payload_limit = self._chunk_packet_payload_limit
        assert chunk_payload_limit is not None

        msg_id = uuid.uuid4().bytes
        total_parts = math.ceil(len(logical_payload) / chunk_payload_limit)

        if total_parts > 0xFFFF:
            raise ValueError("logical payload too large: too many chunks")

        packets: list[bytes] = []

        for part_index in range(total_parts):
            start = part_index * chunk_payload_limit
            end = start + chunk_payload_limit
            chunk_data = logical_payload[start:end]

            packet = (
                self.CHUNK_MAGIC
                + self._CHUNK_META_STRUCT.pack(msg_id, part_index, total_parts)
                + chunk_data
            )
            packets.append(packet)

        return packets

    def _decode_transport_packet(self, raw_packet: bytes) -> list[bytes]:
        """
        Декодирует один physical transport packet.

        Returns:
            Список готовых logical payload.
            Обычно это либо:
            - [payload] для SINGLE
            - [] для промежуточного CHUNK
            - [payload] при завершении сборки CHUNK
        """
        if self._transport_limit is None:
            return [raw_packet]

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
                self._logger.debug(
                    "Created chunk assembly: msg_id={}, total_parts={}",
                    msg_id.hex(),
                    total_parts,
                )
            else:
                if assembly.total_parts != total_parts:
                    raise ValueError("chunk total_parts mismatch")

            assembly.parts[part_index] = chunk_data

            self._logger.debug(
                "Received chunk: msg_id={}, part={}/{}, chunk_size={}, received_parts={}",
                msg_id.hex(),
                part_index + 1,
                total_parts,
                len(chunk_data),
                len(assembly.parts),
            )

            if len(assembly.parts) == assembly.total_parts:
                payload = b"".join(
                    assembly.parts[i] for i in range(assembly.total_parts)
                )
                del self._chunk_assemblies[msg_id]

                self._logger.debug(
                    "Chunk assembly complete: msg_id={}, payload_size={}",
                    msg_id.hex(),
                    len(payload),
                )
                return [payload]

            return []

        raise ValueError("unknown transport packet magic")

    def _cleanup_stale_chunk_assemblies(self) -> None:
        """
        Удаляет старые незавершенные chunk assemblies.
        """
        if not self._chunk_assemblies:
            return

        now = time.monotonic()
        stale_ids = [
            msg_id
            for msg_id, assembly in self._chunk_assemblies.items()
            if now - assembly.created_at > self._chunk_assembly_ttl
        ]

        for msg_id in stale_ids:
            self._logger.warning("Dropping stale chunk assembly: msg_id={}", msg_id.hex())
            del self._chunk_assemblies[msg_id]

    @staticmethod
    def _estimate_frame_size(frame: Frame) -> int:
        """
        Грубая оценка размера logical frame.

        Нужна только для batching decision, а не для точного wire-size.
        """
        return 64 + len(frame.stream_id) + len(frame.frame_type) + len(frame.payload)

    @staticmethod
    def _encode_batch(frames: list[Frame]) -> bytes:
        """
        Кодирует список logical frame в один logical payload.

        Args:
            frames: Список frame.

        Returns:
            Msgpack bytes.
        """
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
        """
        Декодирует logical payload обратно в список Frame.

        Args:
            raw: Msgpack payload.

        Returns:
            Список Frame.

        Raises:
            ValueError: Если формат payload некорректен.
        """
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
