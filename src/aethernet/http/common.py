from __future__ import annotations

from dataclasses import dataclass

import httpx

PROTOCOL_NAME = "http"

# =========================
# Общие сериализаторы
# =========================


def headers_to_list(
    headers: httpx.Headers | list[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, httpx.Headers):
        return list(headers.multi_items())
    return list(headers)


# =========================
# Протокол поверх stream_id
# =========================
#
# frame_type = "meta"  -> JSON payload
# frame_type = "body"  -> raw bytes
#
# Для request:
#   meta: {
#       "kind": "request_start",
#       "method": "...",
#       "url": "...",
#       "headers": [[k,v], ...],
#       "has_body": bool,
#   }
#   body: ... bytes ... (если есть)
#   meta(end=True): {"kind": "request_end"}
#
# Служебный заголовок "Slet-Aethernet-Use-Proxy" в headers сигнализирует
# серверу использовать proxy_http_client вместо обычного http_client.
# Заголовок вырезается сервером перед отправкой апстриму.
#
# Для response non-stream:
#   meta: {
#       "kind": "response_start",
#       "status_code": 200,
#       "headers": [[k,v], ...],
#       "streaming": false
#   }
#   body: ... bytes ...
#   meta(end=True): {"kind": "response_end"}
#
# Для response stream:
#   meta: {
#       "kind": "response_start",
#       "status_code": 200,
#       "headers": [[k,v], ...],
#       "streaming": true
#   }
#   body: ... bytes chunk ...
#   body: ... bytes chunk ...
#   ...
#   meta(end=True): {"kind": "response_end"}
#
# Для errors:
#   meta(end=True): {
#       "kind": "error",
#       "message": "..."
#   }

USE_PROXY_HEADER = "slet-aethernet-use-proxy"


def split_proxy_header(
    headers: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], bool]:
    """Возвращает (headers без служебного заголовка, use_proxy)."""
    use_proxy = False
    clean_headers: list[tuple[str, str]] = []

    for k, v in headers:
        if k.lower() == USE_PROXY_HEADER:
            use_proxy = True
            continue
        clean_headers.append((k, v))

    return clean_headers, use_proxy


@dataclass(slots=True)
class ResponseStart:
    status_code: int
    headers: list[tuple[str, str]]
    streaming: bool
