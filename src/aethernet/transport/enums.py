from enum import Enum


class ReliabilityMode(Enum):
    """Режим надёжности доставки физических пакетов."""

    NONE = "none"
    """Без подтверждений — поведение как в оригинале."""

    STOP_AND_WAIT = "stop_and_wait"
    """Классический Stop-and-Wait ARQ (window_size=1)."""

    PARALLEL = "parallel"
    """Selective-Repeat ARQ с окном window_size > 1."""


class EncryptionMode(str, Enum):
    NONE = "none"
    CHACHA20_POLY1305 = "chacha20_poly1305"
    AES_GCM = "aes_gcm"
    AES_EAX = "aes_eax"
