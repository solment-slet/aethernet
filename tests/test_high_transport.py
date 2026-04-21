"""
Тесты для AggregatingLink.

Запуск:
    pytest test_aggregating_link.py -v --timeout=30

Что покрыто:
    - Все три режима надёжности: NONE, STOP_AND_WAIT, PARALLEL
    - Профили транспорта: быстрый надёжный, медленный надёжный, 30% потерь, 60% потерь
    - Одиночные и множественные фреймы, батчинг
    - Строгий порядок между батчами в PARALLEL (reorder buffer)
    - Ретрансмиссия при потере пакетов (ARQ режимы)
    - Дедупликация дубликатов, bounded _received_seqs
    - Piggyback ACK
    - Множественные стримы, accept_stream
    - Большие payload (chunking, режим NONE)
    - Lifecycle: idempotent start/close, ошибки до start и после close
    - Packet codec: roundtrip, граничные случаи, невалидный magic
    - Batch encode/decode roundtrip
    - Property-based тесты через hypothesis

Импорты:
    Тесты используют реальные импорты из пакета aethernet.
    Никаких sys.modules подмен нет.
    Убедитесь что пакет aethernet доступен в PYTHONPATH.
"""

from __future__ import annotations

import asyncio
import queue
import random
import time

import pytest

from aethernet.transport.enums import ReliabilityMode
from aethernet.transport.high_transport import (
    AggregatingLink,
    Frame,
    _build_packet,
    _parse_packet,
)

# ---------------------------------------------------------------------------
# FakeLowTransport
# ---------------------------------------------------------------------------


class FakeLowTransport:
    """
    Тестовый транспорт с настраиваемыми потерями и задержкой.

    Параметры:
        drop_rate:  вероятность [0..1] потери каждого пакета
        delay:      задержка доставки (сек)
    """

    def __init__(
        self,
        *,
        drop_rate: float = 0.0,
        delay: float = 0.0,
        min_send_interval: float = 0.0,
        min_recv_interval: float = 0.0,
        delay_before_resending: float = 0.3,
    ):
        self.drop_rate = drop_rate
        self.delay = delay
        self.min_send_interval = min_send_interval
        self.min_recv_interval = min_recv_interval
        self.delay_before_resending = delay_before_resending

        self._inbox: queue.Queue[bytes] = queue.Queue()
        self._peer: FakeLowTransport | None = None
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._inbox.put_nowait(b"__sentinel__")

    def send(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("transport closed")
        if self.delay > 0:
            time.sleep(self.delay)
        if random.random() < self.drop_rate:
            return  # пакет потерян
        assert self._peer is not None
        self._peer._inbox.put_nowait(data)

    def recv(self, timeout: float = 1.0) -> bytes:
        if self._closed:
            raise RuntimeError("transport closed")
        try:
            data = self._inbox.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            raise TimeoutError("recv timeout")
        if data == b"__sentinel__":
            raise RuntimeError("transport closed")
        return data

    @staticmethod
    def linked_pair(
        drop_rate: float = 0.0,
        delay: float = 0.0,
        min_send_interval: float = 0.0,
        min_recv_interval: float = 0.0,
        delay_before_resending: float = 0.3,
    ) -> tuple[FakeLowTransport, FakeLowTransport]:
        """Создаёт пару связанных транспортов: a.send() → b.recv() и наоборот."""
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
# FakeMediumTransport — обёртка которую видит AggregatingLink
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, low: FakeLowTransport):
        self.min_send_interval = low.min_send_interval
        self.min_recv_interval = low.min_recv_interval
        self.delay_before_resending = low.delay_before_resending


