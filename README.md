# Aethernet (Æthernet)

**Full HTTP and WebSocket traffic over any message-based transport — strings *or* bytes.**

Aethernet is a lightweight tunneling library that lets you keep using familiar `httpx` and WebSocket-style APIs even when you **cannot open direct TCP connections**. All traffic is framed, optionally encrypted, batched, and delivered through a `LowTransport` that you implement (Telegram, SMS, serial, BLE, sockets, etc.).

---

## Why Aethernet?

Some environments can send/receive *messages* but cannot reach the internet directly: Telegram bots, SMS gateways, air‑gapped systems, embedded radios, BLE links, etc. Aethernet bridges that gap: your code keeps using standard HTTP/WebSocket clients while the real bytes travel over whatever channel you have.

---

## Features

- **Drop-in HTTP client transport** — compatible with `httpx.AsyncClient(transport=...)`
- **WebSocket support** — via `AethernetWebSockets`
- **String or bytes low-level transport**
  - `mode="bytes"` (default) for binary-safe channels
  - `mode="string"` for messengers/SMS (alphabet + size constraints)
- **End-to-end encryption (optional)** with selectable AEAD:
  - `CHACHA20_POLY1305`
  - `AES_GCM`
  - `AES_EAX`
- **Reliability (Retry / ARQ) layer (optional)**
  - `NONE` — best-effort
  - `STOP_AND_WAIT` — classic ARQ
  - `PARALLEL` — selective-repeat style with a window
- **Aggregation & flow control** — batching and chunking over small-message channels
- **Bring your own transport** — implement one abstract class (`LowTransport`) and configure it via `LowTransportConfig`

---

## Installation

```bash
# pip
pip install aethernet-core

# uv
uv add aethernet-core
```

---

## Public API (what you import)

Everything below is available from the package root:

```python
from aethernet import (
    EncryptionMode,
    ReliabilityMode,
    LowTransport,
    LowTransportConfig,
    get_transport,
    AethernetHttpx,
    AethernetWebSockets,
    AethernetServer,
)
```

---

## Core concept: `LowTransport`

A `LowTransport` is the only thing you implement. It must be able to:

- `send(data: str | bytes) -> None`
- `recv() -> str | bytes`

### Configuration via `CONFIG` (important)

Transport limits and timings live in a dataclass `LowTransportConfig` and are typically provided via a **class attribute** `CONFIG`.

Key points:

1. You *can* keep the default `CONFIG`, but it’s recommended to set it explicitly for your transport.
2. Every implementor **must**:
   - accept `config: LowTransportConfig | None = None` in `__init__`
   - call `super().__init__(config)` inside `__init__`

This enables users to safely tweak parameters with `dataclasses.replace(...)` and pass the modified config to your transport.

---

## Quick Start

Aethernet has two sides:

- **Client** — makes HTTP/WebSocket requests
- **Server** — runs on a machine with internet access and executes requests on behalf of the client

Both sides must use:
- the same `LowTransport` implementation (or compatible ones)
- the same `encryption_key` (if encryption is enabled)

### 1) Define your transport (bytes-mode example)

```python
from aethernet import LowTransport, LowTransportConfig


class MyTransport(LowTransport):
    # Recommended: define constraints/timings here
    CONFIG = LowTransportConfig(
        mode="bytes",
        max_message_bytes=1024,
        min_send_interval=0.2,
        min_recv_interval=0.2,
        delay_before_resending=8.0,  # used by reliability layer
    )

    def __init__(self, *, config: LowTransportConfig | None = None) -> None:
        super().__init__(config)
        # initialize your channel here (serial/socket/BLE/etc.)

    def close(self) -> None:
        # release resources (optional)
        pass

    def send(self, data: str | bytes) -> None:
        # deliver `data` through your channel
        raise NotImplementedError

    def recv(self) -> str | bytes:
        # return the next incoming message
        raise NotImplementedError
```

### 2) Client side

```python
import asyncio
import secrets
import httpx
from aethernet import (
    get_transport,
    AethernetWebSockets,
    EncryptionMode,
    ReliabilityMode,
)


SHARED_KEY = secrets.token_bytes(32)  # generate once, share securely


async def main() -> None:
    link = await get_transport(
        MyTransport(),
        encryption_mode=EncryptionMode.CHACHA20_POLY1305,
        encryption_key=SHARED_KEY,
        reliability_mode=ReliabilityMode.PARALLEL,
        window_size=8,
    )

    # --- HTTP ---
    async with httpx.AsyncClient(transport=link) as client:
        r = await client.get("https://httpbin.org/get")
        print(r.status_code, r.json())

    # --- WebSocket ---
    async with AethernetWebSockets(link).connect("wss://echo.websocket.events") as ws:
        await ws.send("hello")
        msg = await ws.recv()
        print("Echo:", msg)


if __name__ == "__main__":
    asyncio.run(main())
```

### 3) Server side

Run this where **internet is available** and where your `LowTransport` can exchange messages with the client.

