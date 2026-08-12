# Aethernet (Æthernet)

**Full HTTP, WebSocket, and remote-access traffic over any message-based transport — strings *or* bytes.**

Aethernet is a lightweight tunneling library that lets you keep using familiar `httpx` and WebSocket-style APIs even when you **cannot open direct TCP connections**. All traffic is framed, optionally encrypted, batched, and delivered through a `LowTransport` that you implement (Telegram, SMS, serial, BLE, sockets, etc.).

---

## Why Aethernet?

Some environments can send/receive *messages* but cannot reach the internet directly: Telegram bots, SMS gateways, air-gapped systems, embedded radios, BLE links, and so on. Aethernet bridges that gap: your code keeps using standard HTTP/WebSocket clients while the real bytes travel over whatever channel you have.

Version 3.0.0 adds image-capable transports and **Stasis** — a full remote-access protocol (screen streaming + input forwarding) built on top of the same transport stack.

---

## Features

- **Drop-in HTTP client transport** — compatible with `httpx.AsyncClient(transport=...)`
- **WebSocket support** — via `AethernetWebSockets`
- **Generic, type-safe `LowTransport[T]`** with metaclass validation
  - `LowTransport[str]` / `LowTransport[bytes]` for text or binary channels
  - `LowTransport[StrAndImages]` / `LowTransport[BytesAndImages]` for image-capable channels (required for Stasis)
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
  - `PARALLEL` — selective-repeat style with a configurable window
