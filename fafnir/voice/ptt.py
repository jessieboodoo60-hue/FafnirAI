"""Global push-to-talk key (works even when the terminal isn't focused)."""
from __future__ import annotations

from typing import Callable

from pynput import keyboard


def key_vk(key) -> int | None:
    """Windows virtual-key code for a pynput key event."""
    if isinstance(key, keyboard.Key):
        return getattr(key.value, "vk", None)
    return getattr(key, "vk", None)


def parse_key(spec: str) -> int:
    """'vk:124' -> 124, 'f13' / 'ctrl_r' -> that key's code, 'm' -> its code."""
    spec = spec.strip()
    if spec.lower().startswith("vk:"):
        return int(spec[3:])
    if len(spec) == 1:
        ch = spec.upper()
        if ch.isalnum():
            return ord(ch)  # Windows VK codes for A-Z / 0-9 equal their ASCII codes
    try:
        vk = key_vk(keyboard.Key[spec.lower()])
    except KeyError:
        vk = None
    if vk is None:
        # pynput may not name F13-F24; their VK codes are 0x7C-0x87.
        s = spec.lower()
        if s.startswith("f") and s[1:].isdigit() and 13 <= int(s[1:]) <= 24:
            return 0x7C + int(s[1:]) - 13
        raise ValueError(
            f"Unknown push-to-talk key '{spec}'. Run `python main.py --learn-key` to find its code."
        )
    return vk


class PushToTalk:
    def __init__(self, spec: str, on_press: Callable[[], None], on_release: Callable[[], None]):
        self.vk = parse_key(spec)
        self._down = False
        self._on_press, self._on_release = on_press, on_release
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def _press(self, key) -> None:
        if key_vk(key) == self.vk and not self._down:  # ignore auto-repeat
            self._down = True
            self._on_press()

    def _release(self, key) -> None:
        if key_vk(key) == self.vk and self._down:
            self._down = False
            self._on_release()


def learn_key() -> int:
    """Print the code of every key pressed, so the user can find their talk key."""
    print("Press the key you want to hold to talk (Esc to finish).")
    print("For a Razer macro key (like M5), first assign it a key in Razer Synapse, e.g. F13.\n")

    def on_press(key):
        if key == keyboard.Key.esc:
            return False
        vk = key_vk(key)
        name = key.name if isinstance(key, keyboard.Key) else getattr(key, "char", None)
        print(f"  pressed: {name or '?'}   ->   ptt_key = \"vk:{vk}\"")

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()
    return 0
