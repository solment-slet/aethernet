from typing import Literal
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class LowTransportConfig:
    mode: Literal["string", "bytes"] = "bytes"
    max_message_bytes: int = 1024  # for string mode, calculated automatically
    max_message_chars: int | None = None  # for string mode
    alphabet: str | None = None  # chars for encoding in string mode
    # Timings
    min_send_interval: float = 0.2
    min_recv_interval: float = 0.2
    delay_before_resending: float = 8.0  # for reliable mode


class LowTransport(ABC):
    """
    Abstract base class for low-level text or byte message transport.

    Defines the interface for sending and receiving text or byte messages
    over an underlying channel (e.g. serial port, socket, BLE, etc.).

    Subclasses must implement: send, recv
    optional: _setup, close, CONFIG_CLASS (attribute) and any of your other methods.

    Constraints enforced by the transport layer:
    - Outgoing messages must not exceed `max_message_chars` characters.
    - Outgoing messages must consist solely of characters in `alphabet` (in string mode).
    - Calls to send() must be spaced at least `min_send_interval` seconds apart.
    - Calls to recv() must be spaced at least `min_recv_interval` seconds apart.
    """

    CONFIG: LowTransportConfig | type[LowTransportConfig] = LowTransportConfig

    # ------------------------------------------------------------------ #
    #  Initialization                                                    #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        config: LowTransportConfig | None = None,
    ) -> None:
        """
        Subclass example:

            def __init__(self, port: str, config: LowTransportConfig | None = None):
                super().__init__(config)
                self.port = port
        """
        if config is None:
            cfg = self.CONFIG
            config = cfg() if isinstance(cfg, type) else cfg
        self.config = config

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                         #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Release all resources held by the transport (connections, file handles, etc.)."""

    # ------------------------------------------------------------------ #
    #  I/O                                                               #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def send(self, data: str | bytes) -> None:
        """
        Send a text or byte message over the transport.

        The caller guarantees that *data* satisfies all constraints
        (len(data) <= max_message_chars or max_message_bytes),
        so implementations do not need to validate the input.

        Args:
            data: The data to send.
        """

    @abstractmethod
    def recv(self) -> str | bytes:
        """
        Receive a text or byte message from the transport.

        Returns:
            The received message as a string or bytes.
        """
