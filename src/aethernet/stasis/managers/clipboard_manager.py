import copykitten


class ClipboardManager:
    """Thin sync wrapper around copykitten. Intended to be called only
    from a worker thread (see ActionExecutor), never from the event loop
    directly, since copykitten is blocking."""

    @staticmethod
    def get() -> str:
        return copykitten.paste()

    @staticmethod
    def set(text: str) -> None:
        copykitten.copy(text)
