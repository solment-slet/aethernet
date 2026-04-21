class TransportClosedError(RuntimeError):
    """Исключение возникающее при попытке обратиться"""


class StreamClosed(Exception):
    """Исключение, возникающее при закрытии link во время ожидания frame."""
