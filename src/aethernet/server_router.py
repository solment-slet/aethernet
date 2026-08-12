from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Iterable
from typing import Any, TypedDict

import httpx

from aethernet.http.server import LinkHTTPProxyServer
from aethernet.stasis.server import AethernetStasisServer
from aethernet.transport import AggregatingLink, LowTransport
from aethernet.transport.enums import EncryptionMode, ReliabilityMode
from aethernet.transport.stack import get_link
from aethernet.typing import LoggerLike
from aethernet.ws.server import LinkWebSocketProxyServer


class RouterKwargs(TypedDict):
    http_client: httpx.AsyncClient | None
    proxy_http_client: httpx.AsyncClient | None
    sse_flush_bytes: int
    sse_flush_interval: float
    ws_recv_flush_interval: float
    protocols: Iterable[str] | None
    restart_backoff_seconds: float
    max_consecutive_failures: int
    healthy_run_seconds: float
    logger: LoggerLike | None


class ServerRouter:
    """
    Больше не роутит фреймы вручную по kind первого meta-фрейма — это делает
    сам link через accept_stream(protocol). Задача ServerRouter теперь —
    поднять и держать живыми слушателей всех протоколов поверх одного link.

    Устойчивость: каждый протокольный сервер запускается под собственным
    supervisor-тасом. Если внутренний dispatcher_task сервера падает с
    исключением (а не штатно отменяется при close()), supervisor логирует
    ошибку, выжидает backoff и поднимает сервер заново — падение одного
    протокола не останавливает остальные и не требует рестарта процесса.
    """

    _RESTART_BACKOFF_SECONDS = 3.0
    _MAX_CONSECUTIVE_FAILURES = 5
    _HEALTHY_RUN_SECONDS = 30.0

    def __init__(
        self,
        link: AggregatingLink,
        *,
        # HTTP
        http_client: httpx.AsyncClient | None = None,
        proxy_http_client: httpx.AsyncClient | None = None,
        sse_flush_bytes: int = 32 * 1024,
        sse_flush_interval: float = 0.5,
        # WS
        ws_recv_flush_interval: float = 0.2,
        # Какие протоколы поднимать. None = все (http, ws, stasis).
        protocols: Iterable[str] | None = None,
        # Устойчивость на уровне отдельного протокола
        restart_backoff_seconds: float = _RESTART_BACKOFF_SECONDS,
        # Если протокол падает подряд max_consecutive_failures раз, не
        # прожив на этот раз хотя бы healthy_run_seconds, это уже похоже не
        # на разовый сбой, а на то, что сломан сам link — тогда router
        # перестаёт бесконечно ретраить этот протокол и вместо этого
        # выставляет fatal_event, сигнализируя вызывающему коду, что нужно
        # пересоздавать link целиком (см. AethernetServer / cli.py).
        max_consecutive_failures: int = _MAX_CONSECUTIVE_FAILURES,
        healthy_run_seconds: float = _HEALTHY_RUN_SECONDS,
        # Logging
        logger: LoggerLike | None = None,
    ) -> None:
        self._link = link
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._closed = False
        self._restart_backoff_seconds = restart_backoff_seconds
        self._max_consecutive_failures = max_consecutive_failures
        self._healthy_run_seconds = healthy_run_seconds

        # Взводится, если один из протоколов падает слишком много раз
        # подряд слишком быстро — сигнал наружу, что похоже сломан весь
        # link, а не отдельный протокол, и нужно пересоздавать всё целиком.
        self.fatal_event = asyncio.Event()
        self._fatal_reason: str | None = None

        # Каждая фабрика создаёт СВЕЖИЙ экземпляр протокольного сервера —
        # используется при первом запуске и при рестарте после падения,
        # чтобы не переиспользовать объект с потенциально испорченным
        # внутренним состоянием (например, зависшими event'ами/буферами).
        all_factories: dict[str, Callable[[], Any]] = {
            "http": lambda: LinkHTTPProxyServer(
                link,
                upstream_client=http_client,
                proxy_upstream_client=proxy_http_client,
                sse_flush_bytes=sse_flush_bytes,
                sse_flush_interval=sse_flush_interval,
            ),
            "ws": lambda: LinkWebSocketProxyServer(
                link,
                recv_flush_interval=ws_recv_flush_interval,
                logger=logger,
            ),
            "stasis": lambda: AethernetStasisServer(
                link,
                logger=logger,
            ),
        }

        if protocols is None:
            selected = tuple(all_factories.keys())
        else:
            selected = tuple(protocols)
            unknown = [p for p in selected if p not in all_factories]
            if unknown:
                raise ValueError(
                    f"Unknown protocol(s) for ServerRouter: {', '.join(unknown)}. "
                    f"Supported: {', '.join(all_factories.keys())}."
                )
            if not selected:
                raise ValueError("ServerRouter needs at least one protocol enabled.")

        self._protocol_factories = {name: all_factories[name] for name in selected}

        # self.http / self.ws / self.stasis всегда указывают на ТЕКУЩИЙ живой
        # экземпляр (или отсутствуют вовсе, если протокол не включён). После
        # рестарта объект под этими именами меняется — не сохраняйте ссылку
        # на server.http долгосрочно, обращайтесь через router.http заново.
        for name, factory in self._protocol_factories.items():
            setattr(self, name, factory())

        self._supervisor_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for name in self._protocol_factories:
            task = asyncio.create_task(
                self._supervise(name),
                name=f"ServerRouter.supervise.{name}",
            )
            self._supervisor_tasks.append(task)

    async def _supervise(self, name: str) -> None:
        """
        Создаёт свежий экземпляр протокольного сервера через фабрику,
        запускает его и ждёт завершения его dispatcher_task. Если завершение
        не было штатной отменой (сервер упал сам) — старый объект просто
        выбрасывается, после паузы создаётся НОВЫЙ экземпляр с чистого листа
        и подставляется в self.<name>. Останавливается только когда роутер
        закрыт.

        Если протокол падает подряд max_consecutive_failures раз, ни разу не
        продержавшись дольше healthy_run_seconds — это больше похоже на
        сломанный link, чем на случайный сбой конкретного протокола. В этом
        случае supervisor логирует critical, выставляет self.fatal_event и
        останавливается сам (остальные протоколы продолжают ретраиться
        независимо, пока вызывающий код не решит пересоздать весь router).
        """
        factory = self._protocol_factories[name]
        loop = asyncio.get_running_loop()
        consecutive_failures = 0

        while not self._closed:
            server = factory()
            setattr(self, name, server)
            attempt_started_at = loop.time()

            try:
                await server.start()
            except Exception as e:
                self._logger.error(f"{name}: failed to start: {e!r}")
            else:
                task = getattr(server, "_dispatcher_task", None)
                if task is None:
                    self._logger.error(
                        f"{name}: no _dispatcher_task after start(), "
                        "cannot supervise this server"
                    )
                    return

                try:
                    await task
                except asyncio.CancelledError:
                    # Штатная остановка через close() — supervisor тоже выходит.
                    return
                except Exception as e:
                    self._logger.error(f"{name}: dispatcher died unexpectedly: {e!r}")

            if self._closed:
                return

            # Подчищаем упавший объект, чтобы он не держал ресурсы висящими.
            try:
                await server.close()
            except Exception as e:
                self._logger.error(
                    f"{name}: error while cleaning up failed instance: {e!r}"
                )

            ran_for = loop.time() - attempt_started_at
            if ran_for >= self._healthy_run_seconds:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            if consecutive_failures >= self._max_consecutive_failures:
                self._logger.critical(
                    f"{name}: {consecutive_failures} consecutive failures within "
                    f"{self._healthy_run_seconds}s each — assuming the link itself "
                    "is broken, not just this protocol. Marking router as fatal."
                )
                self._fatal_reason = (
                    f"{name} failed {consecutive_failures} times in a row "
                    "without a healthy run"
                )
                self.fatal_event.set()
                return

            self._logger.warning(
                f"{name}: restarting with a fresh instance in "
                f"{self._restart_backoff_seconds}s "
                f"(consecutive failures: {consecutive_failures}/{self._max_consecutive_failures})"
            )
            await asyncio.sleep(self._restart_backoff_seconds)

    @property
    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for name in self._protocol_factories:
            server = getattr(self, name)
            try:
                await server.close()
            except Exception as e:
                self._logger.error(
                    f"Error while closing {type(server).__name__}: {e!r}"
                )

        for task in self._supervisor_tasks:
            task.cancel()
        for task in self._supervisor_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._supervisor_tasks.clear()
        self.fatal_event.clear()
        self._fatal_reason = None


