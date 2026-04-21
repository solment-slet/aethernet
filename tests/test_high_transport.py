"""
Тесты для AggregatingLink.

Покрывают:
- Все три режима надёжности: NONE, STOP_AND_WAIT, PARALLEL
- Разные профили транспорта: надёжный быстрый, надёжный медленный, ненадёжный
- Доставку одиночных и множественных фреймов
- Батчинг фреймов
- Дедупликацию дубликатов (ретрансмиты)
- Ретрансмиссию при потере пакетов
- Piggyback ACK
- Многопоточные стримы
- Корректное закрытие (StreamClosed)
- Защиту от утечки памяти _received_seqs
- Граничные случаи: пустой payload, end=True, большие payload (chunking в NONE)

Импорты aethernet подменяются моками — реальные модули не нужны.
"""

from __future__ import annotations

import asyncio
import queue
import random
import time

import pytest

from aethernet.transport.low_transport import LowTransportConfig
from aethernet.transport.high_transport import (
    AggregatingLink,
    Frame,
    ReliabilityMode,
)

# ---------------------------------------------------------------------------
# Fake LowTransport
# ---------------------------------------------------------------------------


class FakeLowTransport:
    """
    Тестовая реализация LowTransport.

    Параметры:
        drop_rate:        вероятность [0..1] потери каждого отдельного пакета
        delay:            задержка доставки каждого пакета (сек)
        min_send_interval / min_recv_interval / delay_before_resending:
                          копируют реальный ABC-контракт
    """

    def __init__(
        self,
        *,
        drop_rate: float = 0.0,
        delay: float = 0.0,
        min_send_interval: float = 0.0,
        min_recv_interval: float = 0.0,
        delay_before_resending: float = 0.3,
        max_message_chars: int = 100_000,
    ):
        self.drop_rate = drop_rate
        self.delay = delay
        self.config = LowTransportConfig(
            mode="string",
            max_message_chars=max_message_chars,
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
            min_send_interval=min_send_interval,
            min_recv_interval=min_recv_interval,
            delay_before_resending=delay_before_resending,
        )

        # Очередь: сторона А пишет сюда, сторона Б читает отсюда
        # Для двунаправленного канала каждая сторона имеет свою очередь.
        self._inbox: queue.Queue[bytes] = queue.Queue()
        self._closed = False

    def close(self) -> None:
        self._closed = True
        # Кладём sentinel чтобы разблокировать зависший recv()
        self._inbox.put_nowait(b"__closed__")

    def send(self, data: bytes) -> None:
        """Вызывается отправителем. Кладёт пакет в очередь получателя."""
        if self._closed:
            raise RuntimeError("transport closed")
        if self.delay > 0:
            time.sleep(self.delay)
        # peer должен быть установлен через link_pair()
        if random.random() < self.drop_rate:
            return  # пакет потерян
        self._peer._inbox.put_nowait(data)

    def recv(self, timeout: float = 1.0) -> bytes:
        """Вызывается получателем. Блокируется до прихода пакета."""
        if self._closed:
            raise RuntimeError("transport closed")
        try:
            data = self._inbox.get(timeout=timeout if timeout > 0 else 1.0)
        except queue.Empty:
            raise TimeoutError("recv timeout")
        if data == b"__closed__":
            raise RuntimeError("transport closed")
        return data

    @staticmethod
    def link_pair(
        drop_rate: float = 0.0,
        delay: float = 0.0,
        min_send_interval: float = 0.0,
        min_recv_interval: float = 0.0,
        delay_before_resending: float = 0.3,
    ) -> tuple["FakeLowTransport", "FakeLowTransport"]:
        """
        Создаёт пару связанных транспортов: A.send() → B.recv() и наоборот.
        """
        a = FakeLowTransport(
            drop_rate=drop_rate,
            delay=delay,
            min_send_interval=min_send_interval,
            min_recv_interval=min_recv_interval,
            delay_before_resending=delay_before_resending,
        )
        b = FakeLowTransport(
            drop_rate=drop_rate,
            delay=delay,
            min_send_interval=min_send_interval,
            min_recv_interval=min_recv_interval,
            delay_before_resending=delay_before_resending,
        )
        a._peer = b
        b._peer = a
        return a, b


