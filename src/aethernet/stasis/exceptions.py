# --- Remote Client Errors ---

class ClientError(Exception):
    """Base exception for the remote client."""


class ProtocolError(ClientError):
    """The remote host sent an invalid or unexpected protocol message."""


# --- Remote Server Errors ---

class ServerError(ClientError):
    """Errors sent by the remote host."""
    def __init__(self, *, reason: str | None = None, message: str | None = None) -> None:
        reason = "unknown_error" if reason is None else reason
        message = "Unknown Error" if message is None else message

        self.reason = reason
        super().__init__(message)


class ServerProtocolError(ServerError):
    """The remote server reports that the client has sent an invalid or unexpected protocol message."""


class ConnectionRejectedError(ServerError):
    """The remote host rejected the connection request."""