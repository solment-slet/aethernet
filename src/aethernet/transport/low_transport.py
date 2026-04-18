from abc import ABC, abstractmethod


class LowTransport(ABC):
    """
    Abstract base class for low-level text message transport.

    Defines the interface for sending and receiving text messages
    over an underlying channel (e.g. serial port, socket, BLE, etc.).

    Subclasses must implement: __init__, close, send, recv.

    Constraints enforced by the transport layer:
    - Outgoing messages must not exceed `max_message_chars` characters.
    - Outgoing messages must consist solely of characters in `alphabet`.
    - Calls to send() must be spaced at least `min_send_interval` seconds apart.
    - Calls to recv() must be spaced at least `min_recv_interval` seconds apart.
    """

    @abstractmethod
    def __init__(self) -> None:
        """
        Initialize transport configuration.

        Subclasses must call super().__init__() and may override
        any of the attributes below to suit their channel.
        """
        # Maximum number of characters allowed in a single outgoing message.
        self.max_message_chars: int = 1000

        # Characters permitted in outgoing messages (Base64 alphabet + '=').
        self.alphabet: str = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        )

        # Minimum time (seconds) that must elapse between consecutive send() calls.
        self.min_send_interval: float = 0.2

        # Minimum time (seconds) that must elapse between consecutive recv() calls.
        self.min_recv_interval: float = 0.2

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                         #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def close(self) -> None:
        """Release all resources held by the transport (connections, file handles, etc.)."""

    # ------------------------------------------------------------------ #
    #  I/O                                                               #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def send(self, text: str) -> None:
        """
        Send a text message over the transport.

        The caller guarantees that *text* satisfies all constraints
        (length <= max_message_chars, characters within alphabet),
        so implementations do not need to validate the input.

        Args:
            text: The message to send.
        """

    @abstractmethod
    def recv(self) -> str:
        """
        Receive a text message from the transport.

        Returns:
            The received message as a string.
        """