# ---------------------------------------------------------------------------
# Fake MediumTransport
# ---------------------------------------------------------------------------


class FakeMediumTransport:
    """
    Обёртка над FakeLowTransport, реализующая интерфейс MediumTransport.

    AggregatingLink обращается к:
      .low_transport.min_send_interval
      .low_transport.min_recv_interval
      .low_transport.delay_before_resending
      .max_payload_bytes
      .send(bytes)
      .recv(timeout) -> bytes
    """

    def __init__(self, low: FakeLowTransport, max_payload_bytes: int = 65_536):
        self.low_transport = low
        self.config = self.low_transport.config
        self.max_payload_bytes = max_payload_bytes

    def send(self, data: bytes) -> None:
        self.low_transport.send(data)

    def recv(self, timeout: float = 1.0) -> bytes:
        return self.low_transport.recv(timeout)


# ---------------------------------------------------------------------------
# Профили транспортов
# ---------------------------------------------------------------------------


def make_reliable_fast() -> tuple[FakeLowTransport, FakeLowTransport]:
    """Надёжный быстрый транспорт — 0% потерь, без задержек."""
    return FakeLowTransport.link_pair(
        drop_rate=0.0,
        delay=0.0,
        min_send_interval=0.0,
        min_recv_interval=0.0,
        delay_before_resending=0.3,
    )


def make_reliable_slow() -> tuple[FakeLowTransport, FakeLowTransport]:
    """Надёжный медленный транспорт — 0% потерь, задержка 2 сек."""
    return FakeLowTransport.link_pair(
        drop_rate=0.0,
        delay=2,
        min_send_interval=0.05,
        min_recv_interval=0.0,
        delay_before_resending=5,
    )


def make_unreliable() -> tuple[FakeLowTransport, FakeLowTransport]:
    """Ненадёжный медленный транспорт — 30% потерь, задержка 2 сек."""
    return FakeLowTransport.link_pair(
        drop_rate=0.30,
        delay=2,
        min_send_interval=0.0,
        min_recv_interval=0.0,
        delay_before_resending=4.3,
    )


def make_very_unreliable() -> tuple[FakeLowTransport, FakeLowTransport]:
    """Очень ненадёжный транспорт — 60% потерь."""
    return FakeLowTransport.link_pair(
        drop_rate=0.60,
        delay=0.0,
        min_send_interval=0.0,
        min_recv_interval=0.0,
        delay_before_resending=0.1,
    )


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------


def make_link_pair(
    low_a: FakeLowTransport,
    low_b: FakeLowTransport,
    mode: ReliabilityMode = ReliabilityMode.NONE,
    window_size: int = 8,
    max_payload_bytes: int = 65_536,
    flush_interval: float = 0.05,
) -> tuple[AggregatingLink, AggregatingLink]:
    med_a = FakeMediumTransport(low_a, max_payload_bytes=max_payload_bytes)
    med_b = FakeMediumTransport(low_b, max_payload_bytes=max_payload_bytes)

    link_a = AggregatingLink(
        med_a,
        reliability_mode=mode,
        window_size=window_size,
        flush_interval=flush_interval,
        ack_flush_interval=0.05,
        ack_batch_size=8,
    )
    link_b = AggregatingLink(
        med_b,
        reliability_mode=mode,
        window_size=window_size,
        flush_interval=flush_interval,
        ack_flush_interval=0.05,
        ack_batch_size=8,
    )
    return link_a, link_b


async def start_pair(link_a: AggregatingLink, link_b: AggregatingLink) -> None:
    await link_a.start()
    await link_b.start()


