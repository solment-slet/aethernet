from __future__ import annotations

import json
from typing import Any


def encode_json_bytes(obj: dict[str, Any]) -> bytes:
    """Сериализует dict в компактный UTF-8 JSON."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_json_bytes(data: bytes) -> dict[str, Any]:
    """Десериализует UTF-8 JSON bytes в dict."""
    return json.loads(data.decode("utf-8"))