```python
import asyncio
from aethernet import AethernetServer, EncryptionMode, ReliabilityMode


SHARED_KEY = b"..."  # must match client's key exactly


async def main() -> None:
    server = await AethernetServer.create(
        MyTransport(),
        encryption_mode=EncryptionMode.CHACHA20_POLY1305,
        encryption_key=SHARED_KEY,
        reliability_mode=ReliabilityMode.PARALLEL,
        window_size=8,
    )
    print("Aethernet server is running...")
    await server.start_and_wait()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## String-mode transports (Telegram/SMS/etc.)

If your channel only supports text, use `mode="string"` and specify an `alphabet` (allowed characters) and limits.

```python
from aethernet import LowTransport, LowTransportConfig

B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


class SmsLikeTransport(LowTransport):
    CONFIG = LowTransportConfig(
        mode="string",
        max_message_chars=1000,
        alphabet=B64_ALPHABET,
        min_send_interval=1.0,
        min_recv_interval=1.0,
    )

    def __init__(self, *, config: LowTransportConfig | None = None) -> None:
        super().__init__(config)

    def send(self, data: str | bytes) -> None:
        # In string mode you will typically send `str` messages
        # (the stack will produce appropriate payloads for your mode).
        raise NotImplementedError

    def recv(self) -> str | bytes:
        raise NotImplementedError
```

---

## Tweaking transport limits with `dataclasses.replace`

Because config lives in a dataclass, users can override just a few fields:

```python
from dataclasses import replace

cfg = replace(MyTransport.CONFIG, min_send_interval=0.6, max_message_bytes=2048)
low = MyTransport(config=cfg)
```

---

## Transport stack creation: `get_transport(...)`

```python
async def get_transport(
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
    # Logging
    logger=None,
) -> "AggregatingLink"
```

### Parameters overview

**Encryption**
- `encryption_mode`
  - `EncryptionMode.NONE`
  - `EncryptionMode.CHACHA20_POLY1305`
  - `EncryptionMode.AES_GCM`
  - `EncryptionMode.AES_EAX`
- `encryption_key`: shared secret key (must match on both ends). Required if `encryption_mode != NONE`.

**Aggregating (batching/chunking)**
- `flush_interval`: max time to wait before flushing a batch
- `max_batch_size`: max logical batch size in bytes (before chunking)
- `chunk_assembly_ttl`: time to keep incomplete chunk assemblies

**Reliability (Retry / ARQ)**
- `reliability_mode`
  - `ReliabilityMode.NONE`
  - `ReliabilityMode.STOP_AND_WAIT`
  - `ReliabilityMode.PARALLEL` (uses `window_size`)
- `window_size`: send window for `PARALLEL`
- `ack_flush_interval`, `ack_batch_size`: ACK pacing / batching
- `received_seqs_window`: deduplication memory
- `reorder_buffer_ttl`: how long to keep out-of-order packets while reordering

**Logging**
- `logger`: optional logger for debugging/observability

---

## Server creation: `AethernetServer.create(...)`

`AethernetServer.create(...)` mirrors `get_transport(...)` and additionally configures the HTTP router and SSE flushing:

```python
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
    # Server Router
    http_client=...,
    proxy_http_client=...,
    # HTTP Aggregating
    sse_flush_bytes: int = 65536,
    sse_flush_interval: float = 0.5,
    # Logging
    logger=...,
) -> "AethernetServer"
```

### Extra server parameters

- `http_client`: the `httpx.AsyncClient` used by the server to perform real outgoing requests
- `proxy_http_client`: optional separate client (e.g. for proxying)
- `sse_flush_bytes`, `sse_flush_interval`: flushing behavior for SSE streaming responses

---

## Enums

### Reliability

```python
from aethernet import ReliabilityMode

ReliabilityMode.NONE
ReliabilityMode.STOP_AND_WAIT
ReliabilityMode.PARALLEL
```

### Encryption

```python
from aethernet import EncryptionMode

EncryptionMode.NONE
EncryptionMode.CHACHA20_POLY1305
EncryptionMode.AES_GCM
EncryptionMode.AES_EAX
```

---

## Project Structure

```
aethernet-core/
├── src/
│   └── aethernet/
│       ├── transport/
│       ├── __init__.py
│       ├── exceptions.py
│       ├── server_router.py
│       └── typing.py
├── tests/
├── pyproject.toml
├── LICENSE
├── NOTICE
└── README.md
```

---

## Development

```bash
uv sync --extra dev
pytest

black --check .
ruff check .
mypy .
```

---

## Compatibility

| Python | Platforms |
|--------|-----------|
| 3.12 + | Linux, macOS, Windows |

---

## Bug Reports

Please open an issue and include:

1. Expected behavior
2. Actual behavior
3. Minimal reproduction

---

## License

[Apache License 2.0](LICENSE)

---

## Author

**Walter Kerrigan (esolment)** — https://github.com/esolment