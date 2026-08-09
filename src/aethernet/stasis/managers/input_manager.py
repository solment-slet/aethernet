import subprocess
import sys

from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key as PynputKey

from aethernet.stasis.actions import (
    MoveMouse, ClickButton, PressButton, ReleaseButton, Scroll,
    TypeText, PressKey, ReleaseKey, Key,
)

_BUTTON_MAP: dict[str, Button] = {
    "left": Button.left,
    "right": Button.right,
    "middle": Button.middle,
}

_KEY_MAP: dict[Key, PynputKey] = {
    Key.up: PynputKey.up,
    Key.down: PynputKey.down,
    Key.left: PynputKey.left,
    Key.right: PynputKey.right,
    Key.home: PynputKey.home,
    Key.end: PynputKey.end,
    Key.page_up: PynputKey.page_up,
    Key.page_down: PynputKey.page_down,
    Key.enter: PynputKey.enter,
    Key.tab: PynputKey.tab,
    Key.space: PynputKey.space,
    Key.backspace: PynputKey.backspace,
    Key.delete: PynputKey.delete,
    Key.insert: PynputKey.insert,
    Key.esc: PynputKey.esc,
    Key.shift: PynputKey.shift,
    Key.shift_l: PynputKey.shift_l,
    Key.shift_r: PynputKey.shift_r,
    Key.ctrl: PynputKey.ctrl,
    Key.ctrl_l: PynputKey.ctrl_l,
    Key.ctrl_r: PynputKey.ctrl_r,
    Key.alt: PynputKey.alt,
    Key.alt_l: PynputKey.alt_l,
    Key.alt_r: PynputKey.alt_r,
    Key.alt_gr: PynputKey.alt_gr,
    Key.cmd: PynputKey.cmd,
    Key.cmd_l: PynputKey.cmd_l,
    Key.cmd_r: PynputKey.cmd_r,
    Key.caps_lock: PynputKey.caps_lock,
    Key.num_lock: PynputKey.num_lock,
    Key.scroll_lock: PynputKey.scroll_lock,
    Key.f1: PynputKey.f1, Key.f2: PynputKey.f2, Key.f3: PynputKey.f3,
    Key.f4: PynputKey.f4, Key.f5: PynputKey.f5, Key.f6: PynputKey.f6,
    Key.f7: PynputKey.f7, Key.f8: PynputKey.f8, Key.f9: PynputKey.f9,
    Key.f10: PynputKey.f10, Key.f11: PynputKey.f11, Key.f12: PynputKey.f12,
    Key.f13: PynputKey.f13, Key.f14: PynputKey.f14, Key.f15: PynputKey.f15,
    Key.f16: PynputKey.f16, Key.f17: PynputKey.f17, Key.f18: PynputKey.f18,
    Key.f19: PynputKey.f19, Key.f20: PynputKey.f20,
    Key.menu: PynputKey.menu,
    Key.pause: PynputKey.pause,
    Key.print_screen: PynputKey.print_screen,
    Key.media_play_pause: PynputKey.media_play_pause,
    Key.media_next: PynputKey.media_next,
    Key.media_previous: PynputKey.media_previous,
    Key.media_volume_up: PynputKey.media_volume_up,
    Key.media_volume_down: PynputKey.media_volume_down,
    Key.media_volume_mute: PynputKey.media_volume_mute,
}


def _is_non_ascii_char(key: str) -> bool:
    """True for a single character outside the basic ASCII range (e.g.
    Cyrillic, CJK, accented letters, emoji).

    These have no fixed physical keycode on most keyboard layouts, so
    press/release "hold" semantics don't really apply to them anyway -
    they always need to go through unicode injection rather than a
    layout-dependent keycode lookup (see _type_unicode below).
    """
    return len(key) == 1 and ord(key) > 127


