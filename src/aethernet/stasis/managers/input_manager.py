from pynput.mouse import Controller as MouseController
from pynput.keyboard import Controller as KeyboardController


class InputManager:
    def __init__(self) -> None:
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
