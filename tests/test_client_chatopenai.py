from __future__ import annotations
import asyncio
import os

import httpx
from langchain_openai import ChatOpenAI

from aethernet.transport import LowTransport, MediumTransport, AggregatingLink
from aethernet import LinkHTTPTransport


async def machine_a_http_test(link_a: AggregatingLink):
    transport = LinkHTTPTransport(link_a)
    async_client = httpx.AsyncClient(transport=transport, timeout=None)

    try:
        llm = ChatOpenAI(
            model="openai/gpt-4o-mini",
            base_url="https://models.github.ai/inference",
            api_key=os.getenv("OPENAI_API_KEY"),
            http_async_client=async_client,
        )

        print("Начинаем astream...")
        async for chunk in llm.astream("Привет! Расскажи анекдот."):
            # chunk – это объект BaseMessageChunk
            # Обычно chunk.content содержит текст
            print(chunk)
            if hasattr(chunk, "content") and chunk.content:
                print(chunk.content, end="", flush=True)
    finally:
        await async_client.aclose()


async def machine_a_ws_test(link_a: AggregatingLink):
    print("WS: connecting...")
    ws = await link_websockets_connect(
        link_a,
        url="ws://127.0.0.1:8765/",
    )
    print("WS: connected")

    print("WS: sending...")
    await ws.send("hello over aggregating link")

    print("WS: receiving...")
    msg = await ws.recv()
    print("WS recv:", msg)

    print("WS: closing...")
    await ws.close()
    print("WS: closed")


async def main(config: Config):
    low_transport = LowTransport(
        config,
        config.vk_token_client,
        config.vk_server_peer_id,
    )
    transport = MediumTransport(
        config,
        low_transport,
    )

    link_a = AggregatingLink(
        transport,
        flush_interval=0.8,
        min_send_interval=0.4,
        max_batch_size=128 * 1024,
        recv_restart_delay=0.01,
    )

    try:
        await link_a.start()
        await asyncio.sleep(0.2)

        await machine_a_http_test(link_a)

    finally:
        await link_a.close()


if __name__ == "__main__":
    asyncio.run(main(Config()))
