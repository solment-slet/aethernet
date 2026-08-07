from typing import Literal
from dataclasses import dataclass


type StreamModes = Literal["on_request", "interval"]

@dataclass
class StreamMode:
    mode: StreamModes
    interval_ms: int | None = None


@dataclass
class RemoteHostMetadata:
    width: int
    height: int


@dataclass
class ServerMetadata:
    remote_host: RemoteHostMetadata
    stream_mode: StreamMode