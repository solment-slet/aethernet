# Aethernet (Æthernet)

**Полноценный HTTP и WebSocket поверх любого транспорта с сообщениями — строкового *или* байтового.**

Aethernet — лёгкая библиотека-туннель, позволяющая продолжать пользоваться привычными API (`httpx` и WebSocket‑клиентом), даже если у вас **нет прямого TCP-доступа в интернет**. Весь трафик кадрируется, при необходимости шифруется, батчится и передаётся через ваш `LowTransport` (Telegram/SMS/serial/BLE/радио/сокет и т.п.).

---

## Зачем Aethernet?

В некоторых средах можно отправлять/получать *сообщения*, но нельзя установить прямое соединение с интернетом: Telegram-боты, SMS‑шлюзы, air‑gapped системы, встраиваемые устройства, BLE‑каналы и т.д. Aethernet закрывает этот разрыв: ваш код остаётся с привычными HTTP/WebSocket вызовами, а реальные байты уезжают по любому доступному каналу.

---

## Возможности

- **Drop-in транспорт для HTTP** — работает с `httpx.AsyncClient(transport=...)`
- **WebSocket поддержка** — через `AethernetWebSockets`
- **Нижний транспорт строковый или байтовый**
  - `mode="bytes"` (по умолчанию) для бинарных каналов
  - `mode="string"` для мессенджеров/SMS (алфавит, ограничения по длине)
- **Сквозное шифрование (опционально)**, AEAD-режимы:
  - `CHACHA20_POLY1305`
  - `AES_GCM`
  - `AES_EAX`
- **Надёжная доставка / Retry (опционально)** (ARQ)
  - `NONE` — best-effort
  - `STOP_AND_WAIT` — классический ARQ
  - `PARALLEL` — selective-repeat с окном
- **Агрегация и flow-control** — батчи и чанки для мелких сообщений
- **Свой транспорт за пару методов** — реализуйте `LowTransport`

---

## Установка

```bash
# pip
pip install aethernet-core

# uv
uv add aethernet-core
```

---

## Публичный API (что импортировать)

Всё ниже доступно из корня модуля:

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

## Базовая идея: `LowTransport`

`LowTransport` — единственное, что вы реализуете. Он должен уметь:

- `send(data: str | bytes) -> None`
- `recv() -> str | bytes`

### Конфигурация через `CONFIG` (важно)

Ограничения и тайминги задаются dataclass-ом `LowTransportConfig` и обычно хранятся в **атрибуте класса** `CONFIG`.

Критичные моменты:

1. Можно оставить `CONFIG` по умолчанию, но рекомендуется явно задавать его под ваш канал.
2. Каждый реализатор **обязан**:
   - принимать `config: LowTransportConfig | None = None` в `__init__`
   - вызывать `super().__init__(config)` внутри `__init__`

Это нужно, чтобы пользователь мог точечно менять параметры через `dataclasses.replace(...)` и передавать новый конфиг в ваш транспорт.

---

## Быстрый старт

У Aethernet две стороны:

- **Клиент** — делает HTTP/WebSocket запросы
- **Сервер** — запускается там, где есть интернет, и выполняет запросы за клиента

Обе стороны должны использовать:
- одинаковый `LowTransport` (или совместимые реализации)
- одинаковый `encryption_mode`
- одинаковый `encryption_key` (если включено шифрование)
- одинаковый `reliability_mode`

### 1) Определите транспорт (пример для bytes-mode)

```python
from aethernet import LowTransport, LowTransportConfig


class MyTransport(LowTransport):
    # Рекомендуется: задайте ограничения/тайминги здесь
    CONFIG = LowTransportConfig(
        mode="bytes",
        max_message_bytes=1024,
        min_send_interval=0.2,
        min_recv_interval=0.2,
        delay_before_resending=8.0,  # используется слоем надёжности
    )

    def __init__(self, *, config: LowTransportConfig | None = None) -> None:
        super().__init__(config)
        # инициализация вашего канала (serial/socket/BLE/и т.д.)

    def close(self) -> None:
        # освобождение ресурсов (опционально)
        pass

    def send(self, data: bytes) -> None:
        # отправьте `data` через ваш канал
        raise NotImplementedError

    def recv(self) -> bytes:
        # верните следующее входящее сообщение
        raise NotImplementedError
```

### 2) Клиентская сторона

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


SHARED_KEY = secrets.token_bytes(32)  # сгенерируйте один раз и передайте второй стороне


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

### 3) Серверная сторона