class InputManager:
    """Thin sync wrapper around pynput. Intended to be called only from a
    worker thread (see ActionExecutor), never from the event loop directly,
    since pynput calls are blocking."""

    def __init__(self, offset_x: int, offset_y: int) -> None:
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self._offset_x = offset_x
        self._offset_y = offset_y

    @staticmethod
    def _resolve_button(button: str) -> Button:
        try:
            return _BUTTON_MAP[button]
        except KeyError as e:
            raise ValueError(f"Unknown mouse button '{button}'.") from e

    @staticmethod
    def _resolve_key(key: str) -> PynputKey | str:
        try:
            return _KEY_MAP[Key(key)]
        except ValueError:
            pass
        if len(key) == 1:
            return key
        raise ValueError(f"Unknown key '{key}'.")

    def move_mouse(self, action: MoveMouse) -> None:
        self.mouse.position = (action.x + self._offset_x, action.y + self._offset_y)

    def click_button(self, action: ClickButton) -> None:
        self.mouse.click(self._resolve_button(action.button), action.clicks)

    def press_button(self, action: PressButton) -> None:
        self.mouse.press(self._resolve_button(action.button))

    def release_button(self, action: ReleaseButton) -> None:
        self.mouse.release(self._resolve_button(action.button))

    def scroll(self, action: Scroll) -> None:
        self.mouse.scroll(action.dx, action.dy)

    def type_text(self, action: TypeText) -> None:
        self._type_unicode(action.text)

    def press_key(self, action: PressKey) -> None:
        if _is_non_ascii_char(action.key):
            # See _type_unicode: routing single non-ASCII characters
            # through press()/release() is what produced garbled output
            # (e.g. Cyrillic arriving as uppercase Latin) when the
            # remote host's active layout doesn't contain that
            # character. There's no meaningful "hold" for an arbitrary
            # unicode character anyway, so we just type it immediately
            # on press and no-op on the matching release below.
            self._type_unicode(action.key)
            return
        self.keyboard.press(self._resolve_key(action.key))

    def release_key(self, action: ReleaseKey) -> None:
        if _is_non_ascii_char(action.key):
            # Already emitted on press - nothing left to release.
            return
        self.keyboard.release(self._resolve_key(action.key))

    def _type_unicode(self, text: str) -> None:
        """Types arbitrary unicode text in a way that's independent of
        the remote host's currently active keyboard layout.

        pynput's default press()/type() path resolves each character to
        a keycode via the *active system layout* on at least the Xlib
        (Linux) backend, and silently degrades to a garbled/incorrect
        keycode instead of raising when the character isn't in that
        layout - this is what turned Cyrillic input into uppercase
        Latin letters when the remote host's layout was English. We
        bypass that resolution entirely and inject the unicode
        codepoints directly, the same mechanism OS-level IME/emoji
        input uses.
        """
        if sys.platform == "win32":
            self._type_unicode_windows(text)
        elif sys.platform.startswith("linux"):
            self._type_unicode_linux(text)
        else:
            # macOS: pynput's Quartz backend already injects unicode via
            # CGEventKeyboardSetUnicodeString rather than resolving a
            # layout-dependent keycode, so it isn't affected by this bug.
            self.keyboard.type(text)

    @staticmethod
    def _type_unicode_windows(text: str) -> None:
        # SendInput with KEYEVENTF_UNICODE injects a raw UTF-16 code unit
        # directly, bypassing keyboard-layout translation entirely -
        # works regardless of which layout is active on the remote host.
        import ctypes
        from ctypes import wintypes

        INPUT_KEYBOARD = 1
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ki", KEYBDINPUT)]

        def _send(code_unit: int, flags: int) -> None:
            inp = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE | flags, 0, None),
            )
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

        # encode as UTF-16 so characters outside the BMP (e.g. some
        # emoji) are sent as the correct surrogate pair, matching what
        # KEYEVENTF_UNICODE expects.
        units = text.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code_unit = units[i] | (units[i + 1] << 8)
            _send(code_unit, 0)
            _send(code_unit, KEYEVENTF_KEYUP)

    @staticmethod
    def _type_unicode_linux(text: str) -> None:
        # xdotool resolves each character through X11's proper unicode
        # input path (XTestFakeKeyEvent against a temporarily-mapped
        # keycode that it manages itself) rather than depending on the
        # layout already active on the display, so it doesn't suffer the
        # same garbling as pynput's Xlib backend.
        #
        # Requires the `xdotool` package to be installed on the remote
        # host (e.g. `apt install xdotool`). Falls back to pynput if it's
        # missing so text still gets typed somehow rather than silently
        # dropped, though the fallback can still mis-render non-ASCII
        # text depending on the active layout.
        try:
            subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--", text],
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            KeyboardController().type(text)