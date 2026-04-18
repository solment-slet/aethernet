import pytest
from unittest.mock import MagicMock, patch
from hypothesis import given, strategies as st

from aethernet.transport.medium_transport import MediumTransport
from aethernet.exceptions import TransportClosedError
from .conftest import ENCRYPTION_KEY

# ======================================================================
# _encode / _decode roundtrip
# ======================================================================


def test_encode_returns_string(mock_medium_transport):
    result = mock_medium_transport._encode(b"hello")
    assert isinstance(result, str)


def test_encode_decode_roundtrip(mock_medium_transport):
    data = b"test payload"
    assert mock_medium_transport._decode(mock_medium_transport._encode(data)) == data


def test_encode_produces_only_alphabet_chars(mock_medium_transport):
    alphabet_set = set(mock_medium_transport.low_transport.alphabet)
    encoded = mock_medium_transport._encode(b"some data")
    assert all(c in alphabet_set for c in encoded)


def test_encode_nonce_is_random(mock_medium_transport):
    """Два вызова encode дают разный результат из-за случайного nonce."""
    e1 = mock_medium_transport._encode(b"data")
    e2 = mock_medium_transport._encode(b"data")
    assert e1 != e2


def test_decode_wrong_tag_raises(mock_medium_transport):
    payload = mock_medium_transport._encode(b"data")
    decoded_bytes = mock_medium_transport._basex.decode(payload)
    # Портим тег (байты 12-28)
    tampered = decoded_bytes[:12] + bytes(16) + decoded_bytes[28:]
    tampered_encoded = mock_medium_transport._basex.encode(tampered)
    with pytest.raises(Exception):
        mock_medium_transport._decode(tampered_encoded)


@given(
    data=st.binary(min_size=0, max_size=256),
    key=st.binary(min_size=32, max_size=32),
)
def test_encode_decode_roundtrip_hypothesis(data, key):
    mock_low_transport = MagicMock()
    mock_low_transport.max_message_chars = 4000
    mock_low_transport.alphabet = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "abcdefghijklmnopqrstuvwxyz" "0123456789+/="
    )
    mock_low_transport.min_send_interval = 0.001
    mock_low_transport.min_recv_interval = 0.001

    transport = MediumTransport(
        transport=mock_low_transport,
        encryption_key=key,
    )

    assert transport._decode(transport._encode(data)) == data


# ======================================================================
# _calc_max_payload_bytes
# ======================================================================


def test_max_payload_bytes_fits_in_max_chars(mock_medium_transport):
    encoded = mock_medium_transport._encode(
        b"\x00" * mock_medium_transport.max_payload_bytes
    )
    assert len(encoded) <= mock_medium_transport.max_message_chars


def test_max_payload_bytes_plus_one_exceeds_max_chars(mock_medium_transport):
    encoded = mock_medium_transport._encode(
        b"\x00" * (mock_medium_transport.max_payload_bytes + 1)
    )
    assert len(encoded) > mock_medium_transport.max_message_chars


# ======================================================================
# send
# ======================================================================


def test_send_calls_low_transport(mock_medium_transport, mock_low_transport):
    mock_medium_transport.send(b"hello")
    mock_low_transport.send.assert_called_once()
    sent = mock_low_transport.send.call_args.args[0]
    assert isinstance(sent, str)


def test_send_encrypted_payload_decodes_back(mock_medium_transport, mock_low_transport):
    mock_medium_transport.send(b"secret data")
    sent = mock_low_transport.send.call_args.args[0]
    assert mock_medium_transport._decode(sent) == b"secret data"


# ======================================================================
# recv
# ======================================================================


def test_recv_returns_decoded_message(mock_medium_transport, mock_low_transport):
    encoded = mock_medium_transport._encode(b"incoming")
    mock_low_transport.recv.return_value = encoded
    assert mock_medium_transport.recv() == b"incoming"


def test_recv_retries_on_exception(mock_medium_transport, mock_low_transport):
    encoded = mock_medium_transport._encode(b"ok")
    mock_low_transport.recv.side_effect = [
        RuntimeError("temporary error"),
        encoded,
    ]
    with patch("time.sleep"):
        result = mock_medium_transport.recv()
    assert result == b"ok"
    assert mock_low_transport.recv.call_count == 2


def test_recv_propagates_transport_closed_error(
    mock_medium_transport, mock_low_transport
):
    mock_low_transport.recv.side_effect = TransportClosedError()
    with pytest.raises(TransportClosedError):
        mock_medium_transport.recv()


def test_max_payload_bytes_is_deterministic_across_instances(mock_low_transport):
    m1 = MediumTransport(
        transport=mock_low_transport,
        encryption_key=ENCRYPTION_KEY,
    )
    m2 = MediumTransport(
        transport=mock_low_transport,
        encryption_key=ENCRYPTION_KEY,
    )

    assert m1.max_payload_bytes == m2.max_payload_bytes


def test_encode_for_size_is_deterministic(mock_medium_transport):
    data = b"\x00" * 100

    e1 = mock_medium_transport._encode_for_size(data)
    e2 = mock_medium_transport._encode_for_size(data)

    assert e1 == e2


def test_max_payload_boundary_is_stable_under_many_initializations(mock_low_transport):
    """
    Стресс‑тест на случай недетерминированного расчёта max_payload_bytes.

    Если бинарный поиск снова станет зависеть от случайного nonce,
    этот тест с высокой вероятностью начнёт падать.
    """
    for _ in range(300):
        m = MediumTransport(
            transport=mock_low_transport,
            encryption_key=ENCRYPTION_KEY,
        )

        encoded = m._encode(b"\x00" * m.max_payload_bytes)
        assert len(encoded) <= m.max_message_chars
