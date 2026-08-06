from __future__ import annotations

import uuid
import os
import logging
import time

import basex
from PIL import Image
from Crypto.Cipher import ChaCha20_Poly1305, AES
from Crypto.Cipher._mode_gcm import GcmMode
from Crypto.Cipher._mode_eax import EaxMode
from Crypto.Cipher.ChaCha20_Poly1305 import ChaCha20Poly1305Cipher

from aethernet.transport.enums import EncryptionMode
from aethernet.exceptions import TransportClosedError
from aethernet.transport.low_transport import LowTransport
from aethernet.typing import LoggerLike


class MediumTransport:
    """
    Middle transport layer:
    - encryption (optional)
    - basex encoding
    - maximum payload size calculation

    Guarantee:
        For any payload of length <= max_payload_bytes
        len(encoded) <= max_message_chars
    """

    # Nonce/tag sizes for different algorithms
    _CHACHA_NONCE = 12
    _CHACHA_TAG = 16

    _AES_GCM_NONCE = 12
    _AES_GCM_TAG = 16

    _AES_EAX_NONCE = 16
    _AES_EAX_TAG = 16

    _OVERHEAD: dict[EncryptionMode, tuple[int, int]] = {
        EncryptionMode.CHACHA20_POLY1305: (_CHACHA_NONCE, _CHACHA_TAG),
        EncryptionMode.AES_GCM: (_AES_GCM_NONCE, _AES_GCM_TAG),
        EncryptionMode.AES_EAX: (_AES_EAX_NONCE, _AES_EAX_TAG),
    }

    def __init__(
        self,
        transport: LowTransport,
        encryption_mode: EncryptionMode = EncryptionMode.NONE,
        encryption_key: bytes | None = None,
        logger: LoggerLike = logging.getLogger(__name__),
    ) -> None:
        MediumTransport._validate_key(encryption_key, encryption_mode)
        self.low_transport = transport
        self.config = self.low_transport.config
        self._logger = logger
        self._mode = self.config.mode
        self._encryption_mode = encryption_mode
        self._key: bytes = encryption_key if encryption_key is not None else b""

        self._basex = basex.init(alphabet=self.config.alphabet)
        self.max_message_chars = self.config.max_message_chars

        if self._mode == "string" and self.config.max_message_bytes is None:
            self.max_payload_bytes = self._calc_max_payload_bytes()
        elif self.config.max_message_bytes is not None:
            self.max_payload_bytes = self.config.max_message_bytes
        else:
            raise ValueError(
                "max_message_bytes must be set in config when mode is not 'string'"
            )

        logger.debug(
            f"{self._mode=}\n"
            f"{self.max_payload_bytes=}\n"
            f"{self._encryption_mode=}"
        )

    # ============================================================
    # Public API
    # ============================================================

    def send(self, data: bytes) -> None:
        encoded = self._encode(data)
        self.low_transport.send(encoded)

    def send_image(self, image: Image.Image, stream_id: uuid.UUID) -> None:
        self.low_transport.send((image, stream_id))

    def recv(self, recv_restart_delay: float = 0.01) -> bytes | tuple[Image.Image, uuid.UUID]:
        while True:
            try:
                data = self.low_transport.recv()
                # ── image tuple ──────────────────────────────────────────────
                if (
                        isinstance(data, tuple)
                        and len(data) == 2
                        and isinstance(data[0], Image.Image)
                        and isinstance(data[1], uuid.UUID)
                ):
                    return data  # (Image.Image, uuid.UUID) — пробрасываем как есть
                # ── bytes / str ──────────────────────────────────────────────
                if isinstance(data, (str, bytes)):
                    break
                self._logger.warning(
                    f"Invalid type {type(data).__name__!r} received from low_transport.recv"
                )
                continue
            except TransportClosedError:
                raise
            except Exception:
                self._logger.exception("Error receiving message from low_transport")
                time.sleep(recv_restart_delay)

        return self._decode(data)  # type: ignore[arg-type]

    # ============================================================
    # Encryption
    # ============================================================

    def _make_cipher(self, nonce: bytes) -> ChaCha20Poly1305Cipher | GcmMode | EaxMode:
        """Creates a cipher object for the current encryption mode."""
        if self._encryption_mode == EncryptionMode.CHACHA20_POLY1305:
            return ChaCha20_Poly1305.new(key=self._key, nonce=nonce)
        if self._encryption_mode == EncryptionMode.AES_GCM:
            return AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        if self._encryption_mode == EncryptionMode.AES_EAX:
            return AES.new(self._key, AES.MODE_EAX, nonce=nonce)
        raise ValueError("Unsupported encryption mode")

    def _encryption_overhead(self) -> int:
        if self._encryption_mode == EncryptionMode.NONE:
            return 0
        nonce_size, tag_size = self._OVERHEAD[self._encryption_mode]
        return nonce_size + tag_size

    def _encode(self, data: bytes) -> str | bytes:
        if self._encryption_mode == EncryptionMode.NONE:
            payload = data

        else:
            cipher: ChaCha20Poly1305Cipher | GcmMode | EaxMode = self._make_cipher(
                nonce := os.urandom(self._OVERHEAD[self._encryption_mode][0])
            )
            ciphertext, tag = cipher.encrypt_and_digest(data)
            payload = nonce + tag + ciphertext

        if self._mode == "string":
            return self._basex.encode(payload)

        return payload

    def _decode(self, data: str | bytes) -> bytes:
        payload = self._basex.decode(data) if isinstance(data, str) else data

        if self._encryption_mode == EncryptionMode.NONE:
            return payload

        nonce_size, tag_size = self._OVERHEAD[self._encryption_mode]
        nonce = payload[:nonce_size]
        tag = payload[nonce_size : nonce_size + tag_size]
        ciphertext = payload[nonce_size + tag_size :]

        return self._make_cipher(nonce).decrypt_and_verify(ciphertext, tag)

    # ============================================================
    # Size calculation (mathematically correct)
    # ============================================================

    def _encode_for_size(self, data_len: int) -> int:
        """
        Returns the basex-encoded string length for a worst-case payload.
        """
        payload_len = self._encryption_overhead() + data_len
        return max(
            len(self._basex.encode(b"\x00" * payload_len)),
            len(self._basex.encode(b"\xff" * payload_len)),
        )

    def _calc_max_payload_bytes(self) -> int:
        """
        Finds the maximum payload that is guaranteed to fit
        within max_message_chars (tight bound).
        """
        if self.max_message_chars is None:
            raise ValueError(
                "max_message_chars must be set when max_message_bytes is None"
            )

        C = self.max_message_chars

        low = 0
        high = C  # rough upper bound (definitely not larger)

        while low < high:
            mid = (low + high + 1) // 2

            if self._encode_for_size(mid) <= C:
                low = mid
            else:
                high = mid - 1

        return low

    # ============================================================
    # Validation
    # ============================================================

    @staticmethod
    def _validate_key(key: bytes | None, mode: EncryptionMode) -> None:
        if mode == EncryptionMode.NONE:
            return
        if not key:
            raise ValueError("encryption_key required for selected encryption_mode")
        if mode == EncryptionMode.CHACHA20_POLY1305 and len(key) != 32:
            raise ValueError("ChaCha20-Poly1305 requires 32-byte key")
        if mode in (EncryptionMode.AES_GCM, EncryptionMode.AES_EAX) and len(
            key
        ) not in (16, 24, 32):
            raise ValueError("AES key must be 16, 24, or 32 bytes")