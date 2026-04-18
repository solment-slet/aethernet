# Aethernet (Æthernet)

**Full HTTP and WebSocket traffic over any text-based transport — messengers, SMS, or anything else that can carry a string.**

Aethernet is a lightweight tunneling library that lets you use the familiar `httpx` and `websockets` APIs without a direct internet connection. All traffic is serialized, optionally encrypted, and forwarded through a custom transport that you implement in a few lines of code.

---

## Why Aethernet?

Some environments can send and receive text but cannot make direct TCP connections — think Telegram bots, SMS gateways, air-gapped systems, or embedded radios. Aethernet bridges that gap: your code keeps using standard HTTP and WebSocket clients while the actual bytes travel over whatever channel you have available.

---

## Features

- **Drop-in HTTP client** — fully compatible `httpx.AsyncClient` transport
- **WebSocket support** — API modeled on `websockets 16.0`
- **End-to-end encryption** — AEAD_CHACHA20_POLY1305, key is shared out-of-band
- **Configurable constraints** — message size, character set, and rate limits match your transport's capabilities
- **Bring your own transport** — implement one abstract class and you're done

---

## Installation

```bash
# pip
pip install aethernet-core

# uv
uv add aethernet-core
```

---

## Quick Start

Aethernet has two sides: a **client** that issues HTTP/WebSocket requests, and a **server** that has real internet access and executes those requests on behalf of the client.

Both sides share the same `LowTransport` implementation and the same `encryption_key`.

### 1. Define Your Transport

A transport is any object that can send and receive short text strings — a Telegram message, an SMS, a serial port line, and so on. Implement the three abstract methods:

```python
from aethernet import LowTransport


class MyTransport(LowTransport):
    """
    Replace the bodies of send() and recv() with your real
    channel logic (Telegram API, SMS gateway, etc.).
    """

    def __init__(self) -> None:
        super().__init__()
        # Tune these to match your channel's actual limits:
        self.max_message_chars = 1000
        self.alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        self.min_send_interval = 0.2   # seconds between sends
        self.min_recv_interval = 0.2   # seconds between reads

    def close(self) -> None:
        pass  # release connections, file handles, etc.

    def send(self, text: str) -> None:
        pass  # deliver `text` through your channel

    def recv(self, timeout: float | None = None) -> str:
        pass  # return the next incoming message
```

### 2. Client Side

```python
import asyncio
import secrets
import httpx
from aethernet import AethernetHttpx, AethernetWebSockets, get_transport


SHARED_KEY = secrets.token_bytes(32)  # generate once, share securely


async def main() -> None:
    transport = await get_transport(MyTransport(), encryption_key=SHARED_KEY)

    # --- HTTP ---
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://httpbin.org/get")
        print(response.status_code, response.json())

    # --- WebSocket ---
    async with AethernetWebSockets(transport).connect("wss://echo.websocket.events") as ws:
        await ws.send("hello")
        message = await ws.recv()
        print("Echo:", message)


if __name__ == "__main__":
    asyncio.run(main())
```

### 3. Server Side

Run this on a machine that **does** have internet access and can receive messages from the client's transport.

```python
import asyncio
import secrets
from aethernet import AethernetServer


SHARED_KEY = b"..."  # must match the client's key exactly


async def main() -> None:
    server = await AethernetServer.create(
        MyTransport(),
        encryption_key=SHARED_KEY,
    )
    print("Aethernet server is running...")
    await server.start_and_wait()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Project Structure

```
aethernet-core/
├── .github/
│   └── workflows/
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
# Install all dependencies including dev extras
uv sync --extra dev

# Run the test suite
pytest

# Linting, formatting, and type checking
black --check .
ruff check .
mypy .

# Run the full CI pipeline locally (requires act)
act -W .github/workflows/ci.yml
```

---

## Compatibility

| Python | Platforms |
|--------|-----------|
| 3.12 + | Linux, macOS, Windows |

Tested on Ubuntu with Python 3.12 and 3.13.

---

## Bug Reports

Please open an issue and include:

1. **Expected behavior** — what you intended to happen
2. **Actual behavior** — what happened instead
3. **Reproduction steps** — the minimal code or sequence of actions that triggers the problem

---

## License

[Apache License 2.0](LICENSE)

---

## Author

**Walter Kerrigan (esolment)** — [github.com/esolment](https://github.com/esolment)