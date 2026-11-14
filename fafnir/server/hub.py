"""Live events for connected apps: what Fafnir hears, says, does, and asks.

publish() is safe to call from any thread (the engine's workers, the desktop window). Each connected
WebSocket gets its own bounded queue; a slow phone loses old events rather than slowing Fafnir down.
The last 100 events are kept so a phone that reconnects can catch up.
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import threading
import time

_lock = threading.Lock()
_subs: set = set()                         # (loop, queue)
_recent: collections.deque = collections.deque(maxlen=100)
_seq = itertools.count(1)
_last_now: dict = {"briefing": "", "items": []}


def publish(kind: str, **data) -> None:
    ev = {"seq": next(_seq), "type": kind, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), **data}
    with _lock:
        _recent.append(ev)
        if kind == "now":
            _last_now.update(briefing=data.get("briefing", ""), items=data.get("items", []))
        subs = list(_subs)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(_put, q, ev)
        except RuntimeError:
            pass   # that connection's loop has closed


def _put(q: asyncio.Queue, ev: dict) -> None:
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(ev)


def subscribe() -> tuple:
    loop = asyncio.get_running_loop()
    sub = (loop, asyncio.Queue(maxsize=200))
    with _lock:
        _subs.add(sub)
    return sub


def unsubscribe(sub) -> None:
    with _lock:
        _subs.discard(sub)


def recent(after: int = 0) -> list[dict]:
    with _lock:
        return [e for e in _recent if e["seq"] > after]


def last_now() -> dict:
    with _lock:
        return dict(_last_now)


def tee_engine_events(engine) -> None:
    """Copy everything the engine tells the desktop to connected apps too."""
    inner = engine.events

    class Tee:
        def __getattr__(self, name):
            return getattr(inner, name)

        def heard(self, text):
            inner.heard(text); publish("heard", text=text)

        def reply_start(self):
            inner.reply_start(); publish("reply_start")

        def reply_text(self, delta):
            inner.reply_text(delta); publish("reply_text", text=delta)

        def tool(self, name, args, result, is_error):
            inner.tool(name, args, result, is_error); publish("tool", name=name, error=bool(is_error))

        def reply_end(self, interrupted, stats):
            inner.reply_end(interrupted, stats); publish("reply_end", interrupted=bool(interrupted))

        def notice(self, text):
            inner.notice(text); publish("notice", text=text)

        def error(self, text):
            inner.error(text); publish("error", text=text)

        def confirm_request(self, action):
            inner.confirm_request(action); publish("approval", action=action)

        def confirm_done(self, approved, timed_out):
            inner.confirm_done(approved, timed_out); publish("approval_done", approved=bool(approved), timed_out=bool(timed_out))

    engine.events = Tee()
