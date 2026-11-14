"""Long-term memory, version 2: encrypted, searched by meaning, de-duplicated.

Storage. One encrypted file, data/memory.vault (AES-256-GCM, key in fafnir/vault.py, locked to the
owner's Windows account). It holds the facts and the summaries of older conversations. The first
run imports the old plain-text data/memory.md. An encrypted file rather than an encrypted SQLite
database on purpose: the data is tiny (hundreds of short lines), every write is atomic, and it
needs no extra database driver.

Finding things. Each fact is embedded locally (fafnir/embed.py, on the CPU; nothing leaves the PC).
  - relevant(query) returns only the few facts that matter for a request, so a small local model
    doesn't carry the whole memory in every prompt.
  - search(query) mixes meaning and keywords.
  - remember() refuses near-duplicates (same meaning) and anything that looks like a secret
    (passwords, card numbers, ID numbers, keys): those never go into memory.

The owner stays in control: `python main.py --memory-export file.md` writes everything as readable
Markdown, `--memory-import file.md` reads an edited copy back, and facts can be corrected or
deleted by asking Fafnir or with the memory tools.

Facts are background knowledge, never instructions (see prompts.py).
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from . import vault

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
PROMPT_LIMIT = 200       # facts put in a cloud brain's prompt; beyond this, the rest are found by search
DUPLICATE_AT = 0.93      # cosine similarity at which two facts say the same thing (tuned on test pairs)

_SECRET_WORDS = re.compile(r"\b(pass\s?word|passcode|pass\s?phrase|pin( code| number)?|cvv|cvc|security code|"
                           r"api[ _-]?key|secret key|private key|token|recovery key|seed phrase|"
                           r"passport (no|number|#)|id (no|number|#)|licen[cs]e (no|number|#)|permit (no|number|#)|"
                           r"social security|ssn|bank account|account (no|number|#)|routing|iban|card (no|number|#))\b", re.I)
_HAS_VALUE = re.compile(r"\b(is|are|=|:)\s*\S*[\d@#$%^&*!]\S*|\b\d{4,}\b", re.I)
_KEY_LIKE = re.compile(r"\b(sk|pk|ghp|gho|xox[abp]|AKIA|AIza)[-_A-Za-z0-9]{12,}|\b(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}\b")


def looks_secret(text: str) -> bool:
    """A password, key, or card/ID/account number that must not be stored in memory."""
    if _KEY_LIKE.search(text):
        return True
    digits = re.sub(r"[ -]", "", "".join(re.findall(r"[\d -]{13,}", text)))
    if 13 <= len(digits) <= 19 and _luhn(digits):
        return True
    return bool(_SECRET_WORDS.search(text) and _HAS_VALUE.search(text))


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


@dataclass
class Fact:
    id: str
    text: str
    category: str      # heading text
    added: str = ""
    uses: int = 0      # how often it was pulled into a request (for relevance)
    last_used: str = ""


class MemoryStore:
    def __init__(self, path: Path):
        path = Path(path)
        self.path = path.with_suffix(".vault") if path.suffix == ".md" else path
        self.legacy = self.path.with_suffix(".md")
        self._lock = threading.RLock()
        self._facts: list[Fact] = []
        self._summaries: list[dict] = []      # [{"date": "2026-11-12", "text": "..."}]
        self._mtime = -1.0
        self._vectors: dict[str, object] = {}  # text -> embedding (kept in memory only)
        with self._lock:
            if not self.path.exists() and self.legacy.exists():
                self._facts = _parse_markdown(self.legacy.read_text(encoding="utf-8"))
                self._save()
            self._load()

    # ---- encrypted file ---------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self._facts, self._summaries, self._mtime = [], [], -1.0
            return
        m = self.path.stat().st_mtime
        if m == self._mtime:
            return
        data = json.loads(vault.decrypt(self.path.read_bytes()).decode("utf-8"))
        known = Fact.__dataclass_fields__
        self._facts = [Fact(**{k: v for k, v in f.items() if k in known}) for f in data.get("facts", [])]
        self._summaries = list(data.get("summaries", []))
        self._mtime = m

    def _save(self) -> None:
        blob = json.dumps({"version": 2, "facts": [asdict(f) for f in self._facts], "summaries": self._summaries},
                          ensure_ascii=False).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(vault.encrypt(blob))
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ---- embeddings (local) ---------------------------------------------------------------
    def _embed(self, texts: list[str], query: bool = False):
        from .embed import embed
        if query:
            return embed(texts, query=True)
        missing = [t for t in texts if t not in self._vectors]
        if missing:
            for t, v in zip(missing, embed(missing)):
                self._vectors[t] = v
        import numpy as np
        return np.vstack([self._vectors[t] for t in texts]) if texts else np.zeros((0, 384))

    def _similar(self, query: str, texts: list[str], as_query: bool = True) -> list[float]:
        """Cosine similarity of the query to each text; zeros if the embedding model isn't available."""
        if not texts:
            return []
        try:
            return list((self._embed([query], query=as_query)[0] @ self._embed(texts).T).tolist())
        except Exception:
            return [0.0] * len(texts)

    # ---- operations ---------------------------------------------------------------------
    def all(self) -> list[Fact]:
        with self._lock:
            self._load()
            return list(self._facts)

    def remember(self, fact: str, category: str = "other") -> dict:
        text = " ".join(fact.split())
        if not text:
            raise ValueError("Empty fact.")
        if looks_secret(text):
            raise ValueError("That looks like a password, key or card/ID number. Secrets are never stored in memory.")
        heading = CATEGORIES.get(category, CATEGORIES["other"])
        with self._lock:
            self._load()
            for f in self._facts:
                if f.text.lower().rstrip(".") == text.lower().rstrip("."):
                    return {"already_known": {"id": f.id, "fact": f.text}}
            sims = self._similar(text, [f.text for f in self._facts], as_query=False)
            if sims and max(sims) >= DUPLICATE_AT:
                f = self._facts[sims.index(max(sims))]
                return {"already_known": {"id": f.id, "fact": f.text},
                        "note": "A fact with the same meaning is already stored. Use memory_update if this one replaces it."}
            n = max((int(f.id[1:]) for f in self._facts if f.id[1:].isdigit()), default=0) + 1
            new = Fact(f"m{n}", text, heading, date.today().isoformat())
            self._facts.append(new)
            self._save()
        return {"remembered": {"id": new.id, "fact": new.text, "category": heading}}

    def update(self, id: str, fact: str) -> dict:
        text = " ".join(fact.split())
        if looks_secret(text):
            raise ValueError("That looks like a password, key or card/ID number. Secrets are never stored in memory.")
        with self._lock:
            self._load()
            f = next((f for f in self._facts if f.id == id), None)
            if f is None:
                raise KeyError(id)
            old, f.text, f.added = f.text, text, date.today().isoformat()
            self._save()
        return {"updated": {"id": id, "was": old, "now": text}}

    def forget(self, id: str) -> dict:
        with self._lock:
            self._load()
            f = next((f for f in self._facts if f.id == id), None)
            if f is None:
                raise KeyError(id)
            self._facts.remove(f)
            self._vectors.pop(f.text, None)
            self._save()
        return {"forgotten": {"id": id, "fact": f.text}}

    def search(self, query: str, limit: int = 15) -> list[Fact]:
        """Facts matching the query by meaning or by shared words, best first."""
        facts = self.all()
        words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2]
        sims = self._similar(query, [f.text for f in facts])
        scored = []
        for f, s in zip(facts, sims):
            hay = f"{f.text} {f.category}".lower()
            kw = sum(1 for w in words if w in hay) / max(1, len(words))
            score = s + 0.35 * kw
            if s >= 0.45 or kw > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:limit]]

    def relevant(self, query: str, limit: int = 8, floor: float = 0.38) -> list[Fact]:
        """The few facts worth putting in the prompt for this request (meaning first, with a small
        boost for facts that keep proving useful). Counts each use."""
        with self._lock:
            self._load()
            facts = list(self._facts)
            if len(facts) <= limit:
                return facts
            sims = self._similar(query, [f.text for f in facts])
            ranked = sorted(zip(sims, facts), key=lambda x: -(x[0] + min(x[1].uses, 20) * 0.003))
            picked = [f for s, f in ranked[:limit] if s >= floor] or [f for _, f in ranked[:3]]
            today = date.today().isoformat()
            for f in picked:
                f.uses, f.last_used = f.uses + 1, today
            return picked

    def flush_usage(self) -> None:
        """Write the use counters (cheap bookkeeping, done occasionally, not on every request)."""
        with self._lock:
            if self.path.exists():
                self._save()

    def prompt_block(self) -> str:
        """The facts to load into a new conversation's system prompt (cloud brains, large context)."""
        facts = self.all()
        if not facts:
            return "(Nothing yet.)"
        shown = sorted(facts, key=lambda f: f.added or "", reverse=True)[:PROMPT_LIMIT]
        shown_ids = {f.id for f in shown}
        lines = [f"- ({f.id}) [{f.category}] {f.text}" for f in facts if f.id in shown_ids]
        if len(facts) > len(shown):
            lines.append(f"({len(facts) - len(shown)} older facts not shown; use memory_search to find them.)")
        return "\n".join(lines)

    # ---- summaries of older conversations ---------------------------------------------------
    def summaries(self) -> list[dict]:
        with self._lock:
            self._load()
            return list(self._summaries)

    def add_summary(self, day: str, text: str) -> None:
        with self._lock:
            self._load()
            self._summaries = [s for s in self._summaries if s["date"] != day] + [{"date": day, "text": text.strip()}]
            self._summaries.sort(key=lambda s: s["date"])
            self._save()

    def search_summaries(self, query: str, limit: int = 3, floor: float = 0.4) -> list[dict]:
        items = self.summaries()
        sims = self._similar(query, [s["text"] for s in items])
        ranked = sorted(zip(sims, items), key=lambda x: -x[0])
        return [s for sim, s in ranked[:limit] if sim >= floor]

    # ---- the owner's controls ---------------------------------------------------------------
    def export_markdown(self, target: Path) -> int:
        facts = self.all()
        Path(target).write_text(_to_markdown(facts, self.summaries()), encoding="utf-8")
        return len(facts)

    def import_markdown(self, source: Path) -> dict:
        """Replace the stored facts with an edited export. Lines that look like secrets are skipped."""
        facts = _parse_markdown(Path(source).read_text(encoding="utf-8"))
        kept = [f for f in facts if not looks_secret(f.text)]
        with self._lock:
            self._load()
            old = {f.text: f for f in self._facts}
            for f in kept:   # keep the usage counters of unchanged facts
                if f.text in old:
                    f.uses, f.last_used = old[f.text].uses, old[f.text].last_used
            self._facts = kept
            self._save()
        return {"facts": len(kept), "skipped_as_secrets": len(facts) - len(kept)}

    def delete_everything(self) -> None:
        with self._lock:
            self._facts, self._summaries, self._vectors = [], [], {}
            self._save()


def _parse_markdown(text: str) -> list[Fact]:
    facts, heading = [], CATEGORIES["other"]
    for raw in text.splitlines():
        if raw.startswith("## "):
            heading = raw[3:].strip() or CATEGORIES["other"]
            if heading.lower().startswith("conversation summaries"):
                break
            continue
        m = LINE.match(raw)
        if not m or not m.group(2).strip():
            continue
        facts.append(Fact(m.group(1) or "", m.group(2).strip(), heading, m.group(3) or ""))
    used = {int(f.id[1:]) for f in facts if f.id}
    n = max(used, default=0)
    for f in facts:          # lines added by hand get IDs
        if not f.id:
            n += 1
            f.id = f"m{n}"
    return facts


def _to_markdown(facts: list[Fact], summaries: list[dict]) -> str:
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
    if summaries:
        lines.append("## Conversation summaries (read-only; not imported back)")
        for s in summaries:
            lines.append(f"### {s['date']}")
            lines.append(s["text"])
            lines.append("")
    return "\n".join(lines)
