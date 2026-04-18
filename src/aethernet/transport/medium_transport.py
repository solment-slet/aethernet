from __future__ import annotations
from typing import TYPE_CHECKING
import os
import logging
import time

import basex
from Crypto.Cipher import ChaCha20_Poly1305

from aethernet.exceptions import TransportClosedError

if TYPE_CHECKING:
    from aethernet.transport import LowTransport
    from aethernet.typing import LoggerLike, Bytes32


class MediumTransport:
    def __init__(
        self,
        transport: LowTransport,
        encryption_key: Bytes32,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        self.low_transport = transport
        self._logger = logger
        self._encryption_key = encryption_key
        self._basex = basex.init(alphabet=self.low_transport.alphabet)
        self.max_message_chars = self.low_transport.max_message_chars
        self.max_payload_bytes = (
            self._calc_max_payload_bytes()
        )  # рассчитываем максимум байтов на отправку за раз

        logger.debug(f"{self.max_message_chars=}\n{self.max_payload_bytes=}")

    def send(self, data: bytes) -> None:
        self._logger.debug("Отправляем сообщение")
        self.low_transport.send(self._encode(data))
        self._logger.debug("Отправлено сообщение")

    def recv(self, recv_restart_delay: float = 0.01) -> bytes:
        while True:
            try:
                text = self.low_transport.recv()
                if text is not None:
                    break
            except TransportClosedError as e:
                raise e
            except Exception:
                self._logger.exception(
                    "Ошибка при получении сообщения от `low_transport`"
                )
                time.sleep(recv_restart_delay)

        self._logger.debug(f"Поучено сообщение: {text}")

        return self._decode(text)

    def _encode_for_size(self, data: bytes) -> str:
        """
        Детерминированная версия для расчёта размера.
        Использует payload из 0xFF, чтобы получить максимально возможную
        длину basex-представления для данного размера.
        """
        payload_len = 12 + 16 + len(data)
        payload = b"\xff" * payload_len
        return self._basex.encode(payload)

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
            if len(self._encode_for_size(b"\x00" * mid)) <= self.max_message_chars:
                lo = mid
            else:
                hi = mid - 1
        return lo
