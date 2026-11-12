"""The link between Fafnir's tools and the desktop app's screen.

Tools run on background threads and have no idea whether a screen exists. They call
SCREEN.send(action, payload); the desktop app attaches a handler that forwards it to the
UI thread (a Qt signal). With no app running (terminal mode), send() returns False and
the tool tells Fafnir there's no screen.
"""
from __future__ import annotations

import threading
from typing import Callable

PANELS = ["today", "tasks", "notices", "email", "business", "activity"]


class ScreenBus:
    def __init__(self):
        self._handler: Callable[[str, dict], None] | None = None
        self._lock = threading.Lock()
        self.showing: str = ""          # title of what's on the stage now ("" = nothing)
        self.layout: dict = {"order": list(PANELS), "hidden": [], "conversation": "show"}

    def attach(self, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._handler = handler

    @property
    def available(self) -> bool:
        return self._handler is not None

    def send(self, action: str, payload: dict) -> bool:
        with self._lock:
            handler = self._handler
        if handler is None:
            return False
        if action == "show":
            self.showing = payload.get("title", "")
        elif action == "clear":
            self.showing = ""
        elif action == "layout":
            self.layout.update({k: v for k, v in payload.items() if v is not None})
        handler(action, payload)
        return True


SCREEN = ScreenBus()
