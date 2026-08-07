from dataclasses import dataclass


@dataclass
class Action:
    type: str


@dataclass
class MoveMouse(Action):
    x: int
    y: int

    def __init__(self, x, y):
        super().__init__("move_mouse")
        self.x = x
        self.y = y


@dataclass
class ClickMouse(Action):
    button: str
    clicks: int = 1

    def __init__(self, button, clicks=1):
        super().__init__("click")
        self.button = button
        self.clicks = clicks


@dataclass
class Delay(Action):
    seconds: float

    def __init__(self, seconds):
        super().__init__("delay")
        self.seconds = seconds