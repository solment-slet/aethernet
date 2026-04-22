from __future__ import annotations

from aethernet.transport.enums import EncryptionMode, ReliabilityMode
from aethernet.transport.low_transport import LowTransport
from aethernet.transport import AggregatingLink, MediumTransport
from aethernet.typing import LoggerLike


async def get_transport(
    low_transport: LowTransport,
    *,
    # Encryption
    encryption_mode: EncryptionMode = EncryptionMode.NONE,
    encryption_key: bytes | None = None,
    # Aggregating
    flush_interval: float = 0.5,
    max_batch_size: int = 64 * 1024,
    chunk_assembly_ttl: float = 60.0,
    # Reliability
    reliability_mode: ReliabilityMode = ReliabilityMode.NONE,
    window_size: int = 8,
    ack_flush_interval: float = 0.1,
    ack_batch_size: int = 8,
    received_seqs_window: int = 256,
    reorder_buffer_ttl: float = 500.0,
    # Logging
    logger: LoggerLike | None = None,
) -> AggregatingLink:
    """
    Creates a fully configured transport stack with low-, medium-, and high-level layers.

    The transport is composed of three layers:
    - LowTransport: communication layer
    - MediumTransport: encryption and buffering layer
    - AggregatingLink: batching and flow-control layer

    Args:
        low_transport: low-level transport.
        encryption_mode: method for encrypting messages or disabling it
        encryption_key: bytes encryption key used for securing medium transport layer.
        flush_interval: Maximum waiting time before sending a batch.
        max_batch_size: Maximum logical batch size in bytes.
        chunk_assembly_ttl: Lifetime of an incomplete chunk assembly.
        reliability_mode: Delivery reliability mode.
        window_size: Window size for PARALLEL mode.
        ack_flush_interval: Maximum ACK accumulation time before sending.
        ack_batch_size: Maximum number of ACKs in a single flush.
        received_seqs_window: How many recent seqs to store for deduplication.
        reorder_buffer_ttl: How much time to reorder buffered messages.
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
            encryption_mode=encryption_mode,
            encryption_key=encryption_key,
            logger=logger,
        )
    else:
        medium = MediumTransport(
            transport=low_transport,
            encryption_mode=encryption_mode,
            encryption_key=encryption_key,
        )

    # --- High layer ---
    if logger is not None:
        high = AggregatingLink(
            transport=medium,
            flush_interval=flush_interval,
            max_batch_size=max_batch_size,
            chunk_assembly_ttl=chunk_assembly_ttl,
            reliability_mode=reliability_mode,
            window_size=window_size,
            ack_flush_interval=ack_flush_interval,
            ack_batch_size=ack_batch_size,
            received_seqs_window=received_seqs_window,
            reorder_buffer_ttl=reorder_buffer_ttl,
            logger=logger,
        )
    else:
        high = AggregatingLink(
            transport=medium,
            flush_interval=flush_interval,
            max_batch_size=max_batch_size,
            chunk_assembly_ttl=chunk_assembly_ttl,
            reliability_mode=reliability_mode,
            window_size=window_size,
            ack_flush_interval=ack_flush_interval,
            ack_batch_size=ack_batch_size,
            received_seqs_window=received_seqs_window,
            reorder_buffer_ttl=reorder_buffer_ttl,
        )

    await high.start()
    return high
