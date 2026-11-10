"""The notice inbox: things the heartbeat noticed, held until the owner dismisses them.

Stored in data/notices.json so nothing is lost if the app is closed or the PC restarts:
notices are held and shown on return, never fired once and forgotten. Each notice has a
`key`, so the same thing is never reported twice; dismissed notices are kept for a while
just to remember their keys.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

URGENCIES = ("info", "important", "urgent")  # info = silent; important/urgent = spoken
KEEP_DISMISSED_DAYS = 14


@dataclass
class Notice:
    key: str
    title: str
    detail: str = ""
    urgency: str = "info"
    check: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "new"          # new | dismissed
    announced: bool = False      # spoken aloud already (important/urgent)
    dismissed_at: str = ""
    pushed: bool = False         # sent to the owner's phone already


class NoticeStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> list[Notice]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Notice(**n) for n in raw]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self, items: list[Notice]) -> None:
        cutoff = (datetime.now() - timedelta(days=KEEP_DISMISSED_DAYS)).isoformat()
        items = [n for n in items if n.status != "dismissed" or (n.dismissed_at or n.created) > cutoff]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(n) for n in items], indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- writing (heartbeat) ------------------------------------------------
    def add(self, notice: Notice, silent: bool = False) -> bool:
        """Add unless a notice with the same key exists. `silent` records it as already
        handled (used to seed a check's first run without flooding the inbox)."""
        with self._lock:
            items = self._load()
            if any(n.key == notice.key for n in items):
                return False
            if silent:
                notice.status, notice.announced = "dismissed", True
                notice.dismissed_at = notice.created
            items.append(notice)
            self._save(items)
            return not silent

    # ---- reading / handling (front ends, tools) -----------------------------
    def active(self) -> list[Notice]:
        items = [n for n in self._load() if n.status != "dismissed"]
        rank = {"urgent": 0, "important": 1, "info": 2}
        newest_first = sorted(items, key=lambda n: n.created, reverse=True)
        return sorted(newest_first, key=lambda n: rank.get(n.urgency, 3))  # most urgent, then newest

    def dismiss(self, notice_id: str) -> int:
        """Dismiss one notice by id, or every active one with 'all'. Returns how many."""
        with self._lock:
            items, count = self._load(), 0
            for n in items:
                if n.status != "dismissed" and (notice_id == "all" or n.id == notice_id):
                    n.status, n.dismissed_at = "dismissed", datetime.now().isoformat(timespec="seconds")
                    count += 1
            self._save(items)
            return count

    def mark_pushed(self, notice_id: str) -> None:
        with self._lock:
            items = self._load()
            for n in items:
                if n.id == notice_id:
                    n.pushed = True
            self._save(items)

    def mark_announced(self, notice_id: str) -> None:
        with self._lock:
            items = self._load()
            for n in items:
                if n.id == notice_id:
                    n.announced = True
            self._save(items)
