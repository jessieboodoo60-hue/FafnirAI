"""Emergency kill switch: stop everything Fafnir is doing, and keep it stopped.

While engaged:
  - every tool call is refused (no mouse/keyboard, files, programs, email, cameras...),
  - the current reply and speech are cut off, queued requests are dropped,
  - new requests (spoken, typed, phone) are refused with a short reminder,
  - heartbeat checks and announcements stop,
  - any mouse button or key Fafnir was holding is let go.

The state is saved to data/killswitch.json, so it survives a restart: Fafnir comes back
stopped until the owner releases it.

Engage:  Ctrl+Alt+K, or say/type "kill switch" / "emergency stop" / "stop everything",
         or send /kill to the Discord bot.
Release: Ctrl+Alt+Shift+K, or type /release in the app, or send /release on Discord,
         or run  python main.py --release.  (Not by voice: anyone in the room could say it.)
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

from .config import ROOT

STATE_FILE = ROOT / "data" / "killswitch.json"
KILL_PHRASES = ("kill switch", "killswitch", "emergency stop", "stop everything")

_lock = threading.Lock()
_listeners: list = []
_state = {"engaged": False, "since": None, "by": None}


_mtime = 0.0


def _load() -> None:
    global _mtime
    try:
        _mtime = STATE_FILE.stat().st_mtime
        _state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def _save() -> None:
    global _mtime
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(_state, indent=2), encoding="utf-8")
        _mtime = STATE_FILE.stat().st_mtime
    except OSError:
        pass


def sync() -> None:
    """Pick up a change made by another process (python main.py --kill / --release)."""
    try:
        m = STATE_FILE.stat().st_mtime
    except OSError:
        return
    if m != _mtime:
        was = engaged()
        _load()
        if engaged() != was:
            _notify(engaged())


def _log(text: str) -> None:
    try:
        with open(ROOT / "logs" / "actions.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"),
                                "tool": "KILL_SWITCH", "result": text}) + "\n")
    except OSError:
        pass
    print(f"[kill switch] {text}", flush=True)


def engaged() -> bool:
    return bool(_state.get("engaged"))


def status() -> dict:
    return dict(_state)


def on_change(fn) -> None:
    """fn(engaged: bool) is called whenever the switch flips."""
    _listeners.append(fn)


def _notify(on: bool) -> None:
    for fn in list(_listeners):
        try:
            fn(on)
        except Exception as e:
            print(f"[kill switch] a listener failed: {type(e).__name__}: {e}", flush=True)


def engage(by: str = "owner") -> bool:
    """Stop everything. Returns False if it was already on."""
    with _lock:
        was = engaged()
        _state.update(engaged=True, since=datetime.now().isoformat(timespec="seconds"), by=by)
        _save()
    _log(f"ENGAGED by {by}")
    _notify(True)  # even if already on: stop anything that slipped through
    return not was


def release(by: str = "owner") -> bool:
    """Let Fafnir act again. Returns False if it wasn't on."""
    with _lock:
        if not engaged():
            return False
        _state.update(engaged=False, since=datetime.now().isoformat(timespec="seconds"), by=by)
        _save()
    _log(f"released by {by}")
    _notify(False)
    return True


def is_kill_command(text: str) -> bool:
    """Does what the owner said or typed ask for the kill switch? (Checked before the AI sees it.)"""
    t = " ".join(re.findall(r"[a-z]+", text.lower()))
    return t in ("kill", "/kill") or text.strip().lower() == "/kill" or any(p in t for p in KILL_PHRASES)


def is_release_command(text: str) -> bool:
    return text.strip().lower() in ("/release", "/unkill")


MESSAGE = ("The kill switch is on, so I'm stopped and won't do anything. "
           "Release it with Ctrl+Alt+Shift+K, or type /release.")


class Hotkeys:
    """Ctrl+Alt+K engages, Ctrl+Alt+Shift+K releases, from anywhere in Windows.

    A small combo detector of our own: with Ctrl+Alt held, Windows reports K only as key
    code 75 (no letter), which pynput's GlobalHotKeys never matched."""

    K_VK = 75

    def __init__(self):
        from pynput import keyboard
        self._kb = keyboard
        self._down: set[str] = set()
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def _name(self, key) -> str | None:
        K = self._kb.Key
        if key in (K.ctrl, K.ctrl_l, K.ctrl_r):
            return "ctrl"
        if key in (K.alt, K.alt_l, K.alt_r, getattr(K, "alt_gr", None)):
            return "alt"
        if key in (K.shift, K.shift_l, K.shift_r):
            return "shift"
        vk = getattr(key, "vk", None)
        ch = getattr(key, "char", None)
        if vk == self.K_VK or (ch and ch.lower() == "k"):
            return "k"
        return None

    def _press(self, key) -> None:
        n = self._name(key)
        if not n:
            return
        self._down.add(n)
        if n == "k" and {"ctrl", "alt"} <= self._down:
            if "shift" in self._down:
                release("hotkey Ctrl+Alt+Shift+K")
            else:
                engage("hotkey Ctrl+Alt+K")

    def _release(self, key) -> None:
        n = self._name(key)
        if n:
            self._down.discard(n)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()


_load()
