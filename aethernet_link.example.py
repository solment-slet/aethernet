"""Example link configuration.

Copy this file to `aethernet_link.py` (default lookup location) or
anywhere else and point --link-config at it, then fill in the transport
details for your setup.

This same file is shared between the server (mode="server") and any
client such as stasis-client (mode="client"), so both sides of the
connection can be described in one place instead of two.
"""

import logging
from typing import Literal

from aethernet.transport import AggregatingLink


async def create_link(
    mode: Literal["server", "client"], logger: logging.Logger
) -> AggregatingLink:
    """Builds the AggregatingLink used for this end of the connection.

    `mode` tells you which side of the connection this call is for -
    useful if the transport, address, or role differs between server and
    client (e.g. the server binds/listens while the client dials out).

    `logger` is a standard library logger, already wired to the caller's
    logging setup. Pass it into your transport if it accepts one, or
    ignore it entirely - either is fine.
    """
    raise NotImplementedError("Fill in your transport/encryption setup here.")