class FakeMediumTransport:
    """
    Минимальная реализация интерфейса MediumTransport для тестов.

    AggregatingLink обращается к:
        .config.min_send_interval
        .config.min_recv_interval
        .config.delay_before_resending
        .max_payload_bytes
        .low_transport.close()
        .send(bytes)
        .recv(timeout) -> bytes
    """

    def __init__(self, low: FakeLowTransport, max_payload_bytes: int = 65_536):
        self.low_transport = low
        self.config = _FakeConfig(low)
        self.max_payload_bytes = max_payload_bytes

    def send(self, data: bytes) -> None:
        self.low_transport.send(data)

    def recv(self, timeout: float = 1.0) -> bytes:
        return self.low_transport.recv(timeout)


# ---------------------------------------------------------------------------
# Профили транспортов
# ---------------------------------------------------------------------------


def reliable_fast() -> tuple[FakeLowTransport, FakeLowTransport]:
    return FakeLowTransport.linked_pair(
        drop_rate=0.0, delay=0.0, delay_before_resending=0.3
    )


def reliable_slow() -> tuple[FakeLowTransport, FakeLowTransport]:
    return FakeLowTransport.linked_pair(
        drop_rate=0.0, delay=0.05, min_send_interval=0.05, delay_before_resending=0.5
    )


def unreliable_30() -> tuple[FakeLowTransport, FakeLowTransport]:
    return FakeLowTransport.linked_pair(
        drop_rate=0.30, delay=0.0, delay_before_resending=0.15
    )


def unreliable_60() -> tuple[FakeLowTransport, FakeLowTransport]:
    return FakeLowTransport.linked_pair(
        drop_rate=0.60, delay=0.0, delay_before_resending=0.1
    )


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------


def make_pair(
    low_a: FakeLowTransport,
    low_b: FakeLowTransport,
    mode: ReliabilityMode = ReliabilityMode.NONE,
    window_size: int = 8,
    max_payload_bytes: int = 65_536,
    flush_interval: float = 0.05,
) -> tuple[AggregatingLink, AggregatingLink]:
    med_a = FakeMediumTransport(low_a, max_payload_bytes=max_payload_bytes)
    med_b = FakeMediumTransport(low_b, max_payload_bytes=max_payload_bytes)
    kw = dict(
        reliability_mode=mode,
        window_size=window_size,
        flush_interval=flush_interval,
        ack_flush_interval=0.05,
        ack_batch_size=8,
    )
    return AggregatingLink(med_a, **kw), AggregatingLink(med_b, **kw)


