from __future__ import annotations
from typing import TYPE_CHECKING
import os

import basex
from Crypto.Cipher import ChaCha20_Poly1305

from logger import logger
from config import Config

if TYPE_CHECKING:
    from transport import LowTransport


class Transport:
    def __init__(
        self,
        config: Config,
        transport: LowTransport,
    ) -> None:
        self.cfg = config
        self._basex = basex.init(alphabet=Transport.make_alphabet())
        self.max_message_chars = 4096
        self.max_payload_bytes = self._calc_max_payload_bytes()
        self.low_transport = transport

        logger.debug(f"{self.max_message_chars=}\n{self.max_payload_bytes=}")

    def send(self, data: bytes) -> None:
        self.low_transport.send_message(self._encode(data))
        logger.info("Отправлено сообщение!")

    def recv(self) -> bytes:
        while True:
            try:
                text = self.low_transport.wait_for_message()
                if text is not None:
                    break
            except Exception:
                logger.exception("Ошибка при получении сообщения от `low_transport`")

        return self._decode(text)

    def _encode(self, data: bytes) -> str:
        nonce = os.urandom(12)
        cipher = ChaCha20_Poly1305.new(key=self.cfg.encryption_key, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(data)
        payload = nonce + tag + ciphertext
        return self._basex.encode(payload)

    def _decode(self, data: str) -> bytes:
        payload = self._basex.decode(data)
        nonce = payload[:12]
        tag = payload[12:28]
        ciphertext = payload[28:]

        cipher = ChaCha20_Poly1305.new(key=self.cfg.encryption_key, nonce=nonce)
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
