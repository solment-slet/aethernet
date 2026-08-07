class TransportClosedError(RuntimeError):
    """An exception that occurs when attempting to access the transport when it is already closed."""


class StreamClosed(Exception):
    """An exception that occurs when closing a link while waiting for a frame."""
