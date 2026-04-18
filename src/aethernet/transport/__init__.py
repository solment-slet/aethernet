from __future__ import annotations
from typing import TYPE_CHECKING

from aethernet.transport.high_transport import AggregatingLink, Frame
from aethernet.transport.medium_transport import MediumTransport
from aethernet.transport.low_transport import LowTransport

if TYPE_CHECKING:
    from aethernet.typing import LoggerLike, Bytes32


async def get_transport(
    low_transport: LowTransport,
    *,
    encryption_key: Bytes32,
    flush_interval: float = 0.5,
    max_batch_size: int = 64 * 1024,
    chunk_assembly_ttl: float = 60.0,
    logger: LoggerLike | None = None,
) -> AggregatingLink:
    """
    Creates a fully configured transport stack with low-, mock_medium_transport-, and high-level layers.

    The transport is composed of three layers:
    - LowTransport: communication layer
    - MediumTransport: encryption and buffering layer
    - AggregatingLink: batching and flow-control layer

    Args:
        low_transport: low-level transport.
        encryption_key: 32-byte encryption key used for securing mock_medium_transport transport layer.
        flush_interval: Time interval (in seconds) for flushing buffered data.
        max_batch_size: Maximum batch size in bytes for outgoing data.
        chunk_assembly_ttl: Time-to-live (in seconds) for assembling incoming chunks.
        logger: Optional logger instance for debugging and observability.

    Returns:
        AggregatingLink: Fully configured transport stack ready for use.

    Raises:
        ValueError: If configuration parameters are invalid.
        TypeError: If required parameters have incorrect types.
    """

    # --- Medium layer ---
    if logger is not None:
        medium = MediumTransport(
            transport=low_transport,
            encryption_key=encryption_key,
            logger=logger,
        )
    else:
        medium = MediumTransport(
            transport=low_transport,
            encryption_key=encryption_key,
        )

    # --- High layer ---
    if logger is not None:
        high = AggregatingLink(
            transport=medium,
            flush_interval=flush_interval,
            max_batch_size=max_batch_size,
            chunk_assembly_ttl=chunk_assembly_ttl,
            logger=logger,
        )
    else:
        high = AggregatingLink(
            transport=medium,
            flush_interval=flush_interval,
            max_batch_size=max_batch_size,
            chunk_assembly_ttl=chunk_assembly_ttl,
        )

    await high.start()
    return high


__all__ = [
    "AggregatingLink",
    "Frame",
    "MediumTransport",
    "LowTransport",
    "get_transport",
]
