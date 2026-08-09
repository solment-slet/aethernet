import asyncio
import logging

from aethernet.low_transports.tcp_low_transport import TCPLowTransport
from aethernet.stasis import AethernetStasisServer
from aethernet import get_link, ReliabilityMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")


async def handle_client(low_transport: TCPLowTransport) -> None:
    # noinspection PyTypeChecker
    link = await get_link(
        low_transport,
        reliability_mode=ReliabilityMode.STOP_AND_WAIT,
        image_reliability_mode=ReliabilityMode.NONE,
        logger=logger,
    )

    try:
        stasis = AethernetStasisServer(link)
        await stasis.start_and_wait()

    except asyncio.CancelledError:
        print("Задача была отменена!")
        raise

    except Exception as e:
        logger.error(e)

    finally:
        print("CLOSE IS HERE!")
        await link.close()


async def main() -> None:
    server_sock = await asyncio.to_thread(TCPLowTransport.listen, "0.0.0.0", 9876)
    logger.info("Listening on 0.0.0.0:9876")

    try:
        low_transport = await asyncio.to_thread(TCPLowTransport.accept, server_sock)
        logger.info("Client connected")
        await handle_client(low_transport)
    finally:
        server_sock.close()


if __name__ == "__main__":
    asyncio.run(main())