def run(coro) -> None:
    """Запускает корутину в свежем event loop."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


async def start_and_close(link_a, link_b, coro):
    """Стартует пару, выполняет coro, закрывает пару."""
    await link_a.start()
    await link_b.start()
    try:
        await coro
    finally:
        await link_a.close()
        await link_b.close()


# ---------------------------------------------------------------------------
# Параметризация
# ---------------------------------------------------------------------------

ALL_MODES = [
    ReliabilityMode.NONE,
    ReliabilityMode.STOP_AND_WAIT,
    ReliabilityMode.PARALLEL,
]
ARQ_MODES = [ReliabilityMode.STOP_AND_WAIT, ReliabilityMode.PARALLEL]
MODE_IDS = {
    ReliabilityMode.NONE: "none",
    ReliabilityMode.STOP_AND_WAIT: "saw",
    ReliabilityMode.PARALLEL: "parallel",
}


# ===========================================================================
# 1. Базовая доставка
# ===========================================================================


class TestBasicDelivery:

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_single_frame(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "data", b"hello", end=True)
                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=3.0)
                assert frame.payload == b"hello"
                assert frame.frame_type == "data"
                assert frame.end is True

            await start_and_close(la, lb, work())

        run(body())

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_empty_payload(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "ping", b"")
                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=3.0)
                assert frame.payload == b""
                assert frame.frame_type == "ping"

            await start_and_close(la, lb, work())

        run(body())

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_multiple_frames_ordered(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                n = 10
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(f.payload)
                assert received == [str(i).encode() for i in range(n)]

            await start_and_close(la, lb, work())

        run(body())

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_bidirectional(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode)

            async def work():
                sid_ab = AggregatingLink.new_stream_id()
                sid_ba = AggregatingLink.new_stream_id()
                await la.send_frame(sid_ab, "req", b"from-a", end=True)
                await lb.send_frame(sid_ba, "req", b"from-b", end=True)
                f_at_b = await asyncio.wait_for(lb.recv_frame(sid_ab), timeout=3.0)
                f_at_a = await asyncio.wait_for(la.recv_frame(sid_ba), timeout=3.0)
                assert f_at_b.payload == b"from-a"
                assert f_at_a.payload == b"from-b"

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 2. Порядок между батчами (reorder buffer) — ключевой тест
# ===========================================================================


class TestReorderBuffer:
    """
    Проверяет что PARALLEL сохраняет строгий порядок батчей даже при потерях.

    Сценарий: искусственно задерживаем пакеты в inbox получателя так чтобы
    они пришли не по порядку. Ожидаем что reorder buffer восстановит порядок.
    """

    def test_out_of_order_packets_reordered(self):
        """
        Вручную кладём пакеты в inbox получателя в обратном порядке.
        Reorder buffer должен выдать frame в исходном порядке.
        """

        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(
                low_a, low_b, mode=ReliabilityMode.PARALLEL, window_size=8
            )

            # Перехватываем send чтобы собрать пакеты не отправляя их
            captured: list[bytes] = []

            def capturing_send(data: bytes):
                captured.append(data)
                # Не отправляем — перехватываем

            low_a.send = capturing_send

            await la.start()
            await lb.start()
            try:
                sid = AggregatingLink.new_stream_id()

                # Отправляем 3 фрейма — каждый в отдельном батче (flush_interval=0)
                for i in range(3):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == 2))
                    await asyncio.sleep(0.1)  # даём writer создать отдельный батч

                # Теперь у нас captured = [pkt_seq1, pkt_seq2, pkt_seq3]
                # Кладём в inbox lb в обратном порядке: seq3, seq2, seq1
                assert len(captured) >= 3, f"Expected 3 packets, got {len(captured)}"
                for pkt in reversed(captured[:3]):
                    low_b._inbox.put_nowait(pkt)

                # Ожидаем фреймы — должны прийти в исходном порядке
                received = []
                for _ in range(3):
                    frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=5.0)
                    received.append(frame.payload)

                assert received == [b"0", b"1", b"2"], f"Order broken: {received}"
            finally:
                await la.close()
                await lb.close()

        run(body())

    def test_parallel_ordered_on_reliable(self):
        """PARALLEL на надёжном транспорте — порядок строгий."""

        async def body():
            la, lb = make_pair(*reliable_fast(), mode=ReliabilityMode.PARALLEL)

            async def work():
                sid = AggregatingLink.new_stream_id()
                n = 20
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(int(f.payload.decode()))
                assert received == list(range(n)), f"Order broken: {received}"

            await start_and_close(la, lb, work())

        run(body())

    def test_parallel_ordered_on_unreliable(self):
        """PARALLEL на ненадёжном транспорте — порядок строгий несмотря на ретрансмиты."""

        async def body():
            la, lb = make_pair(
                *unreliable_30(), mode=ReliabilityMode.PARALLEL, window_size=4
            )

            async def work():
                sid = AggregatingLink.new_stream_id()
                n = 8
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(int(f.payload.decode()))
                assert received == list(range(n)), f"Order broken: {received}"

            await start_and_close(la, lb, work())

        run(body())

    def test_reorder_buffer_empty_after_delivery(self):
        """После доставки всех фреймов reorder buffer пуст и next_expected_seq > 1."""

        async def body():
            low_a, low_b = reliable_fast()
            # flush_interval=0 чтобы каждый фрейм шёл отдельным физическим пакетом
            la, lb = make_pair(
                low_a, low_b, mode=ReliabilityMode.PARALLEL, flush_interval=0.0
            )
            await la.start()
            await lb.start()
            try:
                sid = AggregatingLink.new_stream_id()
                n = 5
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(f.payload)

                assert received == [str(i).encode() for i in range(n)]
                # Reorder buffer пуст — всё флашнуто
                assert lb._reorder_buffer == {}
                # next_expected_seq продвинулся за 1
                assert lb._next_expected_seq > 1
            finally:
                await la.close()
                await lb.close()

        run(body())


# ===========================================================================
# 3. Батчинг
# ===========================================================================


class TestBatching:

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_all_frames_delivered(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode, flush_interval=0.2)

            async def work():
                sid = AggregatingLink.new_stream_id()
                n = 20
                for i in range(n):
                    await la.send_frame(sid, "d", f"x{i}".encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(f.payload)
                assert len(received) == n
                assert received == [f"x{i}".encode() for i in range(n)]

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 4. Множественные стримы
# ===========================================================================


class TestMultipleStreams:

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_independent_streams(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode)

            async def work():
                streams = [AggregatingLink.new_stream_id() for _ in range(5)]
                for sid in streams:
                    await la.send_frame(sid, "d", sid.encode(), end=True)
                for sid in streams:
                    frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=3.0)
                    assert frame.payload == sid.encode()

            await start_and_close(la, lb, work())

        run(body())

    @pytest.mark.parametrize("mode", ALL_MODES, ids=[MODE_IDS[m] for m in ALL_MODES])
    def test_accept_stream(self, mode):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "hello", b"world")
                discovered = await asyncio.wait_for(lb.accept_stream(), timeout=3.0)
                assert discovered == sid

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 5. Ретрансмиссия на ненадёжном транспорте (ARQ режимы)
# ===========================================================================


class TestRetransmission:

    @pytest.mark.parametrize("mode", ARQ_MODES, ids=[MODE_IDS[m] for m in ARQ_MODES])
    def test_delivery_30pct_loss(self, mode):
        async def body():
            la, lb = make_pair(*unreliable_30(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "data", b"must arrive", end=True)
                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=10.0)
                assert frame.payload == b"must arrive"

            await start_and_close(la, lb, work())

        run(body())

    @pytest.mark.parametrize("mode", ARQ_MODES, ids=[MODE_IDS[m] for m in ARQ_MODES])
    def test_delivery_60pct_loss(self, mode):
        async def body():
            la, lb = make_pair(*unreliable_60(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "data", b"still arrives", end=True)
                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=15.0)
                assert frame.payload == b"still arrives"

            await start_and_close(la, lb, work())

        run(body())

    @pytest.mark.parametrize("mode", ARQ_MODES, ids=[MODE_IDS[m] for m in ARQ_MODES])
    def test_multiple_frames_unreliable(self, mode):
        async def body():
            la, lb = make_pair(*unreliable_30(), mode=mode, window_size=4)

            async def work():
                sid = AggregatingLink.new_stream_id()
                n = 6
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(f.payload)
                assert len(received) == n
                assert received == [str(i).encode() for i in range(n)]

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 6. Дедупликация и bounded received_seqs
# ===========================================================================


class TestDeduplication:

    def test_duplicate_not_delivered_twice(self):
        """Дубликат физического пакета не порождает дубликат frame."""

        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(low_a, low_b, mode=ReliabilityMode.PARALLEL)

            sent: list[bytes] = []
            orig = low_a.send

            def capture(data):
                sent.append(data)
                orig(data)

            low_a.send = capture

            await la.start()
            await lb.start()
            try:
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "data", b"once", end=True)
                await asyncio.sleep(0.3)

                # Дублируем последний отправленный пакет напрямую
                if sent:
                    low_b._inbox.put_nowait(sent[-1])

                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=3.0)
                assert frame.payload == b"once"

                # Второй frame не должен появиться
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(lb.recv_frame(sid), timeout=0.5)
            finally:
                await la.close()
                await lb.close()

        run(body())

    def test_received_seqs_bounded(self):
        """_received_seqs никогда не превышает maxlen."""

        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(
                low_a, low_b, mode=ReliabilityMode.PARALLEL, window_size=4
            )

            import collections

            lb._received_seqs = collections.deque(maxlen=16)
            lb._received_seqs_set = set()

            await la.start()
            await lb.start()
            try:
                sid = AggregatingLink.new_stream_id()
                n = 50
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(f.payload)
                assert len(received) == n
                assert len(lb._received_seqs) <= 16
                assert set(lb._received_seqs) == lb._received_seqs_set
            finally:
                await la.close()
                await lb.close()

        run(body())


# ===========================================================================
# 7. Piggyback ACK
# ===========================================================================


class TestAckPiggyback:

    def test_packet_count_reasonable_bidirectional(self):
        """При двунаправленной передаче общее число физических пакетов разумно."""

        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(low_a, low_b, mode=ReliabilityMode.PARALLEL)

            total = 0
            for low in (low_a, low_b):
                orig = low.send

                def counting(data, _orig=orig):
                    nonlocal total
                    total += 1
                    _orig(data)

                low.send = counting

            await la.start()
            await lb.start()
            try:
                sid_ab = AggregatingLink.new_stream_id()
                sid_ba = AggregatingLink.new_stream_id()
                await asyncio.gather(
                    la.send_frame(sid_ab, "d", b"a->b", end=True),
                    lb.send_frame(sid_ba, "d", b"b->a", end=True),
                )
                await asyncio.wait_for(lb.recv_frame(sid_ab), timeout=3.0)
                await asyncio.wait_for(la.recv_frame(sid_ba), timeout=3.0)
                await asyncio.sleep(0.3)
                # 2 data + до 4 ACK-only = максимум 6, с запасом 10
                assert total <= 10, f"Too many packets: {total}"
            finally:
                await la.close()
                await lb.close()

        run(body())


# ===========================================================================
# 8. Режим NONE — формат пакетов
# ===========================================================================


class TestModeNone:

    def test_legacy_ags1_format(self):
        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(low_a, low_b, mode=ReliabilityMode.NONE)

            sent: list[bytes] = []
            orig = low_a.send

            def capture(d):
                sent.append(d)
                orig(d)

            low_a.send = capture

            await la.start()
            await lb.start()
            try:
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "t", b"x", end=True)
                await asyncio.sleep(0.3)
                assert sent and sent[0][:4] == b"AGS1"
            finally:
                await la.close()
                await lb.close()

        run(body())

    def test_arq_uses_agp1_format(self):
        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(low_a, low_b, mode=ReliabilityMode.PARALLEL)

            sent: list[bytes] = []
            orig = low_a.send

            def capture(d):
                sent.append(d)
                orig(d)

            low_a.send = capture

            await la.start()
            await lb.start()
            try:
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "t", b"x", end=True)
                await asyncio.sleep(0.3)
                assert sent and sent[0][:4] == b"AGP1"
            finally:
                await la.close()
                await lb.close()

        run(body())


# ===========================================================================
# 9. Большие payload — chunking (режим NONE)
# ===========================================================================


class TestChunking:

    def test_large_payload_delivered(self):
        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(
                low_a, low_b, mode=ReliabilityMode.NONE, max_payload_bytes=512
            )

            async def work():
                sid = AggregatingLink.new_stream_id()
                big = b"X" * 2000
                await la.send_frame(sid, "data", big, end=True)
                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=5.0)
                assert frame.payload == big

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 10. Lifecycle
# ===========================================================================


class TestLifecycle:

    def test_start_idempotent(self):
        async def body():
            la, lb = make_pair(*reliable_fast())
            await la.start()
            await la.start()  # второй вызов безопасен
            await la.close()
            await lb.close()

        run(body())

    def test_close_idempotent(self):
        async def body():
            la, lb = make_pair(*reliable_fast())
            await la.start()
            await lb.start()
            await la.close()
            await la.close()  # второй вызов безопасен
            await lb.close()

        run(body())

    def test_send_after_close_raises(self):
        async def body():
            la, lb = make_pair(*reliable_fast())
            await la.start()
            await lb.start()
            await la.close()
            await lb.close()
            with pytest.raises(RuntimeError, match="closed"):
                await la.send_frame("sid", "t", b"x")

        run(body())

    def test_send_before_start_raises(self):
        async def body():
            low_a, _ = reliable_fast()
            med = FakeMediumTransport(low_a)
            link = AggregatingLink(med)
            with pytest.raises(RuntimeError, match="start"):
                await link.send_frame("sid", "t", b"x")
            await link.close()

        run(body())

    def test_invalid_payload_type_raises(self):
        async def body():
            la, lb = make_pair(*reliable_fast())
            await la.start()
            await lb.start()
            try:
                with pytest.raises(TypeError):
                    await la.send_frame("sid", "t", "not bytes")  # type: ignore
            finally:
                await la.close()
                await lb.close()

        run(body())

    def test_start_after_close_raises(self):
        async def body():
            la, lb = make_pair(*reliable_fast())
            await la.start()
            await lb.start()
            await la.close()
            await lb.close()
            with pytest.raises(RuntimeError, match="closed"):
                await la.start()

        run(body())


# ===========================================================================
# 11. Медленный транспорт
# ===========================================================================


class TestSlowTransport:

    @pytest.mark.parametrize("mode", ARQ_MODES, ids=[MODE_IDS[m] for m in ARQ_MODES])
    def test_delivery_slow_reliable(self, mode):
        async def body():
            la, lb = make_pair(*reliable_slow(), mode=mode)

            async def work():
                sid = AggregatingLink.new_stream_id()
                await la.send_frame(sid, "data", b"slow but sure", end=True)
                frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=10.0)
                assert frame.payload == b"slow but sure"

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 12. STOP_AND_WAIT специфика
# ===========================================================================


class TestStopAndWait:

    def test_window_size_forced_to_one(self):
        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(
                low_a, low_b, mode=ReliabilityMode.STOP_AND_WAIT, window_size=8
            )
            assert la._window_size == 1
            await la.close()
            await lb.close()

        run(body())

    def test_ordered_delivery(self):
        async def body():
            la, lb = make_pair(*reliable_fast(), mode=ReliabilityMode.STOP_AND_WAIT)

            async def work():
                sid = AggregatingLink.new_stream_id()
                n = 10
                for i in range(n):
                    await la.send_frame(sid, "d", str(i).encode(), end=(i == n - 1))
                received = []
                async for f in lb.iter_stream(sid):
                    received.append(int(f.payload.decode()))
                assert received == list(range(n))

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 13. PARALLEL специфика
# ===========================================================================


class TestParallel:

    def test_window_semaphore_initial_value(self):
        async def body():
            low_a, low_b = reliable_fast()
            la, lb = make_pair(
                low_a, low_b, mode=ReliabilityMode.PARALLEL, window_size=4
            )
            assert la._window_size == 4
            assert la._window_semaphore._value == 4
            await la.close()
            await lb.close()

        run(body())

    def test_many_concurrent_streams(self):
        async def body():
            la, lb = make_pair(
                *reliable_fast(), mode=ReliabilityMode.PARALLEL, window_size=8
            )

            async def work():
                streams = [AggregatingLink.new_stream_id() for _ in range(6)]
                n = 4

                async def send_stream(sid):
                    for i in range(n):
                        await la.send_frame(
                            sid, "d", f"{sid}:{i}".encode(), end=(i == n - 1)
                        )

                async def recv_stream(sid) -> list[bytes]:
                    result = []
                    async for f in lb.iter_stream(sid):
                        result.append(f.payload)
                    return result

                results = await asyncio.wait_for(
                    asyncio.gather(
                        *[send_stream(sid) for sid in streams],
                        *[recv_stream(sid) for sid in streams],
                    ),
                    timeout=15.0,
                )
                for i, received in enumerate(results[len(streams) :]):
                    sid = streams[i]
                    assert received == [f"{sid}:{j}".encode() for j in range(n)]

            await start_and_close(la, lb, work())

        run(body())


# ===========================================================================
# 14. new_stream_id
# ===========================================================================


class TestStreamId:

    def test_unique(self):
        ids = {AggregatingLink.new_stream_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_hex_string(self):
        sid = AggregatingLink.new_stream_id()
        assert isinstance(sid, str)
        int(sid, 16)  # не должно бросать


# ===========================================================================
# 15. Packet codec — unit tests
# ===========================================================================


class TestPacketCodec:

    def test_roundtrip_with_acks(self):
        pkt = _build_packet(seq=42, ack_seqs=[1, 2, 3], payload=b"hello")
        seq, acks, payload = _parse_packet(pkt)
        assert seq == 42
        assert acks == [1, 2, 3]
        assert payload == b"hello"

    def test_roundtrip_ack_only(self):
        pkt = _build_packet(seq=0, ack_seqs=[7, 8], payload=b"")
        seq, acks, payload = _parse_packet(pkt)
        assert seq == 0
        assert acks == [7, 8]
        assert payload == b""

    def test_roundtrip_no_acks(self):
        pkt = _build_packet(seq=100, ack_seqs=[], payload=b"data")
        seq, acks, payload = _parse_packet(pkt)
        assert seq == 100
        assert acks == []
        assert payload == b"data"

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            _parse_packet(b"AG")

    def test_wrong_magic_raises(self):
        bad = b"XXXX" + b"\x00" * 10
        with pytest.raises(ValueError, match="unknown packet magic"):
            _parse_packet(bad)

    def test_batch_roundtrip(self):
        frames = [
            Frame("abc", "data", b"hello", False),
            Frame("xyz", "ping", b"", True),
        ]
        encoded = AggregatingLink._encode_batch(frames)
        decoded = AggregatingLink._decode_batch(encoded)
        assert len(decoded) == 2
        assert decoded[0].stream_id == "abc"
        assert decoded[0].payload == b"hello"
        assert decoded[1].end is True


# ===========================================================================
# 16. Property-based тесты (hypothesis)
# ===========================================================================

try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st

    _HYPOTHESIS = True
except ImportError:
    _HYPOTHESIS = False

if _HYPOTHESIS:

    class TestHypothesis:

        @given(
            payload=st.binary(min_size=0, max_size=4096),
            frame_type=st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
                min_size=1,
                max_size=20,
            ),
        )
        @settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow])
        def test_arbitrary_payload_none(self, payload, frame_type):
            async def body():
                la, lb = make_pair(*reliable_fast(), mode=ReliabilityMode.NONE)

                async def work():
                    sid = AggregatingLink.new_stream_id()
                    await la.send_frame(sid, frame_type, payload, end=True)
                    frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=3.0)
                    assert frame.payload == payload
                    assert frame.frame_type == frame_type

                await start_and_close(la, lb, work())

            run(body())

        @given(payload=st.binary(min_size=0, max_size=1024))
        @settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
        def test_arbitrary_payload_parallel(self, payload):
            async def body():
                la, lb = make_pair(*reliable_fast(), mode=ReliabilityMode.PARALLEL)

                async def work():
                    sid = AggregatingLink.new_stream_id()
                    await la.send_frame(sid, "data", payload, end=True)
                    frame = await asyncio.wait_for(lb.recv_frame(sid), timeout=5.0)
                    assert frame.payload == payload

                await start_and_close(la, lb, work())

            run(body())

        @given(st.lists(st.integers(min_value=1, max_value=2**32 - 1), max_size=255))
        @settings(max_examples=100)
        def test_codec_roundtrip(self, ack_seqs):
            pkt = _build_packet(seq=999, ack_seqs=ack_seqs, payload=b"test")
            seq, decoded, payload = _parse_packet(pkt)
            assert seq == 999
            assert decoded == ack_seqs
            assert payload == b"test"
