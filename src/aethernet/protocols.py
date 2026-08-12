"""Known protocol names the server can expose, and their defaults.

Kept as a plain tuple (not an enum) so a new protocol only needs adding
here - the CLI's --protocols validation and default-all behavior both
derive from this list automatically.
"""

SUPPORTED_PROTOCOLS: tuple[str, ...] = ("ws", "http", "stasis")
