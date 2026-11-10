"""Business management: a local, human-readable tracker of tasks, clients, deadlines and notes.

Stored as JSON in data/business.json so the owner can open and edit it by hand.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path

from .registry import Registry, Tool, ToolError

KINDS = ["task", "client", "deadline", "note"]
STATUSES = ["open", "done"]


class BusinessStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "items": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ToolError(f"The business file {self.path} can't be read ({e}). It may need fixing by hand.")

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    # operations -----------------------------------------------------------
    def add(self, kind: str, title: str, details: str = "", due: str = "", client: str = "") -> dict:
        due = _check_date(due)
        with self._lock:
            data = self._load()
            item = {
                "id": data["next_id"], "kind": kind, "title": title.strip(), "details": details.strip(),
                "due": due, "client": client.strip(), "status": "open",
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            data["next_id"] += 1
            data["items"].append(item)
            self._save(data)
        return {"added": item}

    def list(self, kind: str = "", status: str = "open", due_on_or_before: str = "", search: str = "") -> dict:
        cutoff = _check_date(due_on_or_before)
        items = self._load()["items"]
        out = []
        for it in items:
            if kind and it["kind"] != kind:
                continue
            if status != "all" and it["status"] != status:
                continue
            if cutoff and (not it["due"] or it["due"] > cutoff):
                continue
            if search:
                hay = " ".join(str(it.get(k, "")) for k in ("title", "details", "client")).lower()
                if search.lower() not in hay:
                    continue
            out.append(it)
        out.sort(key=lambda i: (i["due"] or "9999-99-99", i["id"]))
        return {"today": date.today().isoformat(), "count": len(out), "items": out}

    def update(self, id: int, status: str = "", title: str = "", details: str = "", due: str = "", client: str = "") -> dict:
        due = _check_date(due)
        with self._lock:
            data = self._load()
            item = next((i for i in data["items"] if i["id"] == id), None)
            if item is None:
                raise ToolError(f"No business item with id {id}.")
            for k, v in (("status", status), ("title", title), ("details", details), ("due", due), ("client", client)):
                if v:
                    item[k] = v
            item["updated"] = datetime.now().isoformat(timespec="seconds")
            self._save(data)
        return {"updated": item}

    def delete(self, id: int) -> dict:
        with self._lock:
            data = self._load()
            before = len(data["items"])
            data["items"] = [i for i in data["items"] if i["id"] != id]
            if len(data["items"]) == before:
                raise ToolError(f"No business item with id {id}.")
            self._save(data)
        return {"deleted": id}


def _check_date(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ToolError(f"'{s}' isn't a date in YYYY-MM-DD form.")


def register(reg: Registry, data_dir: Path) -> None:
    store = BusinessStore(data_dir / "business.json")
    reg.register(Tool(
        name="business_add_item",
        description=(
            "Record something for the owner's business: a task to do, a client/contact, a deadline, "
            "or a business note. Use when the owner asks to add, track, or remind them of something "
            "work-related. Dates must be YYYY-MM-DD (work out relative dates like 'Friday' from today's date)."
        ),
        properties={
            "kind": {"type": "string", "enum": KINDS, "description": "What sort of item this is."},
            "title": {"type": "string", "description": "Short one-line title."},
            "details": {"type": "string", "description": "Optional longer details."},
            "due": {"type": "string", "description": "Optional due date, YYYY-MM-DD."},
            "client": {"type": "string", "description": "Optional client or contact this relates to."},
        },
        required=["kind", "title"],
        handler=store.add,
    ))
    reg.register(Tool(
        name="business_list_items",
        description=(
            "Look up the owner's tracked business items: tasks, clients, deadlines, notes. Use for "
            "questions like 'what's on my list today', 'what's due this week', 'who are my clients'. "
            "Results are sorted by due date. For 'today' pass today's date as due_on_or_before."
        ),
        properties={
            "kind": {"type": "string", "enum": KINDS + [""], "description": "Filter by kind; empty for all."},
            "status": {"type": "string", "enum": STATUSES + ["all"], "description": "Default 'open'."},
            "due_on_or_before": {"type": "string", "description": "Only items due on or before this YYYY-MM-DD date."},
            "search": {"type": "string", "description": "Only items whose title, details or client contain this text."},
        },
        handler=store.list,
    ))
    reg.register(Tool(
        name="business_update_item",
        description=(
            "Change an existing business item by id: mark it done, reopen it, or edit its title, details, "
            "due date or client. Get the id from business_list_items first. Only fields you pass are changed."
        ),
        properties={
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": STATUSES + [""]},
            "title": {"type": "string"},
            "details": {"type": "string"},
            "due": {"type": "string", "description": "YYYY-MM-DD"},
            "client": {"type": "string"},
        },
        required=["id"],
        handler=store.update,
    ))
    reg.register(Tool(
        name="business_delete_item",
        description="Permanently delete a business item by id. Prefer marking items done instead.",
        properties={"id": {"type": "integer"}},
        required=["id"],
        handler=store.delete,
        confirm_if=lambda a: f"Permanently delete business item number {a['id']}.",  # deletes data
    ))
