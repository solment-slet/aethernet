import asyncio

from aethernet import ReliabilityMode, get_link
from aethernet.low_transports.aethernet_tcp_transport import (
    TCPLowTransport,
    TCPLowTransportServer,
)


async def create_link(mode, logger):
    if mode == "client":
        low_transport = await asyncio.to_thread(
            TCPLowTransport.connect,
            "127.0.0.1",
            9876,
        )
    else:
        low_transport = await asyncio.to_thread(
            TCPLowTransportServer,
            "127.0.0.1",
            9876,
        )

    return await get_link(
        low_transport,
        reliability_mode=ReliabilityMode.STOP_AND_WAIT,
        image_reliability_mode=ReliabilityMode.NONE,
        logger=logger,
    )
