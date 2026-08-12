import argparse
import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aethernet.config import ServerConfig
from aethernet.link_loading import LinkConfigError, load_link_factory
from aethernet.protocols import SUPPORTED_PROTOCOLS
from aethernet.server_router import ServerRouter

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_LOG_BACKUP_COUNT = 5

LINK_RESTART_BACKOFF_SECONDS = float(
    os.environ.get("AETHERNET_LINK_RESTART_BACKOFF", "5")
)


def parse_protocols(value: str) -> tuple[str, ...]:
    protocols = tuple(p.strip() for p in value.split(",") if p.strip())
    unknown = [p for p in protocols if p not in SUPPORTED_PROTOCOLS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"Unknown protocol(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(SUPPORTED_PROTOCOLS)}."
        )
    if not protocols:
        raise argparse.ArgumentTypeError("--protocols must list at least one protocol.")
    return protocols


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aethernet server.")
    parser.add_argument(
        "--link-config",
        type=Path,
        default=None,
        help="Path to the link config file defining create_link(mode, logger) "
        "(default: ./aethernet_link.py, or $AETHERNET_LINK_CONFIG).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Logging verbosity (default: DEBUG, or $AETHERNET_LOG_LEVEL).",
    )
    parser.add_argument(
        "--protocols",
        type=parse_protocols,
        default=None,
        help=f"Comma-separated list of protocols to enable "
        f"({', '.join(SUPPORTED_PROTOCOLS)}). Default: all "
        f"(or $AETHERNET_PROTOCOLS).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Path to a log file. If omitted, logs go to stderr only "
        "(default: $AETHERNET_LOG_FILE, if set).",
    )
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=None,
        help="Rotate the log file after it reaches this size in bytes "
        f"(default: {DEFAULT_LOG_MAX_BYTES}, or $AETHERNET_LOG_MAX_BYTES).",
    )
    parser.add_argument(
        "--log-backup-count",
        type=int,
        default=None,
        help="How many rotated log files to keep "
        f"(default: {DEFAULT_LOG_BACKUP_COUNT}, or $AETHERNET_LOG_BACKUP_COUNT).",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> ServerConfig:
    config = ServerConfig.from_env()
    return config.with_overrides(
        link_config_path=args.link_config,
        log_level=args.log_level,
        protocols=args.protocols,
    )


def resolve_log_file_settings(args: argparse.Namespace) -> tuple[Path | None, int, int]:
    """
    Настройки файлового логирования не заведены в ServerConfig, поэтому
    читаются отдельно: CLI-флаг > переменная окружения > дефолт.
    """
    log_file = args.log_file
    if log_file is None:
        env_value = os.environ.get("AETHERNET_LOG_FILE")
        log_file = Path(env_value) if env_value else None

    max_bytes = args.log_max_bytes
    if max_bytes is None:
        max_bytes = int(
            os.environ.get("AETHERNET_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)
        )

    backup_count = args.log_backup_count
    if backup_count is None:
        backup_count = int(
            os.environ.get("AETHERNET_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)
        )

    return log_file, max_bytes, backup_count


def configure_logging(
    level: str,
    log_file: Path | None,
    max_bytes: int,
    backup_count: int,
) -> None:
    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    # На случай повторного вызова (например, в тестах) — не плодим хендлеры.
    root.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


async def run_forever(config, logger: logging.Logger) -> None:
    """
    Внешний supervisor-цикл: даже если весь link или весь ServerRouter
    признан сломанным (ServerRouter.fatal_event), пересоздаёт link заново
    через link_factory и поднимает новый ServerRouter — процесс не падает
    и не требует внешнего перезапуска (systemd/docker restart и т.п.).
    Всё логируется на каждом шаге.
    """
    try:
        link_factory = load_link_factory(config.link_config_path)
    except LinkConfigError as e:
        raise SystemExit(str(e))

    stop_event = asyncio.Event()
    event_loop = asyncio.get_event_loop()
    event_loop.add_signal_handler(signal.SIGTERM, stop_event.set)  # type: ignore[arg-type]
    event_loop.add_signal_handler(signal.SIGINT, stop_event.set)  # type: ignore[arg-type]

    link_attempt = 0

    while not stop_event.is_set():
        link_attempt += 1
        try:
            link = await link_factory("server", logger)
        except Exception as e:
            logger.error(
                f"Failed to establish link (attempt {link_attempt}): {e!r}. "
                f"Retrying in {LINK_RESTART_BACKOFF_SECONDS}s."
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=LINK_RESTART_BACKOFF_SECONDS
                )
            except TimeoutError:
                pass
            continue

        logger.info(
            f"Link established. Enabled protocols: {', '.join(config.protocols)}"
        )

        # AggregatingLink уже готов (link установлен выше самим CLI), поэтому
        # AethernetServer.create() тут не нужен — он существует для тех, кто
        # поднимает сервер вручную без CLI и хочет одним вызовом получить и
        # link, и роутер, и lifecycle. Здесь всё это делает сам CLI.
        # noinspection PyTypeChecker
        router = ServerRouter(link, protocols=config.protocols, logger=logger)

        try:
            await router.start()
        except Exception as e:
            logger.error(f"Failed to start ServerRouter: {e!r}")
            await _safe_close(router, link, logger)
            await _wait_or_stop(stop_event, LINK_RESTART_BACKOFF_SECONDS)
            continue

        fatal_wait = asyncio.create_task(router.fatal_event.wait())
        stop_wait = asyncio.create_task(stop_event.wait())
        await asyncio.wait({fatal_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        for t in (fatal_wait, stop_wait):
            if not t.done():
                t.cancel()

        if stop_event.is_set():
            logger.info("Shutting down...")
            await _safe_close(router, link, logger)
            return

        logger.critical(
            f"ServerRouter reported the link as fatally broken "
            f"({router.fatal_reason}); tearing down and recreating the "
            "link + router from scratch."
        )
        await _safe_close(router, link, logger)
        await _wait_or_stop(stop_event, LINK_RESTART_BACKOFF_SECONDS)


async def _safe_close(router: ServerRouter, link, logger: logging.Logger) -> None:
    try:
        await router.close()
    except Exception as e:
        logger.error(f"Error while closing ServerRouter: {e!r}")
    try:
        await link.close()
    except Exception as e:
        logger.error(f"Error while closing link: {e!r}")


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        pass


async def main() -> None:
    args = parse_args()
    config = build_config(args)

    log_file, log_max_bytes, log_backup_count = resolve_log_file_settings(args)
    configure_logging(config.log_level, log_file, log_max_bytes, log_backup_count)

    if log_file is not None:
        logger.info(f"File logging enabled: {log_file}")

    await run_forever(config, logger)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
