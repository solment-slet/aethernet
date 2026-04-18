# Aethernet (Æthernet)

**Полноценный HTTP и WebSocket трафик поверх любого текстового транспорта — мессенджеров, SMS или любого другого канала, способного передавать строки.**

Aethernet — это легковесная библиотека-туннель, которая позволяет использовать привычные API `httpx` и `websockets` там, где прямого интернет-соединения нет. Весь трафик сериализуется, при необходимости шифруется и передаётся через транспорт, который вы реализуете в несколько строк кода.

---

## Зачем это нужно?

Некоторые среды умеют отправлять и получать текст, но не могут устанавливать прямые TCP-соединения — Telegram-боты, SMS-шлюзы, изолированные системы, встроенные радиомодули. Aethernet закрывает этот пробел: ваш код продолжает использовать стандартные HTTP и WebSocket клиенты, а байты путешествуют по любому доступному каналу.

---

## Возможности

- **Drop-in HTTP клиент** — полностью совместимый транспорт для `httpx.AsyncClient`
- **Поддержка WebSocket** — API по образцу `websockets 16.0`
- **Сквозное шифрование** — AEAD_CHACHA20_POLY1305, ключ передаётся вне канала
- **Настраиваемые ограничения** — размер сообщений, допустимые символы и интервалы отправки подстраиваются под возможности вашего транспорта
- **Собственный транспорт** — достаточно реализовать один абстрактный класс

---

## Установка

```bash
# pip
pip install aethernet-core

# uv
uv add aethernet-core
```

---

## Быстрый старт

У Aethernet две стороны: **клиент**, который отправляет HTTP/WebSocket-запросы, и **сервер**, у которого есть выход в интернет и который выполняет эти запросы от имени клиента.

Обе стороны используют одну и ту же реализацию `LowTransport` и один и тот же `encryption_key`.

### 1. Реализация транспорта

Транспорт — это любой объект, умеющий отправлять и получать короткие текстовые строки: сообщение в Telegram, SMS, строка из последовательного порта и так далее. Реализуйте три абстрактных метода:

```python
from aethernet import LowTransport


class MyTransport(LowTransport):
    """
    Замените тела методов send() и recv() реальной логикой
    вашего канала (Telegram API, SMS-шлюз и т.д.).
    """

    def __init__(self) -> None:
        super().__init__()
        # Настройте под реальные лимиты вашего канала:
        self.max_message_chars = 1000
        self.alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        self.min_send_interval = 0.2   # секунд между отправками
        self.min_recv_interval = 0.2   # секунд между чтениями

    def close(self) -> None:
        pass  # освободить соединения, дескрипторы и т.д.

    def send(self, text: str) -> None:
        pass  # доставить `text` через ваш канал

    def recv(self, timeout: float | None = None) -> str:
        pass  # вернуть следующее входящее сообщение
```

### 2. Клиент

```python
import asyncio
import secrets
import httpx
from aethernet import AethernetHttpx, AethernetWebSockets, get_transport


SHARED_KEY = secrets.token_bytes(32)  # сгенерировать один раз, передать безопасно


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
        print("Эхо:", message)


if __name__ == "__main__":
    asyncio.run(main())
```

### 3. Сервер

Запускается на машине, у которой **есть** выход в интернет и которая может получать сообщения от клиентского транспорта.

```python
import asyncio
from aethernet import AethernetServer


SHARED_KEY = b"..."  # должен точно совпадать с ключом клиента


async def main() -> None:
    server = await AethernetServer.create(
        MyTransport(),
        encryption_key=SHARED_KEY,
    )
    print("Сервер Aethernet запущен...")
    await server.start_and_wait()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Структура проекта

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

## Разработка

```bash
# Установить все зависимости, включая dev
uv sync --extra dev

# Запустить тесты
pytest

# Линтинг, форматирование и типизация
black --check .
ruff check .
mypy .

# Запустить CI-пайплайн локально (требуется act)
act -W .github/workflows/ci.yml
```

---

## Совместимость

| Python | Платформы |
|--------|-----------|
| 3.12 + | Linux, macOS, Windows |

Протестировано на Ubuntu с Python 3.12 и 3.13.

---

## Сообщить об ошибке

Пожалуйста, создайте issue и укажите:

1. **Ожидаемое поведение** — что должно было произойти
2. **Фактическое поведение** — что произошло на самом деле
3. **Шаги воспроизведения** — минимальный код или последовательность действий, приводящая к проблеме

---

## Лицензия

[Apache License 2.0](LICENSE)

---

## Автор

**Walter Kerrigan (esolment)** — [github.com/esolment](https://github.com/esolment)