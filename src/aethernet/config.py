import os
from dataclasses import dataclass
from pathlib import Path

from aethernet.link_loading import DEFAULT_LINK_CONFIG_FILENAME
from aethernet.protocols import SUPPORTED_PROTOCOLS


@dataclass(frozen=True)
class ServerConfig:
    """Launch-time configuration for the Aethernet server."""

    link_config_path: Path
    log_level: str
    protocols: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "ServerConfig":
        protocols_env = os.environ.get("AETHERNET_PROTOCOLS")
        protocols = (
            tuple(p.strip() for p in protocols_env.split(",") if p.strip())
            if protocols_env
            else SUPPORTED_PROTOCOLS
        )
        return cls(
            link_config_path=Path(
                os.environ.get("AETHERNET_LINK_CONFIG", DEFAULT_LINK_CONFIG_FILENAME)
            )
            .expanduser()
            .resolve(),
            log_level=os.environ.get("AETHERNET_LOG_LEVEL", "DEBUG").upper(),
            protocols=protocols,
        )

    def with_overrides(
        self,
        *,
        link_config_path: Path | None = None,
        log_level: str | None = None,
        protocols: tuple[str, ...] | None = None,
    ) -> "ServerConfig":
        """Returns a copy with CLI-supplied values taking priority over env/defaults."""
        return ServerConfig(
            link_config_path=(
                self.link_config_path
                if link_config_path is None
                else link_config_path.expanduser().resolve()
            ),
            log_level=self.log_level if log_level is None else log_level.upper(),
            protocols=self.protocols if protocols is None else protocols,
        )