class AethernetServer(ServerRouter):
    def __init__(
        self,
        transport: AggregatingLink,
        *,
        # Server Router / HTTP
        http_client: httpx.AsyncClient | None = None,
        proxy_http_client: httpx.AsyncClient | None = None,
        sse_flush_bytes: int = 65536,
        sse_flush_interval: float = 0.5,
        # WS
        ws_recv_flush_interval: float = 0.2,
        # Какие протоколы поднимать
        protocols: Iterable[str] | None = None,
        # Устойчивость на уровне отдельного протокола
        restart_backoff_seconds: float = ServerRouter._RESTART_BACKOFF_SECONDS,
        max_consecutive_failures: int = ServerRouter._MAX_CONSECUTIVE_FAILURES,
        healthy_run_seconds: float = ServerRouter._HEALTHY_RUN_SECONDS,
        # Пауза перед пересозданием ВСЕГО транспорта, если fatal_event
        # выставлен (link целиком считается сломанным).
        transport_restart_backoff_seconds: float = ServerRouter._RESTART_BACKOFF_SECONDS,
        # Logging
        logger: LoggerLike | None = None,
    ) -> None:
        self.stop_event = asyncio.Event()
        self.transport = transport
        self._transport_restart_backoff_seconds = transport_restart_backoff_seconds
        # Заполняется только внутри create(): async-фабрика, которая с нуля
        # поднимает новый AggregatingLink с теми же параметрами, что и
        # исходный. Если сервер создан напрямую (не через create()), она
        # остаётся None, и восстановиться после гибели всего link мы не
        # можем — только залогировать это и остановиться.
        self._rebuild_transport: Any = None

        # Параметры роутера запоминаем, чтобы иметь возможность заново
        # проинициализировать ServerRouter на новом transport после
        # пересоздания link (см. _reinit_router).
        # noinspection PyTypeChecker
        self._router_kwargs: RouterKwargs = {
            "http_client": http_client
            or httpx.AsyncClient(timeout=None, trust_env=False),
            "proxy_http_client": proxy_http_client or httpx.AsyncClient(timeout=None),
            "sse_flush_bytes": sse_flush_bytes,
            "sse_flush_interval": sse_flush_interval,
            "ws_recv_flush_interval": ws_recv_flush_interval,
            "protocols": protocols,
            "restart_backoff_seconds": restart_backoff_seconds,
            "max_consecutive_failures": max_consecutive_failures,
            "healthy_run_seconds": healthy_run_seconds,
            "logger": logger if logger is not None else logging.getLogger(__name__),
        }

        super().__init__(transport, **self._router_kwargs)

    def _reinit_router(self, transport: AggregatingLink) -> None:
        """Заново поднимает состояние ServerRouter поверх нового transport."""
        self.transport = transport
        ServerRouter.__init__(self, transport, **self._router_kwargs)

    @classmethod
    async def create(
        cls,
        low_transport: LowTransport,
        *,
        # Encryption
        encryption_mode: EncryptionMode = EncryptionMode.NONE,
        encryption_key: bytes | None = None,
        # Aggregating
        flush_interval: float = 0.5,
        max_batch_size: int = 64 * 1024,
        chunk_assembly_ttl: float = 60.0,
        # Reliability
        reliability_mode: ReliabilityMode = ReliabilityMode.NONE,
        window_size: int = 8,
        ack_flush_interval: float = 0.1,
        ack_batch_size: int = 8,
        received_seqs_window: int = 256,
        reorder_buffer_ttl: float = 500.0,
        # Server Router / HTTP
        http_client: httpx.AsyncClient | None = None,
        proxy_http_client: httpx.AsyncClient | None = None,
        sse_flush_bytes: int = 65536,
        sse_flush_interval: float = 0.5,
        # WS
        ws_recv_flush_interval: float = 0.2,
        # Какие протоколы поднимать
        protocols: Iterable[str] | None = None,
        # Устойчивость
        transport_restart_backoff_seconds: float = ServerRouter._RESTART_BACKOFF_SECONDS,
        # Logging
        logger: LoggerLike | None = None,
    ) -> AethernetServer:
        logger = logger if logger is not None else logging.getLogger(__name__)

        async def build_transport() -> AggregatingLink:
            return await get_link(
                low_transport,
                encryption_mode=encryption_mode,
                encryption_key=encryption_key,
                flush_interval=flush_interval,
                max_batch_size=max_batch_size,
                reliability_mode=reliability_mode,
                window_size=window_size,
                ack_flush_interval=ack_flush_interval,
                ack_batch_size=ack_batch_size,
                received_seqs_window=received_seqs_window,
                chunk_assembly_ttl=chunk_assembly_ttl,
                reorder_buffer_ttl=reorder_buffer_ttl,
                logger=logger,
            )

        transport = await build_transport()

        server = cls(
            transport,
            http_client=http_client,
            proxy_http_client=proxy_http_client,
            sse_flush_bytes=sse_flush_bytes,
            sse_flush_interval=sse_flush_interval,
            ws_recv_flush_interval=ws_recv_flush_interval,
            protocols=protocols,
            transport_restart_backoff_seconds=transport_restart_backoff_seconds,
            logger=logger,
        )
        # low_transport, скорее всего, одноразовый (например, уже открытый
        # сокет) — если он не переиспользуем, попытка автоматического
        # пересоздания транспорта после фатального сбоя тоже, скорее всего,
        # не сработает, но мы всё равно пробуем и логируем результат.
        server._rebuild_transport = build_transport
        return server

    async def start_and_wait(self) -> None:
        """
        Запуск сервера и ожидание завершения программы.

        Устойчиво к гибели ВСЕГО link/ServerRouter: если один из протоколов
        падает подряд слишком много раз подряд без здорового периода работы,
        ServerRouter взводит fatal_event (см. ServerRouter._supervise). Здесь
        это ловится, весь router и старый transport закрываются, логируется
        critical, и (если сервер создан через create()) transport и router
        пересоздаются с нуля через build_transport(). Если пересоздать
        transport невозможно (сервер создан напрямую с готовым transport),
        сервер логирует это и завершает работу.
        """
        event_loop = asyncio.get_event_loop()
        event_loop.add_signal_handler(signal.SIGTERM, self.stop_event.set)  # type: ignore[arg-type]
        event_loop.add_signal_handler(signal.SIGINT, self.stop_event.set)  # type: ignore[arg-type]

        try:
            while not self.stop_event.is_set():
                await self.start()

                fatal_wait = asyncio.create_task(self.fatal_event.wait())
                stop_wait = asyncio.create_task(self.stop_event.wait())
                await asyncio.wait(
                    {fatal_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in (fatal_wait, stop_wait):
                    if not t.done():
                        t.cancel()

                if self.stop_event.is_set():
                    break

                reason = self.fatal_reason
                self._logger.critical(
                    f"AethernetServer: link/router considered fatally broken "
                    f"({reason}); tearing down and recreating from scratch."
                )

                await ServerRouter.close(self)
                try:
                    await self.transport.close()
                except Exception as e:
                    self._logger.error(f"Error closing broken transport: {e!r}")

                if self._rebuild_transport is None:
                    self._logger.critical(
                        "AethernetServer was created with a pre-built transport "
                        "(not via create()), so it cannot rebuild it automatically. "
                        "Stopping."
                    )
                    break

                self._logger.warning(
                    f"Reconnecting in {self._transport_restart_backoff_seconds}s..."
                )
                await asyncio.sleep(self._transport_restart_backoff_seconds)

                try:
                    new_transport = await self._rebuild_transport()
                except Exception as e:
                    self._logger.error(
                        f"Failed to rebuild transport: {e!r}. Will retry."
                    )
                    continue

                self._logger.info("Transport rebuilt successfully, resuming.")
                self._reinit_router(new_transport)
        finally:
            await self.close()

    async def close(self) -> None:
        await ServerRouter.close(self)
        try:
            await self.transport.close()
        except Exception as e:
            self._logger.error(f"Error closing transport: {e!r}")
