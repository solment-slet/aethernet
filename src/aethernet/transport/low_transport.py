import uuid
from abc import ABC, ABCMeta, abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, get_args, get_origin

from PIL import Image

type StrAndImages = str | tuple[Image.Image, uuid.UUID]
type BytesAndImages = bytes | tuple[Image.Image, uuid.UUID]

_BASE_CLASS = None

TransportData = TypeVar(
    'TransportData',
    str,
    bytes,
    StrAndImages,
    BytesAndImages,
)

@dataclass(slots=True)
class LowTransportConfig:
    mode: Literal["string", "bytes"] = "bytes"
    supports_images: bool = False  # required for Aethernet Stasis
    max_message_bytes: int = 1024  # for string mode, calculated automatically
    max_message_chars: int | None = None  # for string mode
    alphabet: str | None = None  # chars for encoding in string mode
    # Timings
    min_send_interval: float = 0.2
    min_recv_interval: float = 0.2
    delay_before_resending: float = 8.0  # for reliable mode


class LowTransportMeta(ABCMeta):
    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any) -> Any:
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        global _BASE_CLASS
        if _BASE_CLASS is None:
            _BASE_CLASS = cls
            return cls

        if cls is _BASE_CLASS:
            return cls

        # Extract the type passed to the Generic parameter of the subclass
        generic_type = None
        for base in cls.__orig_bases__:  # type: ignore
            if get_origin(base) is LowTransport:
                args = get_args(base)
                if args:
                    generic_type = args[0]
                break

        # Validation 1: Forbid passing the raw TypeVar 'TransportData' itself
        if isinstance(generic_type, TypeVar) and generic_type.__name__ == 'TransportData':
            raise TypeError(
                f"Class {name} cannot use the raw TypeVar 'TransportData'. "
                f"You must specify a concrete type, e.g. LowTransport[str] or LowTransport[str_and_images]."
            )

        # Extract the class configuration for business-logic checks
        cfg_source = namespace.get("CONFIG", getattr(cls, "CONFIG", LowTransportConfig))
        config = cfg_source() if isinstance(cfg_source, type) else cfg_source

        # Helper to normalise Union types into a set for order-independent comparison
        def _to_set(t: Any) -> set[Any]:
            return set(get_args(t)) if get_origin(t) is Literal or get_origin(t) is type(str | int) else {t}

        if config.supports_images:
            allowed_image_types = [StrAndImages, BytesAndImages]
            # Normalise the provided type into a set so that Union order (A | B vs B | A) does not break the check
            actual_set = _to_set(generic_type)

            is_valid = any(actual_set == _to_set(expected) for expected in allowed_image_types)

            # Validation 2: If supports_images=True, require strictly str_and_images or bytes_and_images
            if not is_valid:
                raise TypeError(
                    f"Critical error in {name}: because `supports_images=True`, "
                    f"the class must be able to handle image tuples. "
                    f"Use the Generic type `str_and_images` or `bytes_and_images`."
                )

        return cls


class LowTransport(Generic[TransportData], ABC, metaclass=LowTransportMeta):
    """
    Abstract base class for low-level text or byte message transport.

    Defines the interface for sending and receiving text or byte messages
    over an underlying channel (e.g. serial port, socket, BLE, etc.).

    Subclasses must implement: __init__, send, recv
    optional: close, CONFIG and any of your other methods and attributes.

    Constraints enforced by the transport layer:
    - Outgoing messages must not exceed `max_message_chars` characters.
    - Outgoing messages must consist solely of characters in `alphabet` (in string mode).
    - Calls to send() must be spaced at least `min_send_interval` seconds apart.
    - Calls to recv() must be spaced at least `min_recv_interval` seconds apart.
    """

    CONFIG: LowTransportConfig | type[LowTransportConfig] = LowTransportConfig

    # ------------------------------------------------------------------ #
    #  Initialization                                                     #
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
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Release all resources held by the transport (connections, file handles, etc.)."""

    # ------------------------------------------------------------------ #
    #  I/O                                                                #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def send(self, data: TransportData) -> None:
        """
        Send a message or a screen frame over the transport.

        In text/bytes mode, `data` must be a str or bytes satisfying all
        constraints (len <= max_message_chars / max_message_bytes, alphabet).
        The caller guarantees this; implementations do not need to validate.

        In image mode (only when `config.supports_images` is True), `data`
        is a tuple of (image, stream_id). The implementation is responsible
        for encoding the PIL Image into whatever wire format it uses (raw
        bytes, base64, .png/.jpg file, etc.) and associating the stream_id
        with the frame so the receiver can return it from recv().

        Args:
            data: Either a str/bytes message, or a (Image.Image, uuid.UUID)
                tuple representing a screen frame and its stream identifier.
        """

    @abstractmethod
    def recv(self) -> TransportData:
        """
        Receive a message or a screen frame from the transport.

        Returns either a str/bytes message, or a (Image.Image, uuid.UUID)
        tuple if the transport received a screen frame. Image frames are only
        possible when `config.supports_images` is True.

        The returned PIL Image must have the same pixel dimensions as the
        original sent by the server — the caller relies on these dimensions
        to convert normalized input-event coordinates (0.0–1.0) back to
        absolute server pixel coordinates.

        The implementation must block until a message or frame is available,
        consistent with `min_recv_interval` timing constraints.

        Returns:
            A str or bytes message, or a tuple of (Image.Image, uuid.UUID)
            for screen frames.
        """
