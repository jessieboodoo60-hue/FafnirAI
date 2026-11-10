"""A plain log of what was said, so Fafnir remembers recent conversations across restarts.

One JSON-lines file per day in data/conversations/. New conversations start with the last
day or so of exchanges in the prompt (as background, not instructions), and older ones can
be searched with the conversation_search tool. The owner can read or delete these files.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path


class ConversationLog:
    def __init__(self, folder: Path):
        self.folder = folder
        self._lock = threading.Lock()

    def append(self, role: str, text: str, source: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        now = datetime.now()
        entry = {"time": now.isoformat(timespec="seconds"), "role": role, "text": text}
        if source:
            entry["source"] = source
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / f"{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _entries(self, days: int) -> list[dict]:
        out = []
        today = datetime.now().date()
        for i in range(days, -1, -1):
            p = self.folder / f"{today - timedelta(days=i):%Y-%m-%d}.jsonl"
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
            except OSError:
                pass
        return out

    def recent(self, hours: float = 36, max_entries: int = 30) -> list[dict]:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        return [e for e in self._entries(2) if e["time"] >= cutoff][-max_entries:]

    def today(self) -> list[dict]:
        return self._entries(0)

    def prompt_block(self) -> str:
        """Recent exchanges for a new conversation's system prompt."""
        lines = []
        for e in self.recent():
            who = "Owner" if e["role"] == "user" else "You"
            text = e["text"] if len(e["text"]) <= 300 else e["text"][:297] + "..."
            lines.append(f"[{e['time'][5:16].replace('T', ' ')}] {who}: {text}")
        return "\n".join(lines) if lines else "(No recent conversation.)"

    def search(self, query: str, days: int = 30, limit: int = 20) -> list[dict]:
        words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2]
        hits = []
        for e in self._entries(days):
            low = e["text"].lower()
            score = sum(low.count(w) for w in words)
            if score:
                hits.append((score, e))
        hits.sort(key=lambda h: (-h[0], h[1]["time"]), reverse=False)
        return [h[1] for h in hits[:limit]]
