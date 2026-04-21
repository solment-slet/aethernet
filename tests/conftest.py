from unittest.mock import MagicMock, AsyncMock

import pytest

from aethernet import EncryptionMode
from aethernet.transport import AggregatingLink
from aethernet.transport.medium_transport import MediumTransport
from aethernet.transport.low_transport import LowTransportConfig

ENCRYPTION_KEY = b"\x00" * 32


# ===================================================================
# Фикстуры
# ===================================================================


@pytest.fixture
def mock_link() -> MagicMock:
    """Мок AggregatingLink."""
    link = MagicMock(spec=AggregatingLink)
    link.new_stream_id.return_value = "test-stream-123"
    link.send_frame = AsyncMock()
    link.recv_frame = AsyncMock()
    link.accept_stream = AsyncMock()
    return link


@pytest.fixture
def mock_low_transport():
    transport = MagicMock()
    transport.send = MagicMock()
    transport.recv = MagicMock()
    transport.config = LowTransportConfig(
        mode="string",
        max_message_chars=4000,
        alphabet=(
            "".join(
                [chr(c) for c in range(ord("A"), ord("Z") + 1)]
                + [chr(c) for c in range(ord("a"), ord("z") + 1)]
                + [chr(c) for c in range(ord("0"), ord("9") + 1)]
            )
            + "+/="
        ),
        min_send_interval=0.001,
        min_recv_interval=0.001,
    )

    return transport


@pytest.fixture
def mock_medium_transport(mock_low_transport):
    return MediumTransport(
        transport=mock_low_transport,
        encryption_mode=EncryptionMode.CHACHA20_POLY1305,
        encryption_key=ENCRYPTION_KEY,
    )
