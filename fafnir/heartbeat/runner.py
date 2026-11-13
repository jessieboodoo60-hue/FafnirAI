"""The heartbeat loop: runs each check when it's due and files what it finds as notices.

Rules it follows (Tier 5):
- Quiet by default: checks only file a notice when something is worth it; `info` notices
  are silent, `important`/`urgent` ones are spoken by the front end.
- Survives restarts: when each check is next due is saved in data/heartbeat_state.json,
  so a restart doesn't reset timers, and overdue checks are staggered instead of all
  firing at once on boot.
- No overlapping runs: if a check is still running when it's due again, that run is skipped.
- Never waits on a human: checks only read; nothing here asks for confirmation.
- Kill switch: pause() stops all proactive behaviour until resume() (or a set time).
"""
from __future__ import annotations

import json
import os
import threading
import time
import tomllib
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..config import ROOT
from .notices import Notice, NoticeStore

TICK_SECONDS = 15


@dataclass
class Check:
    name: str
    every_minutes: float
    fn: Callable[["CheckContext"], list[Notice]]
    seed_first_run: bool = False  # first ever run: remember what exists, don't notify


@dataclass
class CheckContext:
    registry: object
    settings: dict      # this check's [heartbeat.<name>] table
    state: dict         # this check's own saved state (persisted between runs)
    google_ready: bool


def load_settings() -> dict:
    try:
        with open(ROOT / "config.toml", "rb") as f:
            return tomllib.load(f).get("heartbeat", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


class Heartbeat:
    def __init__(self, registry, store: NoticeStore, checks: list[Check],
                 state_path: Path = ROOT / "data" / "heartbeat_state.json",
                 log_path: Path = ROOT / "logs" / "heartbeat.log"):
        self.registry, self.store, self.state_path, self.log_path = registry, store, state_path, log_path
        self.settings = load_settings()
        self.checks = [c for c in checks if self.settings.get(c.name, {}).get("enabled", True)]
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="heartbeat")
        self._stop = threading.Event()
        self.state = self._load_state()
        self._stagger_overdue()

    # ---- persisted state -----------------------------------------------------
    def _load_state(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        s.setdefault("next_due", {})
        s.setdefault("checks", {})
        s.setdefault("paused_until", None)
        return s

    def _save_state(self) -> None:
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, self.state_path)

    def _stagger_overdue(self) -> None:
        """After a restart, run overdue checks one at a time, 20s apart, not all at once."""
        now, n = datetime.now(), 0
        for c in self.checks:
            due = self.state["next_due"].get(c.name)
            if due is None or datetime.fromisoformat(due) <= now:
                self.state["next_due"][c.name] = (now + timedelta(seconds=10 + 20 * n)).isoformat(timespec="seconds")
                n += 1
        self._save_state()

    def log(self, msg: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        except OSError:
            pass

    # ---- kill switch -----------------------------------------------------------
    @property
    def paused(self) -> bool:
        until = self.state.get("paused_until")
        if until == "forever":
            return True
        if until and datetime.fromisoformat(until) > datetime.now():
            return True
        return False

    def pause(self, minutes: float = 0) -> str:
        self.state["paused_until"] = "forever" if not minutes else (
            datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self._save_state()
        self.log(f"paused until {self.state['paused_until']}")
        return self.state["paused_until"]

    def resume(self) -> None:
        self.state["paused_until"] = None
        self._save_state()
        self.log("resumed")

    def status(self) -> dict:
        return {"paused": self.paused, "paused_until": self.state.get("paused_until"),
                "checks": {c.name: {"every_minutes": c.every_minutes,
                                    "next_due": self.state["next_due"].get(c.name),
                                    "last_run": self.state["checks"].get(c.name, {}).get("_last_run"),
                                    "last_error": self.state["checks"].get(c.name, {}).get("_last_error")}
                           for c in self.checks}}

    # ---- loop ----------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="heartbeat").start()
        self.log(f"started with checks: {', '.join(c.name for c in self.checks)}")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            self.tick()

    def tick(self) -> None:
        from .. import killswitch
        if self.paused or killswitch.engaged():
            return
        now = datetime.now()
        for c in self.checks:
            due = datetime.fromisoformat(self.state["next_due"].get(c.name, now.isoformat()))
            if due > now:
                continue
            self.state["next_due"][c.name] = (now + timedelta(minutes=c.every_minutes)).isoformat(timespec="seconds")
            with self._lock:
                if c.name in self._running:
                    self.log(f"{c.name}: still running from last time, skipped")
                    continue
                self._running.add(c.name)
            self._pool.submit(self._run_check, c)
        self._save_state()

    def run_now(self, name: str) -> None:
        """Run one check immediately (used by tests and 'check now')."""
        c = next(c for c in self.checks if c.name == name)
        with self._lock:
            if c.name in self._running:
                return
            self._running.add(c.name)
        self._run_check(c)

    def _run_check(self, c: Check) -> None:
        cstate = self.state["checks"].setdefault(c.name, {})
        first = "_last_run" not in cstate
        ctx = CheckContext(self.registry, self.settings.get(c.name, {}), cstate,
                           google_ready=(ROOT / "data" / "google_token.json").exists())
        try:
            found = c.fn(ctx)
            silent = first and c.seed_first_run
            added = sum(self.store.add(n, silent=silent) for n in found)
            cstate.pop("_last_error", None)
            if added:
                self.log(f"{c.name}: {added} new notice(s)")
            elif first and c.seed_first_run and found:
                self.log(f"{c.name}: first run, remembered {len(found)} existing item(s) without notifying")
        except Exception as e:  # a failing check never stops the heartbeat
            cstate["_last_error"] = f"{type(e).__name__}: {e}"[:300]
            self.log(f"{c.name}: error {cstate['_last_error']}")
        finally:
            cstate["_last_run"] = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                self._running.discard(c.name)
            self._save_state()
