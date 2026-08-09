import time
import asyncio

from aethernet.stasis.actions import Action, Delay, SetClipboard, GetClipboard
from aethernet.stasis.managers.input_manager import InputManager
from aethernet.stasis.managers.clipboard_manager import ClipboardManager

_INPUT_HANDLERS = {
    "move_mouse": InputManager.move_mouse,
    "click_button": InputManager.click_button,
    "press_button": InputManager.press_button,
    "release_button": InputManager.release_button,
    "scroll": InputManager.scroll,
    "type_text": InputManager.type_text,
    "press_key": InputManager.press_key,
    "release_key": InputManager.release_key,
}


class ActionExecutor:
    """Executes a whole action batch sequentially in a single worker
    thread, so that ordering and Delay timing are preserved exactly as
    recorded, without event-loop scheduling jitter between actions."""

    def __init__(self, input_manager: InputManager, clipboard_manager: ClipboardManager) -> None:
        self._input = input_manager
        self._clipboard = clipboard_manager

    async def execute(self, actions: list[Action]) -> dict[int, str]:
        return await asyncio.to_thread(self._execute_sync, actions)

    def _execute_sync(self, actions: list[Action]) -> dict[int, str]:
        results: dict[int, str] = {}
        for i, action in enumerate(actions):
            if isinstance(action, Delay):
                time.sleep(action.seconds)
            elif isinstance(action, SetClipboard):
                self._clipboard.set(action.text)
            elif isinstance(action, GetClipboard):
                results[i] = self._clipboard.get()
            else:
                handler = _INPUT_HANDLERS.get(action.type)
                if handler is None:
                    raise ValueError(f"Unknown action type '{action.type}'.")
                handler(self._input, action)
        return results