Запускайте там, где **есть интернет** и где ваш `LowTransport` может обмениваться сообщениями с клиентом.

```python
import asyncio
from aethernet import AethernetServer, EncryptionMode, ReliabilityMode


SHARED_KEY = b"..."  # должен в точности совпадать с ключом клиента


async def main() -> None:
    server = await AethernetServer.create(
        MyTransport(),
        encryption_mode=EncryptionMode.CHACHA20_POLY1305,
        encryption_key=SHARED_KEY,
        reliability_mode=ReliabilityMode.PARALLEL,
        window_size=8,
    )
    print("Aethernet server запущен...")
    await server.start_and_wait()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Строковый транспорт (Telegram/SMS и т.п.)

Если канал принимает только текст — используйте `mode="string"`, задайте `alphabet` (разрешённые символы) и лимиты.

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

    def send(self, data: str) -> None:
        # В string-mode вы обычно отправляете `str`
        # (стек сам сформирует корректный payload под выбранный режим).
        raise NotImplementedError

    def recv(self) -> str:
        raise NotImplementedError
```

---

## Как пользователю менять лимиты: `dataclasses.replace`

Поскольку конфиг — dataclass, пользователь может переопределить конфигурацию:

```python
from dataclasses import replace

cfg = replace(MyTransport.CONFIG, min_send_interval=0.6, max_message_bytes=2048)
low = MyTransport(config=cfg)
```

---

## Создание стека: `get_transport(...)`

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

### Обзор параметров

**Шифрование**
- `encryption_mode`
  - `EncryptionMode.NONE`
  - `EncryptionMode.CHACHA20_POLY1305`
  - `EncryptionMode.AES_GCM`
  - `EncryptionMode.AES_EAX`
- `encryption_key`: общий секретный ключ (на обеих сторонах одинаковый). Нужен, если `encryption_mode != NONE`.

**Агрегация (батчи/чанки)**
- `flush_interval`: максимальная задержка перед отправкой батча
- `max_batch_size`: максимальный логический размер батча (до чанкинга)
- `chunk_assembly_ttl`: сколько хранить незавершённые сборки чанков

**Надёжность (Retry / ARQ)**
- `reliability_mode`
  - `ReliabilityMode.NONE`
  - `ReliabilityMode.STOP_AND_WAIT`
  - `ReliabilityMode.PARALLEL` (использует `window_size`)
- `window_size`: размер окна отправки в `PARALLEL`
- `ack_flush_interval`, `ack_batch_size`: частота/батчинг ACK
- `received_seqs_window`: «память» для дедупликации
- `reorder_buffer_ttl`: сколько держать пакеты для переупорядочивания

**Логирование**
- `logger`: опциональный logger для отладки/наблюдаемости

---

## Создание сервера: `AethernetServer.create(...)`

`AethernetServer.create(...)` повторяет параметры `get_transport(...)` и добавляет конфигурацию роутера и SSE flush:

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

### Дополнительные параметры сервера

- `http_client`: `httpx.AsyncClient`, которым сервер делает реальные запросы в интернет
- `proxy_http_client`: отдельный клиент (например, для проксирования)
- `sse_flush_bytes`, `sse_flush_interval`: параметры flush для SSE-стриминга

---

## Enums

### Надёжность

```python
from aethernet import ReliabilityMode

ReliabilityMode.NONE
ReliabilityMode.STOP_AND_WAIT
ReliabilityMode.PARALLEL
```

### Шифрование

```python
from aethernet import EncryptionMode

EncryptionMode.NONE
EncryptionMode.CHACHA20_POLY1305
EncryptionMode.AES_GCM
EncryptionMode.AES_EAX
```

---

## Структура проекта

```
aethernet-core/
├── .github/
├── src/
│   └── aethernet/
│       ├── transport/
│       ├── __init__.py
│       ├── exceptions.py
│       ├── server_router.py
│       └── typing.py
├── tests/
├── pyproject.toml
├── .gitignore
├── LICENSE
├── NOTICE
├── uv.lock
├── README.ru.md
└── README.md
```

---

## Разработка

```bash
uv sync --extra dev
pytest

black --check .
ruff check .
mypy .
```

---

## Совместимость

| Python | Платформы |
|--------|-----------|
| 3.12 + | Linux, macOS, Windows |

---

## Баг-репорты

Откройте issue и приложите:

1. Ожидаемое поведение
2. Фактическое поведение
3. Минимальный пример для воспроизведения

---

## Лицензия

[Apache License 2.0](LICENSE)

---

## Автор

**Walter Kerrigan (esolment)** — https://github.com/esolment