"""Two logs the owner can trust and read.

1. The action log (logs/actions.log) is tamper-evident. Every entry carries `h`, a SHA-256 hash of
   the previous entry's hash plus this entry's own content. Changing, removing or inserting a line
   breaks every hash after it, which `python main.py --verify-log` reports. It can't stop someone
   with access to the PC from rewriting the whole file, but it makes quiet edits show.

2. The outbound log (logs/outbound.log) records every time Fafnir sends something off this PC:
   where to, why, and roughly how much. Never the content itself. `outbound_summary()` feeds the
   privacy panel in Settings and the privacy_report tool.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT

ACTIONS = ROOT / "logs" / "actions.log"
OUTBOUND = ROOT / "logs" / "outbound.log"
_lock = threading.Lock()
_last: dict[str, str] = {}      # log path -> hash of its last entry


def _digest(prev: str, entry: dict) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "h"}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()[:32]


def _tail_hash(path: Path) -> str:
    """Hash of the last chained entry in the file ('' if none yet)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                h = json.loads(line).get("h")
                if h:
                    return h
            except ValueError:
                continue
    except OSError:
        pass
    return ""


def record(entry: dict, path: Path = ACTIONS) -> None:
    """Append one entry to the action log, chained to the one before it."""
    key = str(path)
    try:
        with _lock:
            if key not in _last:
                _last[key] = _tail_hash(path)
            entry = dict(entry)
            entry["h"] = _digest(_last[key], entry)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            _last[key] = entry["h"]
    except OSError:
        pass


def verify(path: Path = ACTIONS) -> dict:
    """Check the chain. Lines from before hashing was added (no `h`) are counted, not judged."""
    prev, checked, unchained, bad = "", 0, 0, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"ok": True, "entries": 0, "note": "no log yet"}
    for n, line in enumerate(lines, 1):
        try:
            e = json.loads(line)
        except ValueError:
            bad.append(n)
            continue
        if "h" not in e:
            if checked:
                bad.append(n)          # an unhashed line after hashing began = inserted
            else:
                unchained += 1
            continue
        if _digest(prev, e) != e["h"]:
            bad.append(n)
        prev = e["h"]
        checked += 1
    return {"ok": not bad, "entries": checked, "older_unhashed_entries": unchained,
            "first_problem_line": bad[0] if bad else None, "problem_lines": len(bad)}


# ---- outbound requests ------------------------------------------------------------------------
def outbound(destination: str, why: str, size: int = 0) -> None:
    try:
        with _lock:
            OUTBOUND.parent.mkdir(parents=True, exist_ok=True)
            with open(OUTBOUND, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "to": destination,
                                    "why": " ".join(str(why).split())[:120], "size": int(size)}) + "\n")
    except OSError:
        pass


def outbound_summary(hours: float = 24) -> dict:
    """Who Fafnir contacted in the last `hours`: {destination: {"requests": n, "kb": x, "why": [...]}}."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    out: dict[str, dict] = {}
    try:
        for line in OUTBOUND.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("time", "") < cutoff:
                continue
            d = out.setdefault(e.get("to", "?"), {"requests": 0, "kb": 0.0, "why": []})
            d["requests"] += 1
            d["kb"] = round(d["kb"] + (e.get("size", e.get("chars_sent", 0)) or 0) / 1024, 1)
            if e.get("why") and e["why"] not in d["why"] and len(d["why"]) < 4:
                d["why"].append(e["why"])
    except OSError:
        pass
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["requests"]))


# Which outside service a tool talks to (tools not listed stay on this PC or the home network).
TOOL_DESTINATIONS = (
    (("gmail_", "calendar_", "drive_", "contacts_", "google_tasks_", "youtube_"), "Google"),
    (("weather",), "Weather service (Open-Meteo)"),
    (("software_install", "software_search", "software_uninstall"), "Software sources (winget / Steam / download sites)"),
    (("web_search", "web_fetch", "WebSearch", "WebFetch"), "The web, via the cloud brain"),
)


def tool_destination(tool: str) -> str | None:
    for prefixes, dest in TOOL_DESTINATIONS:
        if tool.startswith(prefixes):
            return dest
    return None
