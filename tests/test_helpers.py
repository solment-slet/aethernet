import pytest
import httpx
from hypothesis import given, strategies as st
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from aethernet.transport.http_over_link.http_over_link import _headers_to_list
from aethernet.transport.ws_over_link.ws_over_link import (
    _normalize_headers,
    _make_connection_closed,
)
from aethernet.transport.utils import (
    encode_json_bytes,
    decode_json_bytes,
)


def test_json_roundtrip():
    data = {"привет": "мир", "number": 42, "nested": {"a": True}}
    assert decode_json_bytes(encode_json_bytes(data)) == data


@given(st.lists(st.tuples(st.text(), st.text())))
def test_normalize_headers_from_list_of_tuples(headers):
    normalized = _normalize_headers(headers)
    assert isinstance(normalized, list)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in normalized)


def test_normalize_headers_raises_on_invalid_type():
    with pytest.raises(TypeError):
        _normalize_headers([("key", 123)])
    with pytest.raises(TypeError):
        _normalize_headers((123, "value"))


def test_make_connection_closed_normal():
    exc = _make_connection_closed(1000, "Normal closure")
    assert isinstance(exc, ConnectionClosedOK)


def test_make_connection_closed_error():
    exc = _make_connection_closed(1006, "Abnormal closure")
    assert isinstance(exc, ConnectionClosedError)


# ======================================================================
# _headers_to_list
# ======================================================================


def test_none_returns_empty():
    assert _headers_to_list(None) == []


def test_plain_list_returned_as_is():
    h = [("a", "1"), ("b", "2")]
    assert _headers_to_list(h) == [("a", "1"), ("b", "2")]


def test_empty_list_returned_as_is():
    assert _headers_to_list([]) == []


def test_httpx_headers_converted():
    h = httpx.Headers({"content-type": "application/json", "x-foo": "bar"})
    result = _headers_to_list(h)
    assert ("content-type", "application/json") in result
    assert ("x-foo", "bar") in result


# ======================================================================
# Hypothesis
# ======================================================================

header_name = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll"), whitelist_characters="-"),
    min_size=1,
    max_size=30,
)
header_value = st.text(min_size=0, max_size=100)
header_pair = st.tuples(header_name, header_value)
header_list = st.lists(header_pair, min_size=0, max_size=20)


@given(headers=header_list)
def test_plain_list_roundtrip(headers):
    """Список кортежей возвращается без изменений."""
    result = _headers_to_list(headers)
    assert result == headers


@given(headers=header_list)
def test_result_is_always_list(headers):
    """Результат всегда list, независимо от входа."""
    assert isinstance(_headers_to_list(headers), list)
    assert isinstance(_headers_to_list(None), list)


@given(headers=header_list)
def test_all_elements_are_tuples(headers):
    """Все элементы результата — кортежи из двух строк."""
    for name, value in _headers_to_list(headers):
        assert isinstance(name, str)
        assert isinstance(value, str)


@given(headers=header_list)
def test_length_preserved_for_list(headers):
    """Длина результата совпадает с длиной входного списка."""
    assert len(_headers_to_list(headers)) == len(headers)
