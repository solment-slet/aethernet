import asyncio

import httpx

from config import Config
from transport import LowTransport, Transport, AggregatingLink
from transport.server_router import MachineBRouter


async def main_task(link_b: AggregatingLink) -> None:
    await link_b.start()

    router = MachineBRouter(
        link_b,
        http_client=httpx.AsyncClient(timeout=None),
        sse_flush_bytes=64 * 1024,
        sse_flush_interval=0.5,
    )
    await router.start()

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await router.close()
        await link_b.close()


async def main(config: Config):
    low_transport = LowTransport(
        config,
        config.vk_token_server,
        config.vk_client_peer_id,
    )
    transport = Transport(
        config,
        low_transport,
    )

    link = AggregatingLink(
        transport,
        flush_interval=0.8,
        min_send_interval=0.4,
        max_batch_size=128 * 1024,
        recv_restart_delay=0.01,
    )

    asyncio.create_task(main_task(link))
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main(Config()))
