from __future__ import annotations
from typing import Annotated, TYPE_CHECKING
import os
import logging

import basex
from Crypto.Cipher import ChaCha20_Poly1305

from aethernet.typing import LoggerLike

if TYPE_CHECKING:
    from aethernet.transport import LowTransport
    Bytes32 = Annotated[bytes, 32]


class MediumTransport:
    def __init__(
        self,
        transport: LowTransport,
        encryption_key: Bytes32,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self._logger = logger
        self._encryption_key = encryption_key
        self._basex = basex.init(alphabet=MediumTransport.make_alphabet())
        self.max_message_chars = 4096
        self.max_payload_bytes = self._calc_max_payload_bytes()
        self.low_transport = transport

        logger.debug(f"{self.max_message_chars=}\n{self.max_payload_bytes=}")

    def send(self, data: bytes) -> None:
        self.low_transport.send_message(self._encode(data))
        self._logger.debug("Отправлено сообщение")

    def recv(self) -> bytes:
        while True:
            try:
                text = self.low_transport.wait_for_message()
                if text is not None:
                    break
            except Exception:
                self._logger.exception("Ошибка при получении сообщения от `low_transport`")

        return self._decode(text)

    def _encode(self, data: bytes) -> str:
        nonce = os.urandom(12)
        cipher = ChaCha20_Poly1305.new(key=self._encryption_key, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(data)
        payload = nonce + tag + ciphertext
        return self._basex.encode(payload)

    def _decode(self, data: str) -> bytes:
        payload = self._basex.decode(data)
        nonce = payload[:12]
        tag = payload[12:28]
        ciphertext = payload[28:]

        cipher = ChaCha20_Poly1305.new(key=self._encryption_key, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag)

    def _calc_max_payload_bytes(self) -> int:
        lo, hi = 0, 16384
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(self._encode(b"\x00" * mid)) <= self.max_message_chars:
                lo = mid
            else:
                hi = mid - 1
        return lo

    @staticmethod
    def make_alphabet() -> str:
        ascii_safe = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789"
            "-_.~!$()*,:;@[]^{}|"
        )

        # Только реальные русские буквы
        cyrillic = (
            "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ" "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        )

        return ascii_safe + cyrillic
