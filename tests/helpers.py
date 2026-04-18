import json

from unittest.mock import MagicMock

# ===================================================================
# Тестовые хелперы
# ===================================================================


def meta_frame(kind: str, **kwargs) -> MagicMock:
    """
    Создаёт фейковый meta-фрейм для тестов.
    Используется как в тестах клиента, так и сервера.
    """
    payload = json.dumps(
        {"kind": kind, **kwargs},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return MagicMock(frame_type="meta", payload=payload)


def make_frame(frame_type: str, payload: bytes) -> MagicMock:
    frame = MagicMock()
    frame.frame_type = frame_type
    frame.payload = payload
    return frame


def make_frame_with_end(
    frame_type: str, payload: bytes, end: bool = False
) -> MagicMock:
    frame = MagicMock()
    frame.frame_type = frame_type
    frame.payload = payload
    frame.end = end
    return frame


def meta_payload(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode("utf-8")
