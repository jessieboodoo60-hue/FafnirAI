"""A log of what was said, so Fafnir remembers recent conversations across restarts.

One file per day in data/conversations/. Since 2026-11-13 every line is encrypted on its own
(AES-256-GCM, fafnir/vault.py), so the files are unreadable on disk but appending stays cheap;
lines written before that are read as they are until `encrypt_existing()` converts them. New
conversations start with the last day or so of exchanges in the prompt (as background, not
instructions), older ones can be searched with conversation_search, and files older than the
retention period are deleted (`purge_older_than`). `python main.py --conversations-export <dir>`
writes readable copies for the owner.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import vault


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
                f.write(vault.encrypt_line(json.dumps(entry, ensure_ascii=False)) + "\n")

    def day(self, day: str) -> list[dict]:
        """Every entry from one day ("2026-11-12")."""
        out = []
        try:
            for line in (self.folder / f"{day}.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(vault.decrypt_line(line)))
                except Exception:
                    pass   # a damaged line: skip it, keep the rest
        except OSError:
            pass
        return out

    def _entries(self, days: int) -> list[dict]:
        out = []
        today = datetime.now().date()
        for i in range(days, -1, -1):
            out += self.day(f"{today - timedelta(days=i):%Y-%m-%d}")
        return out

    def encrypt_existing(self) -> int:
        """Convert any plain lines left from before encryption. Returns how many were converted."""
        import os
        n = 0
        with self._lock:
            for p in sorted(self.folder.glob("*.jsonl")):
                lines = p.read_text(encoding="utf-8").splitlines()
                if all(ln.startswith(vault.LINE_PREFIX) or not ln.strip() for ln in lines):
                    continue
                new = [ln if ln.startswith(vault.LINE_PREFIX) or not ln.strip() else vault.encrypt_line(ln) for ln in lines]
                n += sum(1 for a, b in zip(lines, new) if a != b)
                tmp = p.with_suffix(".tmp")
                tmp.write_text("\n".join(new) + "\n", encoding="utf-8")
                os.replace(tmp, p)
        return n

    def purge_older_than(self, days: int) -> int:
        """Delete conversation files older than `days` (0 = keep forever). Returns files removed."""
        if days <= 0:
            return 0
        cutoff = f"{datetime.now().date() - timedelta(days=days):%Y-%m-%d}"
        gone = 0
        for p in self.folder.glob("*.jsonl"):
            if p.stem < cutoff:
                p.unlink(missing_ok=True)
                gone += 1
        return gone

    def export(self, target: Path) -> int:
        """Readable copies of every day, for the owner."""
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in sorted(self.folder.glob("*.jsonl")):
            (target / p.name).write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in self.day(p.stem)) + "\n",
                                         encoding="utf-8")
            n += 1
        return n

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
