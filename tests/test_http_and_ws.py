from __future__ import annotations

import asyncio
import queue
from dataclasses import dataclass

import httpx

from transport import AggregatingLink
from transport.http_over_link import LinkHTTPTransport
from transport.ws_over_link import link_websockets_connect
from transport.server_router import MachineBRouter


class QueueTransport:
    def __init__(
        self, incoming: queue.Queue[bytes], outgoing: queue.Queue[bytes], name: str
    ):
        self._incoming = incoming
        self._outgoing = outgoing
        self._name = name

    def send(self, data: bytes) -> None:
        self._outgoing.put(data)

    def recv(self) -> bytes:
        return self._incoming.get()


@dataclass
class TransportPair:
    a: QueueTransport
    b: QueueTransport


def make_transport_pair() -> TransportPair:
    q_ab: queue.Queue[bytes] = queue.Queue()
    q_ba: queue.Queue[bytes] = queue.Queue()
    return TransportPair(
        a=QueueTransport(q_ba, q_ab, "A"),
        b=QueueTransport(q_ab, q_ba, "B"),
    )


async def machine_b_main(link_b: AggregatingLink):
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


async def machine_a_http_test(link_a: AggregatingLink):
    transport = LinkHTTPTransport(link_a)
    client = httpx.AsyncClient(transport=transport, timeout=None)

    try:
        r = await client.get("https://httpbin.org/get")
        print("HTTP status:", r.status_code)
        print("HTTP body prefix:", r.text[:120])
    finally:
        await client.aclose()


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


async def main():
    pair = make_transport_pair()

    link_a = AggregatingLink(
        pair.a,
        flush_interval=0.8,
        min_send_interval=0.4,
        max_batch_size=128 * 1024,
        recv_restart_delay=0.01,
    )

    link_b = AggregatingLink(
        pair.b,
        flush_interval=0.8,
        min_send_interval=0.4,
        max_batch_size=128 * 1024,
        recv_restart_delay=0.01,
    )

    task_b = asyncio.create_task(machine_b_main(link_b))

    try:
        await link_a.start()
        await asyncio.sleep(0.2)

        await machine_a_http_test(link_a)
        await machine_a_ws_test(link_a)

    finally:
        await link_a.close()
        task_b.cancel()
        try:
            await task_b
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
