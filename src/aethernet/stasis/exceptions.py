# --- Local Device Errors ---

class LocalDeviceError(Exception):
    """Base exception for the local device."""


class LocalDeviceProtocolError(LocalDeviceError):
    """The remote device sent an invalid or unexpected protocol message."""


class NotConnectedError(LocalDeviceError):
    """The client is not connected to a remote device."""


# --- Remote Device Errors ---

class RemoteDeviceError(LocalDeviceError):
    """Errors sent by the remote device."""
    def __init__(self, *, reason: str | None = None, message: str | None = None) -> None:
        reason = "unknown_error" if reason is None else reason
        message = "Unknown Error" if message is None else message

        self.reason = reason
        super().__init__(message)


class RemoteDeviceProtocolError(RemoteDeviceError):
    """The remote device reports that the client has sent an invalid or unexpected protocol message."""


class ConnectionRejectedError(RemoteDeviceError):
    """The remote device rejected the connection request."""


class InvalidSessionError(RemoteDeviceError):
    """The session ID is missing or invalid."""


class InternalError(RemoteDeviceError):
    """The remote device encountered an internal error."""