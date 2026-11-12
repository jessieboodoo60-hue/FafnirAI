"""Tools that let Fafnir control what's on the desktop app's screen."""
from __future__ import annotations

from ..screen import PANELS, SCREEN
from .registry import Registry, Tool, ToolError

NO_SCREEN = "There's no screen in this mode (only the desktop app has one). Say it instead."
KINDS = ["list", "cards", "table", "text"]
ITEM = {"type": "object", "description": "One entry: title, and optionally subtitle, meta (e.g. a time) and tag "
        "(urgent | important | good | info) for its colour."}


def register(reg: Registry) -> None:
    def screen_show(title: str, kind: str = "list", items: list | None = None, columns: list | None = None,
                    text: str = "", subtitle: str = "") -> dict:
        items = items or []
        clean = []
        for it in items[:40]:
            if isinstance(it, dict):
                clean.append({k: str(it.get(k, ""))[:400] for k in ("title", "subtitle", "meta", "tag", "body")
                              if it.get(k) not in (None, "")})
            elif isinstance(it, list):
                clean.append([str(c)[:200] for c in it])  # a table row
            else:
                clean.append({"title": str(it)[:400]})
        if kind == "text" and not text:
            raise ToolError("kind 'text' needs `text`.")
        if kind != "text" and not clean:
            raise ToolError("Give some `items` to show (or use kind 'text' with `text`).")
        payload = {"title": title, "subtitle": subtitle, "kind": kind, "items": clean,
                   "columns": [str(c) for c in (columns or [])], "text": text[:8000]}
        if not SCREEN.send("show", payload):
            return {"shown": False, "note": NO_SCREEN}
        return {"shown": True, "on_screen": title}

    def screen_clear() -> dict:
        if not SCREEN.send("clear", {}):
            return {"cleared": False, "note": NO_SCREEN}
        return {"cleared": True}

    def screen_layout(order: list | None = None, hidden: list | None = None, conversation: str = "") -> dict:
        bad = [p for p in (order or []) + (hidden or []) if p not in PANELS]
        if bad:
            raise ToolError(f"Unknown panel(s) {bad}. Panels are: {', '.join(PANELS)}.")
        payload = {"order": order or None, "hidden": hidden if hidden is not None else None,
                   "conversation": conversation or None}
        if not SCREEN.send("layout", payload):
            return {"changed": False, "note": NO_SCREEN}
        return {"changed": True, "layout": SCREEN.layout}

    def screen_briefing(text: str) -> dict:
        if not SCREEN.send("briefing", {"text": text[:220]}):
            return {"set": False, "note": NO_SCREEN}
        return {"set": True}

    def screen_status() -> dict:
        if not SCREEN.available:
            return {"screen": False, "note": NO_SCREEN}
        return {"screen": True, "showing": SCREEN.showing or "nothing (the orb)", "layout": SCREEN.layout}

    reg.register(Tool(
        name="screen_show",
        description=(
            "Put something on the owner's big screen (the centre stage of the desktop app). Use it whenever they "
            "say show / pull up / put on screen / let me see, and when a visual helps (lists of emails, events, "
            "files, tasks, search results, a document). Fetch the data with your other tools first, then show it. "
            "kind: list (rows with title/subtitle/meta/tag), cards (tiles with title/body), table (columns + rows "
            "as lists), text (markdown in `text`). Replaces what's on the stage. Keep talking briefly as well."
        ),
        properties={
            "title": {"type": "string", "description": "Heading, e.g. 'Unread email' or 'Today'."},
            "subtitle": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "items": {"type": "array", "items": ITEM,
                      "description": "For list/cards: objects {title, subtitle, meta, tag, body}. For table: lists of cell strings."},
            "columns": {"type": "array", "items": {"type": "string"}, "description": "Table column headings."},
            "text": {"type": "string", "description": "Markdown, for kind 'text'."},
        },
        required=["title"],
        handler=screen_show,
    ))
    reg.register(Tool(
        name="screen_clear",
        description="Clear the stage and bring the orb back to the middle (when the owner says close / clear / hide that).",
        properties={},
        handler=screen_clear,
    ))
    reg.register(Tool(
        name="screen_layout",
        description=(
            f"Rearrange the side panels: `order` (most important first) and `hidden`, from: {', '.join(PANELS)}. "
            "`conversation`: show, small or hide. Use when the owner asks (\"hide the activity\", \"put email "
            "first\", \"focus mode\") or when something clearly matters more right now."
        ),
        properties={
            "order": {"type": "array", "items": {"type": "string", "enum": PANELS}},
            "hidden": {"type": "array", "items": {"type": "string", "enum": PANELS}},
            "conversation": {"type": "string", "enum": ["", "show", "small", "hide"]},
        },
        handler=screen_layout,
    ))
    reg.register(Tool(
        name="screen_briefing",
        description="Set the one-line briefing at the top of the screen (the 'Right now' line), in your own words.",
        properties={"text": {"type": "string"}},
        required=["text"],
        handler=screen_briefing,
    ))
    reg.register(Tool(
        name="screen_status",
        description="What's on the owner's screen right now (the stage and the panel layout).",
        properties={},
        handler=screen_status,
    ))
