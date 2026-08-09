from enum import StrEnum
from dataclasses import dataclass, field


class Key(StrEnum):
    """Non-character keyboard buttons: modifiers, navigation, function
    and system keys, common to all supported platforms."""

    # --- Navigation ---
    up = "up"
    down = "down"
    left = "left"
    right = "right"
    home = "home"
    end = "end"
    page_up = "page_up"
    page_down = "page_down"

    # --- Editing ---
    enter = "enter"
    tab = "tab"
    space = "space"
    backspace = "backspace"
    delete = "delete"
    insert = "insert"
    esc = "esc"

    # --- Modifiers ---
    shift = "shift"
    shift_l = "shift_l"
    shift_r = "shift_r"
    ctrl = "ctrl"
    ctrl_l = "ctrl_l"
    ctrl_r = "ctrl_r"
    alt = "alt"
    alt_l = "alt_l"
    alt_r = "alt_r"
    alt_gr = "alt_gr"
    cmd = "cmd"
    cmd_l = "cmd_l"
    cmd_r = "cmd_r"

    # --- Lock keys ---
    caps_lock = "caps_lock"
    num_lock = "num_lock"
    scroll_lock = "scroll_lock"

    # --- Function row ---
    f1 = "f1"
    f2 = "f2"
    f3 = "f3"
    f4 = "f4"
    f5 = "f5"
    f6 = "f6"
    f7 = "f7"
    f8 = "f8"
    f9 = "f9"
    f10 = "f10"
    f11 = "f11"
    f12 = "f12"
    f13 = "f13"
    f14 = "f14"
    f15 = "f15"
    f16 = "f16"
    f17 = "f17"
    f18 = "f18"
    f19 = "f19"
    f20 = "f20"

    # --- System / misc ---
    menu = "menu"
    pause = "pause"
    print_screen = "print_screen"

    # --- Media ---
    media_play_pause = "media_play_pause"
    media_next = "media_next"
    media_previous = "media_previous"
    media_volume_up = "media_volume_up"
    media_volume_down = "media_volume_down"
    media_volume_mute = "media_volume_mute"


@dataclass
class Action:
    type: str = field(init=False)


# --- Mouse ---

@dataclass
class MoveMouse(Action):
    x: int
    y: int
    type: str = field(default="move_mouse", init=False)


@dataclass
class ClickButton(Action):
    button: str
    clicks: int = 1
    type: str = field(default="click_button", init=False)


@dataclass
class PressButton(Action):
    button: str
    type: str = field(default="press_button", init=False)


@dataclass
class ReleaseButton(Action):
    button: str
    type: str = field(default="release_button", init=False)


@dataclass
class Scroll(Action):
    dx: int
    dy: int
    type: str = field(default="scroll", init=False)


# --- Keyboard ---

@dataclass
class TypeText(Action):
    text: str
    type: str = field(default="type_text", init=False)


@dataclass
class PressKey(Action):
    key: str | Key
    type: str = field(default="press_key", init=False)


@dataclass
class ReleaseKey(Action):
    key: str | Key
    type: str = field(default="release_key", init=False)


# --- Clipboard ---

@dataclass
class SetClipboard(Action):
    text: str
    type: str = field(default="set_clipboard", init=False)


@dataclass
class GetClipboard(Action):
    type: str = field(default="get_clipboard", init=False)


# --- Delay ---

@dataclass
class Delay(Action):
    seconds: float
    type: str = field(default="delay", init=False)


ACTION_TYPES: dict[str, type[Action]] = {
    "move_mouse": MoveMouse,
    "click_button": ClickButton,
    "press_button": PressButton,
    "release_button": ReleaseButton,
    "scroll": Scroll,
    "type_text": TypeText,
    "press_key": PressKey,
    "release_key": ReleaseKey,
    "set_clipboard": SetClipboard,
    "get_clipboard": GetClipboard,
    "delay": Delay,
}


def action_from_dict(d: dict) -> Action:
    try:
        cls = ACTION_TYPES[d["type"]]
    except KeyError as e:
        raise ValueError(f"Unknown action type '{d.get('type')}'.") from e
    kwargs = {k: v for k, v in d.items() if k != "type"}
    try:
        # noinspection PyArgumentList
        return cls(**kwargs)
    except TypeError as e:
        raise ValueError(f"Malformed '{d['type']}' action: {e}") from e