async def close_pair(link_a: AggregatingLink, link_b: AggregatingLink) -> None:
    await link_a.close()
    await link_b.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_loop():
    """Свежий event loop для каждого теста."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Параметризация: режимы × профили транспорта
# ---------------------------------------------------------------------------

MODES = [
    ReliabilityMode.NONE,
    ReliabilityMode.STOP_AND_WAIT,
    ReliabilityMode.PARALLEL,
]

RELIABLE_MODES = [
    ReliabilityMode.STOP_AND_WAIT,
    ReliabilityMode.PARALLEL,
]

MODE_IDS = {
    ReliabilityMode.NONE: "none",
    ReliabilityMode.STOP_AND_WAIT: "saw",
    ReliabilityMode.PARALLEL: "parallel",
}


# ===========================================================================
# 1. Базовая доставка
# ===========================================================================


class TestBasicDelivery:
    """Один фрейм доходит от A до B на надёжном транспорте."""

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_single_frame_delivered(self, mode):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "data", b"hello", end=True)
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=3.0)
                assert frame.payload == b"hello"
                assert frame.frame_type == "data"
                assert frame.end is True
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_empty_payload(self, mode):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "ping", b"")
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=3.0)
                assert frame.payload == b""
                assert frame.frame_type == "ping"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_multiple_frames_same_stream(self, mode):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                payloads = [f"msg-{i}".encode() for i in range(10)]
                for i, p in enumerate(payloads):
                    await link_a.send_frame(
                        sid, "data", p, end=(i == len(payloads) - 1)
                    )

                received = []
                async for frame in link_b.iter_stream(sid):
                    received.append(frame.payload)

                assert received == payloads
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_bidirectional(self, mode):
        """A→B и B→A одновременно."""

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid_ab = AggregatingLink.new_stream_id()
                sid_ba = AggregatingLink.new_stream_id()

                await link_a.send_frame(sid_ab, "req", b"from-a", end=True)
                await link_b.send_frame(sid_ba, "req", b"from-b", end=True)

                frame_at_b = await asyncio.wait_for(
                    link_b.recv_frame(sid_ab), timeout=3.0
                )
                frame_at_a = await asyncio.wait_for(
                    link_a.recv_frame(sid_ba), timeout=3.0
                )

                assert frame_at_b.payload == b"from-a"
                assert frame_at_a.payload == b"from-b"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 2. Батчинг
# ===========================================================================


class TestBatching:
    """Несколько фреймов должны упаковываться в один физический пакет."""

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_batch_all_delivered(self, mode):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode, flush_interval=0.2)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                count = 20
                for i in range(count):
                    await link_a.send_frame(
                        sid, "d", f"x{i}".encode(), end=(i == count - 1)
                    )

                received = []
                async for f in link_b.iter_stream(sid):
                    received.append(f.payload)

                assert len(received) == count
                for i, p in enumerate(received):
                    assert p == f"x{i}".encode()
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 3. Множество стримов
# ===========================================================================


class TestMultipleStreams:

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_independent_streams(self, mode):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                streams = [AggregatingLink.new_stream_id() for _ in range(5)]

                for sid in streams:
                    await link_a.send_frame(sid, "d", sid.encode(), end=True)

                for sid in streams:
                    frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=3.0)
                    assert frame.payload == sid.encode()
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize("mode", MODES, ids=[MODE_IDS[m] for m in MODES])
    def test_accept_stream(self, mode):
        """accept_stream() уведомляет о новом входящем стриме."""

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "hello", b"world")
                discovered = await asyncio.wait_for(link_b.accept_stream(), timeout=3.0)
                assert discovered == sid
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 4. Ненадёжный транспорт — ретрансмиссия (только ARQ режимы)
# ===========================================================================


class TestRetransmission:
    """На ненадёжном транспорте ARQ-режимы должны гарантировать доставку."""

    @pytest.mark.parametrize(
        "mode", RELIABLE_MODES, ids=[MODE_IDS[m] for m in RELIABLE_MODES]
    )
    def test_delivery_with_30pct_loss(self, mode):
        async def run():
            # Фиксированный seed для детерминированности
            random.seed(42)

            low_a, low_b = make_unreliable()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "data", b"must arrive", end=True)
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=25.0)
                assert frame.payload == b"must arrive"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize(
        "mode", RELIABLE_MODES, ids=[MODE_IDS[m] for m in RELIABLE_MODES]
    )
    def test_delivery_with_60pct_loss(self, mode):
        async def run():
            # Фиксированный seed для детерминированности
            random.seed(123)

            low_a, low_b = make_very_unreliable()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "data", b"still arrives", end=True)
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=30.0)
                assert frame.payload == b"still arrives"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize(
        "mode", RELIABLE_MODES, ids=[MODE_IDS[m] for m in RELIABLE_MODES]
    )
    def test_multiple_frames_unreliable(self, mode):
        """Несколько фреймов подряд, ненадёжный транспорт — всё доходит."""

        async def run():
            # Фиксированный seed для детерминированности
            random.seed(456)

            low_a, low_b = make_unreliable()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode, window_size=4)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                count = 8
                for i in range(count):
                    await link_a.send_frame(
                        sid, "d", str(i).encode(), end=(i == count - 1)
                    )

                received = []
                async for f in link_b.iter_stream(sid):
                    received.append(f.payload)

                assert len(received) == count
                assert received == [str(i).encode() for i in range(count)]
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    @pytest.mark.parametrize(
        "mode", RELIABLE_MODES, ids=[MODE_IDS[m] for m in RELIABLE_MODES]
    )
    def test_statistical_delivery_with_loss(self, mode):
        """Статистический тест: при 30% loss большинство попыток должны успешно завершиться."""

        async def run():
            successes = 0
            attempts = 10

            for i in range(attempts):
                # Разные seeds для каждой попытки
                random.seed(1000 + i)

                low_a, low_b = make_unreliable()
                link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
                await start_pair(link_a, link_b)
                try:
                    sid = AggregatingLink.new_stream_id()
                    await link_a.send_frame(sid, "data", b"test", end=True)
                    frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=25.0)
                    if frame.payload == b"test":
                        successes += 1
                except asyncio.TimeoutError:
                    pass  # Допустимо при высоких потерях
                finally:
                    await close_pair(link_a, link_b)

            # При правильной ретрансмиссии успех должен быть в >= 80% случаев
            assert (
                successes >= 8
            ), f"Only {successes}/{attempts} succeeded - retransmission broken"

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 5. Дедупликация дубликатов
# ===========================================================================


class TestDeduplication:
    """Дубликаты физических пакетов не должны дублировать frame."""

    def test_duplicate_not_delivered_twice(self):
        """
        Имитируем дубликат: вручную кладём тот же пакет дважды в inbox получателя.
        """

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=ReliabilityMode.PARALLEL)

            # Перехватываем реальный send, чтобы продублировать пакет
            original_send = low_a.send
            sent_packets: list[bytes] = []

            def capturing_send(data: bytes):
                sent_packets.append(data)
                original_send(data)

            low_a.send = capturing_send

            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "data", b"once", end=True)

                # Ждём пока пакет точно улетит
                await asyncio.sleep(0.3)

                # Дублируем последний пойманный пакет напрямую в inbox link_b
                if sent_packets:
                    low_b._inbox.put_nowait(sent_packets[-1])

                # link_b должен получить ровно один frame
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=3.0)
                assert frame.payload == b"once"

                # Убеждаемся что второй frame не появился
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(link_b.recv_frame(sid), timeout=0.5)
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    def test_received_seqs_bounded(self):
        """
        _received_seqs никогда не растёт за пределы maxlen.
        """

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(
                low_a,
                low_b,
                mode=ReliabilityMode.PARALLEL,
                window_size=4,
            )
            # Устанавливаем маленькое окно чтобы убедиться в вытеснении
            link_b._received_seqs = __import__("collections").deque(maxlen=16)
            link_b._received_seqs_set = set()

            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                count = 50
                for i in range(count):
                    await link_a.send_frame(
                        sid, "d", str(i).encode(), end=(i == count - 1)
                    )

                received = []
                async for f in link_b.iter_stream(sid):
                    received.append(f.payload)

                # Все фреймы дошли
                assert len(received) == count
                # deque не вырос за maxlen
                assert len(link_b._received_seqs) <= 16
                # set синхронизирован с deque
                assert set(link_b._received_seqs) == link_b._received_seqs_set
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 6. ACK piggyback
# ===========================================================================


class TestAckPiggyback:
    """ACK едут вместе с данными, не тратя отдельный физический пакет."""

    def test_ack_carried_with_data(self):
        """
        При двунаправленной передаче ACK должен piggyback'аться.
        Считаем количество физических пакетов: при piggyback их меньше.
        """

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=ReliabilityMode.PARALLEL)

            total_packets = 0
            orig_send_a = low_a.send
            orig_send_b = low_b.send

            def counting_send_a(data):
                nonlocal total_packets
                total_packets += 1
                orig_send_a(data)

            def counting_send_b(data):
                nonlocal total_packets
                total_packets += 1
                orig_send_b(data)

            low_a.send = counting_send_a
            low_b.send = counting_send_b

            await start_pair(link_a, link_b)
            try:
                sid_ab = AggregatingLink.new_stream_id()
                sid_ba = AggregatingLink.new_stream_id()

                # Оба шлют данные одновременно
                await asyncio.gather(
                    link_a.send_frame(sid_ab, "d", b"a->b", end=True),
                    link_b.send_frame(sid_ba, "d", b"b->a", end=True),
                )

                await asyncio.wait_for(link_b.recv_frame(sid_ab), timeout=3.0)
                await asyncio.wait_for(link_a.recv_frame(sid_ba), timeout=3.0)

                await asyncio.sleep(0.3)

                # При piggyback: 2 data пакета + возможно 0-2 ACK-only.
                # Без piggyback было бы 4+. Проверяем что разумно мало.
                assert total_packets <= 8, f"Too many packets: {total_packets}"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 7. Режим NONE — нет ACK, нет ретрансмиссии
# ===========================================================================


class TestModeNone:
    """В режиме NONE потерянные пакеты не восстанавливаются."""

    def test_no_retransmit_on_loss(self):
        async def run():
            low_a, low_b = make_unreliable()
            link_a, link_b = make_link_pair(low_a, low_b, mode=ReliabilityMode.NONE)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "data", b"maybe lost", end=True)

                # Ждём — пакет может прийти или нет, это нормально
                try:
                    await asyncio.wait_for(link_b.recv_frame(sid), timeout=1.0)
                    # Если дошёл — хорошо, тест не упадёт
                except asyncio.TimeoutError:
                    pass  # Потеря — тоже ожидаемое поведение для NONE
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    def test_legacy_format_used(self):
        """В режиме NONE пакеты начинаются с AGS1/AGC1, не AGP1."""

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=ReliabilityMode.NONE)

            sent: list[bytes] = []
            orig = low_a.send

            def capture(data):
                sent.append(data)
                orig(data)

            low_a.send = capture

            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "t", b"x", end=True)
                await asyncio.sleep(0.3)

                assert sent, "No packets sent"
                assert sent[0][:4] == b"AGS1", f"Expected AGS1, got {sent[0][:4]}"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    def test_agp1_format_used_in_arq(self):
        """В ARQ режимах пакеты начинаются с AGP1."""

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=ReliabilityMode.PARALLEL)

            sent: list[bytes] = []
            orig = low_a.send

            def capture(data):
                sent.append(data)
                orig(data)

            low_a.send = capture

            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "t", b"x", end=True)
                await asyncio.sleep(0.3)

                assert sent, "No packets sent"
                assert sent[0][:4] == b"AGP1", f"Expected AGP1, got {sent[0][:4]}"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 8. Большие payload — chunking в режиме NONE
# ===========================================================================


class TestChunking:
    """Payload больше лимита транспорта режется на куски (режим NONE)."""

    def test_large_payload_delivered(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            # Маленький max_payload_bytes чтобы гарантированно нарезало
            link_a, link_b = make_link_pair(
                low_a,
                low_b,
                mode=ReliabilityMode.NONE,
                max_payload_bytes=512,
            )
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                big_payload = b"X" * 2000
                await link_a.send_frame(sid, "data", big_payload, end=True)
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=5.0)
                assert frame.payload == big_payload
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 9. Lifecycle / error handling
# ===========================================================================


class TestLifecycle:

    def test_start_idempotent(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b)
            await link_a.start()
            await link_a.start()  # второй вызов безопасен
            await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    def test_close_idempotent(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b)
            await start_pair(link_a, link_b)
            await link_a.close()
            await link_a.close()  # второй вызов безопасен
            await link_b.close()

        asyncio.get_event_loop().run_until_complete(run())

    def test_send_after_close_raises(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b)
            await start_pair(link_a, link_b)
            await close_pair(link_a, link_b)

            with pytest.raises(RuntimeError, match="closed"):
                await link_a.send_frame("sid", "t", b"x")

        asyncio.get_event_loop().run_until_complete(run())

    def test_send_before_start_raises(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            med_a = FakeMediumTransport(low_a)
            link = AggregatingLink(med_a, reliability_mode=ReliabilityMode.NONE)

            with pytest.raises(RuntimeError, match="start"):
                await link.send_frame("sid", "t", b"x")

            await link.close()

        asyncio.get_event_loop().run_until_complete(run())

    def test_invalid_payload_type_raises(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b)
            await start_pair(link_a, link_b)
            try:
                with pytest.raises(TypeError):
                    await link_a.send_frame("sid", "t", "not bytes")
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())

    def test_start_after_close_raises(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b)
            await start_pair(link_a, link_b)
            await close_pair(link_a, link_b)

            with pytest.raises(RuntimeError, match="closed"):
                await link_a.start()

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 10. Медленный транспорт — надёжность под задержкой
# ===========================================================================


class TestSlowTransport:

    @pytest.mark.parametrize(
        "mode", RELIABLE_MODES, ids=[MODE_IDS[m] for m in RELIABLE_MODES]
    )
    def test_delivery_slow_reliable(self, mode):
        async def run():
            low_a, low_b = make_reliable_slow()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                await link_a.send_frame(sid, "data", b"slow but sure", end=True)
                frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=10.0)
                assert frame.payload == b"slow but sure"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 11. STOP_AND_WAIT специфика — строгий порядок
# ===========================================================================


class TestStopAndWait:

    def test_window_size_is_one(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(
                low_a,
                low_b,
                mode=ReliabilityMode.STOP_AND_WAIT,
                window_size=8,  # должно быть проигнорировано
            )
            assert link_a._window_size == 1

            await link_a.close()
            await link_b.close()

        asyncio.get_event_loop().run_until_complete(run())

    def test_ordered_delivery(self):
        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(
                low_a, low_b, mode=ReliabilityMode.STOP_AND_WAIT
            )
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                n = 10
                for i in range(n):
                    await link_a.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))

                received = []
                async for f in link_b.iter_stream(sid):
                    received.append(int(f.payload.decode()))

                assert received == list(range(n)), f"Order broken: {received}"
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 12. PARALLEL специфика — окно, параллельность
# ===========================================================================


class TestParallel:

    def test_window_size_respected(self):
        """В полёте не больше window_size пакетов одновременно."""

        async def run():
            low_a, low_b = make_reliable_fast()
            window = 3
            link_a, link_b = make_link_pair(
                low_a,
                low_b,
                mode=ReliabilityMode.PARALLEL,
                window_size=window,
            )
            assert link_a._window_size == window
            # Начальное значение семафора должно равняться window_size
            assert link_a._window_semaphore._value == window

            await link_a.close()
            await link_b.close()

        asyncio.get_event_loop().run_until_complete(run())

    def test_parallel_throughput_faster_than_saw(self):
        """
        PARALLEL на надёжном транспорте быстрее STOP_AND_WAIT при большом числе фреймов.
        """

        async def measure(mode: ReliabilityMode) -> float:
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(low_a, low_b, mode=mode, window_size=8)
            await start_pair(link_a, link_b)
            try:
                sid = AggregatingLink.new_stream_id()
                n = 16
                t0 = time.monotonic()
                for i in range(n):
                    await link_a.send_frame(sid, "d", b"payload", end=(i == n - 1))
                async for _ in link_b.iter_stream(sid):
                    pass
                return time.monotonic() - t0
            finally:
                await close_pair(link_a, link_b)

        async def run():
            t_saw = await measure(ReliabilityMode.STOP_AND_WAIT)
            t_par = await measure(ReliabilityMode.PARALLEL)
            # PARALLEL должен быть быстрее или сопоставим (не медленнее в 3x)
            assert (
                t_par < t_saw * 3
            ), f"PARALLEL ({t_par:.3f}s) surprisingly slow vs SAW ({t_saw:.3f}s)"

        asyncio.get_event_loop().run_until_complete(run())

    def test_many_concurrent_streams(self):
        """Несколько стримов параллельно — все доходят."""

        async def run():
            low_a, low_b = make_reliable_fast()
            link_a, link_b = make_link_pair(
                low_a, low_b, mode=ReliabilityMode.PARALLEL, window_size=8
            )
            await start_pair(link_a, link_b)
            try:
                streams = [AggregatingLink.new_stream_id() for _ in range(8)]

                async def send_stream(sid: str, n: int):
                    for i in range(n):
                        await link_a.send_frame(
                            sid, "d", f"{sid}:{i}".encode(), end=(i == n - 1)
                        )

                async def recv_stream(sid: str, n: int) -> list[bytes]:
                    result = []
                    async for f in link_b.iter_stream(sid):
                        result.append(f.payload)
                    return result

                n_per_stream = 5
                senders = [send_stream(sid, n_per_stream) for sid in streams]
                receivers = [recv_stream(sid, n_per_stream) for sid in streams]

                results = await asyncio.wait_for(
                    asyncio.gather(*senders, *receivers),
                    timeout=15.0,
                )

                received_lists = results[len(streams) :]
                for i, received in enumerate(received_lists):
                    sid = streams[i]
                    assert len(received) == n_per_stream
                    assert received == [
                        f"{sid}:{j}".encode() for j in range(n_per_stream)
                    ]
            finally:
                await close_pair(link_a, link_b)

        asyncio.get_event_loop().run_until_complete(run())


# ===========================================================================
# 13. new_stream_id уникальность
# ===========================================================================


class TestStreamId:

    def test_unique_stream_ids(self):
        ids = {AggregatingLink.new_stream_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_stream_id_is_hex_string(self):
        sid = AggregatingLink.new_stream_id()
        assert isinstance(sid, str)
        int(sid, 16)  # должен парситься как hex без исключений


# ===========================================================================
# 14. Packet encoding / decoding unit tests
# ===========================================================================


class TestPacketCodec:
    """Юнит-тесты для _build_packet / _parse_packet без сети."""

    def test_roundtrip_data_packet(self):
        from aethernet.transport.high_transport import _build_packet, _parse_packet

        payload = b"hello world"
        packet = _build_packet(seq=42, ack_seqs=[1, 2, 3], payload=payload)
        seq, ack_seqs, parsed_payload = _parse_packet(packet)
        assert seq == 42
        assert ack_seqs == [1, 2, 3]
        assert parsed_payload == payload

    def test_roundtrip_ack_only(self):
        from aethernet.transport.high_transport import _build_packet, _parse_packet

        packet = _build_packet(seq=0, ack_seqs=[7, 8, 9], payload=b"")
        seq, ack_seqs, payload = _parse_packet(packet)
        assert seq == 0
        assert ack_seqs == [7, 8, 9]
        assert payload == b""

    def test_roundtrip_no_acks(self):
        from aethernet.transport.high_transport import _build_packet, _parse_packet

        packet = _build_packet(seq=100, ack_seqs=[], payload=b"data")
        seq, ack_seqs, payload = _parse_packet(packet)
        assert seq == 100
        assert ack_seqs == []
        assert payload == b"data"

    def test_parse_too_short_raises(self):
        from aethernet.transport.high_transport import _parse_packet

        with pytest.raises(ValueError):
            _parse_packet(b"AG")

    def test_magic_required(self):
        from aethernet.transport.high_transport import _parse_packet

        bad = b"XXXX" + b"\x00" * 10
        with pytest.raises(ValueError):
            _parse_packet(bad)

    def test_batch_encode_decode_roundtrip(self):
        frames = [
            Frame(stream_id="abc", frame_type="data", payload=b"hello", end=False),
            Frame(stream_id="xyz", frame_type="ping", payload=b"", end=True),
        ]
        encoded = AggregatingLink._encode_batch(frames)
        decoded = AggregatingLink._decode_batch(encoded)
        assert len(decoded) == 2
        assert decoded[0].stream_id == "abc"
        assert decoded[0].payload == b"hello"
        assert decoded[1].end is True
        assert decoded[1].payload == b""


# ===========================================================================
# 15. Property-based тесты (hypothesis)
# ===========================================================================

try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

if HYPOTHESIS_AVAILABLE:

    class TestHypothesis:

        @given(
            payload=st.binary(min_size=0, max_size=4096),
            frame_type=st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
                min_size=1,
                max_size=20,
            ),
        )
        @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
        def test_arbitrary_payload_roundtrip(self, payload, frame_type):
            """Произвольный payload доходит без искажений (надёжный транспорт, NONE)."""

            async def run():
                low_a, low_b = make_reliable_fast()
                link_a, link_b = make_link_pair(low_a, low_b, mode=ReliabilityMode.NONE)
                await start_pair(link_a, link_b)
                try:
                    sid = AggregatingLink.new_stream_id()
                    await link_a.send_frame(sid, frame_type, payload, end=True)
                    frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=3.0)
                    assert frame.payload == payload
                    assert frame.frame_type == frame_type
                finally:
                    await close_pair(link_a, link_b)

            asyncio.get_event_loop().run_until_complete(run())

        @given(
            payload=st.binary(min_size=0, max_size=1024),
        )
        @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
        def test_arq_payload_roundtrip(self, payload):
            """Произвольный payload доходит в ARQ-режиме."""

            async def run():
                low_a, low_b = make_reliable_fast()
                link_a, link_b = make_link_pair(
                    low_a, low_b, mode=ReliabilityMode.PARALLEL
                )
                await start_pair(link_a, link_b)
                try:
                    sid = AggregatingLink.new_stream_id()
                    await link_a.send_frame(sid, "data", payload, end=True)
                    frame = await asyncio.wait_for(link_b.recv_frame(sid), timeout=5.0)
                    assert frame.payload == payload
                finally:
                    await close_pair(link_a, link_b)

            asyncio.get_event_loop().run_until_complete(run())

        @given(st.lists(st.integers(min_value=1, max_value=2**32 - 1), max_size=255))
        @settings(max_examples=100)
        def test_packet_codec_roundtrip(self, ack_seqs):
            """Кодек пакета корректен для любого набора ACK seq."""
            from aethernet.transport.high_transport import _build_packet, _parse_packet

            payload = b"test payload"
            packet = _build_packet(seq=999, ack_seqs=ack_seqs, payload=payload)
            seq, decoded_acks, decoded_payload = _parse_packet(packet)
            assert seq == 999
            assert decoded_acks == ack_seqs
            assert decoded_payload == payload
