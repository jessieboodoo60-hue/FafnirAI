"""Long-term memory: small, human-readable facts that survive restarts.

Stored in data/memory.md as a plain Markdown list, one fact per line, grouped under
headings. The owner can open it, fix a fact, add one or delete one at any time; the
file is re-read on every operation, so hand edits are picked up straight away.

Facts are background knowledge, never instructions (see prompts.py).
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CATEGORIES = {
    "about_me": "About me",
    "preferences": "Preferences",
    "people": "People",
    "business": "Business",
    "other": "Other",
}
HEADER = """# Fafnir's memory

Things Fafnir has learned about you. Edit freely: one fact per line, starting with "- ".
The (m1) tags are IDs Fafnir uses to update or forget a fact; new lines without one get one automatically.
"""
LINE = re.compile(r"^\s*[-*]\s+(?:\((m\d+)\)\s*)?(.*?)\s*(?:<!--\s*(\d{4}-\d{2}-\d{2})\s*-->)?\s*$")
PROMPT_LIMIT = 200  # facts put in the prompt; beyond this, the rest are found by search


@dataclass
class Fact:
    id: str
    text: str
    category: str      # heading text as written in the file
    added: str = ""


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    # ---- file format ------------------------------------------------------
    def _read(self) -> list[Fact]:
        if not self.path.exists():
            return []
        facts, heading, needs_ids = [], CATEGORIES["other"], False
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if raw.startswith("## "):
                heading = raw[3:].strip() or CATEGORIES["other"]
                continue
            m = LINE.match(raw)
            if not m or not m.group(2).strip():
                continue
            fid = m.group(1) or ""
            needs_ids |= not fid
            facts.append(Fact(fid, m.group(2).strip(), heading, m.group(3) or ""))
        if needs_ids:  # lines the owner added by hand get IDs
            used = {int(f.id[1:]) for f in facts if f.id}
            n = max(used, default=0)
            for f in facts:
                if not f.id:
                    n += 1
                    f.id = f"m{n}"
            self._write(facts)
        return facts

    def _write(self, facts: list[Fact]) -> None:
        order = list(CATEGORIES.values())
        headings = order + sorted({f.category for f in facts} - set(order))
        lines = [HEADER]
        for h in headings:
            group = [f for f in facts if f.category == h]
            if not group:
                continue
            lines.append(f"## {h}")
            for f in group:
                stamp = f"  <!-- {f.added} -->" if f.added else ""
                lines.append(f"- ({f.id}) {f.text}{stamp}")
            lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- operations -------------------------------------------------------
    def all(self) -> list[Fact]:
        with self._lock:
            return self._read()

    def remember(self, fact: str, category: str = "other") -> dict:
        text = " ".join(fact.split())
        if not text:
            raise ValueError("Empty fact.")
        heading = CATEGORIES.get(category, CATEGORIES["other"])
        with self._lock:
            facts = self._read()
            for f in facts:
                if f.text.lower().rstrip(".") == text.lower().rstrip("."):
                    return {"already_known": {"id": f.id, "fact": f.text}}
            n = max((int(f.id[1:]) for f in facts), default=0) + 1
            new = Fact(f"m{n}", text, heading, date.today().isoformat())
            facts.append(new)
            self._write(facts)
        return {"remembered": {"id": new.id, "fact": new.text, "category": heading}}

    def update(self, id: str, fact: str) -> dict:
        text = " ".join(fact.split())
        with self._lock:
            facts = self._read()
            f = next((f for f in facts if f.id == id), None)
            if f is None:
                raise KeyError(id)
            old, f.text, f.added = f.text, text, date.today().isoformat()
            self._write(facts)
        return {"updated": {"id": id, "was": old, "now": text}}

    def forget(self, id: str) -> dict:
        with self._lock:
            facts = self._read()
            f = next((f for f in facts if f.id == id), None)
            if f is None:
                raise KeyError(id)
            facts.remove(f)
            self._write(facts)
        return {"forgotten": {"id": id, "fact": f.text}}

    def search(self, query: str, limit: int = 15) -> list[Fact]:
        words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2]
        scored = []
        for f in self.all():
            hay = f"{f.text} {f.category}".lower()
            score = sum(hay.count(w) for w in words)
            if score:
                scored.append((score, f))
        scored.sort(key=lambda s: -s[0])
        return [f for _, f in scored[:limit]]

    def prompt_block(self) -> str:
        """The facts to load into a new conversation's system prompt."""
        facts = self.all()
        if not facts:
            return "(Nothing yet.)"
        shown = sorted(facts, key=lambda f: f.added or "", reverse=True)[:PROMPT_LIMIT]
        shown_ids = {f.id for f in shown}
        lines = [f"- ({f.id}) [{f.category}] {f.text}" for f in facts if f.id in shown_ids]
        if len(facts) > len(shown):
            lines.append(f"({len(facts) - len(shown)} older facts not shown; use memory_search to find them.)")
        return "\n".join(lines)