- **Separate reliability mode for image frames** — tune ARQ independently for screen frames vs. regular traffic
- **Aggregation & flow control** — batching and chunking over small-message channels
- **Stasis** — remote-access protocol: screen streaming and input forwarding; server runs inside `aethernet-core`, client lives at [github.com/solment-slet/stasis-client](https://github.com/solment-slet/stasis-client)
- **Built-in TCP transport** — `TCPLowTransport` and `TCPLowTransportServer`, ready to use for testing or local setups
- **`athnet` CLI** — start the Aethernet server from the command line with a single command and a link config file
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

## Public API

Everything below is available from the package root:

```python
from aethernet import (
    EncryptionMode,
    ReliabilityMode,
    LowTransport,
    LowTransportConfig,
    get_link,
    AethernetHttpx,
    AethernetWebSockets,
    AethernetServer,
)
```

The built-in TCP transport lives under its own sub-package:

```python
from aethernet.low_transports.aethernet_tcp_transport import (
    TCPLowTransport,
    TCPLowTransportServer,
)
```

---

## Core concept: `LowTransport[T]`

`LowTransport` is the only class you implement. It is now **generic**: you declare what data type your channel carries, and the metaclass validates your declaration at class definition time — not at runtime.

```
LowTransport[str]            # text-only channel
LowTransport[bytes]          # binary channel
LowTransport[StrAndImages]   # text channel that can also carry screen frames (Stasis)
LowTransport[BytesAndImages] # binary channel that can also carry screen frames (Stasis)
```

The metaclass enforces two rules automatically:

- You **cannot** pass the raw `TransportData` TypeVar — you must specify a concrete type.
- If `supports_images=True` in your `CONFIG`, you **must** use `StrAndImages` or `BytesAndImages` as the generic type.

A `LowTransport` must be able to:

- `send(data: T) -> None`
- `recv() -> T`

### Configuration via `LowTransportConfig`

Transport limits and timings live in a dataclass and are typically provided via a **class attribute** `CONFIG`.

```python
@dataclass(slots=True)
class LowTransportConfig:
    mode: Literal["string", "bytes"] = "bytes"
    supports_images: bool = False        # set True to enable Stasis
    max_message_bytes: int = 1024        # for bytes mode
    max_message_chars: int | None = None # for string mode
    alphabet: str | None = None          # allowed chars in string mode
    min_send_interval: float = 0.2
    min_recv_interval: float = 0.2
    delay_before_resending: float = 8.0  # used by the reliability layer
```

Every `LowTransport` subclass **must**:

1. Accept `config: LowTransportConfig | None = None` in `__init__`.
2. Call `super().__init__(config)` inside `__init__`.

This lets users safely tweak individual fields with `dataclasses.replace(...)` and pass the modified config in.

---

## Quick Start

Aethernet has two sides:

- **Client** — makes HTTP/WebSocket requests
- **Server** — runs on a machine with internet access and executes requests on behalf of the client

Both sides must use the same `LowTransport` implementation (or compatible ones), the same `encryption_mode`, the same `encryption_key` (if encryption is enabled), and the same `reliability_mode`.

### 1) Define your transport

```python
from aethernet import LowTransport, LowTransportConfig


class MyTransport(LowTransport[bytes]):
    CONFIG = LowTransportConfig(
        mode="bytes",
        max_message_bytes=1024,
        min_send_interval=0.2,
        min_recv_interval=0.2,
        delay_before_resending=8.0,
    )

    def __init__(self, *, config: LowTransportConfig | None = None) -> None:
        super().__init__(config)
        # initialize your channel here (serial / socket / BLE / etc.)

    def close(self) -> None:
        pass  # release resources

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv(self) -> bytes:
        raise NotImplementedError
```

### 2) Client side

```python
import asyncio
import secrets
import httpx
from aethernet import get_link, AethernetWebSockets, EncryptionMode, ReliabilityMode

SHARED_KEY = secrets.token_bytes(32)  # generate once, share securely


async def main() -> None:
    link = await get_link(
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

SHARED_KEY = b"..."  # must match the client's key exactly


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

## String-mode transports (Telegram / SMS / etc.)

If your channel only supports text, use `mode="string"` and specify an `alphabet` and character limit.

```python
from aethernet import LowTransport, LowTransportConfig

B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


class SmsLikeTransport(LowTransport[str]):
    CONFIG = LowTransportConfig(
        mode="string",
        max_message_chars=1000,
        alphabet=B64_ALPHABET,
        min_send_interval=1.0,
        min_recv_interval=1.0,
    )

    def __init__(self, *, config: LowTransportConfig | None = None) -> None:
        super().__init__(config)

    def send(self, data: str) -> None:
        raise NotImplementedError

    def recv(self) -> str:
        raise NotImplementedError
```

---

## Tweaking transport limits

Because config lives in a dataclass, you can override individual fields without subclassing:

```python
from dataclasses import replace

cfg = replace(MyTransport.CONFIG, min_send_interval=0.6, max_message_bytes=2048)
low = MyTransport(config=cfg)
```

---

## Built-in TCP transport

Aethernet ships with a ready-to-use TCP transport. It supports both regular `bytes` messages and image frames (`BytesAndImages`) for use with Stasis.

### `TCPLowTransport` — single connection

Use this when you manage connection setup yourself: the client connects, and the server wraps an already-accepted socket.

```python
from aethernet.low_transports.aethernet_tcp_transport import TCPLowTransport

# Client
transport = TCPLowTransport.connect("127.0.0.1", 9876)

# Server (manual accept loop)
server_sock = TCPLowTransport.listen("0.0.0.0", 9876)
transport = TCPLowTransport.accept(server_sock)
```

`TCPLowTransport` wraps a single socket. When that connection drops, the transport is done — it does not reconnect.

### `TCPLowTransportServer` — self-managing server

Use this when you want the transport to survive client reconnections on its own. It listens on the given port internally and, whenever the active client disconnects, blocks until the next one connects — without tearing down the `AggregatingLink` above it.

```python
from aethernet.low_transports.aethernet_tcp_transport import TCPLowTransportServer

transport = TCPLowTransportServer("0.0.0.0", 9876, logger=logger)
link = await get_link(transport, ...)
# clients can disconnect and reconnect freely — the link stays alive
```

Both classes enable TCP keepalive by default (configurable via `keepalive_idle`, `keepalive_interval`, `keepalive_count`), which ensures that a peer that disappears without a clean disconnect is detected within roughly 10–15 seconds rather than hanging forever.

---

## `athnet` CLI

Version 3.0.0 ships a console command, `athnet`, that starts the Aethernet server without writing any boilerplate. You describe your transport once in a **link config file** (`aethernet_link.py`), and `athnet` takes care of the rest — including automatic restart if the link breaks fatally.

### Link config file

The link config file must expose a single async function:

```python
async def create_link(
    mode: Literal["server", "client"], logger: logging.Logger
) -> AggregatingLink:
    ...
```

`mode` is `"server"` when called from `athnet` and `"client"` when called from client-side tooling such as stasis-client. This lets you describe both sides of the connection in one file.

A minimal example using the built-in TCP transport:

```python
# aethernet_link.py
import asyncio
from aethernet import ReliabilityMode, get_link
from aethernet.low_transports.aethernet_tcp_transport import (
    TCPLowTransport,
    TCPLowTransportServer,
)


async def create_link(mode, logger):
    if mode == "client":
        low_transport = await asyncio.to_thread(
            TCPLowTransport.connect, "127.0.0.1", 9876
        )
    else:
        low_transport = await asyncio.to_thread(
            TCPLowTransportServer, "127.0.0.1", 9876
        )

    return await get_link(
        low_transport,
        reliability_mode=ReliabilityMode.STOP_AND_WAIT,
        image_reliability_mode=ReliabilityMode.NONE,
        logger=logger,
    )
```

A fully documented template (`aethernet_link.example.py`) is included in the package source.

### Running the server

```bash
# uses ./aethernet_link.py by default
athnet

# explicit config path
athnet --link-config /path/to/my_link.py

# enable specific protocols only
athnet --protocols http,websocket

# tune logging
athnet --log-level DEBUG --log-file /var/log/aethernet.log
```

### CLI reference

| Flag | Default | Env var | Description |
|------|---------|---------|-------------|
| `--link-config` | `./aethernet_link.py` | `AETHERNET_LINK_CONFIG` | Path to the link config file |
| `--log-level` | `DEBUG` | `AETHERNET_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `--protocols` | all | `AETHERNET_PROTOCOLS` | Comma-separated list of protocols to enable |
| `--log-file` | stderr only | `AETHERNET_LOG_FILE` | Path to a rotating log file |
| `--log-max-bytes` | 10 MiB | `AETHERNET_LOG_MAX_BYTES` | Log rotation size |
| `--log-backup-count` | 5 | `AETHERNET_LOG_BACKUP_COUNT` | Number of rotated log files to keep |

All flags can be set via environment variables instead, which is convenient for `systemd` units or Docker environments.

---

## Stasis — remote access protocol

Stasis is a remote-access protocol built on top of the Aethernet transport stack. It streams the remote device's screen to the client and forwards keyboard, mouse, and clipboard events back. The **server** (the remote device being controlled) runs inside `aethernet-core`; the **client** lives in a separate repository:

> [github.com/solment-slet/stasis-client](https://github.com/solment-slet/stasis-client)

To use Stasis, your `LowTransport` must be able to carry image frames — declare it as `LowTransport[BytesAndImages]` (or `LowTransport[StrAndImages]`) and set `supports_images=True` in its `CONFIG`. The metaclass will reject any other combination at class-definition time.

The built-in `TCPLowTransport` and `TCPLowTransportServer` already satisfy this requirement out of the box.

---

## Transport stack: `get_link`

`get_link` assembles the full three-layer stack (low → medium → aggregating) and returns an `AggregatingLink` ready for use with `httpx`, WebSockets, or Stasis.

```python
async def get_link(
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
    image_reliability_mode: ReliabilityMode = ReliabilityMode.NONE,
    window_size: int = 8,
    ack_flush_interval: float = 0.1,
    ack_batch_size: int = 8,
    received_seqs_window: int = 256,
    reorder_buffer_ttl: float = 500.0,
    # Logging
    logger=None,
) -> AggregatingLink
```

> **Renamed in 3.0.0** — `get_transport` from earlier versions is now `get_link`.

### Parameters

**Encryption**

- `encryption_mode` — `NONE` (default), `CHACHA20_POLY1305`, `AES_GCM`, or `AES_EAX`
- `encryption_key` — shared secret (required when `encryption_mode != NONE`)

**Aggregating**

- `flush_interval` — max time to wait before flushing a batch
- `max_batch_size` — max logical batch size in bytes before chunking
- `chunk_assembly_ttl` — lifetime of an incomplete chunk assembly

**Reliability**

- `reliability_mode` — ARQ mode for regular messages: `NONE`, `STOP_AND_WAIT`, or `PARALLEL`
- `image_reliability_mode` — ARQ mode for image frames (Stasis); tuned independently from regular traffic
- `window_size` — send window for `PARALLEL` mode
- `ack_flush_interval`, `ack_batch_size` — ACK pacing and batching
- `received_seqs_window` — deduplication memory (number of recent sequence numbers)
- `reorder_buffer_ttl` — how long to hold out-of-order packets while reordering

**Logging**

- `logger` — optional standard library `Logger` for debugging and observability

---

## Server creation: `AethernetServer.create`

`AethernetServer.create` accepts the same parameters as `get_link` and additionally configures the HTTP router and SSE flushing:

```python
@classmethod
async def create(
    cls,
    low_transport: LowTransport,
    *,
    # same as get_link ...
    # Server router
    http_client=...,
    proxy_http_client=...,
    # SSE streaming
    sse_flush_bytes: int = 65536,
    sse_flush_interval: float = 0.5,
    logger=...,
) -> AethernetServer
```

Extra parameters:

- `http_client` — the `httpx.AsyncClient` used for outgoing requests
- `proxy_http_client` — optional separate client for proxied requests
- `sse_flush_bytes`, `sse_flush_interval` — flushing behaviour for SSE streaming responses

---

## Enums

```python
from aethernet import ReliabilityMode, EncryptionMode

ReliabilityMode.NONE
ReliabilityMode.STOP_AND_WAIT
ReliabilityMode.PARALLEL

EncryptionMode.NONE
EncryptionMode.CHACHA20_POLY1305
EncryptionMode.AES_GCM
EncryptionMode.AES_EAX
```

---

## Project structure

```
aethernet-core/
├── .github/
├── src/
│   └── aethernet/
│       ├── low_transports/
│       │   └── aethernet_tcp_transport/
│       │       ├── low_transport.py   # TCPLowTransport, TCPLowTransportServer
│       │       └── aethernet_link.py  # ready-to-use link file for athnet
│       ├── transport/
│       ├── __init__.py
│       ├── cli.py
│       ├── exceptions.py
│       ├── server_router.py
│       └── typing.py
├── tests/
├── aethernet_link.example.py
├── pyproject.toml
├── .gitignore
├── LICENSE
├── NOTICE
├── uv.lock
├── README.ru.md
└── README.md
```

---

## Migration from 2.x

| 2.x | 3.0.0 |
|-----|-------|
| `get_transport(...)` | `get_link(...)` |
| `LowTransport` (no generic) | `LowTransport[str]` / `LowTransport[bytes]` / `LowTransport[StrAndImages]` / `LowTransport[BytesAndImages]` |
| No image support | `supports_images=True` + `BytesAndImages` / `StrAndImages` |
| No built-in TCP transport | `TCPLowTransport`, `TCPLowTransportServer` |
| No CLI | `athnet` command + `aethernet_link.py` |
| No Stasis | Remote-access protocol, client at [solment-slet/stasis-client](https://github.com/solment-slet/stasis-client) |

---

## Development

```bash
uv sync --extra dev
pytest

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

1. Expected behaviour
2. Actual behaviour
3. Minimal reproduction

---

## License

[Apache License 2.0](LICENSE)

---

## Author

**Walter Kerrigan (esolment)** — https://github.com/esolment