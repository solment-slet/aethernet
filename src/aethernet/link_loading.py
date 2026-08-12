"""Loading of the user-supplied link configuration file.

The link configuration (transport, addresses, encryption, ...) is
deployment-specific and changes often, so it does not live inside this
package. Instead, callers point us at a Python file that defines an
async `create_link(mode, logger)` function returning a ready-to-use
`AggregatingLink`.

This loader itself is mode-agnostic - it just finds and validates the
file, and hands back a factory function. Both the Aethernet server (which
calls the factory with mode="server") and clients such as stasis-client
(mode="client") import this same module, so one link config file can
describe both ends of a connection instead of duplicating the setup for
each side.

Mirrors how tools like gunicorn (`-c config.py`) or mitmproxy (addons)
let users supply executable config outside the package.
"""

import importlib.util
import inspect
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from aethernet.transport import AggregatingLink

LinkMode = Literal["server", "client"]
LinkFactory = Callable[[LinkMode, logging.Logger], Awaitable[AggregatingLink]]

DEFAULT_LINK_CONFIG_FILENAME = "aethernet_link.py"


class LinkConfigError(Exception):
    """The link configuration file is missing, invalid, or malformed."""


def load_link_factory(config_path: Path) -> LinkFactory:
    """Loads `create_link` from the given Python file.

    Raises LinkConfigError with a message safe to show to the user if the
    file is missing, fails to execute, or doesn't define a valid factory.
    Does not call the factory - the caller decides what `mode` to pass.
    """
    if not config_path.is_file():
        raise LinkConfigError(
            f"Link config file not found: {config_path}\n"
            "Create one (see aethernet_link.example.py) and pass its path "
            f"via --link-config, or place it at ./{DEFAULT_LINK_CONFIG_FILENAME}."
        )

    spec = importlib.util.spec_from_file_location("aethernet_link_config", config_path)
    if spec is None or spec.loader is None:
        raise LinkConfigError(f"Could not load link config file: {config_path}")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise LinkConfigError(f"Error while executing {config_path}: {e}") from e

    factory = getattr(module, "create_link", None)
    if factory is None:
        raise LinkConfigError(
            f"{config_path} must define an async 'create_link(mode, logger)' function."
        )
    if not inspect.iscoroutinefunction(factory):
        raise LinkConfigError(
            f"'create_link' in {config_path} must be an async function."
        )

    params = inspect.signature(factory).parameters
    if len(params) != 2:
        raise LinkConfigError(
            f"'create_link' in {config_path} must accept exactly two arguments (mode, logger), "
            f"got {len(params)}."
        )

    return factory
